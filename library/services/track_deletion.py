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
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, OperationalError, transaction
from django.db.models.signals import post_delete, pre_delete

from library.services import source_paths, waveform_cache

log = logging.getLogger(__name__)
_WITNESS_UNAVAILABLE = object()

#: Source audio changed this recently is kept, not unlinked.  An outside program (the syndicated ingest
#: scripts) exposes a delivered file at its final path seconds before ``sync_track_file`` registers its
#: Track; nothing in this application can coordinate with that interval, so a recently changed file is
#: treated as possibly mid-delivery and left for reconciliation.  An orphaned file is recoverable media
#: clutter; an unlinked delivery is not.  Callers that created the file themselves opt out.
SOURCE_FRESHNESS_GRACE_SECONDS = 60


def _now_ns() -> int:
    return time.time_ns()


def _log_cleanup(level, *, track_id, operation, outcome, name, error=None) -> None:
    """One actionable line per skipped or failed post-commit cleanup.

    The Track row is gone by now (``db_deleted=true`` always), so the line carries what an operator needs
    to find the file: the Track id, the operation, the base name (not the full path), the outcome
    (``failed`` or an intentional ``retained_*``) and the exception category.  Names are repr-quoted."""
    log.log(
        level,
        "track_cleanup track_id=%s operation=%s db_deleted=true outcome=%s file=%r error=%s",
        track_id, operation, outcome, name, error or "-",
    )


@dataclass
class SourceCleanup:
    """Observable result of one requested source-media removal."""

    path: Path | None
    status: str = "not_requested"
    error: str = ""


