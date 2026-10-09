"""THE spoken-item lifecycle authority.

Every state change goes through ``_apply``:

1. lock the item row (``select_for_update(of=("self",))`` -- the nullable-FK
   outer-join rule);
2. check the FIXED transition table (and that the caller's expected source
   state is the one it found);
3. check actor, policy and evidence against the locked, authoritative row;
4. bind any media under ``lock_for_binding`` (the ProductionMedia binding
   rule), update state and timestamps;
5. append the SpokenItemEvent -- state and event commit together or not at all.

Transitions that deliver to, or retire from, the library (``-> delivered``,
``-> superseded``, delivered ``-> recalled``/``expired``) are part of the table
-- their identities are fixed now -- but are refused until the C2 projection
service exists: C1 never touches library Tracks.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.models import ProductionMedia
from production.services import retention

from . import guard
from .models import (
    ActorKind, ItemOrigin, ItemState, RecallPolicy, SpokenContentType, SpokenItem, SpokenItemEvent,
)

S = ItemState
DETAIL_LIMIT_BYTES = 8 * 1024
LEASE_MIN_SECONDS, LEASE_MAX_SECONDS = 30, 3600


class SpokenServiceError(Exception):
    code = "spoken_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class InvalidTransition(SpokenServiceError):
    code = "invalid_transition"


class NotPermitted(SpokenServiceError):
    code = "not_permitted"


class EvidenceRefused(SpokenServiceError):
    code = "evidence_refused"


class ProjectionRequired(InvalidTransition):
    code = "projection_required"


@dataclass(frozen=True)
class Rule:
    event: str
    c2: bool = False          # needs the library-projection service (C2)


TRANSITIONS: dict[tuple, Rule] = {
    (None, S.DRAFT): Rule("created"),
    (S.DRAFT, S.SUBMITTED): Rule("submitted"),
    (S.SUBMITTED, S.VALIDATED): Rule("validated"),
    (S.SUBMITTED, S.INVALID): Rule("invalid"),
    (S.VALIDATED, S.PROCESSING): Rule("processing_started"),
    (S.PROCESSING, S.PROCESSING): Rule("processing_reclaimed"),
    (S.PROCESSING, S.PROCESSED): Rule("processed"),
    (S.PROCESSING, S.FAILED): Rule("failed"),
    (S.FAILED, S.VALIDATED): Rule("retried"),
    (S.PROCESSED, S.AWAITING_REVIEW): Rule("review_requested"),
    (S.AWAITING_REVIEW, S.REJECTED): Rule("rejected"),
    # recall before delivery (no library effect)
    (S.DRAFT, S.RECALLED): Rule("recalled"),
    (S.SUBMITTED, S.RECALLED): Rule("recalled"),
    (S.VALIDATED, S.RECALLED): Rule("recalled"),
    (S.PROCESSED, S.RECALLED): Rule("recalled"),
    (S.AWAITING_REVIEW, S.RECALLED): Rule("recalled"),
    (S.FAILED, S.RECALLED): Rule("recalled"),
    # library projection -- C2
    (S.PROCESSED, S.DELIVERED): Rule("delivered", c2=True),
    (S.AWAITING_REVIEW, S.DELIVERED): Rule("approved_delivered", c2=True),
    (S.DELIVERED, S.SUPERSEDED): Rule("superseded", c2=True),
    (S.SUPERSEDED, S.DELIVERED): Rule("repromoted", c2=True),
    (S.DELIVERED, S.RECALLED): Rule("recalled", c2=True),
    (S.DELIVERED, S.EXPIRED): Rule("expired", c2=True),
}


# -- actors -------------------------------------------------------------------------

def is_manager(user) -> bool:
    """C1: staff/superuser. Per-type grants and capabilities are Phase E."""
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and (user.is_staff or user.is_superuser))


def _actor_columns(actor, actor_kind):
    if actor_kind == ActorKind.USER:
        if actor is None or getattr(actor, "pk", None) is None:
            raise NotPermitted("this transition needs an authenticated user")
        return actor, actor.get_username()[:150]
    if actor is not None and getattr(actor, "pk", None) is not None:
        return actor, actor.get_username()[:150]
    return None, ""


def _bounded(detail) -> dict:
    detail = dict(detail or {})
    encoded = json.dumps(detail, sort_keys=True, default=str)
    if len(encoded.encode()) > DETAIL_LIMIT_BYTES:
        raise ValueError("event detail exceeds its bound")
    return json.loads(encoded)


# -- the core ---------------------------------------------------------------------------

def _apply(item, to_state, *, actor=None, actor_kind=ActorKind.USER, expected_from=None, detail=None,
           check=None, mutate=None, media=None, require_valid_media=True):
    with transaction.atomic(), guard.transition_scope():
        row = SpokenItem.objects.select_for_update(of=("self",)).select_related("content_type").get(
            pk=getattr(item, "pk", item))
        key = (row.state, to_state)
        rule = TRANSITIONS.get(key)
        if rule is None or (expected_from is not None and row.state not in expected_from):
            raise InvalidTransition(f"{row.state} -> {to_state} is not a permitted transition")
        if rule.c2:
            raise ProjectionRequired(f"{row.state} -> {to_state} delivers to the library (Phase C2)")
        user, username = _actor_columns(actor, actor_kind)
        if check is not None:
            check(row, user)
        bound = None
        if media is not None:
            try:
                bound = retention.lock_for_binding(media, require_valid=require_valid_media)
            except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
                raise EvidenceRefused("the media is missing, purged or not valid", code="media_unusable") from exc
        now = timezone.now()
        from_state = row.state
        if mutate is not None:
            mutate(row, bound, now)
        row.state = to_state
        row.state_changed_at = now
        if to_state in (S.RECALLED, S.EXPIRED, S.INVALID, S.REJECTED):
            row.terminal_at = now
        row.save()
        SpokenItemEvent.objects.create(
            item=row, event=rule.event, from_state=from_state, to_state=to_state, actor_kind=actor_kind,
            actor=user, actor_username=username, media=bound, detail=_bounded(detail),
        )
        return row


# -- creation and submission -------------------------------------------------------------

def create_item(content_type, *, actor, title="", notes="", origin=ItemOrigin.SUBMITTED, detail=None) -> SpokenItem:
    """(none) -> draft. The type must be enabled. ``imported`` items are
    created only by the Phase F import tooling."""
    if origin == ItemOrigin.IMPORTED:
        raise NotPermitted("imported items are created by the import tooling (Phase F)")
    if origin not in (ItemOrigin.SUBMITTED, ItemOrigin.ASSIGNED):
        raise NotPermitted("unknown origin")
    user, username = _actor_columns(actor, ActorKind.USER)
    with transaction.atomic(), guard.transition_scope():
        ctype = SpokenContentType.objects.select_for_update().get(pk=getattr(content_type, "pk", content_type))
        if not ctype.enabled:
            raise NotPermitted("this content type is disabled", code="type_disabled")
        now = timezone.now()
        row = SpokenItem.objects.create(
            content_type=ctype, origin=origin, submitted_by=user, submitted_by_username=username,
            title=str(title)[:200], notes=str(notes), state=S.DRAFT, state_changed_at=now,
        )
        SpokenItemEvent.objects.create(
            item=row, event=TRANSITIONS[(None, S.DRAFT)].event, from_state="", to_state=S.DRAFT,
            actor_kind=ActorKind.USER, actor=user, actor_username=username, detail=_bounded(detail),
        )
        return row


def _owner_or_manager(row, user):
    if is_manager(user):
        return
    if row.submitted_by_id is None or row.submitted_by_id != getattr(user, "pk", None):
        raise NotPermitted("only the submitter or a manager may do this")


_SUBMITTABLE_KINDS = (ProductionMedia.KIND_RECORDING, ProductionMedia.KIND_UPLOAD, ProductionMedia.KIND_EDIT)


def submit(item, media, *, actor) -> SpokenItem:
    """draft -> submitted, binding ``media`` as the submission. Validation of
    the media is the next transition; its byte size is checked here."""

    def check(row, user):
        _owner_or_manager(row, user)
        if not SpokenContentType.objects.filter(pk=row.content_type_id, enabled=True).exists():
            raise NotPermitted("this content type is disabled", code="type_disabled")
        candidate = ProductionMedia.objects.filter(pk=getattr(media, "pk", media)).first()
        if candidate is None or candidate.kind not in _SUBMITTABLE_KINDS:
            raise EvidenceRefused("a submission is a recording, upload or edit", code="media_kind")
        limit = row.content_type.max_bytes
        if limit is not None and candidate.byte_size > limit:
            raise EvidenceRefused("the media is larger than this type allows", code="too_large")

    def mutate(row, bound, now):
        row.submission_media = bound
        row.submitted_at = now

    return _apply(item, S.SUBMITTED, actor=actor, check=check, mutate=mutate, media=media,
                  require_valid_media=False, detail={"media": str(getattr(media, "pk", media))})


def record_validation(item, *, actor_kind=ActorKind.SYSTEM) -> SpokenItem:
    """submitted -> validated | invalid, from the submission media's own
    validation verdict and this type's duration bounds."""
    row = SpokenItem.objects.select_related("submission_media", "content_type").get(pk=getattr(item, "pk", item))
    media = row.submission_media
    if media is None:
        raise EvidenceRefused("no submission media is bound", code="no_media")
    if media.validation_state == ProductionMedia.VALIDATION_UNVALIDATED:
        raise EvidenceRefused("the submission media has not been validated yet", code="not_validated_yet")
    code = None
    if not media.is_valid:
        code = media.validation_code or "invalid_media"
    else:
        from production.policy import policy_duration_code
        from .configuration import media_policy_for
        code = policy_duration_code(media.decoded_duration_seconds, media_policy_for(row.content_type))
    if code:
        def fail(row, bound, now):
            row.failure_code, row.failure_detail = code[:64], ""
        return _apply(row, S.INVALID, actor_kind=actor_kind, expected_from=(S.SUBMITTED,), mutate=fail,
                      detail={"code": code})

    def ok(row, bound, now):
        row.validated_at = now
    return _apply(row, S.VALIDATED, actor_kind=actor_kind, expected_from=(S.SUBMITTED,), mutate=ok,
                  media=media, detail={"duration_seconds": str(media.decoded_duration_seconds)})


