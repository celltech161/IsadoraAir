"""Find tracks whose file location no longer matches their primary
category, and (with --apply) move them into the correct category
folder while keeping the DB row in sync.

Background: files are stored at LIBRARY_ROOT/<Category.code>/<basename>
(same convention sync_track_file.py enforces on ingest). When a track
is re-categorized after ingest -- e.g. moving a rocker from 90's Rock
to Deep Cuts -- the DB row updates but the file stays where it was
originally imported. Over months these drift piles up; this command
finds and fixes them.

Safety:
- Default is dry-run. --apply is required to touch anything.
- Per-track atomicity: each file's move + Track.filepath save is one
  unit. Partial progress is preferable to all-or-nothing on a big run.
- Refuses to overwrite -- if the destination path already exists,
  the track is skipped (reported) rather than auto-renamed.
- On DB save failure after a successful filesystem move, best-effort
  reverts the move so the DB and disk stay consistent.

Safe to run while the engine is playing: on Linux ext4, an already-
open file inode survives a rename, and future queries pull the fresh
filepath from the DB. Scheduling during a quiet hour is still good
practice for a large run.
"""
from pathlib import Path
import shutil

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.models import Category, Track


DEFAULT_ROOT = "/srv/isadoraair/music"


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Perform the moves + DB updates. Default is dry-run (report only).",
        )
        parser.add_argument(
            "--only-category", default=None, metavar="CODE",
            help="Restrict scan to one Category by code (safer to test on).",
        )
        parser.add_argument(
            "--verbose", action="store_true",
            help="Emit a line per track processed, not just drifted ones.",
        )

    def handle(self, *args, **options):
        apply_moves = options["apply"]
        only_code = options["only_category"]
        verbose = options["verbose"]

        root = Path(getattr(settings, "LIBRARY_ROOT", DEFAULT_ROOT)).resolve()
        if not root.is_dir():
            raise CommandError(f"LIBRARY_ROOT does not exist: {root}")

        qs = Track.objects.select_related("category").order_by("id")
        if only_code:
            cat = Category.objects.filter(code=only_code).first()
            if cat is None:
                raise CommandError(f"No Category with code={only_code!r}")
            qs = qs.filter(category=cat)

        scanned = 0
        drifted = []
        skipped_missing = 0
        skipped_no_category = 0
        skipped_outside_root = 0

        for track in qs.iterator():
            scanned += 1
            if track.category_id is None:
                skipped_no_category += 1
                if verbose:
                    self.stdout.write(f"[NO CAT] t{track.id} {track.filepath}")
                continue

            current_path = Path(track.filepath)
            if not current_path.is_file():
                skipped_missing += 1
                if verbose:
                    self.stdout.write(f"[MISSING] t{track.id} {track.filepath}")
                continue

            try:
                current_path.resolve().relative_to(root)
            except ValueError:
                skipped_outside_root += 1
                if verbose:
                    self.stdout.write(f"[OUTSIDE ROOT] t{track.id} {track.filepath}")
                continue

            expected_code = track.category.code
            actual_folder = current_path.parent.name

            if actual_folder == expected_code:
                if verbose:
                    self.stdout.write(f"[OK] t{track.id} {track.filepath}")
                continue

            # Drift detected.
            new_path = root / expected_code / current_path.name
            drifted.append({
                "track": track,
                "old_path": current_path,
                "new_path": new_path,
                "actual_folder": actual_folder,
                "expected_folder": expected_code,
            })

        # --- Report ---
        self.stdout.write("")
        self.stdout.write(f"Category drift scan (root={root})")
        self.stdout.write(f"  Scanned tracks: {scanned}")
        self.stdout.write(f"  Drift found: {len(drifted)}")
        self.stdout.write(f"  Missing files (skipped): {skipped_missing}")
        self.stdout.write(f"  No primary category (skipped): {skipped_no_category}")
        self.stdout.write(f"  Filepath outside LIBRARY_ROOT (skipped): {skipped_outside_root}")

        if not drifted:
            self.stdout.write(self.style.SUCCESS("\nNo drift. Nothing to do."))
            return

        # Group by (old_folder -> new_folder) for a compact preview.
        preview_lines = []
        collisions = []
        for row in drifted:
            if row["new_path"].exists():
                collisions.append(row)
            filename = row["old_path"].name
            preview_lines.append(
                f"  [{'COLL' if row['new_path'].exists() else 'MOVE'}] "
                f"{row['actual_folder']!r:28s} -> {row['expected_folder']!r:28s} — {filename}"
            )

        self.stdout.write("\nDrift detail:")
        for line in preview_lines:
            self.stdout.write(line)

        movable = [r for r in drifted if not r["new_path"].exists()]
        self.stdout.write(
            f"\n{len(movable)} moveable, {len(collisions)} collisions "
            f"(destination already exists -- always skipped)."
        )

        if not apply_moves:
            self.stdout.write(self.style.WARNING(
                f"\nDry-run. Re-run with --apply to perform {len(movable)} moves."
            ))
            return

        # --- Apply ---
        moved = 0
        move_errors = 0
        db_errors = 0
        for row in movable:
            track = row["track"]
            old_path = row["old_path"]
            new_path = row["new_path"]
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.stderr.write(f"  [MKDIR FAIL] t{track.id}: {exc}")
                move_errors += 1
                continue

            try:
                shutil.move(str(old_path), str(new_path))
            except OSError as exc:
                self.stderr.write(f"  [MOVE FAIL] t{track.id}: {exc}")
                move_errors += 1
                continue

            # Filesystem move succeeded; try the DB update. If it
            # fails, revert the file so the DB and disk stay
            # consistent -- an operator can rerun.
            try:
                Track.objects.filter(pk=track.pk).update(filepath=str(new_path))
            except Exception as exc:
                self.stderr.write(f"  [DB SAVE FAIL] t{track.id}: {exc} -- attempting revert")
                try:
                    shutil.move(str(new_path), str(old_path))
                except OSError as revert_exc:
                    self.stderr.write(
                        f"  [REVERT FAIL] t{track.id}: {revert_exc}; "
                        f"DB has old={old_path}, disk has new={new_path}"
                    )
                db_errors += 1
                continue

            moved += 1
            if verbose:
                self.stdout.write(f"  [OK] t{track.id}: {old_path.name}")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Applied. Moved: {moved}, move errors: {move_errors}, "
            f"db errors: {db_errors}, collisions skipped: {len(collisions)}"
        ))
