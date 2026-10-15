"""THE spoken projection service (Phase C2B): the only ordinary authority that
creates or changes SpokenAsset / SpokenAssetEdition, the projection-owned
columns of a projected library Track, the projected library file, and the C2
item transitions (delivered, superseded, delivered/superseded -> recalled /
expired).

Authoritative chain (never inferred from a mutable Track):

    SpokenContentType + SpokenItem + its validated rendition (C2A)
    + SpokenAsset / active SpokenAssetEdition            -> projected Track + file

Every write is the exact execution of a single-use grant (spoken.guard):
assets, editions and Tracks are written by the guard's executor from an
immutable payload and read back complete; an item's C2 transition is
executed only with a projection WITNESS the guard re-checks against the
database (an item is delivered only by its active edition on its live asset;
recalled/expired only once nothing of it is projected).

GLOBAL LOCK ORDER (every operation; a subset in this order, never another):

    SpokenContentType -> SpokenItem(s) -> SpokenAsset -> SpokenAssetEdition(s)
    -> ProductionMedia (lock_for_binding) -> ReferenceAudio keys (sorted,
    reference_audio.lock_keys) -> library Track

Every operation that touches an asset locks its content type first, so two
operations on the same asset never interleave; a Track deletion (detach) is
the one exception -- it locks the asset alone, which is still in order.

DELIVERY AND THE DB/FILESYSTEM COMMIT MODEL. PostgreSQL and a rename are not
one atomic transaction, and this module never claims they are. Instead:

  TX-A  lock type -> item -> asset (created pending on first delivery) ->
        verify lifecycle, rendition, exact provenance -> next generation ->
        create a PENDING edition (durable intent).
  TX-B  relock and re-validate; set the Track not Ready-to-Air and asset
        RETIRING, then COMMIT this durable unavailable barrier.
  TX-C  publish the rendition's bytes to the stable path (stage, verify SHA,
        atomic rename) while holding the locks; then, in the same transaction:
        Track create/update, previous active edition -> superseded, new
        edition -> active, asset -> live at the new generation (compare-and-
        write on state+generation), previous current item -> superseded,
        item -> delivered (+ events).
        COMMIT = THE COMMIT POINT: the new edition is authoritative and the
        barrier is removed.
  then  remove a file left behind by a relocation; prune staging.

An exclusive session advisory lock spans TX-B's commit through TX-C and is
shared by deck-open authority. Thus no new deck opens the stable path while
bytes and committed authority differ. A crash releases the lock but leaves
RETIRING durable; ``reconcile`` takes the lock, restores the previous active
edition's exact bytes (or removes the first delivery's uncommitted file), and
only then commits LIVE/readiness again. A failure runs that convergence at
once. A pending edition never makes anything airable.

RECALL / EXPIRY never resurrect anything. An item that is not the current,
live content of its asset (superseded, or projected nowhere) leaves delivery
with NO projection effect -- a stale recall/expiry can never touch a newer
edition. Removing the current content: the active edition -> superseded
(superseded_by empty), the Track -> not Ready-to-Air (committed BEFORE the
file is removed), the file removed after commit; a current-edition asset
becomes ``pending`` (stable asset AND Track kept; nothing airs until a new,
deliberate delivery), an accumulating asset ``retired``. ``repromoted`` is
never performed (and ``on_current_recall=restore_previous`` is refused by
configuration validation): no superseded edition comes back automatically.

DETACH: deleting a projected Track (any path -- the pre_delete receiver
below runs inside the deletion's transaction) marks its asset ``detached``
(the active edition superseded); reconcile never recreates it, and a new
delivery to a detached current-edition slot is refused until a manager
reattaches the asset (``reattach_asset``).
"""
from __future__ import annotations

import logging
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.models import ProductionMedia
from production.services import reference_audio, render, retention
from production.services.render import protocol as render_protocol

from . import guard, library_files, projection, projection_lock, transitions
from .models import (
    ActorKind, AssetMode, AssetState, DeliveryMode, EditionState, ItemState, RecallPolicy, SpokenAsset,
    SpokenAssetEdition, SpokenContentType, SpokenItem,
)

log = logging.getLogger(__name__)
S = ItemState
STALE_PENDING_SECONDS = 300            # a pending edition older than this belongs to a dead attempt
STALE_STAGING_SECONDS = 3600


class ProjectionError(transitions.SpokenServiceError):
    code = "projection_failed"

    def __init__(self, message, *, code=None, retryable=False):
        super().__init__(message, code=code)
        self.retryable = retryable


class SimulatedCrash(BaseException):
    """Tests only: a process death at a fault point (no compensation runs)."""


def _fault(point: str) -> None:
    """Failure-injection hook (tests replace it to fail or crash at ``point``)."""


@dataclass(frozen=True)
class DeliveryResult:
    item_id: str
    state: str
    asset_id: str | None = None
    edition_id: str | None = None
    generation: int | None = None
    track_id: int | None = None
    already: bool = False


# -- grants: exact writes of assets, editions, Tracks ------------------------------------------

def _frozen(model, values: dict) -> tuple:
    return transitions._attname_values(model, values)


def _row_snapshot(row, fields) -> dict:
    return {field.attname: guard.freeze(field, row.__dict__[field.attname]) for field in fields}


def _create_asset(ctype, mode, item, category_code) -> SpokenAsset:
    asset_id = uuid.uuid4()
    values = {"content_type": ctype.pk, "mode": mode, "slot_key": "",
              "item": item.pk if mode == AssetMode.ACCUMULATING else None, "track": None,
              "library_relpath": projection.library_relpath(category_code, asset_id), "current_item": None,
              "current_rendition": None, "state": AssetState.PENDING, "generation": 0, "retired_at": None,
              "retired_reason": ""}
    with guard._authorized(guard.AssetCreate(asset_id=asset_id, values=_frozen(SpokenAsset, values))):
        guard._execute()
    return SpokenAsset.objects.select_for_update().get(pk=asset_id)


