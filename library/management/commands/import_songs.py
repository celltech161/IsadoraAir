import os
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from mutagen import File as MutagenFile

from library.models import Album, Artist, Category, Genre, Track

SUPPORTED_EXT = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".oga", ".aiff", ".aif", ".mp2", ".alac"}

# WAV files can carry metadata in either an ID3 chunk (which mutagen
# reads) OR a LIST INFO chunk (which mutagen does NOT), and some CD
# rippers / ffmpeg -metadata write only the LIST INFO form. Without
# this fallback, RIFF-only WAVs would land as "Unknown Artist" and
# the auto-transcode-to-FLAC step would then bake that name into the
# new file's tags, silently losing the operator's original metadata.
_RIFF_INFO_KEYS = {
    b"INAM": "title",
    b"IART": "artist",
    b"IPRD": "album",
    b"IGNR": "genre",
    b"ICRD": "year",
    b"ITRK": "track_number",
}


def _read_wav_riff_info_tags(path):
    """Read the RIFF LIST INFO chunk from a WAV file. Best-effort:
    swallows all errors and returns whatever it managed to parse.
    Never raises. Returns a `{title, artist, album, genre, year,
    track_number}` dict populated only with keys that were present
    and non-empty in the file."""
    tags = {}
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if (len(header) < 12
                    or header[:4] != b"RIFF"
                    or header[8:12] != b"WAVE"):
                return tags
            while True:
                chunk_hdr = f.read(8)
                if len(chunk_hdr) < 8:
                    break
                chunk_id = chunk_hdr[:4]
                chunk_size = struct.unpack("<I", chunk_hdr[4:8])[0]
                if chunk_id == b"LIST":
                    list_type = f.read(4)
                    if list_type == b"INFO":
                        end = f.tell() + chunk_size - 4
                        while f.tell() < end:
                            sub_hdr = f.read(8)
                            if len(sub_hdr) < 8:
                                break
                            sub_id = sub_hdr[:4]
                            sub_size = struct.unpack("<I", sub_hdr[4:8])[0]
                            value_bytes = f.read(sub_size)
                            # RIFF sub-chunks pad to even byte boundary.
                            if sub_size % 2:
                                f.read(1)
                            if sub_id in _RIFF_INFO_KEYS:
                                try:
                                    val = (value_bytes
                                           .rstrip(b"\x00")
                                           .decode("utf-8", errors="replace")
                                           .strip())
                                    if val:
                                        tags[_RIFF_INFO_KEYS[sub_id]] = val
                                except Exception:
                                    pass
                        continue
                    # Not an INFO LIST -- rewind the 4-byte type peek
                    # so the following seek skips the whole chunk.
                    f.seek(-4, 1)
                # Skip chunk data + pad byte if odd.
                f.seek(chunk_size + (chunk_size % 2), 1)
    except Exception:
        pass
    return tags


def parse_tags(path):
    try:
        audio = MutagenFile(str(path), easy=False)
    except Exception as exc:
        print(f"  [WARN] Failed to open {path}: {exc}")
        return {}, {}

    if audio is None:
        return {}, {}

    tags = {}
    info = {}

    if getattr(audio, "info", None) is not None:
        try:
            info["duration_seconds"] = round(getattr(audio.info, "length", 0) or 0, 3)
        except Exception:
            info["duration_seconds"] = None
        info["sample_rate"] = getattr(audio.info, "sample_rate", None)
        info["channels"] = getattr(audio.info, "channels", None)
        info["bit_depth"] = getattr(audio.info, "bits_per_sample", None)

    def first(tag_names):
        # mutagen's Vorbis __contains__ raises ValueError for non-Vorbis
        # keys (e.g. ID3 "TIT2" against a FLAC); guard so a single bad
        # key doesn't abort the whole tag lookup.
        for name in tag_names:
            try:
                if name not in audio:
                    continue
                value = audio.get(name)
            except Exception:
                continue
            if isinstance(value, list) and value:
                return str(value[0])
            if value is not None:
                return str(value)
        return None

    tags["title"] = first(["TIT2", "TITLE", "\xa9nam"])
    tags["artist"] = first(["TPE1", "ARTIST", "\xa9ART", "aART"])
    tags["album"] = first(["TALB", "ALBUM", "\xa9alb"])
    tags["album_artist"] = first(["TPE2", "ALBUMARTIST", "ALBUM ARTIST", "aART"])
    tags["genre"] = first(["TCON", "GENRE", "\xa9gen"])

    year_raw = first(["TDRC", "TYER", "YEAR", "\xa9day"])
    try:
        tags["year"] = int(str(year_raw)[:4]) if year_raw else None
    except Exception:
        tags["year"] = None

    def parse_num(value):
        if not value:
            return None
        try:
            return int(str(value).split("/")[0])
        except Exception:
            return None

    tags["track_number"] = parse_num(first(["TRCK", "TRACKNUMBER", "trkn"]))
    tags["disc_number"] = parse_num(first(["TPOS", "DISCNUMBER", "disk"]))
    # ISRC: TSRC in ID3v2, ISRC in Vorbis/FLAC. MP4 uses a freeform
    # atom (----:com.apple.iTunes:ISRC) with a different value shape --
    # not tried here; those tracks stay empty and can be backfilled via
    # `backfill_isrc` (mutagen direct) or Phase 5 MusicBrainz lookup.
    isrc_raw = first(["TSRC", "ISRC"])
    tags["isrc"] = str(isrc_raw).strip().upper().replace("-", "") if isrc_raw else ""

    # WAV RIFF-INFO fallback -- see _RIFF_INFO_KEYS comment.
    if str(path).lower().endswith(".wav") and any(
        tags.get(k) is None for k in ("title", "artist", "album", "genre", "year", "track_number")
    ):
        riff = _read_wav_riff_info_tags(path)
        for key in ("title", "artist", "album", "genre"):
            if tags.get(key) is None and key in riff:
                tags[key] = riff[key]
        if tags.get("year") is None and "year" in riff:
            try:
                tags["year"] = int(str(riff["year"])[:4])
            except Exception:
                pass
        if tags.get("track_number") is None and "track_number" in riff:
            tags["track_number"] = parse_num(riff["track_number"])

    return tags, info


