import json
import math
import struct
import subprocess
import sys
import time
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db.models import Q

from library.models import AnalysisConfig, Track
from library.services import audio_identity, cue_markers, waveform_cache
from library.services.related_artists import extract_credited_artists, merge_related_artists
from monitoring.models import emit_event
# Aliased on import: this file uses `existing_artist_names_casefolded`
# as a parameter/local-variable name everywhere below (analyze_one_
# track's own signature), so importing the loader under the SAME name
# would shadow it -- `existing_artist_names_casefolded()` inside the
# `if existing_artist_names_casefolded is None:` branch would then try
# to call None (or raise UnboundLocalError), not the loader function.
from library.services.related_artists import existing_artist_names_casefolded as load_existing_artist_names_casefolded


def repick_cue_points_from_json(json_path, next_start_db, cue_in_db, cue_in_min):
    """Fast-path cue-point re-evaluation: read a track's already-computed
    envelope out of its waveform JSON and apply fresh thresholds without
    another ffmpeg decode. Returns `(next_start, cue_in, payload)` where
    the third value is the loaded JSON with `next_start`/`cue_in_seconds`
    replaced in place -- callers write it back to disk so the JSON stays
    consistent with the DB.

    Raises `LookupError` when the JSON pre-dates envelope persistence
    (older than analyze_tracks's envelope save) so the caller can decide
    to fall back to a full re-analyze. Any other IO/parse failure raises
    the underlying exception -- upstream should treat it as
    "skip this track, log, keep going."

    Extracted so both the /categories/repick-cue-points endpoint and a
    future CLI command can share exactly the same
    load-envelope-apply-thresholds pipeline -- if we drift the two
    apart, cue points from the two paths could disagree, which would be
    a very frustrating bug to track down."""
    import json as _json  # local alias; module imports it at top already
    payload = _json.loads(Path(json_path).read_text(encoding="utf-8"))
    next_start, cue_in = repick_from_payload(payload, next_start_db, cue_in_db, cue_in_min)
    payload["next_start"] = next_start
    payload["cue_in_seconds"] = cue_in
    return next_start, cue_in, payload


def repick_from_payload(payload, next_start_db, cue_in_db, cue_in_min):
    """Cue In and Next Start from an already-validated cache payload's envelope (see
    library.services.waveform_cache): ``(next_start, cue_in)``. LookupError without envelope."""
    times = payload.get("envelope_times")
    envelope_db = payload.get("envelope_db")
    if not times or not envelope_db:
        raise LookupError(
            "waveform cache has no envelope data -- "
            "pre-envelope-persistence file, needs a full re-analyze"
        )
    duration = payload.get("analysis_duration")
    next_start = detect_next_start(times, envelope_db, duration, next_start_db)
    cue_in = detect_cue_in(times, envelope_db, duration, cue_in_db, cue_in_min)
    return next_start, cue_in


# Lossless formats without native metadata support -- normalize to
# FLAC at analysis time so the library ends up single-format and
# tag-friendly. Kept in sync with fix_unknown_artists.TRANSCODE_EXTS.
TRANSCODE_TO_FLAC_EXTS = {".wav", ".aif", ".aiff"}


