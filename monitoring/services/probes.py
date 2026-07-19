"""One probe function per MonitorCheck.kind. Each returns
(status, detail) where status is "ok" | "warning" | "critical" | "unknown"
and detail is a small JSON-serializable dict describing what was
measured, for the dashboard to display."""
import os
import subprocess
import time
from datetime import datetime, timezone

import psutil

from monitoring.services.transmitter_client import parse_numeric

# Real bug hit live: Django's TIME_ZONE/USE_TZ setting makes django.setup()
# set this process's own TZ env var (confirmed: os.environ['TZ'] ==
# 'America/Chicago' after setup, even though the box's actual OS timezone
# is UTC) -- every subprocess this poller spawns inherits that, so
# `systemctl show` printed localized "CDT" timestamps instead of UTC ones,
# and _parse_systemd_timestamp()'s UTC assumption then mis-read those
# local wall-clock numbers as if they were UTC, undercounting uptime by
# exactly the local UTC offset (5 hours in CDT). Force UTC on just this
# subprocess call rather than touching Django's global timezone handling.
_UTC_ENV = {**os.environ, "TZ": "UTC"}


def _threshold_status(value, warning_threshold, critical_threshold, direction):
    if direction == "below":
        if critical_threshold is not None and value <= critical_threshold:
            return "critical"
        if warning_threshold is not None and value <= warning_threshold:
            return "warning"
        return "ok"
    if critical_threshold is not None and value >= critical_threshold:
        return "critical"
    if warning_threshold is not None and value >= warning_threshold:
        return "warning"
    return "ok"