def _write_asset(asset: SpokenAsset, changes: dict, now) -> SpokenAsset:
    """Compare-and-write: the asset must still hold the state and generation
    it was locked with."""
    locked = _row_snapshot(asset, guard.authoritative_asset_fields())
    values = dict(_frozen(SpokenAsset, {**changes, "updated_at": now}))
    match = (("generation", locked["generation"]), ("state", locked["state"]))
    expected = tuple(sorted({**locked, **values}.items()))
    with guard._authorized(guard.AssetWrite(asset_id=asset.pk, match=match, values=tuple(sorted(values.items())),
                                            expected=expected)):
        guard._execute()
    return SpokenAsset.objects.select_for_update().get(pk=asset.pk)


def _create_edition(asset, item, rendition, generation) -> SpokenAssetEdition:
    edition_id = uuid.uuid4()
    values = {"asset": asset.pk, "item": item.pk, "rendition": rendition.pk, "generation": generation,
              "state": EditionState.PENDING, "library_sha256": rendition.sha256, "promoted_at": None,
              "superseded_at": None, "superseded_by": None}
    with guard._authorized(guard.EditionCreate(edition_id=edition_id, values=_frozen(SpokenAssetEdition, values))):
        guard._execute()
    return SpokenAssetEdition.objects.select_for_update().get(pk=edition_id)


def _write_edition(edition: SpokenAssetEdition, changes: dict) -> None:
    locked = _row_snapshot(edition, guard.authoritative_edition_fields())
    values = dict(_frozen(SpokenAssetEdition, changes))
    with guard._authorized(guard.EditionWrite(edition_id=edition.pk, from_state=locked["state"],
                                              values=tuple(sorted(values.items())),
                                              expected=tuple(sorted({**locked, **values}.items())))):
        guard._execute()


def _track_values(ctype, item, asset_id, rendition, relpath, *, ready: bool) -> dict:
    """Every projection-owned Track column, from authoritative state only."""
    from library.models import Artist
    artist, _ = Artist.get_or_create_ci((ctype.artist_label or ctype.name).strip()[:200] or ctype.code)
    duration = float(rendition.decoded_duration_seconds)
    title = projection.render_title(ctype.title_policy, item_title=item.title, submitter=item.submitted_by_username,
                                    when=item.submitted_at or item.created_at)
    return {"filepath": str(library_files.absolute(relpath)), "filename": relpath.split("/")[1], "format": "flac",
            "title": title, "artist_id": artist.pk, "category_id": ctype.target_category_id, "ready2air": bool(ready),
            "duration_seconds": duration, "sample_rate": rendition.sample_rate, "channels": rendition.channels,
            "bit_depth": None, "cue_in_seconds": 0.0, "cue_out_seconds": None,
            # the whole file airs; a non-null next_start also keeps the
            # library's music analyzer (cue/segue detection) off this Track
            "next_start_seconds": duration, "remote_url": ""}


def _create_track(values: dict) -> int:
    from library.models import Track
    with guard._authorized(guard.TrackCreate(values=_frozen(Track, values))):
        return guard._execute()


def _owned_now(track_id) -> dict | None:
    from library.models import Track
    names = list(guard.PROJECTED_TRACK_FIELDS)
    row = Track._base_manager.filter(pk=track_id).values(*names).first()
    if row is None:
        return None
    fields = {field.attname: field for field in guard.projected_track_fields()}
    return {name: guard.freeze(fields[name], row[name]) for name in names}


def _write_track(track_id, values: dict, now) -> bool:
    """Make the Track's projection-owned columns exactly ``values`` (no-op
    when they already are). False if the Track row no longer exists."""
    from library.models import Track
    current = _owned_now(track_id)
    if current is None:
        return False
    target = dict(_frozen(Track, values))
    changed = {name: value for name, value in target.items() if not guard._same(
        Track._meta.get_field(name), value, current[name])}
    if not changed:
        return True
    payload = tuple(sorted({**changed, **dict(_frozen(Track, {"updated_at": now}))}.items()))
    with guard._authorized(guard.TrackWrite(track_id=track_id, values=payload,
                                            expected=tuple(sorted({**current, **target}.items())))):
        guard._execute()
    return True


def _item_move(row, to_state, *, now, actor=None, actor_kind=ActorKind.SYSTEM, changes=None, media=None,
               detail=None, asset_id=None, edition_id=None):
    """One witnessed C2 item transition (plan + exact grants)."""
    rule = transitions.TRANSITIONS[(row.state, to_state)]
    user, username = transitions._actor_columns(actor, actor_kind)
    locked = guard.snapshot(row)
    planned = dict(changes or {})
    planned.update(state=to_state, state_changed_at=now)
    if to_state in (S.RECALLED, S.EXPIRED):
        planned["terminal_at"] = now
    plan = transitions._plan(row, from_state=row.state, to_state=to_state, rule=rule, changes=planned,
                             actor_kind=actor_kind, user=user, username=username, media=media, detail=detail,
                             locked=locked, asset=asset_id, edition=edition_id)
    transitions._commit_projected(row, plan, asset_id=asset_id, edition_id=edition_id)
    return row


# -- locks ---------------------------------------------------------------------------------

def _lock_type(item_pk) -> SpokenContentType:
    type_id = SpokenItem.objects.filter(pk=item_pk).values_list("content_type_id", flat=True).first()
    if type_id is None:
        raise SpokenItem.DoesNotExist("no such spoken item")
    return (SpokenContentType.objects.select_for_update(of=("self",)).select_related("target_category")
            .get(pk=type_id))


def _lock_item(pk) -> SpokenItem:
    return SpokenItem.objects.select_for_update(of=("self",)).select_related("content_type").get(pk=pk)


def _projecting_asset(ctype, item, *, create_with=None):
    """The asset this item projects into (locked), or None. current_edition:
    THE non-retired asset of (type, slot ""); accumulating: the item's own."""
    query = SpokenAsset.objects.select_for_update().exclude(state=AssetState.RETIRED)
    if ctype.delivery_mode == DeliveryMode.CURRENT_EDITION:
        asset = query.filter(content_type=ctype, mode=AssetMode.CURRENT_EDITION, slot_key="").first()
        mode = AssetMode.CURRENT_EDITION
    else:
        asset = query.filter(mode=AssetMode.ACCUMULATING, item=item).first()
        mode = AssetMode.ACCUMULATING
    if asset is None and create_with is not None:
        asset = _create_asset(ctype, mode, item, create_with)
    return asset


