"""Roadmap 1.20 (revised): operator-initiated engine restart safeguard.

One read-only assessment of ``/run/isadoraair/engine_state.json`` that every
operator-initiated *engine* restart entry point consults before it asks the
protected broker (or the protected updater) to act:

  * Update Center ``start_update`` -- only when the trusted plan's
    ``services_requiring_restart`` contains ``isadoraair-engine``;
  * Monitoring ``api_restart_check`` -- only for the engine's own unit;
  * Django Admin Audio Pipeline save -- only when ``sample_rate`` or
    ``program_gain_db`` changed (the fields that restart the engine).

This module is deliberately small and mechanical:

  * It READS engine status; it never writes it, never signals the engine,
    never touches microphone ownership, Remote DJ sessions/WebRTC, systemd
    recovery, the protected updater or the broker.
  * It FAILS CLOSED: anything that is not a fresh, well-formed, supported
    snapshot is ``unknown`` -- never "safe". A microphone that is Off does
    not prove a Remote DJ is disconnected, so Remote DJ is judged from its own
    fields only.
  * It never copies the Remote DJ owner block, attempt ids, tokens or signaling
    credentials out of the status file: only booleans and a few enumerated
    strings leave this module.
  * The override contract (superuser only, explicit, per-request, with a
    written reason, audited) lives here so all three entry points share one
    implementation. There is no persistent bypass flag and nothing is stored
    between requests.

Not in scope (see PROJECT_NOTES.md, 1.20 revised): durable Manual-mode
restoration, any lock held between admission and execution, or changes to
the protected updater / WebRTC admission.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from monitoring.models import emit_event

ENGINE_STATE_PATH = Path("/run/isadoraair/engine_state.json")
# Same freshness bound as the Remote DJ gate / status API (10 s): the engine
# republishes about once a second, so anything older is not evidence.
ENGINE_STATE_MAX_AGE_SECONDS = 10
# A timestamp meaningfully in the future is malformed evidence, not "fresh".
ENGINE_STATE_MAX_FUTURE_SECONDS = 5

ENGINE_SERVICE_IDENTITY = "isadoraair-engine"

SAFE = "safe"
WARNING = "warning"
BLOCKED = "blocked"
UNKNOWN = "unknown"

# Reason codes (stable identifiers; tests and audit rows use these).
STUDIO_MIC_LIVE = "studio_mic_live"
REMOTE_DJ_MIC_LIVE = "remote_dj_mic_live"
REMOTE_DJ_CONNECTED = "remote_dj_connected"
REMOTE_DJ_RECONNECTING = "remote_dj_reconnecting"
REMOTE_DJ_ESTABLISHING = "remote_dj_establishing"
REMOTE_DJ_SLOT_OCCUPIED = "remote_dj_slot_occupied"
REMOTE_DJ_CLEANING = "remote_dj_cleaning"
REMOTE_DJ_SLOT_QUARANTINED = "remote_dj_slot_quarantined"
MANUAL_MODE = "manual_mode"
STATUS_MISSING = "status_missing"
STATUS_UNREADABLE = "status_unreadable"
STATUS_MALFORMED = "status_malformed"
STATUS_STALE = "status_stale"
STATUS_FUTURE = "status_timestamp_in_future"
STATUS_UNSUPPORTED = "status_unsupported"
STATUS_CONTRADICTORY = "status_contradictory"

_REASON_TEXT = {
    STUDIO_MIC_LIVE: "The studio microphone is live.",
    REMOTE_DJ_MIC_LIVE: "A Remote DJ microphone is live.",
    REMOTE_DJ_CONNECTED: "A Remote DJ is connected (a microphone that is Off does not mean the DJ is gone).",
    REMOTE_DJ_RECONNECTING: "A Remote DJ session is reconnecting.",
    REMOTE_DJ_ESTABLISHING: "A Remote DJ connection is being established.",
    REMOTE_DJ_SLOT_OCCUPIED: "A Remote DJ session occupies the connection slot.",
    REMOTE_DJ_CLEANING: "A Remote DJ session's connection is still being cleaned up.",
    REMOTE_DJ_SLOT_QUARANTINED: (
        "The Remote DJ connection slot is quarantined after a failed cleanup; "
        "an engine restart is the documented recovery."
    ),
    MANUAL_MODE: "The engine is in Manual mode; a restart returns it to its startup mode.",
    STATUS_MISSING: "No engine status is available.",
    STATUS_UNREADABLE: "The engine status could not be read.",
    STATUS_MALFORMED: "The engine status is malformed.",
    STATUS_STALE: "The engine status is stale, so it is not evidence of a safe state.",
    STATUS_FUTURE: "The engine status timestamp is in the future.",
    STATUS_UNSUPPORTED: "The engine status does not carry the fields needed to judge a restart.",
    STATUS_CONTRADICTORY: "The engine status contradicts itself, so it is not evidence of a safe state.",
}

_BLOCKING_REASONS = (
    STUDIO_MIC_LIVE, REMOTE_DJ_MIC_LIVE, REMOTE_DJ_CONNECTED,
    REMOTE_DJ_RECONNECTING, REMOTE_DJ_ESTABLISHING,
    REMOTE_DJ_SLOT_OCCUPIED, REMOTE_DJ_CLEANING,
)
_WARNING_REASONS = (REMOTE_DJ_SLOT_QUARANTINED, MANUAL_MODE)

# Remote DJ slot values the engine publishes (None = Remote DJ not built).
_KNOWN_SLOT_STATES = frozenset({"available", "cleaning", "quarantined", "occupied"})
# Connection-attempt statuses (library/services/remote_dj_connection.py).
_KNOWN_ATTEMPT_STATUSES = frozenset({"connecting", "connected", "reconnecting", "failed", "ended"})
_LIVE_ATTEMPT_STATUSES = frozenset({"connecting", "connected", "reconnecting"})

# Transport values PlaybackEngine._write_state publishes ("PLAYING" on every
# tick, "STOPPED" once at shutdown). Anything else is an engine this module
# does not understand.
_KNOWN_TRANSPORTS = frozenset({"PLAYING", "STOPPED"})

# Booleans an engine that can be judged MUST publish.
_REQUIRED_BOOLEANS = (
    "mic_live", "remote_dj_configured", "remote_dj_connected", "remote_dj_live",
    "remote_dj_reconnecting", "manual_mode",
)
# Remote DJ lifecycle keys every supported engine publishes on every tick
# (r0132+ slot, 1.5 attempt evidence). The VALUE may be null, but the KEY
# must be there: a missing key is an engine (or writer) we cannot judge.
_REQUIRED_LIFECYCLE_KEYS = ("remote_dj_slot", "remote_dj_connection")

_IDENTITY_MAX_LENGTH = 64

OVERRIDE_REASON_MIN_LENGTH = 8
OVERRIDE_REASON_MAX_LENGTH = 500


@dataclass(frozen=True)
class EngineRestartAssessment:
    level: str
    reasons: tuple[str, ...]
    # Engine instance identity + observation time, when the status had them.
    engine_instance: str | None = None
    state_revision: dict | None = None
    observed_at: float | None = None
    age_seconds: float | None = None
    facts: dict = field(default_factory=dict)

    @property
    def requires_override(self) -> bool:
        return self.level in (BLOCKED, UNKNOWN)

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(_REASON_TEXT.get(code, code) for code in self.reasons)

    @property
    def summary(self) -> str:
        if self.level == SAFE:
            return "No live microphone or Remote DJ connection was reported."
        return " ".join(self.messages)

    def as_audit_dict(self) -> dict:
        """Bounded, secret-free evidence of the observed engine state."""
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "engine_instance": self.engine_instance,
            "state_revision": dict(self.state_revision) if self.state_revision else None,
            "observed_at": self.observed_at,
            "age_seconds": self.age_seconds,
            "facts": dict(self.facts),
        }

    def as_public_dict(self) -> dict:
        return {
            "level": self.level,
            "reasons": list(self.reasons),
            "messages": list(self.messages),
            "requires_override": self.requires_override,
        }


def _unknown(*reasons, **extra) -> EngineRestartAssessment:
    return EngineRestartAssessment(level=UNKNOWN, reasons=tuple(reasons), **extra)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def read_engine_state(path: Path | None = None):
    """(state, failure_reason) -- exactly one is None."""
    try:
        text = Path(path or ENGINE_STATE_PATH).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, STATUS_MISSING
    except OSError:
        return None, STATUS_UNREADABLE
    try:
        state = json.loads(text)
    except ValueError:
        return None, STATUS_MALFORMED
    if not isinstance(state, dict):
        return None, STATUS_MALFORMED
    return state, None


def assess_engine_restart(*, now: float | None = None, path: Path | None = None) -> EngineRestartAssessment:
    """Read-only judgment of whether an engine restart could interrupt a live
    microphone or an active/establishing Remote DJ WebRTC session."""
    state, failure = read_engine_state(path)
    if state is None:
        return _unknown(failure)

    timestamp = state.get("timestamp")
    if not _is_number(timestamp) or timestamp != timestamp or timestamp <= 0:
        return _unknown(STATUS_MALFORMED)
    current = time.time() if now is None else now
    age = current - timestamp
    identity = dict(observed_at=float(timestamp), age_seconds=round(age, 2))

    # Engine-instance identity is REQUIRED and must be self-consistent: every
    # supported engine stamps engine_session_id and state_revision =
    # {"instance": <that same id>, "seq": <positive, increasing int>} on every
    # write. Without it the snapshot cannot be tied to the running engine.
    instance = state.get("engine_session_id")
    revision = state.get("state_revision")
    if "engine_session_id" not in state or "state_revision" not in state:
        return _unknown(STATUS_UNSUPPORTED, **identity)
    if not (isinstance(instance, str) and 0 < len(instance) <= _IDENTITY_MAX_LENGTH):
        return _unknown(STATUS_MALFORMED, **identity)
    identity["engine_instance"] = instance
    if not isinstance(revision, dict):
        return _unknown(STATUS_MALFORMED, **identity)
    rev_instance, rev_seq = revision.get("instance"), revision.get("seq")
    if not (isinstance(rev_seq, int) and not isinstance(rev_seq, bool) and rev_seq > 0):
        return _unknown(STATUS_MALFORMED, **identity)
    if not isinstance(rev_instance, str):
        return _unknown(STATUS_MALFORMED, **identity)
    if rev_instance != instance:
        return _unknown(STATUS_CONTRADICTORY, **identity)
    identity["state_revision"] = {"instance": rev_instance, "seq": rev_seq}

    if age > ENGINE_STATE_MAX_AGE_SECONDS:
        return _unknown(STATUS_STALE, **identity)
    if age < -ENGINE_STATE_MAX_FUTURE_SECONDS:
        return _unknown(STATUS_FUTURE, **identity)

    if "transport" not in state:
        return _unknown(STATUS_UNSUPPORTED, **identity)
    if not isinstance(state["transport"], str) or state["transport"] not in _KNOWN_TRANSPORTS:
        return _unknown(STATUS_UNSUPPORTED, **identity)
    for key in _REQUIRED_BOOLEANS:
        if key not in state:
            return _unknown(STATUS_UNSUPPORTED, **identity)
        if not isinstance(state[key], bool):
            return _unknown(STATUS_MALFORMED, **identity)
    for key in _REQUIRED_LIFECYCLE_KEYS:
        if key not in state:
            return _unknown(STATUS_UNSUPPORTED, **identity)

    slot = state["remote_dj_slot"]
    if slot is not None and slot not in _KNOWN_SLOT_STATES:
        return _unknown(STATUS_MALFORMED, **identity)
    connection = state["remote_dj_connection"]
    attempt_status = None
    if connection is not None:
        if not isinstance(connection, dict):
            return _unknown(STATUS_MALFORMED, **identity)
        attempt_status = connection.get("status")
        if attempt_status not in _KNOWN_ATTEMPT_STATUSES:
            return _unknown(STATUS_MALFORMED, **identity)

    # Cross-field consistency (PlaybackEngine._write_state): the slot is null
    # exactly when Remote DJ is not built into this engine run, and an engine
    # without Remote DJ can have no session, attempt, live gate or recovery.
    if state["remote_dj_configured"]:
        if slot is None:
            return _unknown(STATUS_CONTRADICTORY, **identity)
    elif (slot is not None or connection is not None or state["remote_dj_connected"]
            or state["remote_dj_live"] or state["remote_dj_reconnecting"]):
        return _unknown(STATUS_CONTRADICTORY, **identity)

    reasons = []
    if state["mic_live"]:
        reasons.append(STUDIO_MIC_LIVE)
    if state["remote_dj_live"]:
        reasons.append(REMOTE_DJ_MIC_LIVE)
    if state["remote_dj_connected"]:
        reasons.append(REMOTE_DJ_CONNECTED)
    if state["remote_dj_reconnecting"]:
        reasons.append(REMOTE_DJ_RECONNECTING)
    if (attempt_status in _LIVE_ATTEMPT_STATUSES
            and not state["remote_dj_connected"] and not state["remote_dj_reconnecting"]):
        # An attempt is being set up (or its status says live) while the
        # engine has not yet claimed/published the session.
        reasons.append(REMOTE_DJ_ESTABLISHING)
    if slot == "occupied" and not state["remote_dj_connected"]:
        reasons.append(REMOTE_DJ_SLOT_OCCUPIED)
    if slot == "cleaning":
        reasons.append(REMOTE_DJ_CLEANING)
    if not any(code in _BLOCKING_REASONS for code in reasons):
        if slot == "quarantined":
            reasons.append(REMOTE_DJ_SLOT_QUARANTINED)
        if state["manual_mode"]:
            reasons.append(MANUAL_MODE)

    if any(code in _BLOCKING_REASONS for code in reasons):
        level = BLOCKED
    elif reasons:
        level = WARNING
    else:
        level = SAFE
    facts = {
        "transport": state["transport"],
        "remote_dj_configured": state["remote_dj_configured"],
        "mic_live": state["mic_live"],
        "remote_dj_connected": state["remote_dj_connected"],
        "remote_dj_live": state["remote_dj_live"],
        "remote_dj_reconnecting": state["remote_dj_reconnecting"],
        "remote_dj_slot": slot,
        "remote_dj_attempt_status": attempt_status,
        "manual_mode": state["manual_mode"],
    }
    return EngineRestartAssessment(level=level, reasons=tuple(reasons), facts=facts, **identity)


def is_engine_service(unit) -> bool:
    """True only for the engine's own systemd unit (with or without .service)."""
    if not isinstance(unit, str):
        return False
    name = unit[: -len(".service")] if unit.endswith(".service") else unit
    return name == ENGINE_SERVICE_IDENTITY


