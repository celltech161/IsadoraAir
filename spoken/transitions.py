"""THE spoken-item lifecycle authority.

Every state change goes through ``_apply``:

1. lock the item row (``select_for_update(of=("self",))`` -- the nullable-FK
   outer-join rule);
2. check the FIXED transition table (and that the caller's expected source
   state is the one it found);
3. check actor, policy and evidence against the locked, authoritative row;
4. bind any media under ``lock_for_binding`` (the ProductionMedia binding
   rule) and verify any further evidence (render completion);
5. compute ONE immutable ``TransitionPlan`` -- the exact protected item
   columns and the exact event -- from the validated data;
6. write the item under an exact, single-use ``ItemWrite`` grant derived from
   the plan, then the event under an exact ``EventCreate`` grant derived from
   the same plan (spoken.guard) -- state and event commit together or not at
   all.

There is no trusted "inside the service" context: each grant authorizes one
statement with one payload, is checked against the transition table, and is
closed before anything else runs. Creation (``create_item``) works the same
way with an ``ItemCreate`` grant for the exact new row.

Render completion additionally locks the item's content type FIRST (the
same row lock configuration saves and ``create_item`` take; order: type ->
item -> media -> reference key), so the configuration it verifies against
cannot change before the processed state commits.

Transitions that deliver to, or retire from, the library (``-> delivered``,
``-> superseded``, delivered ``-> recalled``/``expired``) are part of the table
-- their identities are fixed now -- but are refused until the C2 projection
service exists: C1 never touches library Tracks.
"""
from __future__ import annotations

import json
import uuid
import weakref
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.models import ProductionMedia
from production.services import reference_audio, render, retention
from production.services.render import protocol as render_protocol

from . import guard
from .lifecycle import TRANSITIONS, Rule, writable_columns  # noqa: F401 -- Rule re-exported
from .models import (
    TERMINAL_STATES, ActorKind, ItemOrigin, ItemState, RecallPolicy, SpokenContentType, SpokenItem,
    SpokenItemEvent,
)

S = ItemState
DETAIL_LIMIT_BYTES = 8 * 1024
LEASE_MIN_SECONDS, LEASE_MAX_SECONDS = 30, 3600
_NO_LEASE = object()                 # _apply: the transition is not lease-authorized


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


class InvalidEventDetail(SpokenServiceError, ValueError):
    code = "invalid_event_detail"


# -- actors -------------------------------------------------------------------------

def is_manager(user) -> bool:
    """C1: staff/superuser. Per-type grants and capabilities are Phase E."""
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and (user.is_staff or user.is_superuser))


# 2.22D3C: a domain that OWNS some content types through an explicit link (a
# produced_content.ProducedContentType's managed backend) may name who manages
# the items of exactly those types. Consulted ONLY by recall (pre-delivery
# withdrawal and delivered recall), under the item's row lock, and only for the
# item's own type: an unlinked standalone type gains nothing.
_TYPE_RECALL_AUTHORITIES: list = []


def register_recall_authority(check) -> None:
    """``check(user, content_type_id) -> bool`` -- from an owning domain's
    AppConfig.ready(). Registering the same function twice is a no-op."""
    if check not in _TYPE_RECALL_AUTHORITIES:
        _TYPE_RECALL_AUTHORITIES.append(check)


def may_recall_as_manager(user, row) -> bool:
    """A manager (staff), or a user an owning domain names as managing this
    exact item's type (see register_recall_authority)."""
    if is_manager(user):
        return True
    if user is None or getattr(user, "pk", None) is None:
        return False
    return any(check(user, row.content_type_id) for check in _TYPE_RECALL_AUTHORITIES)


def _actor_columns(actor, actor_kind):
    if actor_kind == ActorKind.USER:
        if actor is None or getattr(actor, "pk", None) is None:
            raise NotPermitted("this transition needs an authenticated user")
        return actor, actor.get_username()[:150]
    if actor is not None and getattr(actor, "pk", None) is not None:
        return actor, actor.get_username()[:150]
    return None, ""


def _bounded(detail) -> str:
    """Event detail as bounded, strict CANONICAL JSON text (guard.canonical_json):
    an immutable value the plan can hold. A value without a strict JSON meaning
    is refused at planning -- never stringified."""
    detail = {} if detail is None else detail
    if not isinstance(detail, dict):
        raise InvalidEventDetail("event detail is a JSON object")
    try:
        encoded = guard.canonical_json(detail)
    except guard.NotCanonicalJSON as exc:
        raise InvalidEventDetail(f"event detail is not strict JSON: {exc}") from None
    if len(encoded.encode()) > DETAIL_LIMIT_BYTES:
        raise InvalidEventDetail("event detail exceeds its bound")
    return encoded