def _active_edition(asset) -> SpokenAssetEdition | None:
    return SpokenAssetEdition.objects.select_for_update().filter(asset=asset, state=EditionState.ACTIVE).first()


# -- checks ----------------------------------------------------------------------------------

def _delivery_mode(ctype) -> str:
    if ctype.delivery_mode not in DeliveryMode.values:
        raise ProjectionError(f"unknown delivery mode {ctype.delivery_mode!r}", code="invalid_delivery_mode")
    return ctype.delivery_mode


def _category_code(ctype) -> str:
    category = ctype.target_category
    if category is None:
        raise ProjectionError("the content type has no target category", code="target_category_missing")
    try:
        projection.library_relpath(category.code, uuid.UUID(int=0))
    except projection.ProjectionPlanError as exc:
        raise ProjectionError(str(exc), code="target_category_invalid") from exc
    return category.code


def _bind_rendition(row) -> ProductionMedia:
    """The item's rendition, locked under the binding rule, valid and exactly
    the executor-made render this item's type authorizes NOW (C1/C2A
    provenance; the references' shared locks in the global sorted order)."""
    if row.rendition_media_id is None:
        raise ProjectionError("the item has no rendition", code="rendition_missing")
    try:
        rendition = retention.lock_for_binding(row.rendition_media_id, require_valid=True)
    except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
        raise ProjectionError("the rendition is missing, purged or not valid", code="rendition_unusable") from exc
    if (rendition.kind != ProductionMedia.KIND_RENDITION or rendition.derived_from_id != row.submission_media_id
            or not rendition.toolchain.endswith(render_protocol.EXECUTOR_MARKER)
            or rendition.decoded_duration_seconds is None):
        raise ProjectionError("the rendition is not this item's executor render", code="rendition_mismatch")
    ctype = row.content_type
    try:
        expected = render.expected(ctype.recipe_key, ctype.recipe_params)
    except (render.UnknownRecipe, render.RecipeParameterError, reference_audio.ReferenceAudioError) as exc:
        raise ProjectionError("the type's recipe configuration cannot be rendered now", code="render_outdated") from exc
    if (rendition.recipe_key, rendition.recipe_version, rendition.recipe_params_digest) != (
            expected.recipe_key, expected.recipe_version, expected.digest):
        raise ProjectionError("the rendition is not what the type's current recipe and references produce",
                              code="render_outdated")
    return rendition


def _deliverable(row, *, approve: bool, actor, actor_kind) -> None:
    ctype = row.content_type
    if approve:
        if row.state != S.AWAITING_REVIEW:
            raise transitions.InvalidTransition(f"{row.state} items are not awaiting review", code="not_deliverable")
        user, _ = transitions._actor_columns(actor, ActorKind.USER)
        if not transitions.is_manager(user):
            raise transitions.NotPermitted("only a manager may approve")
        return
    if row.state != S.PROCESSED:
        raise transitions.InvalidTransition(f"{row.state} items are not deliverable", code="not_deliverable")
    if ctype.requires_review:
        raise transitions.InvalidTransition("this type requires review before delivery", code="review_required")


def _delivery_changes(row, ctype, now, *, approve, actor, note) -> dict:
    changes = {"delivered_at": now,
               "expires_at": now + ctype.default_lifetime if ctype.default_lifetime else None}
    if approve:
        changes.update(reviewed_by=actor, reviewed_by_username=actor.get_username()[:150], reviewed_at=now,
                       review_note=str(note)[:1000])
    return changes


def _result(row, asset=None, edition=None, *, already=False) -> DeliveryResult:
    return DeliveryResult(item_id=str(row.pk), state=row.state, asset_id=str(asset.pk) if asset else None,
                          edition_id=str(edition.pk) if edition else None,
                          generation=edition.generation if edition else None,
                          track_id=asset.track_id if asset else None, already=already)


# -- delivery ----------------------------------------------------------------------------------

def deliver_item(item, *, actor=None, actor_kind=ActorKind.SYSTEM) -> DeliveryResult:
    """processed -> delivered (a type that does not require review)."""
    return _deliver(getattr(item, "pk", item), approve=False, actor=actor, actor_kind=actor_kind, note="")


def approve_and_deliver(item, *, actor, note="") -> DeliveryResult:
    """awaiting_review -> delivered: a manager's approval IS the delivery."""
    return _deliver(getattr(item, "pk", item), approve=True, actor=actor, actor_kind=ActorKind.USER, note=note)


def _deliver(item_pk, *, approve, actor, actor_kind, note) -> DeliveryResult:
    # TX-A: durable intent -------------------------------------------------------------------
    with transaction.atomic():
        ctype = _lock_type(item_pk)
        row = _lock_item(item_pk)
        if row.state == S.DELIVERED:                         # an idempotent replay
            return _result(row, *_current_projection(row), already=True)
        _deliverable(row, approve=approve, actor=actor, actor_kind=actor_kind)
        mode = _delivery_mode(ctype)
        if mode == DeliveryMode.RETAIN_ONLY:
            _bind_rendition(row)
            now = timezone.now()
            _item_move(row, S.DELIVERED, now=now, actor=actor, actor_kind=actor_kind, media=row.rendition_media_id,
                       changes=_delivery_changes(row, ctype, now, approve=approve, actor=actor, note=note),
                       detail={"projection": "retain_only"})
            return _result(row)
        code = _category_code(ctype)
        asset = _projecting_asset(ctype, row, create_with=code)
        if asset.state == AssetState.DETACHED:
            raise ProjectionError("the asset was detached by an operator; a manager must reattach it first",
                                  code="asset_detached")
        if asset.state == AssetState.RETIRING:
            raise ProjectionError("the projection has an unresolved unavailable barrier; reconcile it first",
                                  code="projection_in_transition", retryable=True)
        rendition = _bind_rendition(row)
        _abort_stale_pending(asset, row, timezone.now())
        generation = (SpokenAssetEdition.objects.filter(asset=asset).aggregate(top=Max("generation"))["top"] or 0) + 1
        _fault("before_pending")
        edition = _create_edition(asset, row, rendition, generation)
        planned = (str(asset.pk), str(edition.pk), row.state, rendition.pk)
    _fault("pending_created")
    # TX-B: commit the durable unavailable barrier.  The session lock spans
    # this commit, publication and TX-C, so reconcile/deck-open can distinguish
    # a live operation from a crash-abandoned barrier.
    published = []
    with projection_lock.session(asset.pk, wait=True):
        try:
            _enter_unavailable(item_pk, planned, approve=approve, actor=actor, actor_kind=actor_kind)
            _fault("barrier_committed")
            # TX-C: publish while the asset/type/edition rows are locked, then
            # commit new projection authority and readiness together.
            with transaction.atomic():
                result = _promote(item_pk, planned, approve=approve, actor=actor, actor_kind=actor_kind, note=note,
                                  published=published)
            _fault("committed")
        except SimulatedCrash:
            raise
        except Exception as exc:
            _compensate(planned, published, exc)
            raise
    _cleanup_after_promotion(result)
    return result.delivery