def transcode_lossless_to_flac(track_id, filepath, filename):
    """If `filepath` is a WAV/AIF/AIFF, transcode to sibling .flac,
    copy the current DB row's artist/title/album/genre/year onto the
    FLAC as tags, update the Track row's filepath/filename/format in
    place, and delete the original source. Returns the (new_filepath,
    new_filename) tuple whether or not we transcoded -- callers use
    the returned values for the rest of the analysis pass.

    On any failure returns None so the caller can skip the track and
    let a future analyze pass retry. Never raises. Called at the top
    of analyze_one_track so all three of its entry points (bulk
    analyze_tracks, sync_track_file, api_track_reanalyze) inherit
    the behavior -- fresh WAV uploads land as FLAC once the analyze
    timer catches them (~1 min after upload), same DB row, same track
    id, no dual-row weeding to do afterwards."""
    src = Path(filepath)
    if src.suffix.lower() not in TRANSCODE_TO_FLAC_EXTS:
        return (filepath, filename)

    dst = src.with_suffix(".flac")
    if dst.exists():
        print(f"  [transcode] track {track_id}: target FLAC already exists, "
              f"cannot swap: {dst}")
        return None

    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-i", str(src), "-c:a", "flac", "-compression_level", "5",
             str(dst)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"  [transcode] ffmpeg failed for track {track_id}: {exc}")
        # Clean up any partial output so a retry starts clean.
        if dst.exists():
            try:
                dst.unlink()
            except Exception:
                pass
        return None

    # Write tags into the new FLAC from the current DB row's fields.
    # `parse_tags` already ran at upload time, so the row is the
    # authoritative source; if an operator has since edited artist/
    # title in the DB, this call preserves those edits into the FLAC.
    try:
        from mutagen.flac import FLAC
        db_row = (Track.objects
                  .select_related("artist", "album", "genre")
                  .filter(id=track_id)
                  .first())
        if db_row:
            audio = FLAC(str(dst))
            audio["artist"] = db_row.artist.name
            audio["title"] = db_row.title
            if db_row.album:
                audio["album"] = db_row.album.title
            if db_row.genre:
                audio["genre"] = db_row.genre.name
            if db_row.year:
                audio["date"] = str(db_row.year)
            if db_row.track_number:
                audio["tracknumber"] = str(db_row.track_number)
            audio.save()
    except Exception as exc:
        print(f"  [transcode] tag write failed for track {track_id} "
              f"(non-fatal, DB still authoritative): {exc}")

    Track.objects.filter(id=track_id).update(
        filepath=str(dst), filename=dst.name, format="flac",
    )

    try:
        src.unlink()
    except Exception as exc:
        print(f"  [transcode] failed to delete source {src} "
              f"(non-fatal, disk cleanup): {exc}")

    print(f"  [transcode] track {track_id}: {src.name} -> {dst.name}")
    return (str(dst), dst.name)


def apply_category_thresholds(base_cfg_values, next_start_override, cue_in_override):
    """Overlay per-Category `next_start_threshold_db_override` and
    `cue_in_threshold_db_override` on top of the base AnalysisConfig
    tuple. Either override may be None (meaning "use the global"),
    in which case the base value passes through unchanged.

    Extracted as a standalone helper so all three callers of
    analyze_one_track (this command's bulk loop, sync_track_file, and
    api_track_reanalyze) resolve overrides the same way. Analyzer
    itself stays domain-pure: it takes threshold values and doesn't
    know or care where they came from.

    Returns a fresh tuple in the exact shape analyze_one_track already
    destructures (sample_rate, window_seconds, target_points,
    next_start_db, cue_in_db, cue_in_min)."""
    (sample_rate, window_seconds, target_points,
     next_start_db, cue_in_db, cue_in_min) = base_cfg_values
    if next_start_override is not None:
        next_start_db = float(next_start_override)
    if cue_in_override is not None:
        cue_in_db = float(cue_in_override)
    return (sample_rate, window_seconds, target_points,
            next_start_db, cue_in_db, cue_in_min)


def decode_audio_to_pcm(filepath, sample_rate):
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", str(filepath),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as exc:
        print(f"  ffmpeg failed for {filepath}: {exc}", file=sys.stderr)
        return b""


def decode_audio_to_pcm_stereo(filepath, sample_rate):
    """Same as decode_audio_to_pcm but keeps both channels (interleaved
    L/R s16le) instead of downmixing to mono -- used only for the
    optional stereo waveform display, never for cue-point detection
    (that stays on the mono path, unchanged). A genuinely mono source
    file just comes back with identical L/R here, which is the correct
    behavior, not a bug."""
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", str(filepath),
        "-ac", "2",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as exc:
        print(f"  ffmpeg (stereo) failed for {filepath}: {exc}", file=sys.stderr)
        return b""


