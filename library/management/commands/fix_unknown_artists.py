import re
import subprocess
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from library.models import Artist, Track


# Any of ASCII hyphen, en dash, em dash, with optional surrounding
# whitespace. Split on the FIRST such delimiter.
SPLIT_PATTERN = re.compile(r'\s*[-–—]\s*')

# Skip syndicated-show style titles like "2015-07-30 MITD Part 1" -- the
# leading token is a date, not an artist.
DATE_PREFIX = re.compile(r'^\d{4}[-–—]\d{2}[-–—]\d{2}\b')

# "01 " prefix on ripped-from-album titles ("01 The Lumineers - ...").
LEADING_TRACK_NUM_SPACE = re.compile(r'^\d+\s+')
# "05 - " prefix ("05 - Sugar Moth - ...") -- second pass after the
# first has run, so a two-digit-plus-space wasn't already stripped.
LEADING_TRACK_NUM_DASH = re.compile(r'^\d+\s*[-–—]\s*')

# Formats we can tag directly with mutagen.
TAGGABLE_EXTS = {'.flac', '.mp3', '.m4a', '.ogg', '.oga'}
# Formats without native tag support -- transcode to FLAC first.
TRANSCODE_EXTS = {'.wav', '.aif', '.aiff'}


def parse_artist_title(source):
    """Parse "Artist - Title" out of a source string. Skips
    date-formatted prefixes and strips a leading track number token.
    Returns (artist, title) tuple, or (None, None) if no clean split
    is found."""
    if DATE_PREFIX.match(source):
        return None, None
    cleaned = LEADING_TRACK_NUM_SPACE.sub('', source, count=1)
    cleaned = LEADING_TRACK_NUM_DASH.sub('', cleaned, count=1)
    parts = SPLIT_PATTERN.split(cleaned, maxsplit=1)
    if len(parts) == 2:
        artist = parts[0].strip()
        title = parts[1].strip()
        if artist and title:
            return artist, title
    return None, None


def write_tags(path, artist, title):
    """Write artist + title tags on a directly-taggable audio file
    (formats listed in TAGGABLE_EXTS). Raises on unsupported extension
    or on mutagen write failure -- caller catches."""
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis

    ext = Path(path).suffix.lower()
    if ext == '.flac':
        audio = FLAC(path)
        audio['artist'] = artist
        audio['title'] = title
        audio.save()
    elif ext == '.mp3':
        try:
            audio = EasyID3(path)
        except ID3NoHeaderError:
            audio = EasyID3()
        audio['artist'] = artist
        audio['title'] = title
        audio.save(path)
    elif ext == '.m4a':
        audio = MP4(path)
        audio['\xa9ART'] = [artist]
        audio['\xa9nam'] = [title]
        audio.save()
    elif ext in ('.ogg', '.oga'):
        audio = OggVorbis(path)
        audio['artist'] = artist
        audio['title'] = title
        audio.save()
    else:
        raise ValueError(f"Unsupported taggable format: {ext}")


def transcode_to_flac(src_path):
    """Run ffmpeg to produce a sibling .flac file at compression_level
    5 (default; balanced speed/ratio). Returns Path to the new flac.
    Raises FileExistsError if the target already exists (caller
    handles as an idempotent skip). Raises CalledProcessError on
    ffmpeg failure."""
    src = Path(src_path)
    dst = src.with_suffix('.flac')
    if dst.exists():
        raise FileExistsError(str(dst))
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
         '-i', str(src), '-c:a', 'flac', '-compression_level', '5',
         str(dst)],
        check=True,
    )
    return dst