# -- processing (lease) -----------------------------------------------------------------------

def start_processing(item, *, lease_seconds=600, actor_kind=ActorKind.WORKER) -> tuple[SpokenItem, uuid.UUID]:
    """validated -> processing with a fresh lease; or reclaim an item whose
    lease has EXPIRED (processing -> processing, a new token)."""
    if not (LEASE_MIN_SECONDS <= int(lease_seconds) <= LEASE_MAX_SECONDS):
        raise ValueError("lease length out of range")
    token = uuid.uuid4()

    def check(row, user):
        if row.state == S.PROCESSING and (row.lease_expires_at is None or row.lease_expires_at > timezone.now()):
            raise InvalidTransition("the item is leased to another worker", code="lease_held")

    def mutate(row, bound, now):
        row.lease_token, row.lease_expires_at = token, now + timedelta(seconds=int(lease_seconds))
        row.processing_attempts = min(row.processing_attempts + 1, 1000)
        row.processing_started_at = now
        row.failure_code, row.failure_detail = "", ""

    row = _apply(item, S.PROCESSING, actor_kind=actor_kind, expected_from=(S.VALIDATED, S.PROCESSING),
                 check=check, mutate=mutate, detail={"lease_seconds": int(lease_seconds)})
    return row, token


def _require_lease(row, token):
    if row.lease_token is None or str(row.lease_token) != str(token):
        raise EvidenceRefused("the lease is stale or belongs to another worker", code="stale_lease")


