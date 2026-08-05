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


# Reasonable tolerance for a state-file "timestamp" field to sit in the
# FUTURE relative to this process's own clock. Every writer and reader of
# these state files runs on the same box (see PROJECT_NOTES.md), so there
# is no real cross-host NTP skew to accommodate here -- this margin exists
# purely to distinguish ordinary scheduling jitter between a writer's
# time.time() call and this probe's own time.time() call a moment later
# from a genuinely corrupted or clock-jumped timestamp. It is deliberately
# generous in the "how far future is still plausible" direction without
# weakening any of the staleness (too OLD) checks below, which are
# unaffected by this constant.
CLOCK_SKEW_TOLERANCE_SECONDS = 60


def _state_freshness(data, now):
    """Shared malformed/future-timestamp guard for every probe that reads
    one of the manager's or liquidsoap's JSON state files and computes
    "how old is this."

    Returns (age_seconds, is_valid). is_valid is False -- and callers MUST
    treat the state as untrustworthy, never as fresh -- when "timestamp" is
    missing, not a real number, or implausibly far in the future (more than
    CLOCK_SKEW_TOLERANCE_SECONDS ahead of `now`). Without this guard, a
    missing timestamp defaults harmlessly via .get(..., 0) (reads as
    ancient -- correctly stale), but a malformed one (e.g. an explicit None,
    or a corrupted non-numeric write) would raise TypeError out of a bare
    subtraction, and a future-dated one would produce a negative age that
    silently never trips any `age > THRESHOLD` check -- i.e. treated as
    indefinitely fresh. Both are exactly backwards for a safety check whose
    entire job is "do not trust unverifiable data as healthy.\""""
    raw = data.get("timestamp")
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None, False
    age = now - ts
    if age < -CLOCK_SKEW_TOLERANCE_SECONDS:
        return age, False
    return age, True


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
    age, age_valid = _state_freshness(data, time.time())
    if not age_valid:
        return "unknown", {"reason": "state file has a missing or implausible timestamp", "age_seconds": age}
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

    # Three real states (2026-08-05 hardening), not a boolean --
    # is_blank is `null` while Liquidsoap is still starting up and
    # hasn't verified any real audio yet, which is NOT the same as
    # "confirmed not blank." The previous version of this probe used
    # `if data.get("is_blank"):`, a truthiness test under which `None`
    # is falsy -- exactly the same falsy value as `False` -- so a
    # freshly-(re)started, about-to-crash Liquidsoap process re-writing
    # its own honest "not yet verified" `null` was silently read as
    # "confirmed fine." That was the actual root cause of a real
    # production outage staying green throughout: a crash-looping
    # encoder respawns every 15-20s, each instance re-asserting this
    # same "null" a moment before dying, always beating the 180s
    # staleness window above. Explicit three-way check now.
    is_blank = data.get("is_blank")
    detail["is_blank"] = is_blank
    if is_blank is None:
        detail["reason"] = f"{data.get('status', 'starting')} -- no audio verified yet"
        return "unknown", detail
    if is_blank:
        return "critical", detail
    return "ok", detail


# --- kind="encoder_group" timing constants (2026-08-05 hardening) ---
# How long a fresh child gets to prove itself before "still starting"
# becomes "critical -- something's actually wrong." Matches the
# suggested range from the incident review.
ENCODER_STARTUP_ALLOWANCE_SECONDS = 30
# The encoder manager refreshes its own group-state file on every
# _check_health() tick (every HEALTH_CHECK_SECONDS=5s -- see
# encoders/services/encoder_manager.py) regardless of whether anything
# changed, specifically so this can be a TIGHT staleness bound (a
# genuine "the manager's own supervision loop is stuck" signal, not
# just "nothing happened lately") -- 6x the actual write cadence, a
# comfortable margin against scheduling jitter without being loose
# enough to miss a real stall for long.
ENCODER_GROUP_STALE_SECONDS = 30
# Matches probe_audio_silence's own established 180s bound exactly (3x
# Liquidsoap's 60s heartbeat) -- same file, same convention, not a
# second independently-tuned threshold for the same signal.
AUDIO_STATE_STALE_SECONDS = 180


