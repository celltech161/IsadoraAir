"""Report on (and optionally reclaim) stale production-media staging data.

Dry-run by default, like find_orphaned_tracks: nothing is removed without
--apply. Reports present rows whose bytes are missing or wrong, which are never
auto-repaired. There is no timer behind this command; it is run deliberately.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from production.services import layout, reconcile


class Command(BaseCommand):
    help = (
        "Sweep stale incoming/*.part uploads, orphan permanent media and stale "
        "work/ directories, and report present media whose bytes are missing "
        "or inconsistent. Dry-run unless --apply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Actually remove what is reported.")
        parser.add_argument(
            "--grace-hours", type=float, default=reconcile.DEFAULT_GRACE.total_seconds() / 3600,
            help="Only touch things idle for at least this long (minimum 1; default 24).",
        )
        parser.add_argument("--deep", action="store_true", help="Also re-hash every present media (slow).")

    def handle(self, *args, **options):
        apply = options["apply"]
        grace = timedelta(hours=options["grace_hours"])
        if grace < reconcile.MIN_GRACE:
            raise CommandError("--grace-hours must be at least 1")
        self.stdout.write(f"Production media root: {layout.media_root()}")
        self.stdout.write("" if apply else self.style.WARNING("DRY RUN -- pass --apply to remove anything."))
        reports = (
            ("stale upload parts", reconcile.sweep_stale_parts(grace=grace, apply=apply)),
            ("orphan permanent media", reconcile.sweep_orphan_media(grace=grace, apply=apply)),
            ("stale work directories", reconcile.sweep_stale_work(grace=grace, apply=apply)),
        )
        for label, report in reports:
            verb = "removed" if apply else "would remove"
            self.stdout.write(f"{label}: {verb} {len(report.removed)}, kept (too young) {report.kept_young}, "
                              f"unexpected {len(report.unexpected)}, errors {len(report.errors)}")
            for item in report.removed:
                self.stdout.write(f"  - {item}")
            for item in report.unexpected:
                self.stdout.write(self.style.WARNING(f"  ? unexpected entry left alone: {item}"))
            for item in report.errors:
                self.stderr.write(f"  ! {item}")
        dangling = reconcile.find_purged_media_still_referenced()
        self.stdout.write(f"purged media still referenced by a domain row: {len(dangling)}")
        for media_id, label, field_name, count in dangling:
            self.stdout.write(self.style.ERROR(f"  ! {media_id}: {count} x {label}.{field_name}"))
        problems = reconcile.find_inconsistent_media(deep=options["deep"])
        self.stdout.write(f"present media with inconsistent bytes: {len(problems)}")
        for problem in problems:
            self.stdout.write(self.style.ERROR(f"  ! {problem.media_id}: {problem.problem}"))
