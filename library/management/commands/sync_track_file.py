from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from library.management.commands.analyze_tracks import analyze_one_track, get_waveforms_dir
from library.management.commands.import_songs import parse_tags
from library.models import AnalysisConfig, Album, Artist, Category, Genre, Track


class Command(BaseCommand):
    help = (
        "Sync exactly one file into the library and force fresh analysis. "
        "Built for ingestion pipelines (e.g. syndicated show fetchers) that "
        "write or replace a file at a known path under LIBRARY_ROOT and need "
        "the Track row's tags AND waveform/cue points to reflect the new "
        "content immediately -- unlike import_songs, which leaves analysis "
        "fields stale on a same-path re-import, and unlike analyze_tracks' "
        "default scope, which skips tracks that already have a (possibly "
        "stale) next_start_seconds."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "filepath",
            help="Full path to the file, already written under LIBRARY_ROOT/<category_code>/.",
        )

    def handle(self, *args, **options):
        filepath = Path(options["filepath"]).resolve()

        if not filepath.is_file():
            raise CommandError(f"Not a file: {filepath}")

        root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()
        try:
            rel = filepath.relative_to(root)
        except ValueError:
            raise CommandError(f"{filepath} is not under LIBRARY_ROOT ({root})")

        parts = rel.parts
        category_code = parts[0] if len(parts) > 1 else ""
        if not category_code:
            raise CommandError(
                f"{filepath} is directly in LIBRARY_ROOT -- expected "
                f"LIBRARY_ROOT/<category_code>/..."
            )

        category_obj = Category.objects.filter(code=category_code).first()
        if category_obj is None:
            raise CommandError(
                f"No Category exists for folder '{category_code}'. Create "
                f"it in admin (with a kind) first."
            )

        tags, info = parse_tags(filepath)

        def clean(val):
            return val.replace("\x00", "").strip() if val else val

        title = clean(tags.get("title")) or filepath.stem
        artist_name = clean(tags.get("artist")) or "Unknown Artist"
        album_title = clean(tags.get("album")) or ""
        album_artist_name = clean(tags.get("album_artist")) or ""
        genre_name = clean(tags.get("genre")) or ""
        fmt = filepath.suffix.lstrip(".").lower()

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
            # This command is the landing point for unattended automated
            # ingestion (syndicated show fetchers) -- unlike a manual
            # upload, there's no human in the loop to flip the ready2air
            # gate, so each delivered file is presumed good and ready to
            # air. Set on every sync (not just creation) so a fresh
            # episode overwriting a track a human had pulled from
            # rotation gets its own clean review state, not the old
            # episode's manual override.
            "ready2air": True,
        }

        track, created = Track.objects.update_or_create(
            filepath=str(filepath),
            defaults=defaults,
        )

        cfg = AnalysisConfig.load()
        cfg_values = (
            cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
            cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
        )
        wave_dir = get_waveforms_dir()

        # existing_related="" forces fresh related-artist extraction --
        # new content at this path means the previous episode's
        # related-artist guess shouldn't be carried over. force=True is
        # otherwise irrelevant to analyze_one_track's own behavior: it
        # always recomputes waveform/cue points when called directly.
        row = (
            track.id, track.filepath, track.filename, track.duration_seconds,
            track.title, artist_obj.name, "",
        )
        analyzed = analyze_one_track(row, cfg_values, wave_dir, force=True)

        if not analyzed:
            raise CommandError(
                f"Track row synced (id={track.id}) but analysis failed -- "
                f"check the file is readable by ffmpeg: {filepath}"
            )

        self.stdout.write(
            f"{'Created' if created else 'Updated'} track {track.id} "
            f"({category_code}/{filepath.name}) and refreshed analysis."
        )