def plan_restarts_engine(plan) -> bool:
    services = getattr(plan, "services_requiring_restart", None) or ()
    return any(is_engine_service(service) for service in services)


# ---------------------------------------------------------------- override

ADMITTED = "admitted"
OVERRIDE_REQUIRED = "override_required"
OVERRIDE_FORBIDDEN = "override_forbidden"
REASON_REQUIRED = "reason_required"


@dataclass(frozen=True)
class RestartAdmission:
    allowed: bool
    code: str
    message: str
    assessment: EngineRestartAssessment
    override_used: bool = False
    reason: str = ""


def decide_engine_restart(user, assessment, *, override_requested, reason="") -> RestartAdmission:
    """Pure admission decision; records nothing."""
    if not assessment.requires_override:
        return RestartAdmission(True, ADMITTED, assessment.summary, assessment)
    base = assessment.summary
    if not override_requested:
        return RestartAdmission(
            False, OVERRIDE_REQUIRED,
            f"Engine restart refused: {base} A superuser may override with a written reason.",
            assessment,
        )
    if not (user is not None and getattr(user, "is_authenticated", False)
            and getattr(user, "is_active", False) and getattr(user, "is_superuser", False)):
        return RestartAdmission(
            False, OVERRIDE_FORBIDDEN,
            f"Engine restart refused: {base} Only a superuser may override.",
            assessment,
        )
    clean_reason = " ".join(str(reason or "").split())
    if len(clean_reason) < OVERRIDE_REASON_MIN_LENGTH:
        return RestartAdmission(
            False, REASON_REQUIRED,
            f"Engine restart refused: {base} A written reason of at least "
            f"{OVERRIDE_REASON_MIN_LENGTH} characters is required to override.",
            assessment,
        )
    return RestartAdmission(
        True, ADMITTED, base, assessment,
        override_used=True, reason=clean_reason[:OVERRIDE_REASON_MAX_LENGTH],
    )