def _current_projection(row):
    asset = SpokenAsset.objects.filter(current_item=row).exclude(state=AssetState.RETIRED).first()
    edition = SpokenAssetEdition.objects.filter(item=row, state=EditionState.ACTIVE).first() if asset else None
    return asset, edition


def _abort_stale_pending(asset, row, now) -> None:
    """A pending edition of THIS item older than STALE_PENDING_SECONDS belongs
    to a dead attempt (aborted); a younger one is an attempt in flight."""
    for pending in SpokenAssetEdition.objects.select_for_update().filter(asset=asset, item=row,
                                                                          state=EditionState.PENDING):
        if now - pending.created_at < timedelta(seconds=STALE_PENDING_SECONDS):
            raise ProjectionError("a delivery of this item is already in progress", code="delivery_in_progress",
                                  retryable=True)
        _write_edition(pending, {"state": EditionState.ABORTED})


@dataclass(frozen=True)
class _Promotion:
    delivery: DeliveryResult
    remove_relpath: str | None


def _validate_promotion(item_pk, planned, *, approve, actor, actor_kind, required_asset_states):
    """Lock and re-prove TX-A's intent for either side of the barrier."""
    asset_id, edition_id, from_state, rendition_id = planned
    ctype = _lock_type(item_pk)
    row = _lock_item(item_pk)
    asset = SpokenAsset.objects.select_for_update().get(pk=asset_id)
    editions = {e.pk: e for e in SpokenAssetEdition.objects.select_for_update().filter(asset=asset)
                .order_by("generation")}
    edition = editions.get(uuid.UUID(edition_id))
    if row.state != from_state or row.rendition_media_id != rendition_id:
        raise ProjectionError("the item changed during delivery", code="stale_promotion")
    if edition is None or edition.state != EditionState.PENDING:
        raise ProjectionError("the pending edition was aborted", code="stale_promotion")
    if asset.state not in required_asset_states:
        code = "asset_detached" if asset.state in (AssetState.DETACHED, AssetState.RETIRED) else "stale_promotion"
        raise ProjectionError(f"the asset is {asset.state}", code=code)
    if edition.generation <= asset.generation:
        raise ProjectionError("a newer edition was promoted first", code="stale_promotion")
    _deliverable(row, approve=approve, actor=actor, actor_kind=actor_kind)
    rendition = _bind_rendition(row)
    return ctype, row, asset, editions, edition, rendition


def _enter_unavailable(item_pk, planned, *, approve, actor, actor_kind) -> None:
    """TX-B: make new deck creation impossible before pathname mutation."""
    with transaction.atomic():
        ctype, row, asset, _editions, _edition, _rendition = _validate_promotion(
            item_pk, planned, approve=approve, actor=actor, actor_kind=actor_kind,
            required_asset_states=(AssetState.PENDING, AssetState.LIVE),
        )
        now = timezone.now()
        if asset.track_id is not None:
            _set_ready(asset.track_id, False, now)
        _write_asset(asset, {"state": AssetState.RETIRING}, now)


def _promote(item_pk, planned, *, approve, actor, actor_kind, note, published) -> _Promotion:
    ctype, row, asset, editions, edition, rendition = _validate_promotion(
        item_pk, planned, approve=approve, actor=actor, actor_kind=actor_kind,
        required_asset_states=(AssetState.RETIRING,),
    )
    code = _category_code(ctype)
    target = projection.library_relpath(code, asset.pk)
    before = asset.library_relpath                           # differs only when the category changed
    now = timezone.now()
    _fault("before_stage")
    library_files.publish(target, rendition)
    published.append(target)
    _fault("after_publish")                                  # = before the Track write
    # Authoritative finalization -- ONE transaction.  The Track remains
    # unavailable to other sessions until this transaction commits LIVE.
    values = _track_values(ctype, row, asset.pk, rendition, target, ready=ctype.deliver_ready_to_air)
    track_id = asset.track_id
    if track_id is None or not _write_track(track_id, values, now):
        track_id = _create_track(values)
    _fault("track_written")                                  # = before edition activation
    previous = next((e for e in editions.values() if e.state == EditionState.ACTIVE), None)
    if previous is not None:
        _write_edition(previous, {"state": EditionState.SUPERSEDED, "superseded_at": now, "superseded_by": edition.pk})
    _write_edition(edition, {"state": EditionState.ACTIVE, "promoted_at": now})
    _fault("edition_activated")
    previous_item_id = asset.current_item_id
    asset = _write_asset(asset, {"state": AssetState.LIVE, "generation": edition.generation, "current_item": row.pk,
                                 "current_rendition": rendition.pk, "track": track_id, "library_relpath": target},
                         now)
    if previous is not None and previous_item_id is not None and previous_item_id != row.pk:
        old = _lock_item(previous_item_id)
        if old.state == S.DELIVERED:
            _item_move(old, S.SUPERSEDED, now=now, actor_kind=ActorKind.SYSTEM, asset_id=asset.pk,
                       edition_id=previous.pk, detail={"superseded_by_item": str(row.pk),
                                                       "generation": edition.generation})
    _fault("before_delivered")
    _item_move(row, S.DELIVERED, now=now, actor=actor, actor_kind=actor_kind, media=rendition.pk,
               changes=_delivery_changes(row, ctype, now, approve=approve, actor=actor, note=note),
               asset_id=asset.pk, edition_id=edition.pk,
               detail={"generation": edition.generation, "library_relpath": target, "track": track_id,
                       "library_sha256": edition.library_sha256})
    _fault("before_commit")
    edition.refresh_from_db()
    return _Promotion(delivery=_result(row, asset, edition), remove_relpath=before if before != target else None)


