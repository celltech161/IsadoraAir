"""THE active-message authority (iPortal Phase D1).

Every change to the domain happens here, in one transaction per call, with
one lock order everywhere:

    1. ActiveBroadcastPolicy (the serialization root)
    2. ActiveBroadcastMessage rows, in UUID order
    3. ProductionMedia, through retention.lock_for_binding (the binding rule)
    4. ActiveBroadcastExecution

Rules the code below keeps:

* Every write to an ACTIVE message -- and every change of which messages are
  active -- holds the policy lock, so under that lock the active set cannot
  move. PREPARED messages are changed under their own row lock.
* ``policy.generation`` increases by exactly one per composition change
  (activation incl. replacement, clear of an active message, an expiry sweep
  that expires anything, stinger, native-airing and composition-limit
  changes). Replay never changes it.
* An active set that breaks a policy limit (count, per-message, aggregate)
  is never aired: activation refuses to create it, and materialization /
  Replay Now fail closed with CompositionIneligible and an operator alert.
  Media is never truncated to fit.
* After every generation change, unfinished executions the domain still owns
  (PENDING_RENDER/READY) whose generation is no longer current are SUPPRESSED
  in the same transaction. Executions the engine already holds (D2B) are left
  to the engine's own authoritative re-read.
* An execution's composition is frozen at creation. Clear and expiry act on
  FUTURE airings; nothing here stops audio that is already on air.
* Every state change appends its event in the same transaction.

D1 contains no renderer and no engine path: executions are created
PENDING_RENDER and stay there until D2A binds their aggregate. The functions
that apply engine air evidence (``record_air_completed`` /
``record_air_interrupted``) exist so the cadence rules are reviewable now;
nothing in D1 can move an execution to STARTED.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from authz.evaluator import authorize
from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.models import ProductionMedia
from production.services import reference_audio, retention

from . import _store, composition
from .models import (
    ABSOLUTE_MAX_ACTIVE_MESSAGES, ABSOLUTE_MAX_AGGREGATE_SECONDS, ABSOLUTE_MAX_MESSAGE_SECONDS, POLICY_ID,
    PRESTART_DOMAIN_STATES, REPLAY_INTERVAL_MAX_SECONDS, REPLAY_INTERVAL_MIN_SECONDS, TERMINAL_MESSAGE_STATES,
    UNFINISHED_EXECUTION_STATES, ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent,
    ActiveBroadcastPolicy, ActorKind, ExecutionReason, ExecutionState, MessageKind, MessageState,
)

PRODUCE = "active_message.produce"
CONTROL = "active_message.control"

# The composition limits are operator policy on ActiveBroadcastPolicy, within
# the models' ABSOLUTE_MAX_* ceilings (docs/ACTIVE_MESSAGES.md, "Bounds").
DETAIL_LIMIT_BYTES = 4096
TITLE_LIMIT = 200
REASON_LIMIT = 500

# D2B turns this on together with the engine path. Until then the dormant gate
# cannot be opened: nothing could air.
NATIVE_AIRING_AVAILABLE = False

M = MessageState
X = ExecutionState


class ActiveMessageError(Exception):
    code = "active_message_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotPermitted(ActiveMessageError):
    code = "not_permitted"


class InvalidRequest(ActiveMessageError):
    code = "invalid_request"


class InvalidTransition(ActiveMessageError):
    code = "invalid_transition"


class MediaRefused(ActiveMessageError):
    code = "media_refused"


class LimitExceeded(ActiveMessageError):
    code = "limit_exceeded"


class CompositionIneligible(LimitExceeded):
    """The active set breaks a policy limit, so it is not aired (fail closed).
    ``detail`` explains which limit and by how much, for the operator."""

    def __init__(self, message: str, *, code: str, detail: dict):
        super().__init__(message, code=code)
        self.detail = detail


class NothingActive(ActiveMessageError):
    code = "nothing_active"


class ExecutionBusy(ActiveMessageError):
    code = "execution_busy"


class EvidenceRefused(ActiveMessageError):
    code = "evidence_refused"


class NativeAiringUnavailable(ActiveMessageError):
    code = "native_airing_unavailable"


# -- small helpers -------------------------------------------------------------------------

def _now(now=None):
    value = now or timezone.now()
    if timezone.is_naive(value):
        raise InvalidRequest("times are timezone-aware")
    return value


def can(user, capability: str) -> bool:
    return bool(user is not None and authorize(user, capability))


def _require(user, capability: str) -> None:
    if user is None or getattr(user, "pk", None) is None:
        raise NotPermitted("this action needs an authenticated user", code="unauthenticated")
    result = authorize(user, capability)
    if not result:
        raise NotPermitted(result.reason, code=result.code)


def _username(user) -> str:
    return user.get_username()[:150] if user is not None and getattr(user, "pk", None) is not None else ""


def _detail(detail) -> dict:
    detail = {} if detail is None else detail
    try:
        encoded = composition.canonical_json(detail)
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"event detail is not strict JSON: {exc}") from None
    if len(encoded.encode()) > DETAIL_LIMIT_BYTES:
        raise InvalidRequest("event detail exceeds its bound")
    return json.loads(encoded)


def _event(event, *, generation, actor_kind, user=None, message=None, execution=None, from_state="", to_state="",
           detail=None, request_token=None) -> ActiveBroadcastMessageEvent:
    return _store.insert(
        ActiveBroadcastMessageEvent, message=message, execution=execution, event=event, from_state=from_state,
        to_state=to_state, actor_kind=actor_kind, actor=user if _username(user) else None,
        actor_username=_username(user), generation=generation, request_token=request_token,
        detail=_detail(detail))


def _iso(value):
    return None if value is None else value.isoformat()


def _aware_future(value, now, label):
    if value is None:
        return None
    if timezone.is_naive(value):
        raise InvalidRequest(f"{label} must be timezone-aware")
    if value <= now:
        raise InvalidRequest(f"{label} must be in the future", code="not_in_future")
    return value


def _message_id(message) -> uuid.UUID:
    """The message's UUID (from a row or an id); a malformed id is an unknown message."""
    try:
        return uuid.UUID(str(getattr(message, "pk", message)))
    except (TypeError, ValueError, AttributeError):
        raise InvalidRequest("no such active message", code="unknown_message") from None


# -- locks -----------------------------------------------------------------------------------

def _lock_policy() -> ActiveBroadcastPolicy:
    _store.ensure_policy()
    return ActiveBroadcastPolicy.objects.select_for_update().get(pk=POLICY_ID)


def _lock_message(message) -> ActiveBroadcastMessage:
    try:
        return ActiveBroadcastMessage.objects.select_for_update(of=("self",)).get(pk=_message_id(message))
    except ActiveBroadcastMessage.DoesNotExist:
        raise InvalidRequest("no such active message", code="unknown_message") from None


