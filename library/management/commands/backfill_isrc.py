"""Backfill Track.isrc from tag data for tracks currently lacking one.

Reads the file at each Track.filepath with mutagen, extracts the ISRC
from ID3 TSRC (MP3 / id3-tagged FLAC / WAV+ID3) or Vorbis ISRC (FLAC /
OGG native), normalizes to canonical 12-char uppercase form (strip
hyphens/whitespace/newlines), validates the format, and writes it to
Track.isrc.

Validation: the ISO 3901 ISRC format is exactly 12 characters --
2-char country code (letters), 3-char registrant (alphanumeric),
2-digit year, 5-digit designation code. Anything else after
normalization is discarded rather than truncated -- e.g. a sloppy
tagger writing 'USEE10170188\\n55B' (real ISRC + accidental appendage)
falls through to empty instead of corrupting the field.

Idempotent. Skips tracks that already have an ISRC unless --overwrite
is passed. Skips tracks whose file is missing or unreadable (prints
one line per skip, no crash).

Usage:
    python manage.py backfill_isrc                 # only tracks with empty isrc
    python manage.py backfill_isrc --overwrite     # every track, replace existing
    python manage.py backfill_isrc --limit 200     # process first N (for smoke tests)
    python manage.py backfill_isrc --dry-run       # report what would change, no writes

This does NOT hit MusicBrainz -- it's tag-only. For tracks whose file
carries no ISRC tag, the Phase 5 `backfill_isrc_musicbrainz` command
(coming later) will query MusicBrainz by artist + title + album +
duration."""
import re
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Q
from mutagen import File as MutagenFile

from library.models import Track


ISRC_TAG_KEYS = ("TSRC", "ISRC")

# Canonical ISRC: 12 chars, 2 alpha country + 3 alphanumeric registrant
# + 7 digits (2-digit year + 5-digit designation code). Per ISO 3901.
ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def _extract_isrc(filepath):
    """Return the canonical 12-char uppercase ISRC or empty string.
    Never raises -- unreadable files, missing files, broken tags, or
    non-conforming tag values all become an empty return."""
    try:
        audio = MutagenFile(str(filepath))
    except Exception:
        return ""
    if audio is None:
        return ""
    raw = audio if hasattr(audio, "get") else getattr(audio, "tags", None)
    if raw is None:
        return ""
    for key in ISRC_TAG_KEYS:
        try:
            value = raw[key]
        except (KeyError, ValueError, TypeError):
            continue
        if isinstance(value, list) and value:
            value = value[0]
        if value is None:
            continue
        # Strip every category of whitespace + hyphens. Anything left
        # that doesn't match the ISO 3901 shape is rejected outright;
        # we'd rather leave the field empty for a Phase-5 MusicBrainz
        # lookup than corrupt the DB with an ambiguous value.
        normalized = re.sub(r"[\s\-]+", "", str(value)).upper()
        if ISRC_RE.match(normalized):
            return normalized
    return ""


class Command(BaseCommand):
    help = "Populate Track.isrc from ID3 TSRC / Vorbis ISRC tags."

    def add_arguments(self, parser):
        parser.add_argument("--overwrite", action="store_true",
                             help="Replace existing isrc values, not just empty ones.")
        parser.add_argument("--limit", type=int, default=0,
                             help="Cap number of tracks processed (0 = no cap).")
        parser.add_argument("--dry-run", action="store_true",
                             help="Report what would change without saving.")

    def handle(self, *args, **opts):
        qs = Track.objects.all().order_by("id")
        if not opts["overwrite"]:
            qs = qs.filter(Q(isrc="") | Q(isrc__isnull=True))
        if opts["limit"] > 0:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(f"Scanning {total} tracks for ISRC tags...")

        found = 0
        missing_file = 0
        no_tag = 0
        changed = 0

        for i, track in enumerate(qs.iterator(), start=1):
            if not track.filepath or not Path(track.filepath).is_file():
                missing_file += 1
                continue
            isrc = _extract_isrc(track.filepath)
            if not isrc:
                no_tag += 1
                continue
            found += 1
            if track.isrc == isrc:
                continue
            if opts["dry_run"]:
                self.stdout.write(
                    f"  [dry-run] would set track {track.id} "
                    f"({track.title!r}) isrc={isrc!r} (was {track.isrc!r})"
                )
            else:
                Track.objects.filter(id=track.id).update(isrc=isrc)
            changed += 1
            if i % 500 == 0:
                self.stdout.write(f"  ...processed {i}/{total}")

        self.stdout.write(self.style.SUCCESS(
            f"Done. found={found} changed={changed} "
            f"no_tag={no_tag} missing_file={missing_file} scanned={total}"
        ))