def _actor(user) -> dict:
    return {
        "user_id": getattr(user, "pk", None),
        "username": (user.get_username() if hasattr(user, "get_username") else None),
        "is_superuser": bool(getattr(user, "is_superuser", False)),
    }


def _emit(level, title, detail):
    # A unique key per row so two operator actions are never coalesced.
    emit_event(
        category="restart_safety", level=level, title=title, detail=detail,
        dedupe_key=f"restart_safety|{uuid.uuid4()}",
    )


def admit_engine_restart(
    user, *, entry_point, target, override_requested=False, reason="",
    assessment=None, audit_refusal=True, audit_override=True,
) -> RestartAdmission:
    """Assess (fresh, unless one is supplied), decide, and audit.

    ``target`` names the thing being restarted/installed (release id, unit,
    changed fields) and is recorded verbatim; it must be secret-free.
    Always call this immediately before submitting the restart request so the
    judgment reflects the engine state at that moment.
    """
    if assessment is None:
        assessment = assess_engine_restart()
    admission = decide_engine_restart(
        user, assessment, override_requested=override_requested, reason=reason,
    )
    base = {
        "entry_point": entry_point,
        "target": dict(target),
        "operator": _actor(user),
        "engine_state": assessment.as_audit_dict(),
    }
    if not admission.allowed:
        if audit_refusal:
            _emit("warning", "Engine restart refused by restart safeguard", {
                **base, "decision": admission.code, "override_requested": bool(override_requested),
                "outcome": "not_submitted",
            })
    elif admission.override_used and audit_override:
        _emit("warning", "Superuser override of engine restart safeguard", {
            **base, "decision": "override_admitted", "reason": admission.reason,
            "outcome": "pending_submission",
        })
    return admission


def record_restart_outcome(user, admission, *, entry_point, target, outcome, detail=None):
    """Audit what happened to an OVERRIDDEN restart request.

    ``outcome`` is about the *request*: it never says the engine restarted.
    Only overridden requests get a second row; ordinary admitted restarts keep
    their existing audit trail unchanged.
    """
    if not admission.override_used:
        return
    _emit(
        "warning" if outcome in {"submitted", "accepted"} else "error",
        "Overridden engine restart request outcome",
        {
            "entry_point": entry_point, "target": dict(target), "operator": _actor(user),
            "engine_state": admission.assessment.as_audit_dict(), "reason": admission.reason,
            "outcome": outcome, "detail": dict(detail or {}),
            "completion_claimed": False,
        },
    )