def _lock_messages(ids) -> dict:
    """Lock several messages in UUID order (rows are locked as the sorted scan returns them)."""
    ids = sorted({uuid.UUID(str(value)) for value in ids})
    rows = ActiveBroadcastMessage.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("id")
    return {row.pk: row for row in rows}


def _lock_media(media) -> ProductionMedia:
    try:
        return retention.lock_for_binding(media)
    except MediaPurged:
        raise MediaRefused("the media's bytes have been purged", code="media_purged") from None
    except MediaNotValidated:
        raise MediaRefused("the media has not been validated as valid", code="media_not_valid") from None
    except MediaInconsistent:
        raise MediaRefused("no such production media", code="unknown_media") from None


def _open_execution():
    return (ActiveBroadcastExecution.objects.select_for_update(of=("self",))
            .filter(state__in=UNFINISHED_EXECUTION_STATES).first())


def _current_active_id(producer_key, kind, *, exclude):
    """The producer's ACTIVE message of this kind (the one an activation supersedes)."""
    return (ActiveBroadcastMessage.objects.filter(state=M.ACTIVE, producer_key=producer_key, kind=kind)
            .exclude(pk=exclude).values_list("id", flat=True).first())


def _prior_request(token):
    """The event that already resolved this Replay Now request token, if any."""
    return ActiveBroadcastMessageEvent.objects.select_related("execution").filter(request_token=token).first()


def _active_messages():
    return list(ActiveBroadcastMessage.objects.select_related("message_media").filter(state=M.ACTIVE))


# -- generation and staleness ----------------------------------------------------------------

def _bump(policy) -> ActiveBroadcastPolicy:
    return _store.write(ActiveBroadcastPolicy, POLICY_ID, match={"generation": policy.generation},
                        values={"generation": policy.generation + 1})


def _suppress_stale(policy, *, now, actor_kind, user=None) -> list:
    """Suppress every domain-owned unfinished execution built for an older generation."""
    stale = list(ActiveBroadcastExecution.objects.select_for_update(of=("self",))
                 .filter(state__in=PRESTART_DOMAIN_STATES).exclude(policy_generation=policy.generation)
                 .order_by("id"))
    suppressed = []
    for execution in stale:
        row = _store.write(
            ActiveBroadcastExecution, execution.pk,
            match={"state": execution.state, "policy_generation": execution.policy_generation},
            values={"state": X.SUPPRESSED, "open_slot": None, "terminal_at": now, "failure_code": "stale_generation",
                    "failure_detail": f"built for generation {execution.policy_generation}; "
                                      f"generation {policy.generation} is current"})
        _event("execution_suppressed", execution=row, from_state=execution.state, to_state=X.SUPPRESSED,
               generation=policy.generation, actor_kind=actor_kind, user=user,
               detail={"code": "stale_generation", "built_for": execution.policy_generation})
        suppressed.append(row)
    return suppressed


def _terminal_message_values(state, *, now, user, reason, superseded_by=None) -> dict:
    return {"state": state, "state_changed_at": now, "terminal_at": now, "terminal_by": user if _username(user) else None,
            "terminal_by_username": _username(user), "terminal_reason": reason, "next_due_at": None,
            "superseded_by": superseded_by}


def _expire_locked(policy, now, *, actor_kind):
    """Expire every ACTIVE message whose expiry has passed (policy lock held).
    One generation change for the whole sweep."""
    due = (ActiveBroadcastMessage.objects.filter(state=M.ACTIVE, expires_at__lte=now)
           .values_list("id", flat=True))
    rows = [row for row in _lock_messages(list(due)).values()
            if row.state == M.ACTIVE and row.expires_at is not None and row.expires_at <= now]
    if not rows:
        return policy, []
    policy = _bump(policy)
    expired = []
    for row in rows:
        stored = _store.write(ActiveBroadcastMessage, row.pk, match={"state": M.ACTIVE},
                              values=_terminal_message_values(M.EXPIRED, now=now, user=None, reason="expired"))
        _event("expired", message=stored, from_state=M.ACTIVE, to_state=M.EXPIRED, generation=policy.generation,
               actor_kind=actor_kind, detail={"expires_at": _iso(row.expires_at)})
        expired.append(stored)
    _suppress_stale(policy, now=now, actor_kind=actor_kind)
    return policy, expired


# -- message media -------------------------------------------------------------------------------

def _check_message_media(media, *, actor, control: bool, max_seconds: int) -> Decimal:
    if media.kind == ProductionMedia.KIND_BED:
        raise MediaRefused("station beds are not producer messages", code="bed_not_message")
    if media.owner_id != actor.pk and not control:
        raise MediaRefused("only your own recordings and uploads can be bound", code="not_your_media")
    duration = media.decoded_duration_seconds
    if duration is None or duration <= 0:
        raise MediaRefused("the media has no measured duration", code="duration_unknown")
    if duration > max_seconds:
        raise LimitExceeded(f"the media is {duration} s; a message is at most {max_seconds} s "
                            f"(policy limit; media is never truncated)", code="message_too_long")
    return duration


def _check_eligible(policy, messages, stinger_seconds) -> Decimal:
    """Fail closed unless ``messages`` (an active set, or a candidate one)
    fits every policy limit. Returns the accounted airtime."""
    if len(messages) > policy.max_active_messages:
        raise CompositionIneligible(
            f"{len(messages)} messages would be active; the limit is {policy.max_active_messages}",
            code="too_many_active", detail={"active": len(messages), "limit": policy.max_active_messages})
    for message in sorted(messages, key=lambda candidate: str(candidate.pk)):     # deterministic report
        duration = message.message_media.decoded_duration_seconds
        if duration is None or duration <= 0 or duration > policy.max_message_seconds:
            raise CompositionIneligible(
                f"message {message.pk} is {duration} s; the per-message limit is {policy.max_message_seconds} s",
                code="message_too_long",
                detail={"message": str(message.pk), "seconds": str(duration), "limit": policy.max_message_seconds})
    total = composition.accounted_seconds(messages, stinger_seconds)
    if total > policy.max_aggregate_seconds:
        raise CompositionIneligible(
            f"the active set is {total} s (stinger included when urgent); the aggregate limit is "
            f"{policy.max_aggregate_seconds} s",
            code="aggregate_too_long",
            detail={"seconds": str(total), "limit": policy.max_aggregate_seconds,
                    "stinger_seconds": None if stinger_seconds is None else str(stinger_seconds)})
    return total


def _alert_ineligible(exc: CompositionIneligible) -> None:
    """Operator attention (never history): the active set is not being aired."""
    from monitoring.models import emit_event
    emit_event("active_messages", "Active messages: active set exceeds the policy limits and is not aired",
               level="error", detail={"code": exc.code, "explanation": str(exc), **exc.detail},
               dedupe_key=f"active_messages|ineligible|{exc.code}")