def complete_processing(item, *, lease_token, rendition, render_detail=None,
                        actor_kind=ActorKind.WORKER) -> SpokenItem:
    """processing -> processed, binding the rendition. The rendition must be
    a valid ``rendition`` derived from THIS item's submission by THIS type's
    recipe; the caller's lease must still be the current one."""

    def check(row, user):
        _require_lease(row, lease_token)
        candidate = ProductionMedia.objects.filter(pk=getattr(rendition, "pk", rendition)).first()
        if (candidate is None or candidate.kind != ProductionMedia.KIND_RENDITION
                or candidate.derived_from_id != row.submission_media_id
                or candidate.recipe_key != row.content_type.recipe_key):
            raise EvidenceRefused("the rendition does not derive from this item's submission by its recipe",
                                  code="rendition_mismatch")

    def mutate(row, bound, now):
        row.rendition_media = bound
        row.processed_at = now
        row.lease_token = row.lease_expires_at = None

    detail = {"rendition": str(getattr(rendition, "pk", rendition))}
    detail.update(render_detail or {})
    return _apply(item, S.PROCESSED, actor_kind=actor_kind, expected_from=(S.PROCESSING,), check=check,
                  mutate=mutate, media=rendition, detail=detail)


def fail_processing(item, *, lease_token, code, detail="", actor_kind=ActorKind.WORKER) -> SpokenItem:
    def check(row, user):
        _require_lease(row, lease_token)

    def mutate(row, bound, now):
        row.failure_code, row.failure_detail = str(code)[:64], str(detail)[:500]
        row.lease_token = row.lease_expires_at = None

    return _apply(item, S.FAILED, actor_kind=actor_kind, expected_from=(S.PROCESSING,), check=check,
                  mutate=mutate, detail={"code": str(code)[:64]})