def probe_systemd(check):
    try:
        result = subprocess.run(
            ["systemctl", "show", check.systemd_unit,
             "--property=ActiveState,SubState,ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5, check=False,
            env=_UTC_ENV,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "unknown", {"error": str(exc)}

    props = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value

    active_state = props.get("ActiveState", "unknown")
    sub_state = props.get("SubState", "")
    detail = {"active_state": active_state, "sub_state": sub_state}

    since_raw = props.get("ActiveEnterTimestamp", "")
    if since_raw:
        try:
            entered = datetime.strptime(since_raw, "%a %Y-%m-%d %H:%M:%S %Z")
            entered = entered.replace(tzinfo=timezone.utc)
            detail["uptime_seconds"] = max(0, time.time() - entered.timestamp())
        except ValueError:
            pass

    if active_state != "active":
        return "critical", detail
    return "ok", detail


def probe_disk(check):
    try:
        usage = psutil.disk_usage(check.disk_path)
    except OSError as exc:
        return "unknown", {"error": str(exc)}
    detail = {"percent": usage.percent, "used": usage.used, "total": usage.total}
    status = _threshold_status(usage.percent, check.warning_threshold, check.critical_threshold, "above")
    return status, detail


def probe_cpu(check):
    percent = psutil.cpu_percent(interval=None)
    per_cpu = psutil.cpu_percent(interval=None, percpu=True)
    detail = {"percent": percent, "per_cpu": per_cpu}
    status = _threshold_status(percent, check.warning_threshold, check.critical_threshold, "above")
    return status, detail


def probe_memory(check):
    mem = psutil.virtual_memory()
    detail = {"percent": mem.percent, "used": mem.used, "total": mem.total}
    status = _threshold_status(mem.percent, check.warning_threshold, check.critical_threshold, "above")
    return status, detail


def _per_core_temperatures(zones):
    # "Core N" entries only (e.g. the coretemp chip's per-core readings,
    # confirmed present on this box: Core 0-5 alongside "Package id 0") --
    # surfaced alongside the main reading so the dashboard can show it
    # the same way CPU Usage shows per-core utilization.
    per_core = []
    for entries in zones.values():
        for entry in entries:
            if entry.label.lower().startswith("core "):
                per_core.append({"label": entry.label, "current": entry.current, "critical": entry.critical})
    per_core.sort(key=lambda e: e["label"])
    return per_core


def probe_temperature(check):
    zones = psutil.sensors_temperatures()
    if not zones:
        return "unknown", {"error": "no temperature sensors reported by this host"}
    per_core = _per_core_temperatures(zones)

    if check.thermal_zone_label:
        for chip, entries in zones.items():
            for entry in entries:
                if entry.label == check.thermal_zone_label or chip == check.thermal_zone_label:
                    status = _threshold_status(
                        entry.current, check.warning_threshold, check.critical_threshold, "above"
                    )
                    return status, {"label": entry.label or chip, "current": entry.current, "per_core": per_core}
        return "unknown", {"error": f"no sensor entry matches '{check.thermal_zone_label}'"}

    # No specific zone configured -- watch the hottest reading anywhere.
    hottest_chip, hottest_entry = None, None
    for chip, entries in zones.items():
        for entry in entries:
            if hottest_entry is None or entry.current > hottest_entry.current:
                hottest_chip, hottest_entry = chip, entry
    detail = {"label": hottest_entry.label or hottest_chip, "current": hottest_entry.current, "per_core": per_core}
    status = _threshold_status(hottest_entry.current, check.warning_threshold, check.critical_threshold, "above")
    return status, detail


def probe_transmitter_param(check, tx_client):
    if tx_client is None:
        return "unknown", {"reason": "transmitter unreachable or not configured"}
    try:
        raw = tx_client.get(check.transmitter_parameter)
    except Exception as exc:
        return "unknown", {"error": str(exc)}
    if raw is None:
        return "unknown", {"error": "no response"}
    value = parse_numeric(raw)
    if value is None:
        return "unknown", {"raw": raw, "error": "non-numeric response"}
    status = _threshold_status(value, check.warning_threshold, check.critical_threshold, check.threshold_direction)
    detail = {"value": value, "raw": raw}
    if check.transmitter_parameter == "psu.fwd_power":
        # Only the Forward Power meter needs a "percent of max" reading --
        # narrow special-case rather than a generic reference-max field
        # on MonitorCheck, since no other parameter needs this treatment.
        from monitoring.models import TransmitterConfig
        full_power = TransmitterConfig.load().full_power_watts
        if full_power:
            detail["percent_of_max"] = round(value / full_power * 100, 1)
    return status, detail


def probe_transmitter_indicator(check, tx_client):
    if tx_client is None:
        return "unknown", {"reason": "transmitter unreachable or not configured"}
    try:
        raw = tx_client.get(check.transmitter_indicator)
    except Exception as exc:
        return "unknown", {"error": str(exc)}
    if raw is None:
        return "unknown", {"error": "no response"}

    fault_values = {v.strip() for v in check.fault_values.split(",") if v.strip()}
    warn_values = {v.strip() for v in check.warn_values.split(",") if v.strip()}
    if raw in fault_values:
        return "critical", {"value": raw}
    if raw in warn_values:
        return "warning", {"value": raw}
    return "ok", {"value": raw}


def probe_audio_silence(check):
    import json
    from pathlib import Path

    state_path = Path(f"/run/isadoraair/liquidsoap_silence_{check.silence_device_slug}.json")
    if not state_path.is_file():
        return "unknown", {"reason": "no state file yet -- encoder for this device may be disabled"}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "unknown", {"error": str(exc)}

    # The liquidsoap script re-touches this file every 60s via a
    # thread.run(every=60., ...) heartbeat (see build_liquidsoap_script)
    # even when nothing has transitioned, so a short staleness bound is
    # now the *correct* signal that liquidsoap itself has wedged/died
    # rather than that the audio has simply been fine. Three missed
    # heartbeats (180s) trips "unknown". The earlier 24h bound was left
    # over from before the heartbeat existed and was guaranteed to flip
    # every healthy stream to "unknown" once per day.
    age = time.time() - data.get("timestamp", 0)
    if age > 180:
        return "unknown", {"reason": "state file hasn't updated in over 3 minutes -- liquidsoap heartbeat stopped", "age_seconds": age}
    # `since_seconds` = how long the CURRENT is_blank has held (dashboard's
    # "Stable for X" caption uses this). `age_seconds` is just the freshness
    # of the last write and is only interesting for the staleness check
    # above. Falls back to `age_seconds` when the `since` field is missing
    # so the caption keeps working through the first heartbeat after an
    # upgrade (state file was written by the old script).
    since_raw = data.get("since")
    detail = {"age_seconds": age}
    if since_raw is not None:
        detail["since_seconds"] = time.time() - since_raw
    if data.get("is_blank"):
        detail["is_blank"] = True
        return "critical", detail
    detail["is_blank"] = False
    return "ok", detail


def probe_rbds(check):
    import json
    from pathlib import Path

    state_path = Path("/run/isadoraair/rbds_state.json")
    if not state_path.is_file():
        return "unknown", {"reason": "no state file yet -- rbds service may be disabled/not yet started"}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "unknown", {"error": str(exc)}

    # Unlike audio_silence's state file (only written on a silence<->noise
    # TRANSITION, so long quiet stretches with no writes are normal), the
    # rbds engine writes this file on every ~1s tick regardless of
    # change -- so a short staleness bound here is the correct signal
    # that the process itself is stuck/dead, not a false alarm.
    age = time.time() - data.get("timestamp", 0)
    if age > 60:
        return "unknown", {"reason": "state file hasn't updated recently", "age_seconds": age}
    if not data.get("connected"):
        return "critical", {"last_error": data.get("last_error"), "down_since": data.get("down_since")}
    return "ok", {"current_ps": data.get("current_ps"), "current_rt": data.get("current_rt")}


def probe_log_slot_category(check):
    """Tripwire: verify a specific log-slot position in the CURRENT
    hour's PlaylistLog contains a track from the expected category.
    Primary use: FCC-required legal ID at top of hour -- position=0,
    category=Legal ID. Returns critical if the log doesn't exist, if
    that position doesn't exist in the log, or if the item at that
    position doesn't match the expected category."""
    from django.utils import timezone
    from library.models import PlaylistLog

    if check.log_slot_category_id is None:
        return "unknown", {"reason": "no target category configured on this check"}

    now = timezone.localtime()
    log = PlaylistLog.objects.filter(date=now.date(), hour=now.hour).first()
    if log is None:
        return "critical", {
            "reason": f"no PlaylistLog for {now.date()} h={now.hour:02d}",
            "position": check.log_slot_position,
            "expected_category": check.log_slot_category.code,
        }

    item = log.items.filter(position=check.log_slot_position).select_related("track", "track__category").first()
    if item is None:
        return "critical", {
            "reason": f"log has no item at position {check.log_slot_position}",
            "position": check.log_slot_position,
            "expected_category": check.log_slot_category.code,
        }

    track = item.track
    if track is None:
        return "critical", {
            "reason": "item's track FK is NULL (track deleted from library)",
            "position": check.log_slot_position,
            "expected_category": check.log_slot_category.code,
            "log_item_id": item.id,
        }

    actual_cat = track.category
    if actual_cat is None or actual_cat.id != check.log_slot_category_id:
        return "critical", {
            "reason": "wrong category at this position",
            "position": check.log_slot_position,
            "expected_category": check.log_slot_category.code,
            "actual_category": actual_cat.code if actual_cat else None,
            "log_item_id": item.id,
            "track_title": track.title,
        }

    return "ok", {
        "position": check.log_slot_position,
        "category": actual_cat.code,
        "track_title": track.title,
    }


PROBE_DISPATCH = {
    "systemd": probe_systemd,
    "disk": probe_disk,
    "cpu": probe_cpu,
    "memory": probe_memory,
    "temperature": probe_temperature,
    "transmitter_param": probe_transmitter_param,
    "transmitter_indicator": probe_transmitter_indicator,
    "audio_silence": probe_audio_silence,
    "rbds": probe_rbds,
    "log_slot_category": probe_log_slot_category,
}