# -- message lifecycle --------------------------------------------------------------------------

def prepare_message(*, actor, kind, title="", expires_at=None, now=None) -> ActiveBroadcastMessage:
    """A new PREPARED message produced by ``actor`` (needs active_message.produce)."""
    _require(actor, PRODUCE)
    now = _now(now)
    if kind not in MessageKind.values:
        raise InvalidRequest("kind is urgent or public_address", code="invalid_kind")
    if not isinstance(title, str) or len(title) > TITLE_LIMIT:
        raise InvalidRequest(f"title is text of at most {TITLE_LIMIT} characters")
    expires_at = _aware_future(expires_at, now, "expires_at")
    with transaction.atomic():
        _store.ensure_policy()
        generation = ActiveBroadcastPolicy.objects.values_list("generation", flat=True).get(pk=POLICY_ID)
        row = _store.insert(
            ActiveBroadcastMessage, id=uuid.uuid4(), kind=kind, state=M.PREPARED, title=title, producer=actor,
            producer_key=f"user:{actor.pk}", producer_username=_username(actor), expires_at=expires_at,
            state_changed_at=now)
        _event("prepared", message=row, to_state=M.PREPARED, generation=generation, actor_kind=ActorKind.USER,
               user=actor, detail={"kind": kind, "expires_at": _iso(expires_at)})
    return row


def bind_message_media(message, media, *, actor) -> ActiveBroadcastMessage:
    """Bind (or replace) the exact media a PREPARED message will air.

    Only the producer -- or a control-capable operator -- binds, and only to
    a PREPARED message: an ACTIVE message's media is immutable. Rebinding the
    same media is a no-op."""
    # CONTROL is independently sufficient: a control-room operator may bind
    # eligible media to any draft without also being a producer. Everyone
    # else must hold PRODUCE and remains confined to their own draft/media.
    if not can(actor, CONTROL):
        _require(actor, PRODUCE)
    media_id = getattr(media, "pk", media)
    with transaction.atomic():
        row = _lock_message(message)
        if row.state != M.PREPARED:
            raise InvalidTransition(f"a {row.state} message's media cannot change", code="media_immutable")
        control = can(actor, CONTROL)
        if row.producer_id != actor.pk and not control:
            raise NotPermitted("only the producer binds media to a prepared message")
        if row.message_media_id is not None and str(row.message_media_id) == str(media_id):
            return row
        bound = _lock_media(media_id)
        # Early feedback at binding; activation re-checks under the policy lock.
        duration = _check_message_media(bound, actor=actor, control=control,
                                        max_seconds=current_policy().max_message_seconds)
        previous = row.message_media_id
        row = _store.write(ActiveBroadcastMessage, row.pk, match={"state": M.PREPARED, "message_media": previous},
                           values={"message_media": bound})
        generation = ActiveBroadcastPolicy.objects.values_list("generation", flat=True).get(pk=POLICY_ID)
        _event("media_bound", message=row, from_state=M.PREPARED, to_state=M.PREPARED, generation=generation,
               actor_kind=ActorKind.USER, user=actor,
               detail={"media": str(bound.pk), "sha256": bound.sha256, "duration_seconds": str(duration),
                       "replaced": None if previous is None else str(previous)})
    return row


def set_message_expiry(message, expires_at, *, actor, now=None) -> ActiveBroadcastMessage:
    """Set or remove a message's expiry. PREPARED: its producer or a
    control-capable operator. ACTIVE: control only (the composition does not
    change until the expiry sweep actually expires it). To end a message now,
    clear it."""
    now = _now(now)
    expires_at = _aware_future(expires_at, now, "expires_at")
    if not can(actor, CONTROL):
        _require(actor, PRODUCE)
    with transaction.atomic():
        policy = _lock_policy()
        row = _lock_message(message)
        if row.state == M.ACTIVE:
            _require(actor, CONTROL)
        elif row.state == M.PREPARED:
            if row.producer_id != actor.pk and not can(actor, CONTROL):
                raise NotPermitted("only the producer changes a prepared message")
        else:
            raise InvalidTransition(f"a {row.state} message is final", code="terminal")
        if row.expires_at == expires_at:
            return row
        previous = row.expires_at
        row = _store.write(ActiveBroadcastMessage, row.pk, match={"state": row.state, "expires_at": previous},
                           values={"expires_at": expires_at})
        _event("expiry_set", message=row, from_state=row.state, to_state=row.state, generation=policy.generation,
               actor_kind=ActorKind.USER, user=actor,
               detail={"expires_at": _iso(expires_at), "previous": _iso(previous)})
    return row


def activate_message(message, *, actor, now=None) -> ActiveBroadcastMessage:
    """PREPARED -> ACTIVE (control). Atomically supersedes the same producer's
    ACTIVE message of the same kind. The new message is due immediately (it
    airs at the next safe program boundary once D2 exists). Idempotent: an
    already-ACTIVE message is returned unchanged."""
    _require(actor, CONTROL)
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        target = (ActiveBroadcastMessage.objects.filter(pk=_message_id(message))
                  .values("producer_key", "kind").first())
        if target is None:
            raise InvalidRequest("no such active message", code="unknown_message")
        # Under the policy lock the active set cannot change, and producer_key/kind never change.
        previous_id = _current_active_id(target["producer_key"], target["kind"], exclude=_message_id(message))
        rows = _lock_messages([_message_id(message)] + ([previous_id] if previous_id else []))
        row = rows[_message_id(message)]
        if row.state == M.ACTIVE:
            return row
        if row.state != M.PREPARED:
            raise InvalidTransition(f"a {row.state} message cannot be activated", code="terminal")
        if row.message_media_id is None:
            raise InvalidTransition("bind the message's media first", code="media_required")
        if row.expires_at is not None and row.expires_at <= now:
            raise InvalidTransition("the message has already expired", code="already_expired")
        media = _lock_media(row.message_media_id)
        row.message_media = media
        # The active set this activation would produce must fit every policy limit.
        candidate = [other for other in _active_messages() if other.pk != previous_id] + [row]
        _pin, _unavailable, stinger_seconds = _stinger(policy, candidate)
        _check_eligible(policy, candidate, stinger_seconds)

        policy = _bump(policy)
        if previous_id:
            previous = _store.write(
                ActiveBroadcastMessage, previous_id, match={"state": M.ACTIVE},
                values=_terminal_message_values(M.CLEARED, now=now, user=actor, reason="superseded",
                                                superseded_by=row.pk))
            _event("superseded", message=previous, from_state=M.ACTIVE, to_state=M.CLEARED,
                   generation=policy.generation, actor_kind=ActorKind.USER, user=actor,
                   detail={"superseded_by": str(row.pk)})
        row = _store.write(
            ActiveBroadcastMessage, row.pk, match={"state": M.PREPARED, "message_media": media.pk},
            values={"state": M.ACTIVE, "state_changed_at": now, "activation_generation": policy.generation,
                    "activated_by": actor, "activated_by_username": _username(actor), "activated_at": now,
                    "replay_interval_seconds": policy.default_replay_interval_seconds, "next_due_at": now})
        _event("activated", message=row, from_state=M.PREPARED, to_state=M.ACTIVE, generation=policy.generation,
               actor_kind=ActorKind.USER, user=actor,
               detail={"media": str(media.pk), "sha256": media.sha256,
                       "replay_interval_seconds": policy.default_replay_interval_seconds,
                       "supersedes": None if previous_id is None else str(previous_id)})
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
    return row


