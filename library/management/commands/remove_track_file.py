from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from library.models import Track


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

        track = Track.objects.filter(filepath=str(filepath)).first()
        track_id = track.id if track is not None else None
        if track is not None:
            from library.services.track_deletion import delete_track

            cleanup = delete_track(track, remove_source=True)
            if cleanup.status == "failed":
                raise CommandError(
                    f"Track {track_id} was deleted, but source file {filepath} could not be removed: "
                    f"{cleanup.error}"
                )
        else:
            try:
                filepath.unlink(missing_ok=True)
            except OSError as e:
                raise CommandError(f"Could not remove {filepath}: {e}")

        self.stdout.write(
            f"Removed {filepath}"
            + (f" and Track {track_id}" if track_id is not None else " (no Track row existed)")
        )
