from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, router, transaction

from library.models import Track
from library.services import source_paths


class _PathBusy(Exception):
    """A registration of this path held its lock past the recall's bounded wait."""


class Command(BaseCommand):
    help = (
        "Remove exactly one delivered file and its Track row, if any. "
        "Counterpart to sync_track_file for ingestion pipelines that "
        "support recall (e.g. ogremote -- a producer pulling back a "
        "submitted message): deletes the physical file, its waveform "
        "cache if present, and the Track row, so nothing dangling is "
        "left for the engine to trip over or an admin to puzzle over."
    )

    def add_arguments(self, parser):
        parser.add_argument("filepath", help="Full path to the delivered file.")

    def handle(self, *args, **options):
        filepath = Path(options["filepath"]).resolve()
        using = router.db_for_write(Track)

        # 3.11: the ownership lookup and the decision it authorizes run inside one transaction that holds
        # the path's registration lock (library.services.source_paths), the lock Track registration takes
        # before writing.  A registration in flight therefore either commits first (its Track is found here
        # and deleted through the commit-safe path) or starts after this commit (sync_track_file then finds
        # the file gone and registers nothing).  A lookup made without the lock never authorizes an unlink.
        try:
            with transaction.atomic(using=using):
                try:
                    source_paths.acquire(filepath, using, timeout_ms=source_paths.CLEANUP_LOCK_TIMEOUT_MS)
                except OperationalError as exc:
                    raise _PathBusy from exc
                track = Track.objects.using(using).filter(filepath=str(filepath)).first()
                if track is not None:
                    from library.services.track_deletion import delete_track

                    # The caller names this exact file: no freshness grace.  The source is removed after
                    # this transaction commits, under the same path lock, by delete_track's cleanup.
                    track_id = track.id
                    cleanup = delete_track(track, remove_source=True, source_grace=False)
                else:
                    track_id, cleanup = None, None
                    removed = self._recall_unregistered(filepath)
        except _PathBusy:
            raise CommandError(
                f"{filepath} is being registered by another process (its path lock was not obtained within "
                f"{source_paths.CLEANUP_LOCK_TIMEOUT_MS} ms); nothing was removed -- retry the recall"
            )

        if track_id is None:
            self.stdout.write(
                f"Removed {filepath} (no Track row existed)" if removed
                else f"{filepath} was already absent and no Track row existed; nothing to remove"
            )
            return
        if cleanup.status == "failed":
            raise CommandError(
                f"Track {track_id} was deleted, but source file {filepath} could not be removed: "
                f"{cleanup.error}"
            )
        if cleanup.status.startswith("retained"):
            raise CommandError(
                f"Track {track_id} was deleted, but source file {filepath} was kept ({cleanup.status}); "
                "it needs manual review"
            )
        if cleanup.status == "missing":
            self.stdout.write(f"Removed Track {track_id}; its source file {filepath} was already absent")
        elif cleanup.status == "pending":
            # Called inside a caller's transaction: the file goes when that transaction commits.
            self.stdout.write(f"Removed Track {track_id}; {filepath} is removed when the enclosing transaction commits")
        else:
            self.stdout.write(f"Removed {filepath} and Track {track_id}")

    @staticmethod
    def _recall_unregistered(filepath: Path) -> bool:
        """Unlink the requested file; the caller holds its path lock and found no Track.  True if removed.

        Only the entry this resolved path names is removed: a directory or other non-file is refused, and
        an already-absent file is reported, not treated as removed."""
        try:
            if not filepath.is_file():
                if filepath.exists():
                    raise CommandError(f"{filepath} is not a regular file; nothing was removed")
                return False
            filepath.unlink()
        except FileNotFoundError:
            return False
        except OSError as e:
            raise CommandError(f"Could not remove {filepath}: {e}")
        return True