def clear_message(message, *, actor, reason="", now=None) -> ActiveBroadcastMessage:
    """Clear exactly this message (never "the producer's current one").

    ACTIVE -> CLEARED (control): a new generation; it is in no future airing.
    An airing already on air is not cut (that is the engine's, D2B).
    PREPARED -> CLEARED (discard): its producer or a control-capable operator;
    the composition does not change.
    Idempotent: a cleared or expired message is returned unchanged."""
    if not (can(actor, PRODUCE) or can(actor, CONTROL)):
        _require(actor, CONTROL)
    if not isinstance(reason, str) or len(reason) > REASON_LIMIT:
        raise InvalidRequest(f"reason is text of at most {REASON_LIMIT} characters")
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        row = _lock_message(message)
        if row.state in TERMINAL_MESSAGE_STATES:
            return row
        if row.state == M.PREPARED:
            if row.producer_id != actor.pk and not can(actor, CONTROL):
                raise NotPermitted("only the producer discards a prepared message")
            row = _store.write(ActiveBroadcastMessage, row.pk, match={"state": M.PREPARED},
                               values=_terminal_message_values(M.CLEARED, now=now, user=actor, reason=reason))
            _event("discarded", message=row, from_state=M.PREPARED, to_state=M.CLEARED, generation=policy.generation,
                   actor_kind=ActorKind.USER, user=actor, detail={"reason": reason})
            return row
        _require(actor, CONTROL)
        policy = _bump(policy)
        row = _store.write(ActiveBroadcastMessage, row.pk, match={"state": M.ACTIVE},
                           values=_terminal_message_values(M.CLEARED, now=now, user=actor, reason=reason))
        _event("cleared", message=row, from_state=M.ACTIVE, to_state=M.CLEARED, generation=policy.generation,
               actor_kind=ActorKind.USER, user=actor, detail={"reason": reason})
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
    return row


def expire_due_messages(*, now=None, actor_kind=ActorKind.SYSTEM) -> list:
    """The expiry sweep: every ACTIVE message whose expiry has passed becomes
    EXPIRED (one generation change for the sweep). Returns the expired rows."""
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        _policy, expired = _expire_locked(policy, now, actor_kind=actor_kind)
    return expired


# -- executions ----------------------------------------------------------------------------------

def _usable_stinger(key):
    """Return an exact, currently usable ReferenceAudio pin and duration.

    This deliberately resolves through ReferenceAudio's authority, including
    validation, retention and stored-byte hashing. A runtime failure is data,
    not an exception that may suppress urgent content: callers omit the
    stinger and retain the stable failure code for operator attention.
    """
    try:
        resolved = reference_audio.resolve(key)
    except reference_audio.ReferenceAudioError as exc:
        return None, exc.code, None
    try:
        duration = resolved.media.decoded_duration_seconds
        if duration is None or duration <= 0:
            return None, "reference_duration_unknown", None
        return resolved.provenance(), "", duration
    finally:
        resolved.close()


def validate_stinger_key(key):
    """Validate the optional ReferenceAudio key at configuration time.

    Blank explicitly disables the stinger. A nonblank key must resolve now to
    present, valid, hash-matching media with a measured duration. This is
    early operator feedback only; runtime still degrades to no stinger if a
    once-valid registration later becomes unavailable.
    """
    if key == "":
        return None
    pin, unavailable, duration = _usable_stinger(key)
    if unavailable:
        raise InvalidRequest(f"the stinger ReferenceAudio key is not usable ({unavailable})", code=unavailable)
    return pin, duration


def _stinger(policy, messages):
    """(pin, unavailable code, validated decoded duration) of the stinger this
    set would use -- (None, "", None) when no component is urgent or none is
    configured."""
    if not policy.stinger_key or not any(message.kind == MessageKind.URGENT for message in messages):
        return None, "", None
    return _usable_stinger(policy.stinger_key)


def _warn_stinger(code, key):
    def emit():
        from monitoring.models import emit_event
        emit_event("active_messages", "Active messages: stinger unavailable", level="warning",
                   detail={"stinger_key": key, "code": code}, dedupe_key=f"active_messages|stinger|{code}")
    transaction.on_commit(emit)


def _create_execution(policy, messages, stinger, *, reason, due_at, now, event, actor_kind, user=None,
                      request_token=None):
    stinger_pin, unavailable, stinger_seconds = stinger
    try:
        snapshot = composition.build(messages, stinger_pin=stinger_pin, stinger_unavailable=unavailable,
                                     stinger_seconds=stinger_seconds)
    except composition.CompositionError as exc:
        raise LimitExceeded(str(exc), code=exc.code) from None
    digest = composition.digest(snapshot)
    execution = _store.insert(
        ActiveBroadcastExecution, id=uuid.uuid4(), reason=reason, state=X.PENDING_RENDER, open_slot=1,
        policy_generation=policy.generation, composition_digest=digest, composition_snapshot=snapshot,
        component_count=len(snapshot["components"]), requested_by=user if _username(user) else None,
        requested_by_username=_username(user), due_at=due_at)
    _event(event, execution=execution, to_state=X.PENDING_RENDER, generation=policy.generation, actor_kind=actor_kind,
           user=user, request_token=request_token,
           detail={"reason": reason, "digest": digest, "components": len(snapshot["components"]),
                   "stinger_unavailable": unavailable or None})
    if unavailable:
        _warn_stinger(unavailable, policy.stinger_key)
    return execution


def materialize_due_execution(*, now=None, actor_kind=ActorKind.WORKER):
    """Create the one due airing if the active set is due, else return the
    unfinished one (coalesced), else None. Expiry runs first, in the same
    transaction: an expired message is never in a new airing.

    Concurrent callers serialize on the policy lock, and the database allows
    only one unfinished execution: two workers get the same occurrence.

    An active set that breaks a policy limit is NOT aired: CompositionIneligible
    is raised (after the expiry/suppression work commits) and an operator
    alert is raised."""
    now = _now(now)
    refusal = None
    with transaction.atomic():
        policy = _lock_policy()
        policy, _expired = _expire_locked(policy, now, actor_kind=actor_kind)
        _suppress_stale(policy, now=now, actor_kind=actor_kind)
        unfinished = _open_execution()
        if unfinished is not None:
            return unfinished
        messages = _active_messages()
        due = [message.next_due_at for message in messages if message.next_due_at <= now]
        if not due:
            return None
        stinger = _stinger(policy, messages)
        try:
            _check_eligible(policy, messages, stinger[2])
        except CompositionIneligible as exc:
            refusal = exc
        else:
            return _create_execution(policy, messages, stinger, reason=ExecutionReason.SCHEDULED, due_at=min(due),
                                     now=now, event="execution_materialized", actor_kind=actor_kind)
    _alert_ineligible(refusal)
    raise refusal


