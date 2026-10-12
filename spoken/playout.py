"""Current database authority for opening a Track on an on-air deck."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from django.db.models import Q

from library.models import Track

from . import library_files, projection_lock
from .models import AssetState, EditionState, ItemState, SpokenAsset, SpokenAssetEdition


@dataclass(frozen=True)
class DeckAuthority:
    track: Track | None
    reason: str | None

    @property
    def allowed(self) -> bool:
        return self.track is not None and self.reason is None


def _fresh_track(track_id):
    return Track._base_manager.select_related("artist", "category").filter(pk=track_id).first()


def _projection_asset(track) -> tuple[SpokenAsset | None, str | None]:
    """Resolve projection authority by both durable link and reserved path.

    Either relationship makes the Track projection-sensitive.  They must
    identify one asset; low-level corruption that makes them disagree is an
    authority failure, never a reason to treat the Track as ordinary.
    """
    relpath = library_files.relative(track.filepath)
    predicate = Q(track_id=track.pk)
    if relpath is not None:
        predicate |= Q(library_relpath=relpath)
    assets = list(SpokenAsset._base_manager.select_related("current_item").filter(predicate)[:2])
    if len(assets) > 1:
        return None, "spoken projection Track/path authorities disagree"
    return (assets[0], None) if assets else (None, None)


def _projection_reason(track, asset) -> str | None:
    if asset.track_id != track.pk:
        return "reserved spoken projection path is not owned by this Track"
    if asset.state != AssetState.LIVE:
        return f"spoken projection is {asset.state}, not live"
    if asset.current_item_id is None or asset.current_rendition_id is None:
        return "spoken projection has no current item/rendition authority"
    if not asset.current_item or asset.current_item.state != ItemState.DELIVERED:
        return "spoken projection current item is not delivered"
    active = (SpokenAssetEdition._base_manager.select_related("rendition")
              .filter(asset=asset, state=EditionState.ACTIVE).first())
    if (active is None or active.item_id != asset.current_item_id
            or active.rendition_id != asset.current_rendition_id
            or active.generation != asset.generation
            or active.library_sha256 != active.rendition.sha256):
        return "spoken projection edition authority is inconsistent"
    if track.filepath != str(library_files.absolute(asset.library_relpath)):
        return "spoken projection Track path is not authoritative"
    return None


def inspect(track_id) -> DeckAuthority:
    """A bounded current-state check used by queue filtering.

    Actual deck construction repeats it while holding the projection's shared
    lock (``authorized_open``); this advisory check alone is not the open
    authorization.
    """
    track = _fresh_track(track_id)
    if track is None:
        return DeckAuthority(None, "track deleted from library (FK set NULL)")
    if not track.ready2air:
        return DeckAuthority(None, f"track id={track.pk} is not Ready-to-Air")
    asset, identity_reason = _projection_asset(track)
    if identity_reason:
        return DeckAuthority(None, identity_reason)
    if asset is not None:
        reason = _projection_reason(track, asset)
        if reason:
            return DeckAuthority(None, reason)
    return DeckAuthority(track, None)


@contextmanager
def authorized_open(track_id):
    """Authorize one actual deck construction from current committed state.

    A shared advisory lock is held until the caller has synchronized the new
    deck with the live pipeline.  Delivery/reconcile take the exclusive form,
    so either the old inode was already opened under old authority or the new
    deck observes the post-promotion authority; it cannot race the barrier and
    stable-path rename.
    """
    initial = _fresh_track(track_id)
    if initial is None:
        yield DeckAuthority(None, "track deleted from library (FK set NULL)")
        return
    asset, identity_reason = _projection_asset(initial)
    if identity_reason:
        yield DeckAuthority(None, identity_reason)
        return
    if asset is None:
        fresh = _fresh_track(track_id)
        if fresh is None:
            yield DeckAuthority(None, "track deleted from library (FK set NULL)")
        elif not fresh.ready2air:
            yield DeckAuthority(None, f"track id={track_id} is not Ready-to-Air")
        else:
            yield DeckAuthority(fresh, None)
        return
    with projection_lock.session(asset.pk, shared=True, wait=False) as acquired:
        if not acquired:
            yield DeckAuthority(None, "spoken projection is changing")
            return
        fresh = _fresh_track(track_id)
        asset, identity_reason = _projection_asset(fresh) if fresh is not None else (None, None)
        if fresh is None or asset is None:
            yield DeckAuthority(None, "spoken projection identity disappeared")
            return
        if identity_reason:
            yield DeckAuthority(None, identity_reason)
            return
        if not fresh.ready2air:
            yield DeckAuthority(None, f"track id={track_id} is not Ready-to-Air")
            return
        reason = _projection_reason(fresh, asset)
        yield DeckAuthority(None, reason) if reason else DeckAuthority(fresh, None)