def _cleanup_after_promotion(result: _Promotion) -> None:
    if result.remove_relpath:
        try:
            library_files.remove(result.remove_relpath)
        except Exception:                                  # noqa: BLE001 -- reconcile finishes it
            log.exception("spoken projection: could not remove the relocated file %s", result.remove_relpath)


def _compensate(planned, published, exc) -> None:
    """A delivery FAILED (not crashed) after TX-A: abort its pending edition
    and converge the asset's files to the database right away. If even this
    fails, reconcile finishes it (and the operator is told)."""
    asset_id, edition_id, _from_state, _rendition = planned
    try:
        with transaction.atomic():
            asset = SpokenAsset.objects.get(pk=asset_id)
            ctype = (SpokenContentType.objects.select_for_update(of=("self",)).select_related("target_category")
                     .get(pk=asset.content_type_id))
            asset = SpokenAsset.objects.select_for_update().get(pk=asset_id)
            edition = SpokenAssetEdition.objects.select_for_update().filter(pk=edition_id).first()
            if edition is not None and edition.state == EditionState.PENDING:
                _write_edition(edition, {"state": EditionState.ABORTED})
            if asset.state == AssetState.RETIRING:
                _recover_barrier(asset, ctype, timezone.now(), report=[], apply=True)
                for relpath in published:
                    if relpath != asset.library_relpath and library_files.exists(relpath):
                        library_files.remove(relpath)
            else:
                _converge_files(asset, extra_relpaths=published, report=[])
    except Exception:                                      # noqa: BLE001
        log.exception("spoken projection: compensation after a failed delivery did not complete")
        _attention("compensation_failed", asset_id, f"{type(exc).__name__}: {exc}")


def _recover_barrier(asset, ctype, now, *, report, apply) -> bool:
    """Conservatively roll an abandoned RETIRING asset back to DB authority.

    The caller owns the exclusive projection advisory lock and row locks.
    A prior ACTIVE edition is the only authority that may be restored.  A
    first-delivery barrier has no prior edition, so its uncommitted file is
    removed and the asset returns to PENDING.  The barrier is removed only in
    the same commit that restores final Track readiness.
    """
    if asset.state != AssetState.RETIRING:
        return True
    pending = list(SpokenAssetEdition.objects.select_for_update().filter(
        asset=asset, state=EditionState.PENDING))
    active = SpokenAssetEdition.objects.select_for_update().select_related("rendition").filter(
        asset=asset, state=EditionState.ACTIVE).first()
    if active is None:
        if apply:
            if library_files.exists(asset.library_relpath):
                library_files.remove(asset.library_relpath)
            for edition in pending:
                _write_edition(edition, {"state": EditionState.ABORTED})
            if asset.track_id is not None:
                _set_ready(asset.track_id, False, now)
            _write_asset(asset, {"state": AssetState.PENDING, "current_item": None,
                                 "current_rendition": None}, now)
        report.append({"asset": str(asset.pk), "finding": "abandoned_projection_barrier",
                       "action": "rolled_back_to_pending" if apply else "would_roll_back_to_pending"})
        return True
    coherent = (asset.current_item_id == active.item_id
                and asset.current_rendition_id == active.rendition_id
                and asset.generation == active.generation)
    if not coherent:
        report.append({"asset": str(asset.pk), "finding": "barrier_authority_inconsistent",
                       "action": "none", "attention": True,
                       "detail": "active edition does not match the last committed asset authority"})
        return False
    sha, _size = library_files.current(asset.library_relpath)
    if sha != active.rendition.sha256:
        finding = "file_missing" if sha is None else "file_wrong_bytes"
        if not apply:
            report.append({"asset": str(asset.pk), "finding": finding, "action": "would_restore"})
        else:
            try:
                library_files.publish(asset.library_relpath, active.rendition)
                report.append({"asset": str(asset.pk), "finding": finding, "action": "restored"})
            except library_files.LibraryFileError as restore_error:
                if asset.track_id is not None:
                    _set_ready(asset.track_id, False, now)
                report.append({"asset": str(asset.pk), "finding": finding, "action": "restore_failed",
                               "detail": str(restore_error), "attention": True})
                return False
    if asset.track_id is None or _owned_now(asset.track_id) is None:
        report.append({"asset": str(asset.pk), "finding": "barrier_track_identity_missing", "action": "none",
                       "attention": True, "detail": "cannot remove barrier without the authoritative Track"})
        return False
    if apply:
        for edition in pending:
            _write_edition(edition, {"state": EditionState.ABORTED})
        _set_ready(asset.track_id, bool(ctype.deliver_ready_to_air), now)
        _write_asset(asset, {"state": AssetState.LIVE}, now)
    report.append({"asset": str(asset.pk), "finding": "abandoned_projection_barrier",
                   "action": "restored_previous" if apply else "would_restore_previous"})
    return True


# -- leaving delivery: recall and expiry -----------------------------------------------------------

def recall_delivered(item, *, actor, reason="") -> SpokenItem:
    """delivered/superseded -> recalled. Managers always (for an explicitly
    linked type also whoever its owning domain names --
    transitions.may_recall_as_manager); the submitter only while the type's
    recall policy is ``owner_anytime``."""
    return _end(getattr(item, "pk", item), S.RECALLED, actor=actor, actor_kind=ActorKind.USER, reason=reason)


def expire_item(item) -> SpokenItem:
    """delivered/superseded -> expired once ``expires_at`` has passed."""
    return _end(getattr(item, "pk", item), S.EXPIRED, actor=None, actor_kind=ActorKind.SYSTEM, reason="")


def expire_due(*, limit=200) -> list:
    """The bounded expiry sweep: every delivered/superseded item whose
    lifetime ended, oldest first, each in its own transaction. Deterministic
    and idempotent (an item already ended, or no longer due, is skipped)."""
    now = timezone.now()
    due = list(SpokenItem.objects.filter(state__in=(S.DELIVERED, S.SUPERSEDED), expires_at__isnull=False,
                                         expires_at__lte=now).order_by("expires_at", "pk")
               .values_list("pk", flat=True)[:limit])
    expired = []
    for pk in due:
        try:
            expired.append(expire_item(pk))
        except transitions.InvalidTransition:
            continue
    return expired


