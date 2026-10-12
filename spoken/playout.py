"""Current database authority for opening a Track on an on-air deck.

Only spoken-projection-sensitive Tracks are subject to it.  Two questions
are kept apart:

* ``is_candidate`` -- a database-free test of the reserved filename
  ``spoken-<asset hex>.flac``.  The engine's 250 ms queue/preview filtering
  uses it so ordinary Tracks never cost a query there; only candidates are
  inspected (``inspect``).  The name survives loss of ``SpokenAsset.track_id``,
  which is why the reserved namespace exists.
* ``authorized_open`` -- the actual deck-open authority, re-read from
  committed state for every deck.  It also catches a Track linked by
  ``SpokenAsset.track_id`` whose path no longer looks projected.  An ordinary
  Track keeps the engine's pre-C2B behaviour (no Ready-to-Air rule, no lock).
"""
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from django.db.models import Q

from library.models import Track

from . import library_files, projection_lock
from .models import AssetState, EditionState, ItemState, SpokenAsset, SpokenAssetEdition

log = logging.getLogger(__name__)

_RESERVED_NAME = re.compile(r"spoken-[0-9a-f]{32}\.flac")
_LEASES = threading.local()


@dataclass(frozen=True)
class DeckAuthority:
    track: Track | None
    reason: str | None

    @property
    def allowed(self) -> bool:
        return self.track is not None and self.reason is None


def is_candidate(track) -> bool:
    """Database- and filesystem-free: does this Track carry a reserved
    projected filename?  Every projected Track does (its path is derived from
    the asset), so this is conservative for queue filtering."""
    filepath = getattr(track, "filepath", None)
    return isinstance(filepath, str) and bool(_RESERVED_NAME.fullmatch(filepath.rsplit("/", 1)[-1]))


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
    """A bounded current-state check of a spoken candidate, used by queue
    filtering.  It is advisory only: actual deck construction repeats it
    while holding the projection's shared lock (``authorized_open``)."""
    track = _fresh_track(track_id)
    if track is None:
        return DeckAuthority(None, "track deleted from library (FK set NULL)")
    if not track.ready2air:
        return DeckAuthority(None, f"track id={track.pk} is not Ready-to-Air")
    asset, identity_reason = _projection_asset(track)
    if identity_reason:
        return DeckAuthority(None, identity_reason)
    if asset is None:
        return DeckAuthority(None, "reserved spoken filename has no projection authority")
    reason = _projection_reason(track, asset)
    return DeckAuthority(None, reason) if reason else DeckAuthority(track, None)


def lease_active() -> bool:
    return bool(getattr(_LEASES, "stack", None))


def lease_intact() -> bool:
    """Query-free: is the innermost deck-open lease on this thread still held
    by its own backend?  True when no lease is active (an ordinary Track)."""
    stack = getattr(_LEASES, "stack", None)
    return not stack or stack[-1].intact()


def current_lease():
    stack = getattr(_LEASES, "stack", None)
    return stack[-1] if stack else None


def _report_release(lease) -> None:
    if lease.release_problem is None:
        return
    log.error("spoken projection deck-open lock for asset %s: %s", lease.asset_id, lease.release_problem)
    from monitoring.models import emit_event
    emit_event("spoken_projection", "Spoken projection: deck-open lock release problem", level="error",
               detail={"asset": str(lease.asset_id), "backend_pid": lease.backend_pid,
                       "problem": lease.release_problem},
               dedupe_key=f"spoken_projection|deck_lease_release|{lease.asset_id}")


@contextmanager
def authorized_open(track):
    """Authorize one actual deck construction from current committed state.

    ``track`` is the occurrence's (possibly cached) Track.  An ordinary Track
    is yielded unchanged: no lock, no Ready-to-Air rule.  A projection-
    sensitive one is authorized only under a non-blocking shared lock taken on
    this thread's connection and held until the caller has synchronized the
    deck (opened the file).  Delivery/reconcile take the exclusive form, so
    either the old inode was already opened under old authority or the new
    deck observes the post-promotion authority.  The caller must not recycle
    the connection while the lease is active (``lease_intact`` checks).
    """
    asset, identity_reason = _projection_asset(track)
    if identity_reason:
        yield DeckAuthority(None, identity_reason)
        return
    if asset is None:
        if is_candidate(track):
            yield DeckAuthority(None, "reserved spoken filename has no projection authority")
        else:
            yield DeckAuthority(track, None)
        return
    held = None
    try:
        with projection_lock.deck_lease(asset.pk) as lease:
            if lease is None:
                yield DeckAuthority(None, "spoken projection is changing")
                return
            held = lease
            stack = getattr(_LEASES, "stack", None)
            if stack is None:
                stack = _LEASES.stack = []
            stack.append(lease)
            try:
                yield _locked_authority(track.pk)
            finally:
                stack.remove(lease)
    finally:
        if held is not None:
            _report_release(held)


def _locked_authority(track_id) -> DeckAuthority:
    fresh = _fresh_track(track_id)
    if fresh is None:
        return DeckAuthority(None, "spoken projection identity disappeared")
    asset, identity_reason = _projection_asset(fresh)
    if identity_reason:
        return DeckAuthority(None, identity_reason)
    if asset is None:
        return DeckAuthority(None, "spoken projection identity disappeared")
    if not fresh.ready2air:
        return DeckAuthority(None, f"track id={track_id} is not Ready-to-Air")
    reason = _projection_reason(fresh, asset)
    return DeckAuthority(None, reason) if reason else DeckAuthority(fresh, None)
