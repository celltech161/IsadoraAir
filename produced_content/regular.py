"""Regular produced content: the producer's actions, on the Phase-C backend (D3C).

    start     lock + authorize the exact type (authority.lock_for_production)
              -> only then backend.ensure_regular_backend ->
              spoken.transitions.create_item (one DRAFT SpokenItem, the
              producer as submitter). No Track, no render work.
    submit    the recorder's commit: lock + re-authorize the type, prove the
              item is the user's own, then re-prove the backend, lock
              the exact DRAFT item, compare the optimistic revision UNDER the
              row lock, then spoken.transitions.submit + record_validation in
              ONE transaction -> VALIDATED (or INVALID). Nothing renders or
              projects in the request: the existing leased spoken render worker
              takes VALIDATED items on (render -> processed -> delivered ->
              ordinary library Track), with no review for a managed type.
    withdraw  the owner (or station control: CONTROL / a spoken manager, for any
              producer's item of a MANAGED type) abandons a draft or recalls an
              item through the existing spoken lifecycle (transitions.recall, which hands a
              delivered/superseded item to spoken.delivery.recall_delivered).
              Recall needs no current assignment -- losing it refuses only
              NEW production -- and a PROCESSING item cannot be interrupted
              (the lifecycle has no such transition).

Only items of a D3C-managed backing type are produced-content items; a
standalone Phase-C SpokenItem is not reachable from here.
"""
from __future__ import annotations

import hashlib

from django.db import transaction

from production.models import ProductionMedia
from spoken import transitions
from spoken.models import ItemState, SpokenItem, SpokenItemEvent

from . import authority, backend
from .models import DeliveryMode, ProducedContentType

S = ItemState
SUBMITTABLE_KINDS = (ProductionMedia.KIND_RECORDING, ProductionMedia.KIND_UPLOAD, ProductionMedia.KIND_EDIT)
# A web action never interrupts processing; terminal items have nothing to withdraw.
NOT_WITHDRAWABLE = (S.PROCESSING, S.RECALLED, S.EXPIRED, S.INVALID, S.REJECTED)


class RegularError(Exception):
    code = "regular_error"

    def __init__(self, message: str, *, code: str | None = None, **detail):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail


class NotFound(RegularError):
    code = "not_found"


class NotPermitted(RegularError):
    code = "not_permitted"


class Refused(RegularError):
    """The request is well-formed but cannot proceed (configuration, media)."""
    code = "refused"


class StateConflict(RegularError):
    code = "state_changed"


class RevisionConflict(StateConflict):
    code = "stale_revision"


def produced_type_of(item) -> ProducedContentType | None:
    """The REGULAR produced type whose managed backend ``item`` belongs to (or None)."""
    type_id = item.content_type_id if isinstance(item, SpokenItem) else (
        SpokenItem.objects.filter(pk=item).values_list("content_type_id", flat=True).first())
    if type_id is None:
        return None
    return ProducedContentType.objects.filter(spoken_content_type_id=type_id,
                                              delivery_mode=DeliveryMode.REGULAR).first()


def managed_item(item_id) -> tuple[SpokenItem, ProducedContentType] | None:
    item = (SpokenItem.objects.select_related("content_type", "submission_media", "rendition_media")
            .filter(pk=item_id).first())
    if item is None:
        return None
    produced = produced_type_of(item)
    return (item, produced) if produced is not None else None


def is_owner(user, item) -> bool:
    return getattr(user, "pk", None) is not None and item.submitted_by_id == user.pk


def item_revision(item) -> str:
    """Opaque optimistic-concurrency token of one item, always re-read from
    the database: its state, its submission and its latest history event."""
    pk = getattr(item, "pk", item)
    row = SpokenItem.objects.filter(pk=pk).values("state", "submission_media_id").first()
    if row is None:
        return ""
    last = SpokenItemEvent.objects.filter(item_id=pk).order_by("-id").values_list("id", flat=True).first()
    return hashlib.sha256(f"{pk}|{row['state']}|{row['submission_media_id']}|{last}".encode()).hexdigest()[:32]


def may_see(user, item) -> bool:
    """Its own producer, or station control (CONTROL / spoken manager)."""
    return is_owner(user, item) or authority.is_station_control(user)