def _end(item_pk, to_state, *, actor, actor_kind, reason) -> SpokenItem:
    # Take the cross-transaction lock before the row-lock order.  If a
    # replacement already owns it, recall/expiry waits; after promotion the
    # old item is superseded and the lifecycle transition proceeds with no
    # projection effect.  This preserves operator intent without interleaving
    # stable-path work or deadlocking type-lock -> advisory-lock.
    asset_id = (SpokenAsset.objects.filter(current_item_id=item_pk).exclude(state=AssetState.RETIRED)
                .values_list("pk", flat=True).first())
    scope = projection_lock.session(asset_id, wait=True) if asset_id is not None else nullcontext(True)
    with scope:
        return _end_locked(item_pk, to_state, actor=actor, actor_kind=actor_kind, reason=reason)


def _end_locked(item_pk, to_state, *, actor, actor_kind, reason) -> SpokenItem:
    removed = None
    with transaction.atomic():
        ctype = _lock_type(item_pk)
        row = _lock_item(item_pk)
        if row.state not in (S.DELIVERED, S.SUPERSEDED):
            raise transitions.InvalidTransition(f"{row.state} items are not delivered", code="not_delivered")
        now = timezone.now()
        changes = {}
        if to_state == S.RECALLED:
            user, username = transitions._actor_columns(actor, ActorKind.USER)
            if not transitions.may_recall_as_manager(user, row):
                if ctype.recall_policy != RecallPolicy.OWNER_ANYTIME or row.submitted_by_id != user.pk:
                    raise transitions.NotPermitted("only a manager may recall delivered content of this type")
            changes = {"recalled_by": user, "recalled_at": now, "recalled_by_username": username,
                       "recall_reason": str(reason)[:500]}
        elif row.expires_at is None or row.expires_at > now:
            raise transitions.InvalidTransition("the item has not expired", code="not_due")
        # Exact agreement of asset, item, active edition and current identity --
        # or no projection authority at all (a stale recall/expiry is a no-op
        # for the projection: it can never touch a newer edition).
        asset = SpokenAsset.objects.select_for_update().filter(current_item=row).exclude(
            state=AssetState.RETIRED).first()
        if asset is not None and asset.state == AssetState.RETIRING:
            raise ProjectionError("the projection has an abandoned barrier; reconcile it first",
                                  code="projection_in_transition", retryable=True)
        edition_id, effect = None, "none"
        if asset is not None and asset.state == AssetState.LIVE and row.state == S.DELIVERED:
            active = _active_edition(asset)
            if (active is not None and active.item_id == row.pk and asset.current_item_id == row.pk
                    and asset.generation == active.generation):
                _write_edition(active, {"state": EditionState.SUPERSEDED, "superseded_at": now})
                if asset.track_id is not None:
                    _set_ready(asset.track_id, False, now)       # unavailable BEFORE the file goes
                if asset.mode == AssetMode.CURRENT_EDITION:
                    asset = _write_asset(asset, {"state": AssetState.PENDING, "current_item": None,
                                                 "current_rendition": None}, now)
                else:
                    asset = _write_asset(asset, {"state": AssetState.RETIRED, "retired_at": now,
                                                 "retired_reason": f"item {to_state}"}, now)
                removed, edition_id, effect = asset.library_relpath, active.pk, "removed"
        _item_move(row, to_state, now=now, actor=actor, actor_kind=actor_kind, changes=changes,
                   asset_id=asset.pk if asset else None, edition_id=edition_id,
                   detail={"projection": effect, **({"reason": str(reason)[:500]} if reason else {})})
    _fault("ended_committed")
    if removed:
        try:
            library_files.remove(removed)
        except Exception:                                  # noqa: BLE001 -- reconcile finishes it
            log.exception("spoken projection: could not remove %s", removed)
    return SpokenItem.objects.get(pk=item_pk)


def _set_ready(track_id, ready: bool, now) -> None:
    current = _owned_now(track_id)
    if current is not None:
        _write_track(track_id, {**current, "ready2air": ready}, now)


# -- operator Track deletion: detach ------------------------------------------------------------------

def track_deleted(sender, instance, **kwargs) -> None:
    """pre_delete of a library Track, inside the deletion's transaction: if
    it is a projected Track its asset becomes DETACHED (the active edition
    superseded, the link cleared) -- recorded, never silently recreated."""
    peek = SpokenAsset.objects.filter(track_id=instance.pk).values_list("pk", flat=True).first()
    if peek is None:
        return
    with transaction.atomic():
        # Lock before the asset row.  Promotion/reconcile use the same order,
        # so an operator delete waits across a live barrier without forming
        # advisory-lock <-> row-lock inversion.  Once acquired, intent wins
        # against the now-current generation and the Track cannot be recreated.
        projection_lock.transaction_lock(peek, wait=True)
        asset = SpokenAsset.objects.select_for_update().get(pk=peek)
        if asset.track_id != instance.pk:
            return
        if asset.state == AssetState.RETIRING:
            raise guard.SpokenIntegrityError("reconcile the projection barrier before deleting its Track")
        now = timezone.now()
        if asset.state == AssetState.RETIRED:
            _write_asset(asset, {"track": None}, now)
            return
        active = _active_edition(asset)
        if active is not None:
            _write_edition(active, {"state": EditionState.SUPERSEDED, "superseded_at": now})
        asset = _write_asset(asset, {"state": AssetState.DETACHED, "track": None}, now)
        relpath = asset.library_relpath
        transaction.on_commit(lambda: _remove_quietly(relpath))


def reattach_asset(asset, *, actor) -> SpokenAsset:
    """A manager's deliberate reversal of a detach (current-edition only):
    the asset becomes pending; the next delivery to the slot creates its new
    Track. Nothing is projected by the reattach itself."""
    user, _ = transitions._actor_columns(actor, ActorKind.USER)
    if not transitions.is_manager(user):
        raise transitions.NotPermitted("only a manager may reattach a detached asset")
    pk = getattr(asset, "pk", asset)
    with transaction.atomic():
        type_id = SpokenAsset.objects.filter(pk=pk).values_list("content_type_id", flat=True).get()
        SpokenContentType.objects.select_for_update().filter(pk=type_id).values_list("pk").first()
        asset = SpokenAsset.objects.select_for_update().get(pk=pk)
        if asset.state != AssetState.DETACHED or asset.mode != AssetMode.CURRENT_EDITION:
            raise transitions.InvalidTransition("only a detached current-edition asset is reattached",
                                                code="not_detached")
        return _write_asset(asset, {"state": AssetState.PENDING, "current_item": None, "current_rendition": None},
                            timezone.now())