# -- plans: the exact writes a validated transition makes ------------------------------------

_IMMUTABLE_VALUES = (str, int, float, bool, type(None), datetime, date, Decimal, uuid.UUID)


@dataclass(frozen=True, eq=False)
class TransitionPlan:
    """The already-validated intended writes of one transition. Both grants
    derive from this one object, so the item write and its event cannot
    diverge. Only plans built by ``_plan`` (after validation) are committed.

    DEEPLY immutable: each payload is a tuple of (attname, value) pairs whose
    values are immutable scalars; event ``detail`` is canonical JSON text,
    decoded into a fresh object only by the exact executor (spoken.guard)."""

    item_id: object
    from_state: str | None
    to_state: str
    item_values: tuple            # ((attname, value), ...) -- protected item columns written
    item_expected: tuple          # EVERY authoritative item column afterwards (locked row + values)
    event_values: tuple           # ((attname, value), ...) -- every event column

    def __post_init__(self):
        for name in ("item_values", "item_expected", "event_values"):
            pairs = getattr(self, name)
            if not isinstance(pairs, tuple) or not all(
                    isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)
                    and isinstance(pair[1], _IMMUTABLE_VALUES) for pair in pairs):
                raise TypeError(f"TransitionPlan.{name} must hold only immutable (attname, value) pairs")


_PLANNED = weakref.WeakSet()


def _attname_values(model, values: dict) -> tuple:
    """Field names (with related objects) -> sorted ((attname, frozen value), ...)."""
    out = {}
    for name, value in values.items():
        field = model._meta.get_field(name)
        out[field.attname] = guard.freeze(field, value)
    return tuple(sorted(out.items()))


def _plan(row, *, from_state, to_state, rule, changes, actor_kind, user, username, media, detail,
          locked=None, asset=None, edition=None) -> TransitionPlan:
    """``locked``: guard.snapshot of the row taken right after it was locked
    (None for a creation) -- the authority for every column the transition
    leaves alone; never a later re-read."""
    item_values = _attname_values(SpokenItem, changes)
    if from_state is not None:
        extra = {name for name, _ in item_values} - writable_columns(from_state, to_state)
        if extra:                                            # a service bug, never a caller's choice
            raise InvalidTransition(f"{from_state} -> {to_state} does not write {sorted(extra)}")
        item_expected = tuple(sorted({**dict(locked), **dict(item_values)}.items()))
    else:
        item_expected = guard.creation_expected(item_values)
    detail_text = _bounded(detail)                           # strict canonical JSON, refused before any grant
    event_values = tuple(sorted(dict(_attname_values(SpokenItemEvent, {
        "item": row.pk, "event": rule.event, "from_state": from_state or "", "to_state": to_state,
        "actor_kind": actor_kind, "actor": user, "actor_username": username, "media": media,
        "asset": asset, "edition": edition,
    }), detail=detail_text).items()))
    plan = TransitionPlan(item_id=row.pk, from_state=from_state, to_state=to_state, item_values=item_values,
                          item_expected=item_expected, event_values=event_values)
    _PLANNED.add(plan)
    return plan


def _require_planned(plan, row) -> None:
    if plan not in _PLANNED or str(plan.item_id) != str(row.pk):
        raise guard.SpokenIntegrityError("only a validated transition plan is committed, once")


def _write_event(plan) -> SpokenItemEvent:
    if plan not in _PLANNED:
        raise guard.SpokenIntegrityError("only a validated transition plan is committed, once")
    _PLANNED.discard(plan)                                   # a plan records exactly one event
    with guard._authorized(guard.EventCreate(values=plan.event_values)):
        event_id = guard._execute()
    return SpokenItemEvent.objects.get(pk=event_id)


