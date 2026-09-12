"""P1 1.11 -- independent self-health determination for the Monitoring
Service's OWN dashboard card.

The problem this closes: a MonitorCheck row's status/detail in
monitoring_state.json (kind="systemd", systemd_unit=
"isadoraair-monitoring.service") is written by the very process this
card exists to watch. A dead or wedged poller therefore leaves an OLD
GREEN "Running" card sitting directly underneath the page's own,
separately-computed stale banner (see monitoring/views.py's
STATE_STALE_SECONDS check) -- the one card an operator most needs to
trust when the poller has actually stopped doing useful work is the
one card GUARANTEED to be wrong in exactly that scenario.

monitoring/views.py's api_monitoring_status calls
apply_self_health_override() ONLY when the overall Monitoring state is
already stale (that decision is made by the caller, from data
INDEPENDENT of anything in this module). This module is never
consulted on an ordinary, fresh poll -- see apply_self_health_override's
own docstring for why the one subprocess call this module makes
(`systemctl show`) is deliberately confined to that already-rare path,
rather than running on every 5-second browser poll.

Distinguishes (via detail["reason"]) the useful cases an operator
would want to tell apart when this card goes red:
  * "service_inactive"                     -- systemd itself confirms
    the unit is not running (stopped, failed, crash-looping).
  * "heartbeat_stale_process_alive"         -- systemd says the unit
    IS active, but monitoring_state.json's own timestamp is still
    stale. This is the P1 1.11 poster case: an alive PID that stopped
    doing useful work (wedged in a poll cycle, deadlocked, etc.).
  * "no_heartbeat_recorded"                 -- systemd says active,
    but there has never been a usable timestamp to measure staleness
    against at all (monitoring_state.json missing/malformed -- a
    freshly (re)installed station, or the state file was deleted out
    from under a still-running process).
  * "heartbeat_stale_systemd_unavailable"   -- `systemctl` itself
    could not be consulted (missing, timed out, errored). The
    heartbeat is independently already known to be stale (that's the
    only reason this module is being asked at all) -- this reason
    exists so the operator knows systemd's own opinion was
    unavailable, not that it was consulted and said "active."

A stale heartbeat is ALWAYS reported as unhealthy (status="critical")
regardless of what systemd says -- systemd's ActiveState only adds
detail on WHY, it can never downgrade severity back toward "ok". An
"active" unit with a dead heartbeat is not healthy; that split is the
entire reason this module exists."""
import os
import subprocess

MONITORING_UNIT = "isadoraair-monitoring.service"

_SYSTEMCTL_TIMEOUT_SECONDS = 5
# Same fixed-TZ subprocess environment monitoring/services/probes.py's
# own probe_systemd() already established, for the same reason (see
# that function's own comment): django.setup() sets this process's own
# TZ env var from settings.TIME_ZONE, which every subprocess inherits,
# localizing `systemctl show`'s timestamp output. Not actually needed
# here today (this module doesn't parse ActiveEnterTimestamp), kept
# for parity/safety if a future reason ever wants uptime too.
_UTC_ENV = {**os.environ, "TZ": "UTC"}


