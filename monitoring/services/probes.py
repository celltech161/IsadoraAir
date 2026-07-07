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

    # on_blank/on_noise only fire on a real silence<->noise transition
    # (plus one unconditional write at script start) -- a feed that's
    # been continuously fine for hours is SUPPOSED to leave this file
    # untouched that whole time, so a short staleness window here was
    # simply the wrong model (caught live: it showed "unknown" forever
    # on a perfectly healthy stream). The "Stream Encoders" systemd
    # check already independently verifies the underlying process is
    # alive -- this generous bound is just a sanity trip-wire against a
    # truly ancient, forgotten file (e.g. from a device no longer in use).
    age = time.time() - data.get("timestamp", 0)
    if age > 86400:
        return "unknown", {"reason": "state file hasn't updated in over a day", "age_seconds": age}
    if data.get("is_blank"):
        return "critical", {"is_blank": True, "age_seconds": age}
    return "ok", {"is_blank": False, "age_seconds": age}


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
}