STINGER_PIN_UNAVAILABLE = "stinger_pin_unavailable"


def _reconciliation_of(failed):
    """(already reconciled?, successor or None) -- from the predecessor's one
    authoritative ``reconciled`` event."""
    record = (ActiveBroadcastMessageEvent.objects.filter(execution=failed, event="reconciled")
              .order_by("id").first())
    if record is None:
        return False, None
    successor = record.detail.get("successor")
    return True, (None if successor is None else ActiveBroadcastExecution.objects.get(pk=successor))


def reconcile_stinger_failure(execution, *, now=None, actor_kind=ActorKind.WORKER):
    """After an occurrence FAILED because its FROZEN stinger pin became
    unusable (D2A), make sure the airing it stood for still happens -- whatever
    created it (due cadence, Replay Now) -- without touching it: its snapshot
    stays the truth of what it was, and a Replay Now request token keeps
    resolving to it.

    Under the policy lock (expiry first, stale airings suppressed), exactly one
    outcome is recorded per failed occurrence, as a ``reconciled`` event on it
    (so the operation is idempotent across retries and crashes):

    * the active composition moved on since it was built (a clear, an expiry,
      a replacement, a policy change: a new generation) -> no successor: the
      D1 rules for the new composition govern, and nothing cleared or
      expired is resurrected;
    * an unfinished current occurrence already exists -> it absorbs this one;
    * nothing is active -> no successor;
    * otherwise ONE successor, reason RECONCILE, due now, independent of
      cadence, built from CURRENT authority: the current active set and the
      key's current stinger (an unusable one omitted with a warning; a newer
      valid registration pinned). It carries a ``reconciles`` event naming its
      predecessor.

    A current set that breaks a policy limit fails closed (CompositionIneligible,
    operator alert) and records nothing: an operator change resolves it.
    Returns the successor (new or absorbing), or None."""
    now = _now(now)
    refusal = None
    with transaction.atomic():
        policy = _lock_policy()
        # FAILED is terminal: the row cannot change, so it is read, not locked
        # (the lock order is policy -> messages -> media -> execution).
        try:
            failed = ActiveBroadcastExecution.objects.get(pk=getattr(execution, "pk", execution))
        except (ActiveBroadcastExecution.DoesNotExist, ValueError, TypeError):
            raise InvalidRequest("no such execution", code="unknown_execution") from None
        if failed.state != X.FAILED or failed.failure_code != STINGER_PIN_UNAVAILABLE:
            raise InvalidRequest("only an occurrence failed by its frozen stinger is reconciled",
                                 code="not_reconcilable")
        done, successor = _reconciliation_of(failed)
        if done:
            return successor

        def record(outcome, successor=None):
            _event("reconciled", execution=failed, generation=policy.generation, actor_kind=actor_kind,
                   detail={"outcome": outcome, "successor": None if successor is None else str(successor.pk)})
            return successor

        policy, _expired = _expire_locked(policy, now, actor_kind=actor_kind)
        _suppress_stale(policy, now=now, actor_kind=actor_kind)
        if failed.policy_generation != policy.generation:
            return record("composition_changed")
        unfinished = _open_execution()
        if unfinished is not None:
            return record("absorbed", unfinished)
        messages = _active_messages()
        if not messages:
            return record("nothing_active")
        stinger = _stinger(policy, messages)
        try:
            _check_eligible(policy, messages, stinger[2])
        except CompositionIneligible as exc:
            refusal = exc
        else:
            successor = _create_execution(policy, messages, stinger, reason=ExecutionReason.RECONCILE, due_at=now,
                                          now=now, event="execution_reconciled", actor_kind=actor_kind)
            _event("reconciles", execution=successor, generation=policy.generation, actor_kind=actor_kind,
                   detail={"predecessor": str(failed.pk), "predecessor_reason": failed.reason})
            return record("successor", successor)
    _alert_ineligible(refusal)
    raise refusal


def request_manual_replay(*, actor, request_token, now=None):
    """Replay Now (control), idempotent on ``request_token`` (a client UUID):
    the same token always resolves to the same execution. An unfinished
    airing of the CURRENT composition absorbs the request (no second airing)."""
    _require(actor, CONTROL)
    try:
        token = uuid.UUID(str(request_token))
    except (TypeError, ValueError):
        raise InvalidRequest("request_token is a UUID", code="invalid_request_token") from None
    now = _now(now)
    refusal = None
    with transaction.atomic():
        policy = _lock_policy()
        prior = _prior_request(token)
        if prior is not None:
            if prior.actor_id != actor.pk:
                raise InvalidRequest("that request token belongs to another request", code="request_token_conflict")
            return prior.execution
        policy, _expired = _expire_locked(policy, now, actor_kind=ActorKind.SYSTEM)
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
        unfinished = _open_execution()
        if unfinished is not None:
            if unfinished.policy_generation != policy.generation:
                raise ExecutionBusy("an airing of an earlier composition is still with the engine")
            _event("replay_coalesced", execution=unfinished, generation=policy.generation, actor_kind=ActorKind.USER,
                   user=actor, request_token=token, detail={"state": unfinished.state})
            return unfinished
        messages = _active_messages()
        if not messages:
            raise NothingActive("no message is active")
        stinger = _stinger(policy, messages)
        try:
            _check_eligible(policy, messages, stinger[2])
        except CompositionIneligible as exc:
            refusal = exc
        else:
            return _create_execution(policy, messages, stinger, reason=ExecutionReason.MANUAL, due_at=now, now=now,
                                     event="replay_requested", actor_kind=ActorKind.USER, user=actor,
                                     request_token=token)
    _alert_ineligible(refusal)
    raise refusal