def _remove_quietly(relpath) -> None:
    try:
        library_files.remove(relpath)
    except Exception:                                      # noqa: BLE001 -- reconcile finishes it
        log.exception("spoken projection: could not remove %s", relpath)


# -- reconciliation (the database is the authority) ----------------------------------------------------

def _expected_file(asset):
    """(relpath, rendition) the asset's stable path must hold, or None when
    it must hold nothing (pending / retired / detached)."""
    if asset.state != AssetState.LIVE:
        return None
    active = SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE).select_related(
        "rendition").first()
    if active is None:
        return None
    return asset.library_relpath, active.rendition


def _converge_files(asset, *, extra_relpaths=(), report, apply=True) -> bool:
    """Make the asset's files match the database. Never adopts unexplained
    bytes: a wrong file is replaced by the active edition's immutable
    rendition. True when the stable path is (now) exactly right."""
    expected = _expected_file(asset)
    ok = True
    if expected is not None:
        relpath, rendition = expected
        sha, _size = library_files.current(relpath)
        if sha != rendition.sha256:
            finding = "file_missing" if sha is None else "file_wrong_bytes"
            if apply:
                try:
                    library_files.publish(relpath, rendition)
                    report.append({"asset": str(asset.pk), "finding": finding, "action": "restored"})
                except library_files.LibraryFileError as exc:
                    ok = False
                    report.append({"asset": str(asset.pk), "finding": finding, "action": "restore_failed",
                                   "detail": str(exc), "attention": True})
            else:                                       # dry run: predict a successful restore
                report.append({"asset": str(asset.pk), "finding": finding, "action": "would_restore"})
    elif library_files.exists(asset.library_relpath):
        if apply:
            library_files.remove(asset.library_relpath)
        report.append({"asset": str(asset.pk), "finding": "file_not_authorized",
                       "action": "removed" if apply else "would_remove"})
    for relpath in extra_relpaths:
        if relpath != asset.library_relpath and library_files.exists(relpath):
            if apply:
                library_files.remove(relpath)
            report.append({"asset": str(asset.pk), "finding": "stale_copy", "relpath": relpath,
                           "action": "removed" if apply else "would_remove"})
    return ok


def reconcile(*, apply=True) -> list:
    """Compare every asset's database state with its Track and library file
    and converge them to the database (or, with ``apply=False``, report).
    Idempotent: a healthy asset causes no write at all. An asset an
    operation is working on right now (its type or row locked) is skipped."""
    report = []
    for pk in SpokenAsset.objects.order_by("created_at", "pk").values_list("pk", flat=True):
        report.extend(_reconcile_asset(pk, apply=apply))
    report.extend(_reconcile_orphans(apply=apply))
    if apply:
        for finding in report:
            if finding.get("attention"):
                _attention(finding["finding"], finding.get("asset"), finding.get("detail", ""))
    return report


def _reconcile_asset(pk, *, apply) -> list:
    with projection_lock.session(pk, wait=False) as acquired:
        if not acquired:
            return [{"asset": str(pk), "finding": "busy", "action": "skipped"}]
        return _reconcile_asset_locked(pk, apply=apply)


def _reconcile_asset_locked(pk, *, apply) -> list:
    report = []
    with transaction.atomic():
        type_id = SpokenAsset.objects.filter(pk=pk).values_list("content_type_id", flat=True).first()
        ctype = (SpokenContentType.objects.select_for_update(skip_locked=True, of=("self",))
                 .select_related("target_category").filter(pk=type_id).first())
        asset = SpokenAsset.objects.select_for_update(skip_locked=True).filter(pk=pk).first() if ctype else None
        if asset is None:
            return [{"asset": str(pk), "finding": "busy", "action": "skipped"}]
        now = timezone.now()
        if asset.state == AssetState.RETIRING:
            recovered = _recover_barrier(asset, ctype, now, report=report, apply=apply)
            if not apply or not recovered:
                return report
            asset = SpokenAsset.objects.select_for_update().get(pk=pk)
        for pending in SpokenAssetEdition.objects.select_for_update().filter(asset=asset, state=EditionState.PENDING):
            if now - pending.created_at >= timedelta(seconds=STALE_PENDING_SECONDS):
                if apply:
                    _write_edition(pending, {"state": EditionState.ABORTED})
                report.append({"asset": str(pk), "finding": "stale_pending_edition", "edition": str(pending.pk),
                               "action": "aborted" if apply else "would_abort"})
        moved_from = None
        if asset.state in (AssetState.LIVE, AssetState.PENDING) and ctype.target_category is not None:
            target = projection.library_relpath(ctype.target_category.code, asset.pk)
            if target != asset.library_relpath:
                report.append({"asset": str(pk), "finding": "category_changed", "relpath": target,
                               "action": "relocated" if apply else "would_relocate"})
                if apply:
                    moved_from = asset.library_relpath
                    asset = _relocate(asset, ctype, target, now)
        file_ok = _converge_files(asset, report=report, apply=apply)
        _reconcile_track(asset, ctype, now, file_ok=file_ok, apply=apply, report=report)
    if moved_from is not None:
        _remove_quietly(moved_from)
    return report


def _relocate(asset, ctype, target, now):
    """Publish the active edition at the new category path (copy + verify +
    atomic rename in the NEW directory), then move the database there; the
    old file is removed only after commit (by the caller)."""
    expected = _expected_file(asset)
    if expected is not None:
        library_files.publish(target, expected[1])
    asset = _write_asset(asset, {"library_relpath": target}, now)
    if asset.track_id is not None:
        current = _owned_now(asset.track_id)
        if current is not None:
            _write_track(asset.track_id, {**current, "filepath": str(library_files.absolute(target)),
                                          "filename": target.split("/")[1],
                                          "category_id": ctype.target_category_id}, now)
    return asset


