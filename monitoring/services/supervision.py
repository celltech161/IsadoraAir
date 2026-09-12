"""P1 1.11 -- tiny process-supervision marker for the Monitoring poller
itself, so an operator can tell "Monitoring recently died or was
watchdog-restarted" AFTER the fact, instead of that evidence vanishing
the instant the next (healthy) process writes a fresh green
monitoring_state.json over it.

Deliberately NOT a restart history -- exactly one small JSON object,
overwritten on every process start, describing only the MOST RECENT
invocation. No secrets, no logs, no unbounded growth. Living under
/run/isadoraair (tmpfs) already bounds this to "since the last reboot"
with zero code needed here: an ordinary machine boot must never be
mislabeled as an unclean Monitoring restart, and it isn't, because
there is simply no marker left over to compare against once the
partition itself is gone.

Mechanism: `clean_shutdown` starts False the instant a new invocation
writes its own marker (record_new_invocation), and is flipped to True
-- rewriting that SAME file -- only by mark_clean_shutdown(), called
from MonitorManager's own ordinary SIGTERM/SIGINT-triggered exit path
(an Update Center restart, a plain `systemctl stop`/`restart`, a
normal reboot's shutdown sequence). Anything that does NOT reach that
path -- a watchdog-triggered SIGABRT (see deploy/isadoraair-
monitoring.service's WatchdogSec= comment), an OOM-kill, `kill -9`, an
uncaught exception escaping MonitorManager.start()'s own loop -- never
gets a chance to flip that flag, leaving clean_shutdown=False sitting
in the file for the NEXT invocation's record_new_invocation() call to
discover and report on."""
import json
import os
import time
import uuid
from pathlib import Path

MARKER_PATH = Path("/run/isadoraair/monitoring_supervision.json")


def _read_marker():
    try:
        data = json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_marker(data):
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARKER_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(MARKER_PATH)


def record_new_invocation(*, runtime_commit=None):
    """Called exactly once, from MonitorManager.start(), before the
    poll loop begins. Returns (prior, armed):

      * prior -- the PRIOR marker's dict, or None if this is the first
        invocation since boot, or the prior file was missing/malformed
        -- all three treated identically as "nothing to report." The
        read is unconditional and always attempted regardless of
        whether the write below succeeds.
      * armed -- True if THIS invocation's own fresh marker was
        actually written; False if that write failed (an unwritable/
        full /run/isadoraair, a permissions regression, or any other
        OSError).

    The supervision marker is SUPPLEMENTAL evidence only, never part
    of Monitoring's own authoritative health signal
    (monitoring_state.json) -- this function NEVER raises. A failure
    to persist it must never prevent the poller from starting or
    stopping normally, and must never turn into a Restart=on-failure
    crash loop over a filesystem problem this feature has no business
    being fatal about.

    The caller MUST treat `prior` as reportable evidence ONLY when
    `armed` is True. Rationale: if persistence itself is broken
    (write fails every time), every subsequent invocation would
    otherwise keep re-reading the SAME stale prior marker (nothing
    ever succeeds in overwriting it) and re-report the identical
    incident on every single restart forever. Gating on `armed`
    collapses that to "stay silent while storage is unavailable"
    instead -- a single missed incident report under a rare
    persistence hiccup is the correct trade against spamming the same
    stale incident indefinitely."""
    prior = _read_marker()
    try:
        _write_marker({
            "pid": os.getpid(),
            "invocation_id": uuid.uuid4().hex,
            "started_at": time.time(),
            "runtime_commit": runtime_commit,
            "clean_shutdown": False,
        })
        armed = True
    except OSError:
        armed = False
    return prior, armed


def mark_clean_shutdown():
    """Called exactly once, from MonitorManager.start()'s own loop-exit
    path, right after `self.running` goes False and the in-flight
    cycle/sleep has already returned normally -- i.e. only on the
    genuinely graceful shutdown path. Rewrites the CURRENT invocation's
    marker with clean_shutdown=True, so the NEXT invocation's
    record_new_invocation() sees a clean prior record and stays silent.

    A no-op (never raises) if the marker is missing or belongs to a
    different pid than this process -- either would mean something
    else already replaced it, and there is nothing left here worth
    correcting."""
    current = _read_marker()
    if not isinstance(current, dict) or current.get("pid") != os.getpid():
        return
    current["clean_shutdown"] = True
    try:
        _write_marker(current)
    except OSError:
        pass


def prior_invocation_was_unclean(prior):
    """True only when `prior` (record_new_invocation()'s return value)
    describes a real previous invocation that did NOT reach a graceful
    shutdown. False for None (no prior marker at all -- first start
    since boot, or a missing/malformed file; NEVER treated as an
    incident) and for any prior record already marked clean."""
    return isinstance(prior, dict) and prior.get("clean_shutdown") is not True
