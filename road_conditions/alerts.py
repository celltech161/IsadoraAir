"""Tells station personnel when road information stops being current.

The road report is NOT taken off air when the upstream feed fails or goes
stale (1.24-B): the last published report keeps airing and someone is told,
so that a person can decide what to do.

Everything about delivery is Monitoring's existing contract, reused as it is:
monitoring.services.notify.maybe_notify() with NotificationConfig's
recipients, enabled flag and cooldown, its incident record that gates the
RECOVERED message, and emit_event() rows on status changes. This module only
supplies the road feed's status and remembers it between runs, which the
monitoring poller does for its own checks in /run/isadoraair too.

It is evaluated by sync_road_conditions on each of its roughly one-minute
invocations; there is no separate poller. If that command stops running
altogether, nothing here notices."""
import json
import logging
import os
import socket
from pathlib import Path
from types import SimpleNamespace

from django.utils import timezone as dj_timezone

from monitoring.models import NotificationConfig, emit_event
from monitoring.services.notify import maybe_notify

from .models import RoadConditionsConfiguration

logger = logging.getLogger(__name__)

STATE_PATH = Path("/run/isadoraair/road_conditions_alert_state.json")
# No EMAIL_TIMEOUT is configured; a hung mail server must not stall the sync.
SEND_TIMEOUT_SECONDS = 20

# The shape maybe_notify() expects of a Monitoring check. Deliberately not a
# MonitorCheck row: a new check kind would need a migration.
FEED_CHECK = SimpleNamespace(
    id="road_conditions_feed", name="Road Conditions Data",
    notify_on_warning=True, notify_on_critical=True,
    get_kind_display=lambda: "Road conditions upstream feed",
)
_SEVERITY = {"ok": 0, "warning": 1, "critical": 2}
_EVENT_LEVEL = {"ok": "info", "warning": "warning", "critical": "critical"}


def feed_status(config):
    """("ok" | "warning" | "critical", detail), or (None, {}) when there is
    nothing to judge: the feature is disabled, or no retrieval has been
    attempted yet. critical = the stored data is older than the configured
    freshness limit; warning = the latest retrieval failed but the stored
    data is still within it."""
    if not config.enabled or config.last_fetch_attempted_at is None:
        return None, {}
    succeeded = config.last_fetch_succeeded_at
    detail = {
        "last successful retrieval": (
            dj_timezone.localtime(succeeded).strftime("%Y-%m-%d %H:%M %Z") if succeeded else "never"),
        "freshness limit (minutes)": config.stale_data_threshold_minutes,
        "latest error": config.last_error or "none",
        "on air": "the last published road report is unchanged; Ready-to-Air was not modified",
    }
    if config.is_stale:
        return "critical", {"reason": "stored road data is older than the freshness limit", **detail}
    if config.last_error:
        return "warning", {"reason": "the latest upstream retrieval failed", **detail}
    return "ok", {"reason": "upstream retrieval is succeeding", **detail}


def _load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if (isinstance(state, dict) and state.get("status") in _SEVERITY
                and isinstance(state.get("cooldowns"), dict) and isinstance(state.get("incidents"), dict)):
            return state
    except (OSError, ValueError):
        pass
    return {"status": "ok", "cooldowns": {}, "incidents": {}}


def _save_state(state):
    tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        return True
    except OSError as exc:
        logger.warning("road_conditions: could not save the alert state: %s", exc)
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _needs_delivery(status, state):
    """True until this incident has had a delivery attempt at this severity
    (or a worse one). Attempts held back by the cooldown, or by missing
    recipients or disabled notifications, are not attempts."""
    incident = state["incidents"].get(FEED_CHECK.id) or {}
    return _SEVERITY[status] > _SEVERITY.get(incident.get("attempted_severity"), 0)


def evaluate_feed():
    """Records feed status changes and sends at most one notification per
    severity per incident, plus one recovery. Safe to call every minute.
    Never raises: a notification problem must not fail the sync."""
    try:
        _evaluate()
    except Exception as exc:  # noqa: BLE001
        logger.warning("road_conditions: could not evaluate the feed alert: %s", exc)


def _evaluate():
    config = RoadConditionsConfiguration.load()
    status, detail = feed_status(config)
    state = _load_state()
    previous = state["status"]

    if status is None:
        # Disabled (or never attempted): close any open incident silently.
        if previous != "ok" or state["incidents"] or state["cooldowns"]:
            _save_state({"status": "ok", "cooldowns": {}, "incidents": {}})
        return

    changed = status != previous
    recovery_due = status == "ok" and bool(state["incidents"])
    delivery_due = status != "ok" and _needs_delivery(status, state)
    if not (changed or recovery_due or delivery_due):
        return

    state["status"] = status
    # Saved BEFORE anything is sent: if the state cannot be kept, sending
    # would repeat on every invocation.
    if not _save_state(state):
        return

    if changed:
        emit_event(
            category="road_conditions", level=_EVENT_LEVEL[status],
            title=f"{FEED_CHECK.name}: {previous} → {status}", detail=detail,
            dedupe_key=f"road_conditions|feed-status|to={status}",
        )
    if not (recovery_due or delivery_due):
        return

    default_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(SEND_TIMEOUT_SECONDS)
    try:
        maybe_notify(
            FEED_CHECK, status, detail, previous, state["cooldowns"], state["incidents"],
            config=NotificationConfig.load(),
        )
    finally:
        socket.setdefaulttimeout(default_timeout)
    _save_state(state)