class Command(BaseCommand):
    help = "Scan a directory tree for audio files and import them into the library."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Directory to scan. Defaults to LIBRARY_ROOT from settings.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be created/updated without writing to the DB.",
        )

    def handle(self, *args, **options):
        scan_path = options["path"] or getattr(settings, "LIBRARY_ROOT", None)
        if not scan_path:
            self.stderr.write("No path given and LIBRARY_ROOT is not set in settings.")
            return

        root = Path(scan_path)
        if not root.is_dir():
            self.stderr.write(f"Not a directory: {root}")
            return

        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write("DRY RUN — no changes will be written.\n")

        inserted = 0
        updated = 0
        skipped = 0
        total = 0

        self.stdout.write(f"Scanning: {root}\n")

        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                filepath = Path(dirpath) / name
                ext = filepath.suffix.lower()
                total += 1

                if ext not in SUPPORTED_EXT:
                    skipped += 1
                    continue

                try:
                    ok, status = self._process_file(filepath, root, dry_run)
                except Exception as exc:
                    self.stderr.write(f"  [ERROR] {filepath}: {exc}")
                    skipped += 1
                    continue

                if ok:
                    if status == "inserted":
                        inserted += 1
                    elif status == "updated":
                        updated += 1
                else:
                    skipped += 1

                processed = inserted + updated + skipped
                if processed % 50 == 0:
                    self.stdout.write(
                        f"  ... total={total} inserted={inserted} "
                        f"updated={updated} skipped={skipped}"
                    )

        self.stdout.write(
            f"\nDone. Files seen: {total}, "
            f"inserted: {inserted}, updated: {updated}, skipped: {skipped}"
        )

    def _process_file(self, filepath, root, dry_run):
        tags, info = parse_tags(filepath)

        def clean(val):
            return val.replace("\x00", "").strip() if val else val

        title = clean(tags.get("title")) or filepath.stem
        artist_name = clean(tags.get("artist")) or "Unknown Artist"
        album_title = clean(tags.get("album")) or ""
        album_artist_name = clean(tags.get("album_artist")) or ""
        genre_name = clean(tags.get("genre")) or ""
        fmt = filepath.suffix.lstrip(".").lower()

        rel = filepath.relative_to(root)
        parts = rel.parts
        category_name = parts[0] if len(parts) > 1 else ""

        if dry_run:
            existing = Track.objects.filter(filepath=str(filepath)).exists()
            status = "updated" if existing else "inserted"
            self.stdout.write(f"  [{status}] {filepath}")
            return True, status

        artist_obj, _ = Artist.objects.get_or_create(name=artist_name)

        album_obj = None
        if album_title:
            album_obj, _ = Album.objects.get_or_create(
                title=album_title,
                album_artist=album_artist_name,
                defaults={"year": tags.get("year")},
            )

        genre_obj = None
        if genre_name:
            genre_obj, _ = Genre.objects.get_or_create(name=genre_name)

        category_obj = None
        if category_name:
            category_obj = Category.objects.filter(code=category_name).first()
            if category_obj is None:
                # Category.kind is a required FK (CategoryKind) that can't
                # be inferred from a folder name alone -- silently
                # get_or_create()-ing one here used to hit an IntegrityError
                # on the very first file, aborting the whole run. Skip just
                # this file/folder and keep going; `check_categories` finds
                # folders like this proactively instead of failing mid-import.
                self.stderr.write(
                    f"  [WARN] Skipping {filepath}: no Category exists for "
                    f"folder '{category_name}'. Create it in admin (with a "
                    f"kind) first, or run `check_categories` to see all "
                    f"folders missing a matching category."
                )
                return False, "skipped"

        defaults = {
            "filename": clean(filepath.name),
            "format": fmt,
            "title": clean(title),
            "artist": artist_obj,
            "album": album_obj,
            "genre": genre_obj,
            "year": tags.get("year"),
            "track_number": tags.get("track_number"),
            "disc_number": tags.get("disc_number"),
            "duration_seconds": info.get("duration_seconds"),
            "sample_rate": info.get("sample_rate"),
            "channels": info.get("channels"),
            "bit_depth": info.get("bit_depth"),
            "category": category_obj,
        }
        # Only overwrite isrc if the file actually carries one -- a
        # re-import of a file whose ISRC tag has been stripped must
        # NOT wipe a MusicBrainz backfill or a manual admin entry
        # already sitting in the DB.
        if tags.get("isrc"):
            defaults["isrc"] = tags["isrc"]

        _obj, created = Track.objects.update_or_create(
            filepath=str(filepath),
            defaults=defaults,
        )

        return True, "inserted" if created else "updated"