def _commit(row, plan) -> None:
    """Perform exactly the plan: the item UPDATE under its ItemWrite grant,
    then the event INSERT under its EventCreate grant -- each executed by the
    guard straight from the grant's payload (no model instance, no signals)
    and verified by reading the row back. The plan is spent afterwards."""
    _require_planned(plan, row)
    if plan.from_state is None:
        raise guard.SpokenIntegrityError("creation plans are committed by create_item")
    with guard._authorized(guard.ItemWrite(item_id=plan.item_id, from_state=plan.from_state,
                                           to_state=plan.to_state, values=plan.item_values,
                                           expected=plan.item_expected)):
        guard._execute()
    _write_event(plan)
    row.refresh_from_db()


def _commit_projected(row, plan, *, asset_id, edition_id) -> None:
    """A C2 plan, committed by the projection service (spoken.delivery): the
    item write under a ProjectedItemWrite grant -- whose projection witness
    the guard re-checks against the locked database -- then its event."""
    _require_planned(plan, row)
    write = guard.ItemWrite(item_id=plan.item_id, from_state=plan.from_state, to_state=plan.to_state,
                            values=plan.item_values, expected=plan.item_expected)
    with guard._authorized(guard.ProjectedItemWrite(write=write, asset_id=asset_id, edition_id=edition_id)):
        guard._execute()
    _PLANNED.discard(plan)
    with guard._authorized(guard.ProjectedEventCreate(values=plan.event_values)):
        guard._execute()
    row.refresh_from_db()


# -- the core ---------------------------------------------------------------------------

def _apply(item, to_state, *, actor=None, actor_kind=ActorKind.USER, expected_from=None, detail=None,
           check=None, changes=None, media=None, require_valid_media=True, lock_type=False, verify=None,
           lease_token=_NO_LEASE):
    """``changes(row, bound, now)`` returns the transition's own protected
    columns ({field name: value}); state, its timestamp and terminal_at are
    added here. ``lease_token``: the transition is authorized by the
    processing lease, which must be the item's current AND unexpired lease
    (_require_lease) at the ``now`` taken under the row lock."""
    with transaction.atomic():
        pk = getattr(item, "pk", item)
        if lock_type:
            # content_type is immutable on an item (guard), so reading it
            # unlocked is safe; the TYPE lock is taken before the item lock.
            type_id = SpokenItem.objects.filter(pk=pk).values_list("content_type_id", flat=True).first()
            if type_id is None:
                raise SpokenItem.DoesNotExist("no such spoken item")
            SpokenContentType.objects.select_for_update().filter(pk=type_id).values_list("pk").first()
        row = SpokenItem.objects.select_for_update(of=("self",)).select_related("content_type").get(pk=pk)
        locked = guard.snapshot(row)            # the authoritative pre-write row, before any callback runs
        key = (row.state, to_state)
        rule = TRANSITIONS.get(key)
        if rule is None or (expected_from is not None and row.state not in expected_from):
            raise InvalidTransition(f"{row.state} -> {to_state} is not a permitted transition")
        if rule.c2:
            raise ProjectionRequired(f"{row.state} -> {to_state} delivers to the library (Phase C2)")
        user, username = _actor_columns(actor, actor_kind)
        # ONE timezone-aware server time for the whole transition, taken under
        # the row lock: lease freshness, the transition's own timestamps.
        now = timezone.now()
        if lease_token is not _NO_LEASE:
            _require_lease(row, lease_token, now)
        if check is not None:
            check(row, user, now)
        bound = None
        if media is not None:
            try:
                bound = retention.lock_for_binding(media, require_valid=require_valid_media)
            except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
                raise EvidenceRefused("the media is missing, purged or not valid", code="media_unusable") from exc
        if verify is not None:
            verify(row, bound)
        planned = dict(changes(row, bound, now)) if changes is not None else {}
        planned.update(state=to_state, state_changed_at=now)
        if to_state in TERMINAL_STATES:
            planned["terminal_at"] = now
        plan = _plan(row, from_state=dict(locked)["state"], to_state=to_state, rule=rule, changes=planned,
                     actor_kind=actor_kind, user=user, username=username, media=bound, detail=detail,
                     locked=locked)
        _commit(row, plan)
        return row


# -- creation and submission -------------------------------------------------------------