def snapshot_active_composition() -> dict:
    """The composition (snapshot + digest + generation) an airing created now
    would carry, and ``ineligible`` (None, or why the set would not air under
    the current policy limits). Read-only; takes the policy lock for a
    consistent view."""
    with transaction.atomic():
        policy = _lock_policy()
        messages = _active_messages()
        if not messages:
            return {"generation": policy.generation, "snapshot": None, "digest": None, "ineligible": None}
        stinger_pin, unavailable, stinger_seconds = _stinger(policy, messages)
        try:
            _check_eligible(policy, messages, stinger_seconds)
            ineligible = None
        except CompositionIneligible as exc:
            ineligible = {"code": exc.code, "explanation": str(exc), **exc.detail}
        snapshot = composition.build(messages, stinger_pin=stinger_pin, stinger_unavailable=unavailable,
                                     stinger_seconds=stinger_seconds)
        return {"generation": policy.generation, "snapshot": snapshot, "digest": composition.digest(snapshot),
                "ineligible": ineligible}


# -- aggregate binding and render outcomes (iPortal Phase D2A) ---------------------------------
#
# The render itself runs OUTSIDE any database lock (active_messages.worker /
# rendering). These short transactions re-read and re-lock authority, prove
# nothing relevant changed, and only then record an outcome. Lock order:
# policy -> media (lock_for_binding) -> execution. Rendering never changes a
# message, the generation or a snapshot.

# Stable, bounded D2A outcome codes (ActiveBroadcastExecution.failure_code and
# event detail). Retryable infrastructure failures never terminate an
# execution; they are recorded as ``render_retry`` events.
RENDER_RETRYABLE = "render_unavailable"
FAILURE_CODES = frozenset({
    "input_unavailable",          # a frozen message's media is missing, purged, invalid or does not hash
    "stinger_pin_unavailable",    # the FROZEN stinger pin is unusable (never re-resolved; reconciled anew)
    "provenance_mismatch",        # the rendition does not prove this exact frozen aggregate
    "rendition_invalid",          # the rendered aggregate failed validation
    "aggregate_too_long",         # the measured aggregate exceeds the CURRENT aggregate limit (never truncated)
    "render_failed",              # any other permanent render failure
})
FAILURE_DETAIL_LIMIT = 200


class MixRefused(ActiveMessageError):
    """The offered rendition may not become this execution's aggregate.
    ``code`` is a FAILURE_CODES member, or ``rendition_unavailable`` (the
    rendition vanished: render again), or ``execution_not_pending`` /
    ``stale_generation`` (authority moved on: nothing to bind to)."""


def _lock_execution(execution) -> ActiveBroadcastExecution:
    try:
        return ActiveBroadcastExecution.objects.select_for_update(of=("self",)).get(
            pk=getattr(execution, "pk", execution))
    except (ActiveBroadcastExecution.DoesNotExist, ValueError, TypeError):
        raise InvalidRequest("no such execution", code="unknown_execution") from None


def _bounded_detail(text) -> str:
    """Operator-visible detail: one line, no paths, bounded."""
    cleaned = " ".join(str(text or "").split())
    cleaned = " ".join(word for word in cleaned.split(" ") if "/" not in word)
    return cleaned[:FAILURE_DETAIL_LIMIT]


def bind_execution_mix(execution, media, *, now=None, actor_kind=ActorKind.WORKER) -> ActiveBroadcastExecution:
    """PENDING_RENDER -> READY: bind the rendered aggregate ``media``.

    Under policy -> media (lock_for_binding) -> execution, it proves:
    the execution is still PENDING_RENDER and of the CURRENT generation; its
    frozen snapshot still hashes to its composition digest; ``media`` is a
    valid, present rendition whose stored provenance digest is exactly the
    assembly of the frozen inputs (stinger pin included) -- the complete
    ordered identity, nothing else -- derived from the first message's media,
    by the render executor; and its MEASURED duration is within the CURRENT
    aggregate limit. Idempotent: the same media bound again returns the
    READY row. Raises MixRefused having written nothing -- except a stale
    execution, which is suppressed (committed) before stale_generation."""
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        try:
            bound = retention.lock_for_binding(media)
        except (MediaPurged, MediaNotValidated, MediaInconsistent):
            raise MixRefused("the rendition is missing, purged or not valid", code="rendition_unavailable") from None
        row = _lock_execution(execution)
        if row.state == X.READY and row.mix_media_id == bound.pk:
            return row
        if row.state != X.PENDING_RENDER:
            raise MixRefused(f"a {row.state} execution takes no aggregate", code="execution_not_pending")
        stale = row.policy_generation != policy.generation
        if stale:
            # Built for another generation: suppress it truthfully (stale_generation);
            # that write COMMITS, and the refusal is raised after the transaction.
            _suppress_stale(policy, now=now, actor_kind=actor_kind)
        else:
            row = _bind_proven(policy, row, bound, now=now, actor_kind=actor_kind)
    if stale:
        raise MixRefused("the execution is no longer the current composition", code="stale_generation")
    return row


def _bind_proven(policy, row, bound, *, now, actor_kind):
    """Inside bind_execution_mix's transaction, every lock held: prove, then
    write READY and its event -- or raise MixRefused having written nothing."""
    from . import rendering
    snapshot = row.composition_snapshot
    if composition.digest(snapshot) != row.composition_digest:
        raise MixRefused("the frozen snapshot no longer hashes to its digest", code="provenance_mismatch")
    if not rendering.proves(bound, snapshot):
        raise MixRefused("the rendition is not the aggregate of this frozen composition", code="provenance_mismatch")
    seconds = bound.decoded_duration_seconds
    if seconds is None or seconds <= 0:
        raise MixRefused("the rendition has no measured duration", code="rendition_invalid")
    if seconds > policy.max_aggregate_seconds:
        raise MixRefused(f"the rendered aggregate is {seconds} s; the aggregate limit is "
                         f"{policy.max_aggregate_seconds} s", code="aggregate_too_long")
    row = _store.write(ActiveBroadcastExecution, row.pk,
                       match={"state": X.PENDING_RENDER, "policy_generation": row.policy_generation, "mix_media": None},
                       values={"state": X.READY, "mix_media": bound, "ready_at": now})
    _event("mix_bound", execution=row, from_state=X.PENDING_RENDER, to_state=X.READY, generation=policy.generation,
           actor_kind=actor_kind,
           detail={"media": str(bound.pk), "sha256": bound.sha256, "seconds": str(seconds),
                   "provenance_digest": bound.recipe_params_digest})
    return row


def fail_execution(execution, *, code, detail="", now=None, actor_kind=ActorKind.WORKER) -> ActiveBroadcastExecution:
    """PENDING_RENDER -> FAILED with a stable FAILURE_CODES code. A stale
    execution is suppressed instead (stale_generation); one that already
    left PENDING_RENDER is returned unchanged (its new state is the truth)."""
    if code not in FAILURE_CODES:
        raise InvalidRequest(f"unknown render failure code {str(code)[:64]!r}")
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        row = _lock_execution(execution)
        if row.state != X.PENDING_RENDER:
            return row
        if row.policy_generation != policy.generation:
            _suppress_stale(policy, now=now, actor_kind=actor_kind)
            return ActiveBroadcastExecution.objects.get(pk=row.pk)
        row = _store.write(ActiveBroadcastExecution, row.pk, match={"state": X.PENDING_RENDER},
                           values={"state": X.FAILED, "open_slot": None, "terminal_at": now, "failure_code": code,
                                   "failure_detail": _bounded_detail(detail)})
        _event("render_failed", execution=row, from_state=X.PENDING_RENDER, to_state=X.FAILED,
               generation=policy.generation, actor_kind=actor_kind, detail={"code": code})
    return row