def _authorized(user, produced) -> ProducedContentType:
    """Lock + authorize the exact type before ANY side effect."""
    try:
        return authority.lock_for_production(user, produced, mode=DeliveryMode.REGULAR)
    except authority.ProductionRefused as exc:
        raise (NotFound if exc.code == "unknown_type" else NotPermitted)(str(exc), code=exc.code) from None


def _backend(content_type):
    try:
        return backend.ensure_regular_backend(content_type)
    except backend.BackendError as exc:
        raise Refused(str(exc), code=exc.code) from exc


def start(user, content_type, *, title="") -> SpokenItem:
    """A new DRAFT of one exact regular type, submitted by ``user``.

    Order (one transaction): lock + re-read the exact type and prove it
    enabled and the user's authority over it; ONLY THEN create/synchronize
    the backend; then create the item. A refused start creates or touches
    nothing."""
    with transaction.atomic():
        produced = _authorized(user, content_type)
        backing = _backend(produced)
        try:
            return transitions.create_item(backing, actor=user, title=str(title or "").strip()[:200])
        except transitions.SpokenServiceError as exc:
            raise Refused(str(exc), code=exc.code) from exc


def submit(user, item, media, *, expected_revision) -> SpokenItem:
    """DRAFT -> SUBMITTED -> VALIDATED|INVALID, as one transaction, by the
    item's own producer with a valid take of their own. Lock order: produced
    type (authorized first) -> spoken backend -> item -> media."""
    found = managed_item(getattr(item, "pk", item))
    if found is None:
        raise NotFound("no such item")
    _row, produced = found
    with transaction.atomic():
        _authorized(user, produced)                          # before the backend is touched
        if not is_owner(user, _row):                         # submitted_by never changes
            raise NotPermitted("only the item's own producer submits it")
        _backend(produced)                                   # re-proven before new production
        row = SpokenItem.objects.select_for_update(of=("self",)).get(pk=_row.pk)
        current = item_revision(row)
        if not expected_revision or expected_revision != current:
            raise RevisionConflict("this item changed since the editor opened it", current_revision=current)
        if row.state != S.DRAFT:
            raise StateConflict(f"this item is {row.get_state_display().lower()}; it was already submitted")
        candidate = ProductionMedia.objects.filter(pk=getattr(media, "pk", media)).first()
        if candidate is None or candidate.owner_id != user.pk:
            raise Refused("only your own takes can be submitted", code="not_your_media")
        if candidate.kind not in SUBMITTABLE_KINDS:
            raise Refused("a submission is a recording, upload or edit", code="media_kind")
        if not candidate.is_valid:
            raise Refused("the take is not validated as usable audio", code="media_not_valid")
        try:
            transitions.submit(row, candidate, actor=user)
            return transitions.record_validation(row)
        except transitions.SpokenServiceError as exc:
            raise Refused(str(exc), code=exc.code) from exc


def withdraw(user, item, *, reason="", expected_state=None) -> SpokenItem:
    """The owner -- or station control, for any producer's item of a managed
    type -- abandons (DRAFT), withdraws or recalls one exact item. Spoken's own
    recall authority decides (for control: transitions.may_recall_as_manager,
    which names CONTROL only for explicitly linked types)."""
    found = managed_item(getattr(item, "pk", item))
    if found is None or not may_see(user, found[0]):
        raise NotFound("no such item")                       # never disclose another producer's item
    row = found[0]
    if expected_state is not None and row.state != expected_state:
        raise StateConflict(f"this item is {row.get_state_display().lower()} now")
    if row.state in NOT_WITHDRAWABLE:
        raise StateConflict("an item being processed cannot be interrupted; try again when it finishes"
                            if row.state == S.PROCESSING else
                            f"this item is {row.get_state_display().lower()}; there is nothing to withdraw")
    try:
        return transitions.recall(row.pk, actor=user, reason=str(reason)[:500])
    except transitions.NotPermitted as exc:
        raise NotPermitted(str(exc), code=exc.code) from exc
    except transitions.SpokenServiceError as exc:
        raise StateConflict(str(exc), code=exc.code) from exc
