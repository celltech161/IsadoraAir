"""The managed worker unit's process evidence (iPortal D3E-B).

deploy/isadoraair-active-message-worker.service runs ``manage.py
active_message_worker``; this module decides, from the D3E-A heartbeat
(active_messages.heartbeat -- the ONLY decoder and interpreter of the
document) and from the identity of the process systemd is running, two
things:

* ``startup_failure`` -- the unit's start gate (``manage.py
  active_message_worker_ready``, its ExecStartPost=): has THIS process
  written a valid heartbeat and finished its first pass (or begun a render)?
* ``observe`` -- Monitoring's view of an ACTIVE unit
  (monitoring.services.probes): the heartbeat's readiness verdict, but only
  once the document is proven to belong to the running process.

Identity, without changing the heartbeat schema: the document records the
writer's ``pid`` and ``started_at`` (wall clock, taken just after the
process started). It belongs to the process systemd is running when the pid
equals the unit's MainPID AND ``started_at`` is not earlier than that
process's own start (less START_SLACK_SECONDS: systemd prints whole seconds
and the kernel's start time is derived from a whole-second boot time). A
document from an earlier process fails at least one of the two -- a
different pid, or (pid reuse) a start before the new process existed -- so a
stale file left in /run/isadoraair can never stand in for the current worker.

Both are read-only: a file read and, through ``heartbeat.readiness``'s own
rules, at most a SELECT of the native-airing gate. Nothing here renders,
expires, materializes or writes anything.
"""
from __future__ import annotations

import math
from pathlib import Path

from . import heartbeat

# A whole-second systemd timestamp / kernel start time is at most this much
# earlier than the true start; a previous process started well before.
START_SLACK_SECONDS = 2.0

# Monitoring: how long after the process started a missing or not-yet-current
# heartbeat is "starting" (unknown, never ok) rather than critical. Equal to
# the unit's TimeoutStartSec: by then the start gate has passed or failed.
STARTUP_GRACE_SECONDS = 90

# The start gate's default and ceiling (the unit uses --timeout 45 inside its
# TimeoutStartSec=90). Start-up is Django, the heartbeat's first write and one
# pass: no render is waited for (a render in progress counts).
STARTUP_TIMEOUT_SECONDS = 45.0
MAX_STARTUP_TIMEOUT_SECONDS = 120.0

LIVE_PHASES = frozenset({"starting", "polling", "rendering"})


def start_time_known(process_started_at) -> bool:
    """Is ``process_started_at`` a usable process start (finite epoch seconds)?"""
    return (type(process_started_at) in (int, float) and math.isfinite(process_started_at)
            and heartbeat.MIN_TIMESTAMP <= process_started_at <= heartbeat.MAX_TIMESTAMP)


def belongs(beat: heartbeat.Heartbeat, *, pid, process_started_at) -> bool:
    """Was ``beat`` written by the process ``pid`` that started at
    ``process_started_at`` (epoch seconds)? Fails closed: PID equality alone
    is never enough -- PIDs are reused -- so an unknown or invalid start time
    is never a match."""
    if type(pid) is not int or pid <= 0 or beat.pid != pid:
        return False
    if not start_time_known(process_started_at):
        return False
    return beat.started_at >= process_started_at - START_SLACK_SECONDS


def startup_failure(document, unreadable, *, pid, process_started_at, now) -> str:
    """"" when the worker ``pid`` has started (see the module docstring),
    else a stable reason: missing / unreadable / not_a_regular_file /
    oversized / invalid_json (the file), malformed, future_timestamp,
    previous_process, stopped, first_pass_pending."""
    if document is None:
        return unreadable or "missing"
    try:
        beat = heartbeat.validate(document)
    except heartbeat.Malformed:
        return "malformed"
    except Exception:                                    # noqa: BLE001 -- a refusal, not a crash
        return "malformed"
    if not belongs(beat, pid=pid, process_started_at=process_started_at):
        return "previous_process"
    if beat.checked_at > now + heartbeat.MAX_FUTURE_SKEW_SECONDS:
        return "future_timestamp"
    if beat.phase not in LIVE_PHASES:
        return "stopped"
    if beat.cycle == 0 and beat.phase != "rendering":
        return "first_pass_pending"
    return ""


# What Monitoring shows of a verified heartbeat (heartbeat._verdict's detail):
# short, bounded, no path, pid history or raw document.
_SHOWN = (("phase", "phase"), ("result", "outcome"), ("code", "code"), ("execution", "execution"),
          ("native_airing", "native_airing"), ("age_seconds", "heartbeat_age_seconds"))


def observe(*, main_pid, process_started_at, now, path=None) -> dict:
    """Monitoring's verdict for an ACTIVE unit: {"status", "condition",
    "detail"}. ``main_pid``/``process_started_at`` are systemd's MainPID and
    ExecMainStartTimestamp (epoch seconds, or None when unparseable).

    * no heartbeat -- "starting" (unknown) within STARTUP_GRACE_SECONDS of the
      process start, else "no_heartbeat" (critical);
    * a valid heartbeat from another process -- "starting" (unknown) within
      the grace, else "heartbeat_not_current" (critical);
    * no usable MainPID, or no usable start time (ExecMainStartTimestamp
      missing, empty or unparseable) -- "identity_unavailable" (unknown):
      nothing proves which process wrote the file, so it is never reported
      as current health (and never guessed from the file itself);
    * otherwise exactly heartbeat.assess's status and condition (malformed,
      future-dated, stale, crash loop, render overdue... included)."""
    document, unreadable = heartbeat.read_state(Path(path) if path is not None else heartbeat.state_path())
    verdict = heartbeat.assess(document, now=now, gate_open=heartbeat.gate_open_read_only(),
                               unreadable=unreadable or None)
    detail = {"native_airing": verdict["detail"]["native_airing"]}
    starting = start_time_known(process_started_at) and now - process_started_at < STARTUP_GRACE_SECONDS

    def found(status, condition, **extra):
        return {"status": status, "condition": condition, "detail": {**detail, **extra}}

    if verdict["condition"] == "malformed":               # broken whoever wrote it
        return found("critical", "malformed", problem=str(verdict["detail"].get("reason", ""))[:64])
    if type(main_pid) is not int or main_pid <= 0 or not start_time_known(process_started_at):
        return found("unknown", "identity_unavailable")
    if verdict["condition"] == "no_heartbeat":
        return found("unknown", "starting") if starting else found("critical", "no_heartbeat")
    beat = heartbeat.validate(document)                  # assess accepted it: valid
    if not belongs(beat, pid=main_pid, process_started_at=process_started_at):
        return found("unknown", "starting") if starting else found("critical", "heartbeat_not_current")
    shown = {name: verdict["detail"][key] for key, name in _SHOWN if key in verdict["detail"]}
    return found(verdict["status"], verdict["condition"], **shown)
