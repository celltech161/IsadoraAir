from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import DuplicateCandidate


class Command(BaseCommand):
    help = (
        "Act on DuplicateCandidate rows a human has already resolved in "
        "admin (resolution != 'unresolved', applied=False). Defaults to a "
        "dry run that only reports what it would do -- pass --apply to "
        "actually delete a Track row + its file. 'keep_both' just marks the "
        "pair as reviewed/not-a-duplicate, no deletion either way."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete files/Track rows. Without this, only reports what would happen.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        if not apply:
            self.stdout.write(self.style.WARNING("DRY RUN -- pass --apply to actually delete anything.\n"))

        candidates = DuplicateCandidate.objects.filter(applied=False).exclude(resolution="unresolved")
        self.stdout.write(f"Found {candidates.count()} resolved candidate(s) to process.\n")

        for candidate in candidates:
            if candidate.resolution == "keep_both":
                self.stdout.write(f"  [keep_both] {candidate.track_a} <-> {candidate.track_b} -- no action, marking reviewed.")
                if apply:
                    candidate.applied = True
                    candidate.resolved_at = timezone.now()
                    candidate.save(update_fields=["applied", "resolved_at"])
                continue

            keep, drop = (
                (candidate.track_a, candidate.track_b) if candidate.resolution == "keep_a"
                else (candidate.track_b, candidate.track_a)
            )

            self.stdout.write(f"  [{candidate.resolution}] keep {keep} (id={keep.id}), delete {drop} (id={drop.id})")

            # Preserve the dropped track's rotation eligibility on the
            # survivor -- this is the whole point of Phase 2's
            # additional_categories: consolidating a physical duplicate
            # shouldn't remove it from a category it used to serve.
            if drop.category_id and drop.category_id != keep.category_id:
                already_tagged = keep.additional_categories.filter(id=drop.category_id).exists()
                if not already_tagged:
                    self.stdout.write(f"    + tagging {keep} with additional category '{drop.category}' (from the track being dropped)")
                    if apply:
                        keep.additional_categories.add(drop.category_id)

            fp = Path(drop.filepath) if drop.filepath else None
            if fp and fp.is_file():
                self.stdout.write(f"    - delete file: {fp}")
                if apply:
                    try:
                        fp.unlink()
                    except OSError as exc:
                        self.stderr.write(f"    [WARN] Failed to delete {fp}: {exc}")
            elif fp:
                self.stdout.write(f"    - file already missing on disk: {fp} (will still delete the Track row)")

            self.stdout.write(f"    - delete Track row: {drop} (id={drop.id})")
            if apply:
                # DuplicateCandidate.track_a/track_b both CASCADE -- deleting
                # either track removes this candidate row too, so there's no
                # separate "mark applied" step here (unlike keep_both, where
                # neither track is deleted and the row needs its own flag).
                drop.delete()

        if not apply:
            self.stdout.write(self.style.WARNING("\nNothing was actually deleted -- re-run with --apply to act on this."))