def record_render_retry(execution, *, code, now=None, actor_kind=ActorKind.WORKER):
    """A retryable infrastructure failure of a PENDING_RENDER execution: an
    event only (the worker's backoff reads these). Returns the event, or None
    when the execution is no longer PENDING_RENDER."""
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        row = _lock_execution(execution)
        if row.state != X.PENDING_RENDER:
            return None
        return _event("render_retry", execution=row, generation=policy.generation, actor_kind=actor_kind,
                      detail={"code": str(code)[:64]})


# -- air evidence (applied by the D2B engine path; unreachable in D1) ---------------------------------

def _lock_for_evidence(execution):
    """Lock order: policy -> the airing's messages (UUID order) -> execution."""
    policy = _lock_policy()
    snapshot = (ActiveBroadcastExecution.objects.filter(pk=getattr(execution, "pk", execution))
                .values_list("composition_snapshot", flat=True).first())
    if snapshot is None:
        raise InvalidRequest("no such execution", code="unknown_execution")
    messages = _lock_messages(composition.message_ids(snapshot))
    row = (ActiveBroadcastExecution.objects.select_for_update(of=("self",))
           .get(pk=getattr(execution, "pk", execution)))
    return policy, messages, row


def _apply_cadence(policy, execution, messages, *, anchor, completed, actor_kind):
    """Each still-ACTIVE participant is next due one interval after the
    airing's last air evidence. Messages activated after the airing was built
    keep their own due time (they were not in it)."""
    for message in messages.values():
        if message.state != M.ACTIVE:
            continue
        values = {"next_due_at": anchor + timedelta(seconds=message.replay_interval_seconds)}
        if completed:
            values["last_completed_at"] = anchor
        stored = _store.write(ActiveBroadcastMessage, message.pk, match={"state": M.ACTIVE}, values=values)
        _event("aired" if completed else "air_interrupted", message=stored, execution=execution,
               generation=policy.generation, actor_kind=actor_kind,
               detail={"next_due_at": _iso(stored.next_due_at)})


def _check_evidence(row, attempt_token, ended_at, duration):
    if row.attempt_token is None or str(row.attempt_token) != str(attempt_token):
        raise EvidenceRefused("the evidence is not from the execution's current attempt", code="attempt_mismatch")
    if ended_at is None or timezone.is_naive(ended_at) or ended_at < row.started_at:
        raise EvidenceRefused("the air evidence ends before it started", code="evidence_order")
    if duration is not None and (not isinstance(duration, Decimal) or duration < 0):
        raise EvidenceRefused("confirmed duration is a non-negative Decimal", code="evidence_duration")


def record_air_started(execution, *, attempt_token, started_at, claimed_at=None, now=None,
                       actor_kind=ActorKind.ENGINE):
    """ACCEPTED/CLAIMED -> STARTED (D2B) from the deck's OWN first-real-buffer
    evidence (``LogItem.played_at`` / ``PlayEvent.started_at``) -- never from
    queueing, deck construction or PLAYING state. ``claimed_at`` fills a claim
    the engine committed without its own CLAIMED step. Idempotent for the same
    attempt."""
    now = _now(now)
    if started_at is None or timezone.is_naive(started_at):
        raise EvidenceRefused("air start evidence is a timezone-aware time", code="evidence_order")
    with transaction.atomic():
        policy = _lock_policy()
        row = ActiveBroadcastExecution.objects.select_for_update(of=("self",)).get(
            pk=getattr(execution, "pk", execution))
        if row.attempt_token is None or str(row.attempt_token) != str(attempt_token):
            raise EvidenceRefused("the evidence is not from the execution's current attempt", code="attempt_mismatch")
        if row.state in (X.STARTED, X.COMPLETED, X.INTERRUPTED) and row.started_at is not None:
            return row
        if row.state not in (X.ACCEPTED, X.CLAIMED):
            raise InvalidTransition(f"a {row.state} execution cannot start", code="not_engine_held")
        claimed = row.claimed_at or claimed_at or started_at
        row = _store.write(ActiveBroadcastExecution, row.pk, match={"state": row.state,
                                                                    "attempt_token": row.attempt_token},
                           values={"state": X.STARTED, "claimed_at": claimed, "started_at": started_at})
        _event("air_started", execution=row, from_state=X.CLAIMED, to_state=X.STARTED, generation=policy.generation,
               actor_kind=actor_kind, detail={"started_at": _iso(started_at)})
    return row


def record_air_completed(execution, *, attempt_token, ended_at, confirmed_duration_seconds, now=None,
                         actor_kind=ActorKind.ENGINE):
    """STARTED -> COMPLETED from a clean end of the aggregate, then the cadence:
    every still-active participant is next due ``ended_at + its interval``
    (completed-air-to-next-due). Idempotent for the same attempt."""
    now = _now(now)
    with transaction.atomic():
        policy, messages, row = _lock_for_evidence(execution)
        if row.state == X.COMPLETED and str(row.attempt_token) == str(attempt_token):
            return row
        if row.state != X.STARTED:
            raise InvalidTransition(f"a {row.state} execution cannot complete", code="not_on_air")
        _check_evidence(row, attempt_token, ended_at, confirmed_duration_seconds)
        if confirmed_duration_seconds is None:
            raise EvidenceRefused("completion needs the confirmed duration", code="evidence_duration")
        row = _store.write(ActiveBroadcastExecution, row.pk, match={"state": X.STARTED, "attempt_token": row.attempt_token},
                           values={"state": X.COMPLETED, "open_slot": None, "ended_at": ended_at, "terminal_at": now,
                                   "confirmed_duration_seconds": confirmed_duration_seconds})
        _event("air_completed", execution=row, from_state=X.STARTED, to_state=X.COMPLETED, generation=policy.generation,
               actor_kind=actor_kind, detail={"ended_at": _iso(ended_at),
                                              "confirmed_duration_seconds": str(confirmed_duration_seconds)})
        _apply_cadence(policy, row, messages, anchor=ended_at, completed=True, actor_kind=actor_kind)
    return row


