"""Commit-safe cleanup for ordinary library Track deletion workflows.

The database row is the authority.  Waveform and source-media bytes are
removed only after its deletion commits, so PROTECT failures and transaction
rollbacks cannot destroy a still-live Track's files.

Active Messages publications are deliberately different: that domain owns
its immutable audio and waveform through its evidence-preserving quarantine
workflow.  Its durable Track-id snapshot tells the shared waveform receiver
to leave those bytes to Active Messages even after Django nulls the live FK.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models.signals import post_delete

from library.services import waveform_cache

log = logging.getLogger(__name__)


@dataclass
class SourceCleanup:
    """Observable result of one requested source-media removal."""

    path: Path | None
    status: str = "not_requested"
    error: str = ""


def _publication_model():
    try:
        return apps.get_model("active_messages", "ActiveBroadcastPublication")
    except LookupError:
        return None


def _publication_owns_source(track_id: int, using: str) -> bool:
    """A live publication owns its Track's immutable source audio."""
    publication = _publication_model()
    return bool(
        publication is not None
        and publication._base_manager.using(using).filter(track_id=track_id).exists()
    )


def _publication_owns_waveform(track_id: int, using: str) -> bool:
    """A publication snapshot owns waveform retirement after its FK is NULL."""
    publication = _publication_model()
    return bool(
        publication is not None
        and publication._base_manager.using(using).filter(track_id_snapshot=track_id).exists()
    )


def _canonical_waveform(track_id: int) -> Path:
    return waveform_cache.canonical_path(
        Path(getattr(settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms")),
        track_id,
    )


def _remove_waveform_after_commit(track_id: int, target: Path, *, preserve_file=False) -> None:
    try:
        waveform_cache.remove(
            track_id,
            path=target,
            track_deleted=True,
            preserve_file=preserve_file,
        )
    except (OSError, waveform_cache.WaveformBusy):
        log.exception(
            "Track %s was deleted, but its waveform cache could not be removed: %s",
            track_id,
            target,
        )


def schedule_waveform_cleanup(sender, instance, using, **kwargs) -> None:
    """Track ``post_delete``: remove only its canonical cache after commit."""
    if _publication_owns_waveform(instance.pk, using):
        return

    track_id = instance.pk
    canonical = _canonical_waveform(track_id)
    recorded = getattr(instance, "waveform_path", "")
    if recorded and Path(recorded) != canonical:
        # Never follow an unexpected database pathname during deletion.  The
        # canonical cache is also retained: the inconsistency needs operator
        # review rather than a guess about which file belongs to this Track.
        log.error(
            "Track %s was deleted with unexpected waveform_path %r; refusing "
            "filesystem cleanup (expected %s)",
            track_id,
            recorded,
            canonical,
        )
        preserve_file = True
    else:
        preserve_file = False

    transaction.on_commit(
        lambda: _remove_waveform_after_commit(track_id, canonical, preserve_file=preserve_file),
        using=using,
    )


def register_signals() -> None:
    from library.models import Track

    post_delete.connect(
        schedule_waveform_cleanup,
        sender=Track,
        dispatch_uid="library_track_waveform_cleanup_after_commit",
        weak=False,
    )


def _schedule_source_cleanup(path: Path | None, result: SourceCleanup, using: str, on_error=None) -> None:
    if path is None:
        return
    result.status = "pending"

    def remove() -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            result.status = "missing"
        except OSError as exc:
            result.status = "failed"
            result.error = str(exc)
            log.error("Track row was deleted, but source media could not be removed: %s: %s", path, exc)
            if on_error is not None:
                on_error(path, exc)
        else:
            result.status = "removed"

    transaction.on_commit(remove, using=using)


def delete_track(track, *, remove_source: bool, on_source_error=None) -> SourceCleanup:
    """Delete one Track and register intended source cleanup after commit."""
    using = track._state.db or DEFAULT_DB_ALIAS
    track_id = track.pk
    source = Path(track.filepath) if remove_source and track.filepath else None
    externally_owned = _publication_owns_source(track_id, using)
    result = SourceCleanup(source)
    with transaction.atomic(using=using):
        track.delete(using=using)
        if source is not None and not externally_owned:
            _schedule_source_cleanup(source, result, using, on_source_error)
    return result


def delete_tracks(queryset, *, remove_sources: bool, on_source_error=None) -> list[SourceCleanup]:
    """Delete a queryset as one transaction, with per-row after-commit cleanup."""
    using = queryset.db
    rows = list(queryset.values_list("pk", "filepath"))
    owned = set()
    if rows:
        publication = _publication_model()
        if publication is not None:
            owned = set(
                publication._base_manager.using(using)
                .filter(track_id__in=[track_id for track_id, _filepath in rows])
                .values_list("track_id", flat=True)
            )
    results = [
        SourceCleanup(Path(filepath) if remove_sources and filepath else None)
        for _track_id, filepath in rows
    ]
    with transaction.atomic(using=using):
        queryset.delete()
        for (track_id, _filepath), result in zip(rows, results):
            if result.path is not None and track_id not in owned:
                _schedule_source_cleanup(result.path, result, using, on_source_error)
    return results
