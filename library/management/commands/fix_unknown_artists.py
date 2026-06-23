import re
from pathlib import Path

from django.core.management.base import BaseCommand

from library.models import Artist, Track


SPLIT_PATTERN = re.compile(r'\s*[-–—]\s*')


def parse_artist_title(filename):
    stem = Path(filename).stem

    # Skip date-formatted filenames like "2015-07-30 MITD Part 1"
    if re.match(r'^\d{4}[-–—]\d{2}[-–—]\d{2}\b', stem):
        return None, None

    # Strip leading track numbers: "01 ", "03 ", "1771344024 05 - "
    cleaned = re.sub(r'^\d+\s+', '', stem)
    # Strip leading "NN - " prefix (e.g. "05 - Sugar Moth - ...")
    cleaned = re.sub(r'^\d+\s*[-–—]\s*', '', cleaned)

    parts = SPLIT_PATTERN.split(cleaned, maxsplit=1)
    if len(parts) == 2:
        artist = parts[0].strip()
        title = parts[1].strip()
        if artist and title:
            return artist, title

    return None, None


class Command(BaseCommand):
    help = "Fix tracks with 'Unknown Artist' by parsing Artist - Title from filenames."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without writing to the DB.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("DRY RUN — no changes will be written.\n")

        unknowns = Track.objects.filter(artist__name="Unknown Artist").select_related("artist")
        total = unknowns.count()
        self.stdout.write(f"Found {total} tracks with Unknown Artist.\n")

        fixed = 0
        skipped = 0

        for track in unknowns:
            artist_name, title = parse_artist_title(track.filename)

            if not artist_name:
                self.stdout.write(f"  [skip] {track.filename}")
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"  [fix] {track.filename}")
                self.stdout.write(f"         -> artist={artist_name}, title={title}")
                fixed += 1
                continue

            artist_obj, _ = Artist.objects.get_or_create(name=artist_name)
            track.artist = artist_obj
            track.title = title
            track.save(update_fields=["artist", "title", "updated_at"])
            fixed += 1

        self.stdout.write(f"\nDone. Fixed: {fixed}, skipped: {skipped}")