def record_air_interrupted(execution, *, attempt_token, last_evidence_at, confirmed_duration_seconds=None, now=None,
                           actor_kind=ActorKind.ENGINE):
    """STARTED -> INTERRUPTED (the airing really began but did not end
    cleanly, e.g. an engine restart). No immediate re-airing: participants are
    next due one interval after the last air evidence."""
    now = _now(now)
    with transaction.atomic():
        policy, messages, row = _lock_for_evidence(execution)
        if row.state == X.INTERRUPTED and str(row.attempt_token) == str(attempt_token):
            return row
        if row.state != X.STARTED:
            raise InvalidTransition(f"a {row.state} execution cannot be interrupted", code="not_on_air")
        _check_evidence(row, attempt_token, last_evidence_at, confirmed_duration_seconds)
        row = _store.write(ActiveBroadcastExecution, row.pk, match={"state": X.STARTED, "attempt_token": row.attempt_token},
                           values={"state": X.INTERRUPTED, "open_slot": None, "ended_at": last_evidence_at,
                                   "terminal_at": now, "confirmed_duration_seconds": confirmed_duration_seconds})
        _event("air_interrupted", execution=row, from_state=X.STARTED, to_state=X.INTERRUPTED,
               generation=policy.generation, actor_kind=actor_kind,
               detail={"last_evidence_at": _iso(last_evidence_at)})
        _apply_cadence(policy, row, messages, anchor=last_evidence_at, completed=False, actor_kind=actor_kind)
    return row


# -- policy --------------------------------------------------------------------------------------

def current_policy() -> ActiveBroadcastPolicy:
    """The committed policy -- a plain read that never waits on a lock (the
    engine's advisory checks call this on its audio thread). The singleton
    is created only when it does not exist yet: the conflict-ignoring insert
    would otherwise wait on any uncommitted policy change."""
    policy = ActiveBroadcastPolicy.objects.filter(pk=POLICY_ID).first()
    if policy is None:
        _store.ensure_policy()
        policy = ActiveBroadcastPolicy.objects.get(pk=POLICY_ID)
    return policy


def _policy_write(policy, actor, now, values) -> ActiveBroadcastPolicy:
    return _store.write(ActiveBroadcastPolicy, POLICY_ID, match={"generation": policy.generation},
                        values={**values, "updated_at": now, "updated_by": actor,
                                "updated_by_username": _username(actor)})


def set_default_replay_interval(seconds, *, actor, now=None) -> ActiveBroadcastPolicy:
    """The interval NEW activations snapshot. Active messages keep theirs; the
    composition does not change, so neither does the generation."""
    _require(actor, CONTROL)
    if (not isinstance(seconds, int) or isinstance(seconds, bool)
            or not REPLAY_INTERVAL_MIN_SECONDS <= seconds <= REPLAY_INTERVAL_MAX_SECONDS):
        raise InvalidRequest(f"the interval is {REPLAY_INTERVAL_MIN_SECONDS}..{REPLAY_INTERVAL_MAX_SECONDS} seconds")
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        if policy.default_replay_interval_seconds == seconds:
            return policy
        previous = policy.default_replay_interval_seconds
        policy = _policy_write(policy, actor, now, {"default_replay_interval_seconds": seconds})
        _event("policy_replay_interval", generation=policy.generation, actor_kind=ActorKind.USER, user=actor,
               detail={"seconds": seconds, "previous": previous})
    return policy


def set_stinger_key(key, *, actor, now=None) -> ActiveBroadcastPolicy:
    """Name the ReferenceAudio key whose current registration precedes urgent
    aggregates ("" for none). Output-affecting: a new generation."""
    _require(actor, CONTROL)
    validate_stinger_key(key)
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        if policy.stinger_key == key:
            return policy
        previous = policy.stinger_key
        policy = _bump(policy)
        policy = _policy_write(policy, actor, now, {"stinger_key": key})
        _event("policy_stinger", generation=policy.generation, actor_kind=ActorKind.USER, user=actor,
               detail={"stinger_key": key, "previous": previous})
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
    return policy


LIMIT_CEILINGS = {
    "max_active_messages": ABSOLUTE_MAX_ACTIVE_MESSAGES,
    "max_message_seconds": ABSOLUTE_MAX_MESSAGE_SECONDS,
    "max_aggregate_seconds": ABSOLUTE_MAX_AGGREGATE_SECONDS,
}


def set_limits(*, actor, max_active_messages=None, max_message_seconds=None, max_aggregate_seconds=None,
               now=None) -> ActiveBroadcastPolicy:
    """Change the composition limits (control). ``None`` leaves a limit as is.

    Each limit is a whole number 1..its absolute ceiling, and the per-message
    limit never exceeds the aggregate one. A change can change which active
    set may air, so it is output-affecting: ONE generation step for the whole
    call (however many limits change), and still-pending airings built for the
    old generation are suppressed. Nothing already active is cleared or
    truncated: a set that no longer fits is simply not aired (fail closed)
    until an operator clears messages or raises the limit."""
    _require(actor, CONTROL)
    requested = {"max_active_messages": max_active_messages, "max_message_seconds": max_message_seconds,
                 "max_aggregate_seconds": max_aggregate_seconds}
    for name, value in requested.items():
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= LIMIT_CEILINGS[name]:
            raise InvalidRequest(f"{name} is a whole number from 1 to {LIMIT_CEILINGS[name]}",
                                 code="limit_out_of_range")
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        current = {name: getattr(policy, name) for name in requested}
        merged = {name: current[name] if value is None else value for name, value in requested.items()}
        if merged["max_message_seconds"] > merged["max_aggregate_seconds"]:
            raise InvalidRequest("the per-message limit cannot exceed the aggregate limit",
                                 code="message_exceeds_aggregate")
        changed = {name: value for name, value in merged.items() if value != current[name]}
        if not changed:
            return policy
        policy = _bump(policy)
        policy = _policy_write(policy, actor, now, changed)
        _event("policy_limits", generation=policy.generation, actor_kind=ActorKind.USER, user=actor,
               detail={"previous": {name: current[name] for name in changed}, "limits": changed})
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
    return policy


def set_native_airing(enabled: bool, *, actor, now=None) -> ActiveBroadcastPolicy:
    """The dormant gate. It cannot be opened before D2B
    (NATIVE_AIRING_AVAILABLE); closing it is a new generation."""
    _require(actor, CONTROL)
    if not isinstance(enabled, bool):
        raise InvalidRequest("enabled is a boolean")
    if enabled and not NATIVE_AIRING_AVAILABLE:
        raise NativeAiringUnavailable("native airing is not available in this release; OGRemote stays authoritative")
    now = _now(now)
    with transaction.atomic():
        policy = _lock_policy()
        if policy.native_airing_enabled == enabled:
            return policy
        policy = _bump(policy)
        policy = _policy_write(policy, actor, now, {"native_airing_enabled": enabled})
        _event("policy_native_airing", generation=policy.generation, actor_kind=ActorKind.USER, user=actor,
               detail={"enabled": enabled})
        _suppress_stale(policy, now=now, actor_kind=ActorKind.USER, user=actor)
    return policy