def create_item(content_type, *, actor, title="", notes="", origin=ItemOrigin.SUBMITTED, detail=None) -> SpokenItem:
    """(none) -> draft. The type must be enabled. ``imported`` items are
    created only by the Phase F import tooling. The item and its ``created``
    event are inserted in one transaction, each under its exact grant."""
    if origin == ItemOrigin.IMPORTED:
        raise NotPermitted("imported items are created by the import tooling (Phase F)")
    if origin not in (ItemOrigin.SUBMITTED, ItemOrigin.ASSIGNED):
        raise NotPermitted("unknown origin")
    user, username = _actor_columns(actor, ActorKind.USER)
    with transaction.atomic():
        ctype = SpokenContentType.objects.select_for_update().get(pk=getattr(content_type, "pk", content_type))
        if not ctype.enabled:
            raise NotPermitted("this content type is disabled", code="type_disabled")
        now = timezone.now()
        row = SpokenItem(id=uuid.uuid4(), content_type=ctype, origin=origin, submitted_by=user,
                         submitted_by_username=username, title=str(title)[:200], notes=str(notes),
                         state=S.DRAFT, state_changed_at=now)
        plan = _plan(row, from_state=None, to_state=S.DRAFT, rule=TRANSITIONS[(None, S.DRAFT)],
                     changes={"content_type": ctype, "origin": origin, "submitted_by": user,
                              "submitted_by_username": username, "title": row.title, "notes": row.notes,
                              "state": S.DRAFT, "state_changed_at": now},
                     actor_kind=ActorKind.USER, user=user, username=username, media=None, detail=detail)
        _require_planned(plan, row)
        with guard._authorized(guard.ItemCreate(item_id=plan.item_id, values=plan.item_values,
                                                expected=plan.item_expected)):
            guard._execute()
        _write_event(plan)
        return SpokenItem.objects.select_related("content_type").get(pk=plan.item_id)


def _owner_or_manager(row, user):
    if is_manager(user):
        return
    if row.submitted_by_id is None or row.submitted_by_id != getattr(user, "pk", None):
        raise NotPermitted("only the submitter or a manager may do this")


_SUBMITTABLE_KINDS = (ProductionMedia.KIND_RECORDING, ProductionMedia.KIND_UPLOAD, ProductionMedia.KIND_EDIT)