def compute_stereo_waveform(raw_stereo, sample_rate, window_seconds, target_points):
    """Per-channel windowed-RMS waveform (L and R separately), linear-
    amplitude and peak-normalized against a peak SHARED across both
    channels -- a real L/R level imbalance shows up as one side visually
    smaller rather than getting normalized away. This is the sole
    waveform display data stored per track (see analyze_one_track) --
    independent decode/compute pass, never touches next_start/cue_in
    detection, which stays on the mono envelope below."""
    if not raw_stereo:
        return [], []

    frame_count = len(raw_stereo) // 4  # 2 channels * 2 bytes/sample
    if frame_count == 0:
        return [], []

    window_size = max(1, int(sample_rate * window_seconds))
    max_int16 = 32768.0

    left_db, right_db = [], []
    pos = 0
    while pos + window_size <= frame_count:
        window = raw_stereo[pos * 4: (pos + window_size) * 4]
        acc_l, acc_r = 0.0, 0.0
        for i in range(0, len(window), 4):
            l = struct.unpack_from("<h", window, i)[0]
            r = struct.unpack_from("<h", window, i + 2)[0]
            norm_l = l / max_int16
            norm_r = r / max_int16
            acc_l += norm_l * norm_l
            acc_r += norm_r * norm_r
        rms_l = math.sqrt(acc_l / window_size) if window_size > 0 else 0.0
        rms_r = math.sqrt(acc_r / window_size) if window_size > 0 else 0.0
        left_db.append(20.0 * math.log10(rms_l) if rms_l > 0.0 else -120.0)
        right_db.append(20.0 * math.log10(rms_r) if rms_r > 0.0 else -120.0)
        pos += window_size

    if not left_db:
        return [], []

    num_envelope = len(left_db)
    if num_envelope <= target_points:
        step = 1.0
        points = num_envelope
    else:
        step = num_envelope / float(target_points)
        points = target_points

    left_linear = [10.0 ** (db / 20.0) if db > -120.0 else 0.0 for db in left_db]
    right_linear = [10.0 ** (db / 20.0) if db > -120.0 else 0.0 for db in right_db]
    max_linear = max(max(left_linear, default=0.0), max(right_linear, default=0.0))

    def scale_linear(linear_envelope):
        out = []
        for i in range(points):
            start = int(i * step)
            end = int((i + 1) * step)
            if start >= num_envelope:
                break
            end = max(end, start + 1)
            end = min(end, num_envelope)
            peak = max(linear_envelope[start:end])
            level = int(round((peak / max_linear) * 255.0)) if max_linear > 0 else 0
            out.append(level)
        return out

    return scale_linear(left_linear), scale_linear(right_linear)


def compute_envelope(raw, sample_rate, window_seconds):
    """Windowed-RMS dB envelope of a mono-decoded track -- feeds
    next_start/cue_in detection only. No display waveform is produced
    here; that's compute_stereo_waveform's job now."""
    if not raw:
        return [], []

    sample_count = len(raw) // 2
    if sample_count == 0:
        return [], []

    window_size = max(1, int(sample_rate * window_seconds))
    max_int16 = 32768.0

    envelope_db = []
    times = []
    pos = 0

    while pos + window_size <= sample_count:
        window = raw[pos * 2 : (pos + window_size) * 2]
        acc = 0.0
        for i in range(0, len(window), 2):
            sample = struct.unpack_from("<h", window, i)[0]
            norm = sample / max_int16
            acc += norm * norm
        rms = math.sqrt(acc / window_size) if window_size > 0 else 0.0
        db = 20.0 * math.log10(rms) if rms > 0.0 else -120.0
        t = (pos + window_size / 2.0) / float(sample_rate)
        envelope_db.append(db)
        times.append(t)
        pos += window_size

    return times, envelope_db


def detect_next_start(times, envelope_db, duration, threshold_db):
    if not times or not envelope_db:
        return None

    last_loud = None
    for idx in range(len(envelope_db) - 1, -1, -1):
        if envelope_db[idx] > threshold_db:
            last_loud = idx
            break

    if last_loud is None:
        return float(duration) if duration is not None else None

    if last_loud + 1 < len(times):
        t = times[last_loud + 1]
    else:
        t = times[last_loud]

    if duration is not None and t > duration:
        t = float(duration)
    return float(t)


def detect_cue_in(times, envelope_db, duration, threshold_db, min_seconds):
    if not times or not envelope_db:
        return None

    first_loud = None
    for idx, db in enumerate(envelope_db):
        if db > threshold_db:
            first_loud = idx
            break

    if first_loud is None:
        return 0.0 if duration is not None else None

    t = times[first_loud]
    t = max(0.0, t)
    if duration is not None and t > duration:
        t = float(duration)
    if t < min_seconds:
        t = 0.0
    return float(t)