def _probe_monitoring_unit_active_state():
    """Returns (active_state, sub_state) ONLY when systemd actually
    provided a valid, successful ActiveState -- None in every other
    case:
      * subprocess.run() raised (missing systemctl binary, ...);
      * the command timed out;
      * a NONZERO return code -- systemctl itself failed (e.g. the
        system/session bus is unreachable, dbus is down) rather than
        successfully reporting the unit's state;
      * output that doesn't contain a usable (non-empty) ActiveState
        at all.

    This distinction matters: a failed/unavailable systemctl query
    must surface as reason="heartbeat_stale_systemd_unavailable" (see
    _build_override_status), never be misclassified as
    reason="service_inactive" -- the latter is a POSITIVE claim that
    systemd confirmed the unit is not running, a materially different
    and more specific statement than "we could not ask." Silently
    defaulting a missing ActiveState to "unknown" (the earlier version
    of this function did) would have made an inconclusive query look
    identical to "systemd says the unit is not active," which is
    exactly the misclassification this corrects.

    A narrow, read-only reuse of probe_systemd's exact `systemctl show`
    invocation shape (monitoring/services/probes.py) -- not calling
    that function directly since it takes a MonitorCheck instance and
    this call site has only a fixed, hardcoded unit name, independent
    of whatever a station may or may not have configured in the
    MonitorCheck table."""
    try:
        result = subprocess.run(
            ["systemctl", "show", MONITORING_UNIT,
             "--property=ActiveState,SubState"],
            capture_output=True, text=True, timeout=_SYSTEMCTL_TIMEOUT_SECONDS, check=False,
            env=_UTC_ENV,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    props = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    active_state = props.get("ActiveState")
    if not active_state:
        return None
    return active_state, props.get("SubState", "")


def _build_override_status(heartbeat_age_seconds):
    """The actual (status, detail) pair -- see this module's own
    docstring for the four distinguished `reason` values."""
    detail = {"heartbeat_age_seconds": heartbeat_age_seconds}
    probe = _probe_monitoring_unit_active_state()
    if probe is None:
        detail["reason"] = "heartbeat_stale_systemd_unavailable"
        return "critical", detail

    active_state, sub_state = probe
    detail["active_state"] = active_state
    detail["sub_state"] = sub_state
    if active_state != "active":
        detail["reason"] = "service_inactive"
        return "critical", detail

    detail["reason"] = (
        "no_heartbeat_recorded" if heartbeat_age_seconds is None
        else "heartbeat_stale_process_alive"
    )
    return "critical", detail


def _synthesize_from_config(heartbeat_age_seconds):
    """Builds a card-shaped dict (same keys MonitorManager._build_result
    produces) directly from MonitorCheck's own configuration, with NO
    dependency on monitoring_state.json at all -- used when that state
    is missing/malformed/doesn't (yet) contain a Monitoring Service
    row. Returns None if no ENABLED kind="systemd" check targets this
    exact unit -- this must never invent a card an operator hasn't
    configured. Imports MonitorCheck lazily to avoid a Django-apps-not-
    ready import cycle for any non-Django caller of this module (there
    are none today, but self_health.py otherwise has zero Django
    dependency, and it's one line to keep it that way)."""
    from monitoring.models import MonitorCheck

    check = (
        MonitorCheck.objects
        .filter(enabled=True, kind="systemd", systemd_unit=MONITORING_UNIT)
        .first()
    )
    if check is None:
        return None
    status, detail = _build_override_status(heartbeat_age_seconds)
    return {
        "id": check.id,
        "name": check.name,
        "kind": check.kind,
        "sort_order": check.sort_order,
        "status": status,
        "detail": detail,
        "since": None,
        "show_as_card": check.show_as_card,
        "tx_ref": None,
        "systemd_unit": check.systemd_unit,
    }


def apply_self_health_override(checks, heartbeat_age_seconds):
    """Returns a NEW checks list (the input is never mutated in place)
    where the Monitoring Service's own card -- identified by
    systemd_unit == MONITORING_UNIT, NEVER by its editable display
    name -- reflects INDEPENDENT evidence (systemd's own ActiveState
    plus the heartbeat's own age) instead of the dead/wedged poller's
    last self-report.

    Only ever called by the caller (api_monitoring_status) once it has
    ALREADY decided, from data this module never touches, that the
    overall Monitoring state is stale -- this keeps the one subprocess
    call this module makes off the hot path of a healthy station's
    ordinary 5-second dashboard polling.

    If no matching row appears in `checks` at all (state file missing/
    malformed, or simply doesn't contain this unit's row yet),
    synthesizes one from MonitorCheck's own configuration instead --
    see _synthesize_from_config's own docstring for why that never
    invents an unconfigured card."""
    updated = []
    status_detail = None
    found = False
    for check in checks:
        if check.get("systemd_unit") == MONITORING_UNIT:
            if status_detail is None:
                status_detail = _build_override_status(heartbeat_age_seconds)
            status, detail = status_detail
            check = dict(check)
            check["status"] = status
            check["detail"] = detail
            found = True
        updated.append(check)

    if not found:
        synthesized = _synthesize_from_config(heartbeat_age_seconds)
        if synthesized is not None:
            updated.append(synthesized)

    return updated