def submit(item, media, *, actor) -> SpokenItem:
    """draft -> submitted, binding ``media`` as the submission. Validation of
    the media is the next transition; its byte size is checked here."""

    def check(row, user, now):
        _owner_or_manager(row, user)
        if not SpokenContentType.objects.filter(pk=row.content_type_id, enabled=True).exists():
            raise NotPermitted("this content type is disabled", code="type_disabled")
        candidate = ProductionMedia.objects.filter(pk=getattr(media, "pk", media)).first()
        if candidate is None or candidate.kind not in _SUBMITTABLE_KINDS:
            raise EvidenceRefused("a submission is a recording, upload or edit", code="media_kind")
        limit = row.content_type.max_bytes
        if limit is not None and candidate.byte_size > limit:
            raise EvidenceRefused("the media is larger than this type allows", code="too_large")

    def changes(row, bound, now):
        return {"submission_media": bound, "submitted_at": now}

    return _apply(item, S.SUBMITTED, actor=actor, check=check, changes=changes, media=media,
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
            return {"failure_code": code[:64], "failure_detail": ""}
        return _apply(row, S.INVALID, actor_kind=actor_kind, expected_from=(S.SUBMITTED,), changes=fail,
                      detail={"code": code})

    def ok(row, bound, now):
        return {"validated_at": now}
    return _apply(row, S.VALIDATED, actor_kind=actor_kind, expected_from=(S.SUBMITTED,), changes=ok,
                  media=media, detail={"duration_seconds": str(media.decoded_duration_seconds)})


# -- processing (lease) -----------------------------------------------------------------------

def start_processing(item, *, lease_seconds=600, actor_kind=ActorKind.WORKER) -> tuple[SpokenItem, uuid.UUID]:
    """validated -> processing with a fresh lease; or reclaim an item whose
    lease has EXPIRED (processing -> processing, a new token)."""
    if not (LEASE_MIN_SECONDS <= int(lease_seconds) <= LEASE_MAX_SECONDS):
        raise ValueError("lease length out of range")
    token = uuid.uuid4()

    def check(row, user, now):
        if row.state == S.PROCESSING and (row.lease_expires_at is None or row.lease_expires_at > now):
            raise InvalidTransition("the item is leased to another worker", code="lease_held")

    def changes(row, bound, now):
        return {"lease_token": token, "lease_expires_at": now + timedelta(seconds=int(lease_seconds)),
                "processing_attempts": min(row.processing_attempts + 1, 1000), "processing_started_at": now,
                "failure_code": "", "failure_detail": ""}

    row = _apply(item, S.PROCESSING, actor_kind=actor_kind, expected_from=(S.VALIDATED, S.PROCESSING),
                 check=check, changes=changes, detail={"lease_seconds": int(lease_seconds)})
    return row, token


def _require_lease(row, token, now):
    """THE lease rule, for every lease-authorized transition (via _apply's
    ``lease_token``), on the row-locked item at the transition's one ``now``:
    the item holds a lease, it is the caller's token, and it has not expired
    -- ``lease_expires_at > now``; at ``== now`` it is already expired. An
    expired lease authorizes nothing, even before another worker reclaims it."""
    if row.lease_token is None or token is None or str(row.lease_token) != str(token):
        raise EvidenceRefused("the lease is stale or belongs to another worker", code="stale_lease")
    if row.lease_expires_at is None or now.tzinfo is None or not row.lease_expires_at > now:
        raise EvidenceRefused("the lease has expired", code="lease_expired")


def _parse_evidence(evidence) -> render.RenderEvidence:
    if isinstance(evidence, render.RenderResult):
        evidence = evidence.evidence
    if not isinstance(evidence, render.RenderEvidence):
        raise EvidenceRefused("completion needs the render's RenderEvidence", code="rendition_mismatch")
    try:
        uuid.UUID(evidence.rendition_id)
    except ValueError:
        raise EvidenceRefused("the evidence names no rendition", code="rendition_mismatch") from None
    return evidence


def _verify_render(row, bound, evidence) -> None:
    """The rendition is EXACTLY the render this item's type authorizes now.

    The evidence is only a claim: every field is checked against the
    persisted, row-locked ProductionMedia, and the persisted provenance
    against what the type's CURRENT configuration and CURRENT references
    produce (render.expected -- the helper render() itself uses)."""
    if (bound.kind != ProductionMedia.KIND_RENDITION or row.submission_media_id is None
            or bound.derived_from_id != row.submission_media_id
            or evidence.source_id != str(row.submission_media_id)
            or bound.recipe_key != row.content_type.recipe_key):
        raise EvidenceRefused("the rendition does not derive from this item's submission by its recipe",
                              code="rendition_mismatch")
    if (bound.recipe_key, bound.recipe_version, bound.recipe_params_digest) != (
            evidence.recipe_key, evidence.recipe_version, evidence.provenance_digest):
        raise EvidenceRefused("the render evidence disagrees with the stored rendition", code="rendition_mismatch")
    # Every rendition row is inserted by the render executor's intake (the
    # model refuses any other insert); it records the executor's identity.
    if not bound.toolchain.endswith(render_protocol.EXECUTOR_MARKER):
        raise EvidenceRefused("the rendition was not produced by the render executor", code="rendition_mismatch")
    ctype = row.content_type                                       # locked by _apply(lock_type=True)
    try:
        expected = render.expected(ctype.recipe_key, ctype.recipe_params)
    except (render.UnknownRecipe, render.RecipeParameterError, reference_audio.ReferenceAudioError) as exc:
        raise EvidenceRefused("the type's current recipe configuration cannot be rendered; retry after fixing it",
                              code="render_outdated") from exc
    if (bound.recipe_version, bound.recipe_params_digest) != (expected.recipe_version, expected.digest):
        raise EvidenceRefused("the rendition was made with another recipe version, other parameters or other "
                              "reference audio than the type now specifies; render again", code="render_outdated")
    if SpokenItem.objects.filter(rendition_media_id=bound.pk).exclude(pk=row.pk).exists():
        raise EvidenceRefused("the rendition is already bound to another item", code="rendition_already_bound")


def complete_processing(item, *, lease_token, evidence, render_detail=None,
                        actor_kind=ActorKind.WORKER) -> SpokenItem:
    """processing -> processed, binding the rendition named by ``evidence``
    (a ``render.RenderEvidence``; a ``RenderResult`` is reduced to its
    evidence). Under the content-type lock, the item lock, the rendition's
    binding lock and the references' shared key locks it proves: the caller
    holds the current lease; the rendition is a valid ``rendition`` derived
    from THIS item's submission; its stored recipe key, version and provenance
    digest equal the evidence's AND what the type's current recipe,
    parameters and current reference audio produce; and no other item has
    bound it.

    If the configuration or a reference changed while the render ran, the
    completion is refused (``render_outdated``) and nothing is written: the
    worker must render again (or fail the attempt). An outdated render is
    never bound."""
    evidence = _parse_evidence(evidence)

    def changes(row, bound, now):
        return {"rendition_media": bound, "processed_at": now, "lease_token": None, "lease_expires_at": None}

    detail = {"rendition": evidence.rendition_id, "evidence": evidence.as_dict()}
    detail.update(render_detail or {})
    return _apply(item, S.PROCESSED, actor_kind=actor_kind, expected_from=(S.PROCESSING,), lease_token=lease_token,
                  changes=changes, media=evidence.rendition_id, detail=detail, lock_type=True,
                  verify=lambda row, bound: _verify_render(row, bound, evidence))


def fail_processing(item, *, lease_token, code, detail="", actor_kind=ActorKind.WORKER) -> SpokenItem:
    def changes(row, bound, now):
        return {"failure_code": str(code)[:64], "failure_detail": str(detail)[:500], "lease_token": None,
                "lease_expires_at": None}

    return _apply(item, S.FAILED, actor_kind=actor_kind, expected_from=(S.PROCESSING,), lease_token=lease_token,
                  changes=changes, detail={"code": str(code)[:64]})


def retry(item, *, actor) -> SpokenItem:
    def check(row, user, now):
        if not is_manager(user):
            raise NotPermitted("only a manager may retry a failed item")

    def changes(row, bound, now):
        return {"failure_code": "", "failure_detail": ""}

    return _apply(item, S.VALIDATED, actor=actor, expected_from=(S.FAILED,), check=check, changes=changes)


# -- review and recall ---------------------------------------------------------------------------

def request_review(item, *, actor_kind=ActorKind.SYSTEM) -> SpokenItem:
    def check(row, user, now):
        if not row.content_type.requires_review:
            raise InvalidTransition("this type does not require review", code="review_not_required")
    return _apply(item, S.AWAITING_REVIEW, actor_kind=actor_kind, expected_from=(S.PROCESSED,), check=check)


def reject(item, *, actor, note="") -> SpokenItem:
    def check(row, user, now):
        if not is_manager(user):
            raise NotPermitted("only a manager may review")

    def changes(row, bound, now):
        return {"reviewed_by": actor, "reviewed_at": now, "reviewed_by_username": actor.get_username()[:150],
                "review_note": str(note)[:1000]}

    return _apply(item, S.REJECTED, actor=actor, expected_from=(S.AWAITING_REVIEW,), check=check, changes=changes,
                  detail={"note": str(note)[:1000]})


def require_expected_state(row, expected_state) -> None:
    """``expected_state`` (optional): the state the caller decided on (e.g. a
    confirmation page). Checked ONLY on the authoritatively locked row; a
    mismatch refuses without any change."""
    if expected_state is not None and row.state != expected_state:
        raise InvalidTransition(f"the item is {row.state} now, not {expected_state}", code="state_changed")


def recall(item, *, actor, reason="", expected_state=None) -> SpokenItem:
    """Recall. Before delivery: managers always (and, for an explicitly linked
    type, whoever its owning domain names -- may_recall_as_manager); the
    submitter while the type's recall policy allows it. A delivered (or superseded) item's recall
    removes projected content, so it is the projection service's
    (spoken.delivery.recall_delivered).

    ``expected_state`` (optional; omitted = unchanged behavior) is verified
    under the lifecycle's own lock: here under the locked item row (_apply),
    and for delivered/superseded items inside spoken.delivery after its
    advisory -> type -> item locks. The unlocked read below only routes."""
    state = SpokenItem.objects.filter(pk=getattr(item, "pk", item)).values_list("state", flat=True).first()
    if state in (S.DELIVERED, S.SUPERSEDED):
        from . import delivery
        return delivery.recall_delivered(item, actor=actor, reason=reason, expected_state=expected_state)

    def check(row, user, now):
        require_expected_state(row, expected_state)
        if may_recall_as_manager(user, row):
            return
        if row.content_type.recall_policy == RecallPolicy.MANAGERS_ONLY:
            raise NotPermitted("only managers may recall this type")
        _owner_or_manager(row, user)

    def changes(row, bound, now):
        return {"recalled_by": actor, "recalled_at": now, "recalled_by_username": actor.get_username()[:150],
                "recall_reason": str(reason)[:500]}

    return _apply(item, S.RECALLED, actor=actor, check=check, changes=changes, detail={"reason": str(reason)[:500]})