def retry(item, *, actor) -> SpokenItem:
    def check(row, user):
        if not is_manager(user):
            raise NotPermitted("only a manager may retry a failed item")

    def mutate(row, bound, now):
        row.failure_code, row.failure_detail = "", ""

    return _apply(item, S.VALIDATED, actor=actor, expected_from=(S.FAILED,), check=check, mutate=mutate)


# -- review and recall ---------------------------------------------------------------------------

def request_review(item, *, actor_kind=ActorKind.SYSTEM) -> SpokenItem:
    def check(row, user):
        if not row.content_type.requires_review:
            raise InvalidTransition("this type does not require review", code="review_not_required")
    return _apply(item, S.AWAITING_REVIEW, actor_kind=actor_kind, expected_from=(S.PROCESSED,), check=check)


def reject(item, *, actor, note="") -> SpokenItem:
    def check(row, user):
        if not is_manager(user):
            raise NotPermitted("only a manager may review")

    def mutate(row, bound, now):
        row.reviewed_by, row.reviewed_at = actor, now
        row.reviewed_by_username = actor.get_username()[:150]
        row.review_note = str(note)[:1000]

    return _apply(item, S.REJECTED, actor=actor, expected_from=(S.AWAITING_REVIEW,), check=check, mutate=mutate,
                  detail={"note": str(note)[:1000]})


def recall(item, *, actor, reason="") -> SpokenItem:
    """Recall before delivery (C1). Managers always; the submitter while the
    type's recall policy allows it. A delivered item's recall is C2."""

    def check(row, user):
        if is_manager(user):
            return
        if row.content_type.recall_policy == RecallPolicy.MANAGERS_ONLY:
            raise NotPermitted("only managers may recall this type")
        _owner_or_manager(row, user)

    def mutate(row, bound, now):
        row.recalled_by, row.recalled_at = actor, now
        row.recalled_by_username = actor.get_username()[:150]
        row.recall_reason = str(reason)[:500]

    return _apply(item, S.RECALLED, actor=actor, check=check, mutate=mutate, detail={"reason": str(reason)[:500]})
