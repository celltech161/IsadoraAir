from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import ProtectedError

from library.models import Track


class Command(BaseCommand):
    help = (
        "Find Track rows whose filepath no longer exists on disk -- e.g. "
        "after manually deleting files outside the app (WinSCP, etc.) -- "
        "and remove those rows from the database. Defaults to a dry run "
        "that only reports what it would do; pass --apply to actually "
        "delete the Track rows. Historical LogItem play history is safe "
        "either way (track_title/track_artist are a snapshot, independent "
        "of the Track row -- see LogItem.track's SET_NULL). A track still "
        "referenced by an active Playlist is left alone and reported as "
        "blocked (PlaylistItem.track is PROTECT), since that's a real "
        "problem to fix by hand, not something to silently skip."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete the orphaned Track rows. Without this, only reports what would happen.",
        )
        parser.add_argument(
            "--category",
            action="append",
            default=None,
            help="Restrict the scan to this Category code (repeatable). Default: scan the whole library.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        categories = options["category"]

        qs = Track.objects.select_related("artist", "category")
        if categories:
            qs = qs.filter(category__code__in=categories)

        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN -- pass --apply to actually delete anything.\n"))

        orphaned = [t for t in qs.iterator() if not t.filepath or not Path(t.filepath).is_file()]
        self.stdout.write(f"Scanned {qs.count()} track(s), found {len(orphaned)} orphaned (file missing on disk).\n")

        deleted = 0
        blocked = 0
        for track in orphaned:
            label = f"{track} (id={track.id}, category={track.category.code if track.category else '—'})"
            self.stdout.write(f"  {label} -- file missing: {track.filepath}")
            if apply:
                try:
                    track.delete()
                    deleted += 1
                except ProtectedError:
                    blocked += 1
                    self.stderr.write(
                        f"    [BLOCKED] Still referenced by an active PlaylistItem -- "
                        f"fix or remove it from the playlist first, then re-run."
                    )

        if apply:
            self.stdout.write(f"\nDeleted {deleted} track row(s). {blocked} blocked by an active playlist reference.")
        else:
            self.stdout.write(self.style.WARNING("\nNothing was actually deleted -- re-run with --apply to act on this."))