def _source_witness(path: Path) -> tuple[int, int, int, int, int, int] | None | object:
    """Identity of exactly this directory entry, without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return _WITNESS_UNAVAILABLE
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        stat.S_IFMT(info.st_mode),
    )


def _validated_source_identity(path: Path) -> Path | None:
    """Return the canonical source identity, or None for an unsafe alias.

    Track ingestion records resolved absolute paths.  Refusing relative paths,
    ``..`` spellings and symlink aliases keeps the indexed ownership query an
    exact identity check instead of an unbounded prefix/whole-library scan.
    """
    if not path.is_absolute():
        return None
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    return canonical if canonical == path else None


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


def _remove_waveform_after_commit(
    track_id: int,
    target: Path,
    *,
    using: str,
    preserve_file=False,
) -> None:
    try:
        waveform_cache.remove(
            track_id,
            path=target,
            track_deleted=True,
            preserve_file=preserve_file,
            using=using,
        )
    except Exception as exc:
        _log_cleanup(
            logging.ERROR, track_id=track_id, operation="waveform_remove", outcome="failed",
            name=target.name, error=type(exc).__name__,
        )


def capture_waveform_cleanup(sender, instance, using, **kwargs) -> None:
    """Capture a deferred waveform_path while the Track row still exists."""
    recorded = instance.__dict__.get("waveform_path")
    if "waveform_path" not in instance.__dict__:
        try:
            recorded = (
                sender._base_manager.using(using)
                .filter(pk=instance.pk)
                .values_list("waveform_path", flat=True)
                .first()
            )
        except Exception as exc:
            recorded = None
            log.error(
                "Track %s waveform cleanup metadata could not be captured (%s)",
                instance.pk,
                type(exc).__name__,
            )
    instance._deletion_waveform_path = recorded


def schedule_waveform_cleanup(sender, instance, using, **kwargs) -> None:
    """Track ``post_delete``: remove only its canonical cache after commit."""
    if _publication_owns_waveform(instance.pk, using):
        return

    track_id = instance.pk
    canonical = _canonical_waveform(track_id)
    recorded = getattr(instance, "_deletion_waveform_path", instance.__dict__.get("waveform_path"))
    if recorded is None:
        _log_cleanup(logging.ERROR, track_id=track_id, operation="waveform_remove",
                     outcome="retained_unverified", name=canonical.name, error="waveform_path_not_captured")
        preserve_file = True
    elif recorded and Path(recorded) != canonical:
        # Never follow an unexpected database pathname during deletion.  The
        # canonical cache is also retained: the inconsistency needs operator
        # review rather than a guess about which file belongs to this Track.
        _log_cleanup(logging.ERROR, track_id=track_id, operation="waveform_remove",
                     outcome="retained_unexpected_path", name=canonical.name, error="waveform_path_not_canonical")
        preserve_file = True
    else:
        preserve_file = False

    transaction.on_commit(
        lambda: _remove_waveform_after_commit(
            track_id,
            canonical,
            using=using,
            preserve_file=preserve_file,
        ),
        using=using,
        robust=True,
    )


def register_signals() -> None:
    from library.models import Track

    pre_delete.connect(
        capture_waveform_cleanup,
        sender=Track,
        dispatch_uid="library_track_waveform_cleanup_capture_before_delete",
        weak=False,
    )
    post_delete.connect(
        schedule_waveform_cleanup,
        sender=Track,
        dispatch_uid="library_track_waveform_cleanup_after_commit",
        weak=False,
    )


class _PathBusy(Exception):
    """The source path's registration lock was not obtained within the cleanup's bounded wait."""


def _schedule_source_cleanup(
    path: Path | None,
    result: SourceCleanup,
    using: str,
    *,
    track_id: int,
    on_error=None,
    grace: bool = True,
) -> None:
    if path is None:
        return
    canonical = _validated_source_identity(path)
    scheduled_witness = _source_witness(path)
    result.status = "pending"

    name = path.name

    def retain(status: str, reason: str) -> None:
        result.status = status
        _log_cleanup(logging.WARNING, track_id=track_id, operation="source_unlink",
                     outcome=status, name=name, error=reason)

    def decide_and_unlink() -> None:
        """Runs holding the path's registration lock: no Track registration of this path is in flight."""
        current_witness = _source_witness(path)
        if current_witness is _WITNESS_UNAVAILABLE or scheduled_witness is _WITNESS_UNAVAILABLE:
            return retain("retained_unverified", "identity_unavailable")
        if current_witness is None:
            result.status = "missing"
            return
        if scheduled_witness is None or current_witness != scheduled_witness:
            return retain("retained_replaced", "changed_since_delete")
        if grace and _now_ns() - current_witness[4] < SOURCE_FRESHNESS_GRACE_SECONDS * 1_000_000_000:
            return retain("retained_recent", "changed_within_grace")
        from library.models import Track

        # Under the path lock a registration either committed (visible here) or has not begun (it waits
        # for this transaction, then finds the file gone or decides for itself).
        if Track._base_manager.using(using).filter(filepath=str(canonical)).exists():
            return retain("retained_live_owner", "path_owned_by_track")
        if _source_witness(path) != scheduled_witness:
            return retain("retained_replaced", "changed_before_unlink")
        path.unlink()
        result.status = "removed"

    def remove() -> None:
        try:
            if canonical is None:
                return retain("retained_invalid_path", "not_canonical_absolute")
            try:
                with transaction.atomic(using=using):
                    try:
                        source_paths.acquire(canonical, using, timeout_ms=source_paths.CLEANUP_LOCK_TIMEOUT_MS)
                    except OperationalError as exc:
                        raise _PathBusy from exc
                    decide_and_unlink()
            except _PathBusy:
                retain("retained_busy", "path_lock_timeout")
        except FileNotFoundError:
            result.status = "missing"
        except Exception as exc:
            result.status = "failed"
            result.error = type(exc).__name__
            _log_cleanup(logging.ERROR, track_id=track_id, operation="source_unlink", outcome="failed",
                         name=name, error=result.error)
            if on_error is not None:
                try:
                    on_error(path, exc)
                except Exception as reporter_exc:
                    _log_cleanup(logging.ERROR, track_id=track_id, operation="source_failure_report",
                                 outcome="failed", name=name, error=type(reporter_exc).__name__)

    transaction.on_commit(remove, using=using, robust=True)


def delete_track(track, *, remove_source: bool, on_source_error=None, source_grace: bool = True) -> SourceCleanup:
    """Delete one Track and register intended source cleanup after commit.

    ``source_grace=False`` is for a caller that itself created the source file moments ago and owns its
    path (the dedication render's abandoned audio); everyone else keeps recently changed audio."""
    using = track._state.db or DEFAULT_DB_ALIAS
    track_id = track.pk
    source = Path(track.filepath) if remove_source and track.filepath else None
    externally_owned = _publication_owns_source(track_id, using)
    result = SourceCleanup(source)
    with transaction.atomic(using=using):
        track.delete(using=using)
        if source is not None and not externally_owned:
            _schedule_source_cleanup(
                source,
                result,
                using,
                track_id=track_id,
                on_error=on_source_error,
                grace=source_grace,
            )
    return result


def delete_tracks(queryset, *, remove_sources: bool, on_source_error=None, source_grace: bool = True) -> list[SourceCleanup]:
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
                _schedule_source_cleanup(
                    result.path,
                    result,
                    using,
                    track_id=track_id,
                    on_error=on_source_error,
                    grace=source_grace,
                )
    return results