def probe_encoder_group(check):
    """The truthful aggregate for one encoder input-device group,
    combining every signal a single systemd/audio_silence check could
    not, on its own, tell apart during the real 2026-08-05 outage:

      1. Is the encoder-manager systemd service itself active?
      2. Is there a LIVE Liquidsoap child for this group (not just "a
         process object exists somewhere," but the CURRENT generation,
         matched by pid+generation between the manager's own state
         file and Liquidsoap's self-report)?
      3. Has that child actually observed real, non-blank audio?
      4. Is every enabled Encoder in this group's Shoutcast SID
         actually connected (STREAMSTATUS=1), not just "the process
         that would send it hasn't crashed"?

    None of these four proves the station is on the air by itself --
    see the module-level docstring in encoders/services/encoder_manager.py
    and PROJECT_NOTES.md's 2026-08-05 incident writeup for exactly how
    each one alone stayed green while the stream was actually down.

    Deliberately reuses existing code rather than re-implementing it:
    probe_systemd for the manager's own ActiveState, and
    monitoring.services.shoutcast.fetch_shoutcast_stats (also used by
    MonitorManager's listener poll) for the real, external Shoutcast
    connection state -- the same signal that actually proved the
    station was down during the incident, independent of anything this
    project's own monitoring reported."""
    import json
    from collections import defaultdict
    from types import SimpleNamespace

    from encoders.models import Encoder
    from encoders.services.encoder_manager import (
        DEFAULT_INPUT_DEVICE, STABILIZATION_SECONDS, _audio_state_path_for_slug,
        _effective_input_device, _group_state_path_for_slug, _slug,
    )
    from monitoring.services.shoutcast import fetch_shoutcast_stats

    slug = check.encoder_group_slug
    detail = {"input_device_slug": slug}

    # 1. Which enabled encoders actually belong to this group -- same
    # grouping logic the manager itself uses (_effective_input_device
    # falls back to DEFAULT_INPUT_DEVICE for a blank Encoder.input_device),
    # so this can never disagree with what the manager actually launched.
    encoders = [
        e for e in Encoder.objects.filter(enabled=True)
        if _slug(_effective_input_device(e)) == slug
    ]
    if not encoders:
        return "unknown", {**detail, "reason": "no enabled encoders configured for this input device group"}

    # 2. Manager systemd unit -- reuse probe_systemd's own logic
    # exactly rather than re-parsing `systemctl show` output here too.
    systemd_status, systemd_detail = probe_systemd(SimpleNamespace(systemd_unit=check.encoder_group_systemd_unit))
    detail["manager_systemd"] = systemd_detail
    if systemd_status != "ok":
        return "critical", {**detail, "reason": f"encoder-manager service ({check.encoder_group_systemd_unit}) is not active"}

    # 3. Manager-owned process-supervision state.
    group_state_path = _group_state_path_for_slug(slug)
    if not group_state_path.is_file():
        return "critical", {**detail, "reason": "no group-state file yet -- encoder manager may not have started this group"}
    try:
        group_state = json.loads(group_state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "unknown", {**detail, "error": f"group-state file unreadable: {exc}"}

    now = time.time()
    group_age, group_age_valid = _state_freshness(group_state, now)
    if not group_age_valid:
        return "critical", {
            **detail,
            "reason": "group-state file has a missing or implausible timestamp",
            "group_state_age_seconds": group_age,
        }
    if group_age > ENCODER_GROUP_STALE_SECONDS:
        return "critical", {
            **detail,
            "reason": "encoder manager not reporting -- its own supervision loop may be stuck",
            "group_state_age_seconds": group_age,
        }

    consecutive_failures = group_state.get("consecutive_failures", 0)
    detail.update({
        "consecutive_failures": consecutive_failures,
        "last_failure_message": group_state.get("last_failure_message", ""),
        "last_exit_code": group_state.get("last_exit_code"),
        "next_retry_at": group_state.get("next_retry_at"),
    })
    if consecutive_failures >= 3:
        return "critical", {
            **detail,
            "reason": (
                f"Liquidsoap exited {consecutive_failures} times in a row -- "
                f"last: {group_state.get('last_failure_message') or 'unknown'}"
            ),
        }

    manager_pid = group_state.get("pid")
    manager_generation = group_state.get("generation")
    if manager_pid is None or manager_generation is None:
        return "critical", {**detail, "reason": "no live child process for this group"}

    # 4. Liquidsoap's own self-report -- MUST be the same generation the
    # manager currently considers live (Phase 8's core hardening point:
    # a stale file from an earlier, already-superseded process must
    # never be mistaken for the current one).
    audio_state_path = _audio_state_path_for_slug(slug)
    if not audio_state_path.is_file():
        return "critical", {**detail, "reason": "no audio-state file yet for the current child"}
    try:
        audio_state = json.loads(audio_state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "unknown", {**detail, "error": f"audio-state file unreadable: {exc}"}

    if audio_state.get("generation") != manager_generation or audio_state.get("pid") != manager_pid:
        return "critical", {
            **detail,
            "reason": "audio-state file is stale -- belongs to a previous process, not the current one",
            "audio_state_generation": audio_state.get("generation"),
            "expected_generation": manager_generation,
        }

    audio_age, audio_age_valid = _state_freshness(audio_state, now)
    if not audio_age_valid:
        # Same severity as the plain-staleness branch immediately below
        # (both mean "cannot confirm the audio-state file is current"),
        # not the "critical" used for a missing group-state file or a
        # generation mismatch -- this file parses fine and its generation
        # already matched the manager's, so the failure mode here is
        # squarely "unverifiable freshness," the same epistemic state as
        # an ordinary stale heartbeat.
        return "unknown", {
            **detail,
            "reason": "audio-state file has a missing or implausible timestamp",
            "audio_state_age_seconds": audio_age,
        }
    if audio_age > AUDIO_STATE_STALE_SECONDS:
        return "unknown", {**detail, "reason": "liquidsoap heartbeat stopped", "audio_state_age_seconds": audio_age}

    status = audio_state.get("status", "starting")
    is_blank = audio_state.get("is_blank")
    detail["liquidsoap_status"] = status
    detail["is_blank"] = is_blank

    launched_at = group_state.get("launched_at")
    startup_elapsed = (now - launched_at) if launched_at else None
    detail["startup_elapsed_seconds"] = startup_elapsed

    if status == "starting" or is_blank is None:
        if startup_elapsed is not None and startup_elapsed > ENCODER_STARTUP_ALLOWANCE_SECONDS:
            return "critical", {
                **detail,
                "reason": f"startup allowance ({ENCODER_STARTUP_ALLOWANCE_SECONDS}s) expired without real audio",
            }
        return "warning", {**detail, "reason": "starting"}

    if is_blank:
        return "critical", {**detail, "reason": "silent -- no audio detected"}

    # 5. Real, external Shoutcast destination status -- the signal that
    # actually caught the 2026-08-05 outage, independent of anything
    # this project's own monitoring reported. Listener count never
    # participates -- a stream with zero listeners can still be healthy.
    by_server = defaultdict(list)
    for enc in encoders:
        by_server[(enc.host, enc.port)].append(enc)

    sid_results = []
    server_unreachable = False
    for (host, port), encs_here in by_server.items():
        stats = fetch_shoutcast_stats(host, port)
        if not stats:
            server_unreachable = True
        for enc in encs_here:
            sid = enc.shoutcast_sid
            s = stats.get(sid)
            sid_results.append({
                "encoder_id": enc.id, "name": enc.name, "sid": sid,
                "up": bool(s and s["up"]), "present": s is not None,
            })
    detail["destinations"] = sid_results

    down = [r for r in sid_results if not r["up"]]
    if down:
        if server_unreachable:
            # A single unreachable poll can't distinguish "genuinely
            # down" from "couldn't ask right now" -- warning, not an
            # immediate critical, gives it one cycle to recover before
            # MonitorManager's own consecutive-failure debounce (a
            # SEPARATE, coarser mechanism upstream) escalates a
            # persistent outage on its own.
            return "warning", {**detail, "reason": "Shoutcast server unreachable -- destination status unknown"}
        parts = [
            f"SID {r['sid']} ({r['name']}): {'absent' if not r['present'] else 'STREAMSTATUS=0'}"
            for r in down
        ]
        return "critical", {**detail, "reason": "destination(s) down -- " + "; ".join(parts)}

    # 6. Stabilization gate -- everything above is momentarily green,
    # but has it been true for long enough to trust? `since` on the
    # audio-state file (when the current is_blank=false began) is the
    # one continuous-truth timestamp available -- Shoutcast has no
    # per-poll equivalent of its own, so audio's own transition clock
    # gates the WHOLE aggregate. Same STABILIZATION_SECONDS value
    # encoders/services/encoder_manager.py's own backoff-reset check
    # uses -- one shared "how long is long enough" concept, not two
    # independently-tuned ones that could disagree.
    since = audio_state.get("since")
    stable_seconds = (now - since) if since is not None else 0
    detail["stable_seconds"] = stable_seconds
    if stable_seconds < STABILIZATION_SECONDS:
        return "warning", {**detail, "reason": f"stabilizing ({stable_seconds:.0f}s of {STABILIZATION_SECONDS}s)"}

    return "ok", {**detail, "reason": "healthy"}


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
    "encoder_group": probe_encoder_group,
    "rbds": probe_rbds,
}