def get_waveforms_dir():
    from django.conf import settings as django_settings
    wave_dir = Path(getattr(django_settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"))
    wave_dir.mkdir(parents=True, exist_ok=True)
    return wave_dir


class AnalysisOutcome:
    """What one analysis did: truthy when the Track was analyzed (decode and envelope succeeded),
    with the cue-marker authority's result for the calculated markers (``markers``)."""

    def __init__(self, analyzed, markers=None, reason="", promotion=None):
        self.analyzed, self.markers, self.reason, self.promotion = analyzed, markers, reason, promotion

    def __bool__(self):
        return self.analyzed

    def __repr__(self):
        return f"AnalysisOutcome(analyzed={self.analyzed!r}, reason={self.reason!r})"


def analyze_one_track(row, cfg_values, wave_dir, force, existing_artist_names_casefolded=None, *,
                      media_replaced=False, source=cue_markers.SOURCE_ANALYSIS, requested_by=None,
                      override=(), override_confirmed=False, keep_previous_waveform=False):
    """Analyze exactly one track (waveform + cue points + related artists),
    writing results directly to the DB and a waveform JSON file. `row` is
    (track_id, filepath, filename, duration, title, artist_name,
    existing_related) -- the same shape as the bulk command's own
    `.values_list(...)` rows, so both the command's loop and a one-off
    caller (e.g. right after a fresh upload, see library/views.py's
    api_library_upload) share this one code path instead of the upload
    path needing its own copy of this logic. Returns True if analyzed,
    False if skipped (missing file or decode failure).

    `existing_artist_names_casefolded` gates extract_credited_artists's
    bare '&'/'and' split -- pass the bulk-loaded set when calling this
    in a loop (see this command's handle()); a single-track caller
    (api_track_reanalyze, sync_track_file) can leave it None and this
    loads it itself with one extra query, which is fine off a hot loop.

    Cue In and Next Start reach the Track only through the cue-marker authority, checked
    against the marker revision read here, before the (long) decode: a marker an operator
    edited meanwhile wins, and a protected (locked or historical) marker is preserved unless
    ``media_replaced`` (the caller just published new audio at this Track) or an operator's
    confirmed ``override``. Returns an AnalysisOutcome (truthy when analyzed).

    r0123 Package B: the decodes are bracketed by the file's source witness and followed by its
    decoded-audio identity; a file that changes while it is analyzed yields nothing (no markers,
    no cache). The markers are applied with that audio revision, then the version-2 cache is
    promoted through library.services.waveform_cache (lock, staged validation, atomic rename,
    the Track's markers *at promotion* as its snapshot). If promotion fails after the markers were
    applied, the database stays authoritative and the previous cache stays in place: it is either
    untrusted for the new audio (its source witness no longer matches) or a trusted cache of the
    same audio whose marker snapshot is merely stale -- detectable, never a basis for Repick of
    other audio, and repaired by Refresh Waveform Only or the next analysis."""
    (sample_rate, window_seconds, target_points,
     next_start_db, cue_in_db, cue_in_min) = cfg_values
    track_id, filepath, filename, duration, title, artist_name, existing_related = row
    basis = cue_markers.snapshot(track_id)
    if basis is None:
        return AnalysisOutcome(False, reason="Track not found")

    # Auto-transcode WAV/AIF/AIFF to FLAC before analysis. No-op for
    # already-FLAC/MP3/etc tracks. Failure returns None -- skip this
    # track and let the next analyze pass retry from a clean slate.
    swap = transcode_lossless_to_flac(track_id, filepath, filename)
    if swap is None:
        return AnalysisOutcome(False, reason="transcode failed")
    filepath, filename = swap
    fp = Path(filepath)

    if not fp.is_file():
        print(f"  Missing file for track {track_id}: {filepath}")
        return AnalysisOutcome(False, reason="missing file")
    started_ns = time.time_ns()
    witness_before = audio_identity.stat_source(fp)

    raw = decode_audio_to_pcm(fp, sample_rate)
    if not raw:
        return AnalysisOutcome(False, reason="decode failed")

    times, envelope_db = compute_envelope(raw, sample_rate, window_seconds)
    if not envelope_db:
        return AnalysisOutcome(False, reason="no envelope")

    analysis_duration = times[-1] if times else None
    effective_duration = analysis_duration if analysis_duration else (
        float(duration) if duration else None
    )

    next_start = detect_next_start(times, envelope_db, effective_duration, next_start_db)
    cue_in = detect_cue_in(times, envelope_db, effective_duration, cue_in_db, cue_in_min)

    # Stereo L/R linear waveform -- the sole display data stored, see
    # compute_stereo_waveform. Independent decode pass, best-effort: any
    # unexpected exception here is deliberately swallowed rather than
    # propagated, since this is display-only and must never take down
    # the mono analysis everything else (cue points, detection,
    # rotation) depends on.
    #
    # Move-during-analysis recovery (2026-08 hardening): the ffmpeg
    # stereo decode can legitimately return empty if the file was moved
    # between the mono decode above and this call -- api_track_update in
    # views.py does an atomic shutil.move + `track.filepath = new_path`
    # + `track.save()` when the operator recategorizes a track through
    # the UI, and the analyze timer running against a stale
    # `.values_list("filepath", ...)` snapshot from before the move will
    # try the old path and get ENOENT. When that happens, re-read the
    # Track's current filepath from the DB and retry once with the new
    # path. Without this recovery the mono cue-point work already
    # persisted (`next_start_seconds`, etc.) means the track is no
    # longer "unanalyzed" and won't be re-picked by the timer's default
    # filter -- leaving a permanently empty waveform display until a
    # manual reanalyze.
    samples_left, samples_right = [], []
    try:
        raw_stereo = decode_audio_to_pcm_stereo(fp, sample_rate)
        if not raw_stereo:
            current_filepath = (
                Track.objects.filter(id=track_id)
                .values_list("filepath", flat=True)
                .first()
            )
            if current_filepath and current_filepath != filepath:
                new_fp = Path(current_filepath)
                if new_fp.is_file():
                    print(
                        f"  Track {track_id} moved mid-analysis "
                        f"({filepath!r} -> {current_filepath!r}); "
                        f"retrying stereo decode with new path"
                    )
                    raw_stereo = decode_audio_to_pcm_stereo(new_fp, sample_rate)
                    if raw_stereo:
                        # Adopt the new path so the payload write and
                        # any subsequent DB writes below reference
                        # reality; the mono-derived cue points are
                        # unaffected (same audio content, unchanged).
                        fp = new_fp
                        filepath = current_filepath
                        filename = new_fp.name
                        emit_event(
                            category="library", level="info",
                            title="Track waveform recovered after mid-analysis move",
                            detail={
                                "track_id": track_id,
                                "old_filepath": row[1],
                                "new_filepath": current_filepath,
                            },
                            dedupe_key=f"library|analyze-waveform-recover|{track_id}",
                        )
        if raw_stereo:
            samples_left, samples_right = compute_stereo_waveform(
                raw_stereo, sample_rate, window_seconds, target_points,
            )
    except Exception as exc:
        print(f"  Stereo waveform failed for track {track_id} (non-fatal): {exc}")

    identity, identity_problem = audio_identity.compute_or_none(fp)
    witness = audio_identity.stat_source(fp)
    if not audio_identity.same_content_witness(witness_before, witness):
        print(f"  Track {track_id}: the audio file changed while it was analyzed; nothing applied")
        return AnalysisOutcome(False, reason="source changed during analysis")
    if identity is not None and analysis_duration and not waveform_cache.identity_consistent(
            identity, analysis_duration, window_seconds):
        identity, identity_problem = None, "the identified audio's length disagrees with the decoded envelope"
    if identity is None:
        print(f"  Track {track_id}: audio identity unavailable ({identity_problem}); the cache will not be trusted")

    # Deliberately no filename-based extraction here -- once a track
    # has human-readable title/artist (either genuinely tagged, or
    # already backfilled by the upload/import fallback-metadata path;
    # see related_artists.resolve_fallback_metadata), those DB fields
    # are the only extraction source. Inspecting the filename here too
    # would be a second, potentially-conflicting source of truth for a
    # normally-tagged track.
    #
    # force controls whether we RE-RUN discovery, never whether a
    # discovery is allowed to REPLACE what's already there --
    # merge_related_artists only ever appends, so a forced reanalyze
    # (or the /track/<pk>/ Reanalyze Track action, which calls this
    # with force=True) can never erase a manually-entered related
    # artist, even one this extractor wouldn't have found on its own.
    if force or not existing_related:
        if existing_artist_names_casefolded is None:
            existing_artist_names_casefolded = load_existing_artist_names_casefolded()
        discovered = extract_credited_artists(
            artist_name, title, artist_name,
            existing_artist_names_casefolded=existing_artist_names_casefolded,
        )
        related = merge_related_artists(existing_related, discovered, primary_artist_name=artist_name)
    else:
        related = existing_related

    if effective_duration and effective_duration != duration:
        Track.objects.filter(id=track_id).update(duration_seconds=effective_duration)

    # A detector that found nothing writes nothing: a failed detection never nulls a marker.
    calculated = {name: value for name, value in (("cue_in_seconds", cue_in), ("next_start_seconds", next_start))
                  if value is not None}
    marker_result = cue_markers.apply_calculation(
        track_id, calculated, basis_revision=basis["cue_marker_revision"], source=source,
        requested_by=requested_by, override=override, override_confirmed=override_confirmed,
        media_replaced=media_replaced, audio_revision=audio_identity.revision(identity), duration=effective_duration,
    )
    if not marker_result.ok:
        print(f"  Track {track_id}: calculated cue markers not applied: "
              f"{marker_result.conflict or marker_result.error}")
    # Version-2 cache (library.services.waveform_cache): the stereo display samples, the mono
    # envelope (detect_next_start/detect_cue_in walk it; Repick re-picks from it without another
    # decode once the cache is trusted), the audio identity and source witness, and -- added at
    # promotion -- the Track's markers as they stand then, never values the authority did not apply.
    payload = waveform_cache.build_payload(
        track_id, identity=identity, source=witness, analysis={
            "sample_rate": int(sample_rate), "window_seconds": float(window_seconds),
            "waveform_points": int(target_points)},
        writer="analysis", started_ns=started_ns, analysis_duration=analysis_duration, times=times,
        envelope_db=envelope_db, samples_left=samples_left, samples_right=samples_right)
    update_fields = {"related_artists": related}
    promotion = None
    try:
        promotion = waveform_cache.promote(
            track_id, payload, wave_dir, expected_source=witness,
            keep_previous=keep_previous_waveform,
        )
        update_fields["waveform_path"] = str(promotion.path)
    except (waveform_cache.PromotionRefused, waveform_cache.WaveformBusy, OSError) as exc:
        print(f"  Waveform cache for track {track_id} not replaced (the previous one is kept): {exc}")

    try:
        Track.objects.filter(id=track_id).update(**update_fields)
        # The request this run served is done -- unless a newer one arrived while it ran.
        Track.objects.filter(id=track_id, analysis_requested_at=basis["analysis_requested_at"]).exclude(
            analysis_requested_at__isnull=True).update(analysis_requested_at=None)
    except BaseException:
        if keep_previous_waveform and promotion is not None:
            promotion.undo()
        raise
    return AnalysisOutcome(True, markers=marker_result, promotion=promotion)


class Command(BaseCommand):
    help = "Analyze tracks: compute waveforms, detect cue-in and next-start points, extract related artists."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-analyze all tracks, even those already analyzed.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limit the number of tracks to analyze (0 = no limit).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        limit = options["limit"]

        cfg = AnalysisConfig.load()
        cfg_values = (
            cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
            cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
        )

        wave_dir = get_waveforms_dir()

        # Loaded once for the whole run, not once per track -- see
        # extract_credited_artists's bare '&'/'and' gate.
        existing_artist_names_casefolded = load_existing_artist_names_casefolded()

        qs = Track.objects.filter(filepath__isnull=False)
        if not force:
            # An explicit request, or a Track without a Next Start (never analyzed, or nothing
            # detected yet) -- analyzing it cannot destroy a marker it does not have.
            qs = qs.filter(Q(analysis_requested_at__isnull=False) | Q(next_start_seconds__isnull=True))
        qs = qs.order_by("id")
        if limit:
            qs = qs[:limit]

        # Fetch category threshold overrides in the same query so we
        # don't need a per-track Category round-trip inside the loop.
        # Tracks with no category get None/None for the two extra
        # fields and fall through to the global AnalysisConfig
        # thresholds via apply_category_thresholds() below.
        tracks = list(qs.values_list(
            "id", "filepath", "filename", "duration_seconds",
            "title", "artist__name", "related_artists",
            "category__next_start_threshold_db_override",
            "category__cue_in_threshold_db_override",
        ))

        total = len(tracks)
        self.stdout.write(f"Found {total} tracks to analyze.")

        analyzed = 0
        skipped = 0

        for full_row in tracks:
            # Split the row: analyze_one_track takes the original 7-tuple
            # (id, filepath, filename, duration, title, artist_name,
            # existing_related); the trailing two entries are the
            # category-level overrides we tacked onto values_list above.
            analysis_row = full_row[:7]
            ns_override, ci_override = full_row[7], full_row[8]
            per_track_cfg = apply_category_thresholds(cfg_values, ns_override, ci_override)
            if analyze_one_track(analysis_row, per_track_cfg, wave_dir, force,
                                  existing_artist_names_casefolded=existing_artist_names_casefolded):
                analyzed += 1
            else:
                skipped += 1

            if (analyzed + skipped) % 50 == 0:
                self.stdout.write(
                    f"  ... analyzed={analyzed} skipped={skipped} "
                    f"remaining={total - analyzed - skipped}"
                )

        self.stdout.write(
            f"\nDone. Analyzed: {analyzed}, skipped: {skipped}"
        )