def _reconcile_track(asset, ctype, now, *, file_ok, apply, report) -> None:
    pk = str(asset.pk)
    if asset.state == AssetState.LIVE:
        active = SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE).select_related(
            "rendition").first()
        item = SpokenItem.objects.filter(pk=asset.current_item_id).first()
        if active is None or item is None or ctype.target_category_id is None:
            report.append({"asset": pk, "finding": "live_without_active_edition", "attention": True,
                           "action": "none"})
            return
        values = _track_values(ctype, item, asset.pk, active.rendition, asset.library_relpath,
                               ready=ctype.deliver_ready_to_air and file_ok)
        if asset.track_id is None or _owned_now(asset.track_id) is None:
            # Path equality is never identity.  The append-only delivery
            # event binds this exact active edition/generation/SHA/path to the
            # original Track PK.  Without that witness we fail closed.
            from library.models import Track
            track_id, identity_detail = _delivered_track_identity(asset, active)
            if track_id is None:
                report.append({"asset": pk, "finding": "track_identity_unproven", "action": "none",
                               "attention": True, "detail": identity_detail})
                return
            original = Track._base_manager.filter(pk=track_id).first()
            if original is None:
                report.append({"asset": pk, "finding": "track_identity_missing", "action": "none",
                               "attention": True,
                               "detail": f"delivery witness names vanished Track {track_id}; no recreation guessed"})
                return
            occupant = Track._base_manager.filter(filepath=values["filepath"]).exclude(pk=track_id).first()
            if occupant is not None:
                report.append({"asset": pk, "finding": "reserved_path_occupied", "action": "none",
                               "attention": True,
                               "detail": f"unrelated Track {occupant.pk} occupies the projected path"})
                return
            if not file_ok:
                report.append({"asset": pk, "finding": "track_link_unavailable", "action": "none",
                               "attention": True, "detail": "projection bytes are not proven"})
                return
            report.append({"asset": pk, "finding": "track_missing",
                           "action": "relinked" if apply else "would_relink",
                           "track": track_id, "identity": "delivered_event"})
            if apply:
                _write_track(track_id, values, now)
                _write_asset(asset, {"track": track_id}, now)
            return
        current = _owned_now(asset.track_id)
        target = dict(_frozen(_track_model(), values))
        drift = sorted(name for name, value in target.items()
                       if not guard._same(_track_model()._meta.get_field(name), value, current[name]))
        if drift:
            report.append({"asset": pk, "finding": "track_drift", "fields": drift,
                           "action": "repaired" if apply else "would_repair",
                           **({"attention": True, "detail": "unavailable until its file is restored"}
                              if not file_ok else {})})
            if apply:
                _write_track(asset.track_id, values, now)
        return
    if asset.track_id is not None and asset.state in (AssetState.PENDING, AssetState.RETIRED):
        current = _owned_now(asset.track_id)
        if current is not None and current["ready2air"]:
            report.append({"asset": pk, "finding": "track_ready_without_content",
                           "action": "unavailable" if apply else "would_make_unavailable"})
            if apply:
                _set_ready(asset.track_id, False, now)


def _track_model():
    from library.models import Track
    return Track


def _delivered_track_identity(asset, active) -> tuple[int | None, str]:
    """Track PK durably recorded by the exact active-edition delivery."""
    from .models import SpokenItemEvent
    details = list(SpokenItemEvent._base_manager.filter(
        asset=asset, edition=active, item_id=active.item_id, to_state=ItemState.DELIVERED,
    ).values_list("detail", flat=True))
    proven = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        track_id = detail.get("track")
        if (isinstance(track_id, int) and not isinstance(track_id, bool) and track_id > 0
                and detail.get("generation") == active.generation
                and detail.get("library_relpath") == asset.library_relpath
                and detail.get("library_sha256") == active.library_sha256):
            proven.add(track_id)
    if len(proven) != 1:
        return None, "active-edition delivery has no unique exact Track identity witness"
    return proven.pop(), ""


def _reconcile_orphans(*, apply) -> list:
    """Staging files of dead attempts, and copies of an asset at a path it
    no longer has (an interrupted relocation). An unknown spoken file is
    reported, never deleted; an asset's own stable path is never touched
    here."""
    report = []
    assets = {asset_id.hex: (asset_id, relpath)
              for asset_id, relpath in SpokenAsset.objects.values_list("pk", "library_relpath")}
    for category in library_files.category_dirs():
        for kind, name in library_files.projected_names(category):
            relpath = f"{category}/{name}"
            if kind == "staging":
                if apply and library_files.remove_stale_staging(category, name,
                                                                older_than_seconds=STALE_STAGING_SECONDS):
                    report.append({"finding": "stale_staging", "relpath": relpath, "action": "removed"})
                continue
            known = assets.get(library_files.asset_hex(name))
            if known is None:
                report.append({"finding": "unknown_projected_file", "relpath": relpath, "action": "none",
                               "attention": True})
            elif known[1] != relpath:
                with transaction.atomic():
                    locked = SpokenAsset.objects.select_for_update(skip_locked=True).filter(pk=known[0]).first()
                    if locked is not None and locked.library_relpath != relpath:
                        if apply:
                            library_files.remove(relpath)
                        report.append({"asset": str(known[0]), "finding": "stale_copy", "relpath": relpath,
                                       "action": "removed" if apply else "would_remove"})
    return report


def _attention(code, asset_id, detail="") -> None:
    try:
        from monitoring.models import emit_event
        emit_event("spoken_projection", f"Spoken projection: {code}", level="warning",
                   detail={"asset": str(asset_id or ""), "code": code, "detail": str(detail)[:300]},
                   dedupe_key=f"spoken_projection|{code}|{asset_id}")
    except Exception:                                      # noqa: BLE001 -- attention is best effort
        log.exception("spoken projection: could not raise operator attention")


# -- after processing ---------------------------------------------------------------------------------

def advance_processed(item) -> DeliveryResult | None:
    """What follows ``processed``: review when the type requires it, else
    delivery through the projection (never a bare state change)."""
    row = SpokenItem.objects.select_related("content_type").get(pk=getattr(item, "pk", item))
    if row.state != S.PROCESSED:
        return None
    if row.content_type.requires_review:
        transitions.request_review(row)
        return None
    return deliver_item(row)
