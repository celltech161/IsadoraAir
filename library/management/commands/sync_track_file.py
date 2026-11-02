from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, router, transaction

from library.management.commands.analyze_tracks import (
    analyze_one_track, apply_category_thresholds, get_waveforms_dir,
)
from library.management.commands.import_songs import parse_tags
from library.models import AnalysisConfig, Album, Artist, Category, Genre, Track
from library.services import audio_identity, source_paths
from library.services.related_artists import resolve_fallback_metadata


def sync_track_file(filepath, ready2air=True, wave_dir=None):
    """Sync exactly one file into the library and force fresh analysis.
    Built for ingestion pipelines (e.g. syndicated show fetchers, and
    aircheck.services.recorder's Stop path) that write or replace a file
    at a known path under LIBRARY_ROOT and need the Track row's tags AND
    waveform/cue points to reflect the new content immediately -- unlike
    import_songs, which leaves analysis fields stale on a same-path
    re-import, and unlike analyze_tracks' default scope, which skips
    tracks that already have a (possibly stale) next_start_seconds.

    Extracted from this command's own handle() so both the CLI and a
    direct caller share one code path (same reasoning as
    analyze_tracks.analyze_one_track, also called directly by
    library/views.py's api_library_upload).

    ready2air: True (the CLI default below, unchanged from this
    function's original command-only behavior) presumes an unattended
    ingestion pipeline delivering NEW programming meant to air, with no
    human in the loop to flip the gate. Pass False for content that
    should require human review before ever being schedulable -- e.g.
    an Aircheck recording, which is archival (a record of what already
    aired), not new programming.

    wave_dir: None (default, unchanged behavior for every existing
    caller) uses the real get_waveforms_dir(). A caller may pass an
    explicit scratch directory instead so analysis writes its waveform
    JSON there rather than into the real WAVEFORMS_DIR -- see
    weather/publication.py::publish_weather_asset, which analyzes into
    a publication-owned scratch directory and only publishes the
    resulting <track_id>.json into the real directory once analysis
    has genuinely succeeded, so a failed first-generation analysis
    never touches the real waveform directory AT ALL (nothing to scan
    for or clean up afterward).

    Raises CommandError (a plain exception outside management-command
    context -- callers that aren't a Command themselves should just
    catch it like any other exception) on: not a file, not under
    LIBRARY_ROOT, LIBRARY_ROOT/<category_code>/... folder has no
    matching Category, the file vanishing or changing before its Track
    is registered (see below), or analysis failure. Returns (track,
    created) on success.

    3.11: the Track is registered inside a transaction that holds the
    path's registration lock (library.services.source_paths), and the
    file is re-checked under that lock: a recall (remove_track_file)
    that removed it first makes this fail with nothing registered, and
    a file replaced since its tags were read is not registered with the
    previous file's metadata. The lock is released when that write
    commits -- before analysis -- unless the caller wraps this call in
    its own transaction."""
    filepath = Path(filepath).resolve()

    if not filepath.is_file():
        raise CommandError(f"Not a file: {filepath}")
    # Identity of the file whose tags are read below; re-checked under the path lock before registering.
    witness = audio_identity.stat_source(filepath)

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

    # Same fallback-metadata humanization as every other ingest
    # path -- this pipeline's delivered filenames aren't
    # necessarily any better-formed than a manual upload's.
    title, artist_name = resolve_fallback_metadata(
        filepath.stem, clean(tags.get("title")), clean(tags.get("artist")),
    )
    album_title = clean(tags.get("album")) or ""
    album_artist_name = clean(tags.get("album_artist")) or ""
    genre_name = clean(tags.get("genre")) or ""
    fmt = filepath.suffix.lstrip(".").lower()

    artist_obj, _ = Artist.get_or_create_ci(artist_name)

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
        # Set on every sync (not just creation) so a fresh file
        # overwriting a track a human had pulled from rotation gets its
        # own clean review state, not the old content's manual override.
        "ready2air": ready2air,
    }

    track, created = _register(filepath, witness, defaults)

    cfg = AnalysisConfig.load()
    cfg_values = (
        cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
        cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
    )
    # Fold in the track's category-level threshold overrides if any --
    # sync_track_file is a swap-in-place op so the new content
    # inherits the current row's category (unchanged in defaults
    # above), which may carry its own next-start/cue-in overrides.
    if track.category_id:
        category = Category.objects.only(
            "next_start_threshold_db_override", "cue_in_threshold_db_override"
        ).get(pk=track.category_id)
        cfg_values = apply_category_thresholds(
            cfg_values,
            category.next_start_threshold_db_override,
            category.cue_in_threshold_db_override,
        )
    wave_dir = wave_dir if wave_dir is not None else get_waveforms_dir()

    # existing_related="" forces fresh related-artist extraction --
    # new content at this path means the previous episode's
    # related-artist guess shouldn't be carried over. force=True is
    # otherwise irrelevant to analyze_one_track's own behavior: it
    # always recomputes waveform/cue points when called directly.
    row = (
        track.id, track.filepath, track.filename, track.duration_seconds,
        track.title, artist_obj.name, "",
    )
    # New content at this path: the calculated markers rebind to it (protection that applied to
    # the replaced audio is superseded and audited by the cue-marker authority).
    analyzed = analyze_one_track(row, cfg_values, wave_dir, force=True, media_replaced=True)

    if not analyzed:
        raise CommandError(
            f"Track row synced (id={track.id}) but analysis failed -- "
            f"check the file is readable by ffmpeg: {filepath}"
        )

    return track, created


class _PathBusy(Exception):
    """The path's registration lock was not obtained within the bounded wait."""


def _register(filepath, witness, defaults):
    """update_or_create the path's Track holding its registration lock, if the file is still the one read."""
    using = router.db_for_write(Track)
    try:
        with transaction.atomic(using=using):
            try:
                source_paths.acquire(filepath, using, timeout_ms=source_paths.REGISTRATION_LOCK_TIMEOUT_MS)
            except OperationalError as exc:
                raise _PathBusy from exc
            current = audio_identity.stat_source(filepath)
            if current is None:
                raise CommandError(
                    f"{filepath} disappeared before its Track could be registered (recalled or removed); "
                    f"no Track was registered or updated"
                )
            if not audio_identity.same_content_witness(witness, current):
                raise CommandError(
                    f"{filepath} changed while its metadata was being read; no Track was registered or "
                    f"updated -- sync it again"
                )
            return Track.objects.update_or_create(filepath=str(filepath), defaults=defaults)
    except _PathBusy as exc:
        raise CommandError(
            f"{filepath} is locked by a concurrent recall or cleanup (not obtained within "
            f"{source_paths.REGISTRATION_LOCK_TIMEOUT_MS} ms); no Track was registered or updated -- sync it again"
        ) from exc.__cause__


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
        track, created = sync_track_file(options["filepath"])
        self.stdout.write(
            f"{'Created' if created else 'Updated'} track {track.id} "
            f"({track.category.code}/{Path(track.filepath).name}) and refreshed analysis."
        )