class Command(BaseCommand):
    help = (
        "Fix tracks with 'Unknown Artist' by parsing 'Artist - Title' from "
        "the DB title field, writing the split back to file metadata tags, "
        "and updating the Track row. WAV/AIF files can't carry tags -- "
        "for those, transcode to FLAC, tag the FLAC, add a new Track row "
        "for it, and mark the original WAV Track ready2air=False so the "
        "operator can weed them out manually. Default is dry-run -- pass "
        "--apply to actually write."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually make changes. Without this flag the command "
                 "dry-runs and only prints what it would do.")
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Only process the first N candidate tracks. Useful for "
                 "iterative dry-run tuning.")
        parser.add_argument(
            "--only-format", type=str, default=None,
            help="Only process tracks with this extension "
                 "(e.g. 'wav', 'flac').")

    def handle(self, *args, **options):
        apply = options["apply"]
        limit = options["limit"]
        only_fmt = (options["only_format"] or "").lstrip(".").lower() or None
        mode = "APPLY" if apply else "DRY RUN (pass --apply to write)"
        self.stdout.write(f"Mode: {mode}")
        if only_fmt:
            self.stdout.write(f"Filter: only .{only_fmt} files")

        qs = (Track.objects
                   .filter(artist__name="Unknown Artist")
                   .select_related("artist", "category", "album", "genre")
                   .order_by("id"))
        total = qs.count()
        self.stdout.write(f"Unknown Artist tracks in DB: {total}\n")

        counts = {
            "skipped_nomatch": 0, "skipped_ext": 0, "skipped_missing": 0,
            "skipped_target_exists": 0, "would_tag": 0, "would_transcode": 0,
            "tagged": 0, "transcoded": 0, "errors": 0,
        }

        processed = 0
        for track in qs.iterator():
            artist_name, new_title = parse_artist_title(track.title)
            if not artist_name:
                counts["skipped_nomatch"] += 1
                continue

            path = Path(track.filepath)
            ext = path.suffix.lower()
            if only_fmt and ext.lstrip(".") != only_fmt:
                continue
            if ext not in TAGGABLE_EXTS and ext not in TRANSCODE_EXTS:
                self.stdout.write(
                    f"  [ext-skip] {track.id} unsupported {ext}: "
                    f"{track.filename}"
                )
                counts["skipped_ext"] += 1
                continue
            if not path.is_file():
                self.stdout.write(
                    f"  [missing] {track.id} file gone: {track.filepath}"
                )
                counts["skipped_missing"] += 1
                continue

            if ext in TAGGABLE_EXTS:
                self.stdout.write(
                    f"  [tag] {track.id} {ext} {track.title!r} "
                    f"-> artist={artist_name!r}, title={new_title!r}"
                )
                if not apply:
                    counts["would_tag"] += 1
                else:
                    try:
                        write_tags(str(path), artist_name, new_title)
                        with transaction.atomic():
                            artist_obj, _ = Artist.get_or_create_ci(artist_name)
                            Track.objects.filter(id=track.id).update(
                                artist=artist_obj, title=new_title,
                            )
                        counts["tagged"] += 1
                    except Exception as exc:
                        self.stdout.write(f"    [error] {exc}")
                        counts["errors"] += 1
            else:
                # Transcode path (WAV / AIF).
                flac_target = path.with_suffix(".flac")
                if flac_target.exists():
                    self.stdout.write(
                        f"  [skip] {track.id} target FLAC already exists: "
                        f"{flac_target.name}"
                    )
                    counts["skipped_target_exists"] += 1
                    continue
                self.stdout.write(
                    f"  [transcode+tag] {track.id} {ext} {track.title!r} "
                    f"-> {flac_target.name} artist={artist_name!r}, "
                    f"title={new_title!r}"
                )
                if not apply:
                    counts["would_transcode"] += 1
                else:
                    try:
                        flac_path = transcode_to_flac(str(path))
                        write_tags(str(flac_path), artist_name, new_title)
                        with transaction.atomic():
                            artist_obj, _ = Artist.get_or_create_ci(artist_name)
                            new_track = Track.objects.create(
                                filepath=str(flac_path),
                                filename=flac_path.name,
                                format="flac",
                                title=new_title,
                                artist=artist_obj,
                                album=track.album,
                                genre=track.genre,
                                year=track.year,
                                track_number=track.track_number,
                                disc_number=track.disc_number,
                                composer=track.composer,
                                publisher=track.publisher,
                                record_label=track.record_label,
                                comments=track.comments,
                                duration_seconds=track.duration_seconds,
                                sample_rate=track.sample_rate,
                                channels=track.channels,
                                bit_depth=track.bit_depth,
                                category=track.category,
                                rotation_weight=track.rotation_weight,
                                ready2air=True,
                                energy=track.energy,
                                vocal_type=track.vocal_type,
                                end_type=track.end_type,
                                blocked_slots=list(track.blocked_slots),
                                # Carry manual playout edits forward:
                                # audio content is byte-equivalent, so
                                # operator-set intro/sweep/outro/hook
                                # offsets stay valid. Auto-detected
                                # cue_in / cue_out / next_start are left
                                # for analyze_tracks to recompute.
                                intro_until_seconds=track.intro_until_seconds,
                                sweep_start_seconds=track.sweep_start_seconds,
                                outro_starts_seconds=track.outro_starts_seconds,
                                hook_in_seconds=track.hook_in_seconds,
                                hook_out_seconds=track.hook_out_seconds,
                                cue_out_seconds=track.cue_out_seconds,
                            )
                            # M2M copied after save.
                            new_track.additional_categories.set(
                                track.additional_categories.all()
                            )
                            new_track.holidays.set(track.holidays.all())
                            # Mark original WAV Track ready2air=False
                            # so it drops out of rotation immediately;
                            # operator weeds row + file manually.
                            Track.objects.filter(id=track.id).update(
                                ready2air=False,
                            )
                        counts["transcoded"] += 1
                    except FileExistsError:
                        counts["skipped_target_exists"] += 1
                    except Exception as exc:
                        self.stdout.write(f"    [error] {exc}")
                        counts["errors"] += 1

            processed += 1
            if limit and processed >= limit:
                self.stdout.write(f"\nHit --limit={limit}, stopping.")
                break

        self.stdout.write("")
        self.stdout.write("=== Summary ===")
        if apply:
            self.stdout.write(f"Tagged in place:      {counts['tagged']}")
            self.stdout.write(f"Transcoded to FLAC:   {counts['transcoded']}")
        else:
            self.stdout.write(f"Would tag in place:   {counts['would_tag']}")
            self.stdout.write(f"Would transcode:      {counts['would_transcode']}")
        self.stdout.write(f"Skipped (no match):   {counts['skipped_nomatch']}")
        self.stdout.write(f"Skipped (unsup ext):  {counts['skipped_ext']}")
        self.stdout.write(f"Skipped (missing):    {counts['skipped_missing']}")
        self.stdout.write(f"Skipped (target ok):  {counts['skipped_target_exists']}")
        if counts["errors"]:
            self.stdout.write(self.style.ERROR(f"Errors:               {counts['errors']}"))
