"""Hidden Track Detection -- Phase 1 (diagnostic only).

Scans a track's ALREADY-PERSISTED waveform/envelope JSON (written by
library.management.commands.analyze_tracks.analyze_one_track, read the
same way library.management.commands.analyze_tracks.
repick_cue_points_from_json already does) for the classic CD-rip
"hidden track" shape:

    credible audio -> qualifying internal quiet gap -> credible
    sustained audio return

This module never decodes audio (no ffmpeg/Mutagen/GStreamer/
subprocess) and never writes to the database or filesystem -- it is a
pure, read-only analysis pass over data that already exists. See
library/services/related_artists.py's module docstring for the
project's general "shared service, not embedded in a view" convention
this follows.

Waveform JSON schema (confirmed by reading analyze_tracks.py, not
guessed):
    {
      "track_id": int,
      "samples_left": [...], "samples_right": [...],   # coarse STEREO
          # DISPLAY waveform (compute_stereo_waveform, ~target_points
          # entries total) -- NOT used here, far too low-resolution
          # for a 20s-gap detector.
      "next_start": float or null,
      "cue_in_seconds": float or null,
      "analysis_duration": float or null,               # ~= envelope_times[-1]
      "envelope_times": [float, ...],                    # window-CENTER
          # timestamps in seconds, ascending, one per envelope_db entry
      "envelope_db": [float, ...],                       # windowed-RMS
          # dBFS per window at AnalysisConfig.analysis_window_seconds
          # resolution (default 0.05s -- NOT 1 second; a track's
          # spacing is whatever the operator's analysis config was at
          # analyze time and must never be assumed constant across
          # tracks). -120.0 is the floor value compute_envelope() emits
          # for true digital silence (rms == 0), a real, expected value
          # here -- not a sentinel error.
    }
`envelope_times`/`envelope_db` are the FULL-RESOLUTION mono analysis
envelope (compute_envelope) -- the one detect_next_start/detect_cue_in
already walk for cue-point detection, and the one this detector uses.

Time math throughout uses envelope_times[i] (each window's CENTER)
directly for span durations (times[b] - times[a]) rather than
re-deriving window widths from a fixed window_seconds -- window widths
are a few hundredths of a second, negligible against the
multi-second thresholds this detector cares about, and using actual
timestamps end to end keeps the detector correct regardless of
spacing irregularities (a differently-configured AnalysisConfig at
different points in the library's history, a hypothetical irregular
final window, etc).
"""
import json
import math
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import List, Optional


# =====================================================================
# Operator-editable defaults (surfaced on the report form; the operator
# can override every one of these per scan). See HiddenTrackDetectionForm.
# =====================================================================
DEFAULT_SILENCE_THRESHOLD_DB = -50.0
DEFAULT_MIN_SILENCE_SECONDS = 20.0
DEFAULT_RESUMED_AUDIO_THRESHOLD_DB = -35.0
DEFAULT_MIN_RESUMED_AUDIO_SECONDS = 15.0
DEFAULT_REQUIRED_ACTIVE_RATIO_PERCENT = 60.0
DEFAULT_MIN_POSITION_SECONDS = 60.0

# Sane bounds for form validation -- generous enough to never get in a
# real operator's way, tight enough that a typo (e.g. an extra zero)
# can't make a scan run for an unreasonable amount of internal work.
MAX_MIN_SILENCE_SECONDS = 600.0
MAX_MIN_RESUMED_AUDIO_SECONDS = 600.0
MAX_MIN_POSITION_SECONDS = 3600.0
MIN_DB_BOUND = -180.0
MAX_DB_BOUND = 0.0


# =====================================================================
# Internal tolerance constants -- NOT yet operator-editable (see module
# docstring / task scope: "may remain constants in this first phase").
# Named and isolated here specifically so a later phase can promote any
# of these to form fields without touching the algorithm.
# =====================================================================

# Within a candidate quiet gap, at least this fraction of envelope
# windows must be at or below the silence threshold. Real CD rips carry
# dither/low-level noise/one-window spikes/analog residue -- requiring
# a perfect 100% would reject nearly every real hidden-track gap.
# 0.92 sits inside the task's suggested 90-95% band.
MIN_SILENT_WINDOW_RATIO = 0.92

# A single contiguous run of above-threshold windows inside an
# otherwise-quiet span longer than this many seconds is NOT tolerated
# as noise -- it ends (splits) the candidate gap outright, rather than
# being folded into the ratio above. This is what stops the tolerance
# mechanism from ever merging two ordinary song sections that happen to
# be separated only by a brief (non-qualifying) pause: a real pause
# between two loud sections is itself far too short to reach
# min_silence_seconds on its own, and a LOUD section lasting more than
# this many seconds always splits the gap, so tolerance can only ever
# smooth over brief noise WITHIN a genuinely long quiet span, never
# stitch together two separate quiet spans across real program audio.
MAX_SINGLE_EXCURSION_SECONDS = 1.5

# Minimum total seconds of credible (>= resumed_audio_threshold_db)
# audio required somewhere before a candidate gap's start for that gap
# to be considered at all. Prevents a file with a long silent/malformed
# header from being flagged as if the "silence" were a hidden-track gap
# with no real indexed song preceding it.
MIN_PRE_GAP_ACTIVE_SECONDS = 20.0

# Hard cap on how many candidate resumed-audio crossings a SINGLE CALL
# to detect_hidden_track_candidates will search through in total,
# shared across every qualifying quiet gap in the track -- NOT reset
# per gap. This is load-bearing, not just defensive: a track can have
# many disjoint qualifying quiet spans (e.g. a long excursion between
# two would-be-tolerated blips forces a hard split -- see
# MAX_SINGLE_EXCURSION_SECONDS), and each span's own search-forward
# loop is, by itself, linear in the remaining track length. Without a
# GLOBAL budget, a track with many such spans could trigger that
# linear search once per span, which sums to quadratic overall (a real
# regression measured during development: 800 disjoint spans over a
# ~5-minute synthetic envelope took over 20 seconds -- confirmed
# roughly quadratic scaling before this cap and the _last_resumed_
# crossing_idx short-circuit below were added). A budget this size
# never meaningfully constrains a real recording (a genuine hidden
# track needs at most a handful of attempts to either succeed or be
# abandoned); it only bounds worst-case cost for a pathological or
# adversarially-shaped envelope, where some late-track candidate may
# go unexamined once the budget is spent -- an accepted, documented
# trade-off given this is a diagnostic tool where a bounded, wholly
# deterministic scan matters more than exhaustively re-trying every
# possible offset in an unrealistic input.
MAX_RESUMED_SEARCH_ATTEMPTS_TOTAL = 500

# "High" detection-strength thresholds -- see classify_strength().
HIGH_CONFIDENCE_MIN_SILENCE_SECONDS = 30.0
HIGH_CONFIDENCE_MIN_RESUMED_SECONDS = 30.0
# Resumed audio's representative (median) level must clear the gap's
# own median level by at least this many dB for "strong separation."
HIGH_CONFIDENCE_MIN_SEPARATION_DB = 10.0

STRENGTH_HIGH = "high"
STRENGTH_MEDIUM = "medium"


# =====================================================================
# Waveform JSON loading + validation
# =====================================================================

class WaveformSkipReason:
    """String constants, not an enum, so they serialize trivially into
    the scan summary dict / JSON response without extra handling."""
    NO_PATH = "no_waveform_path"
    FILE_MISSING = "file_missing"
    INVALID_JSON = "invalid_json"
    MISSING_ENVELOPE = "missing_envelope_data"
    LENGTH_MISMATCH = "length_mismatch"
    NON_FINITE = "non_finite_values"
    INSUFFICIENT_DATA = "insufficient_duration_or_resolution"


SKIP_REASON_LABELS = {
    WaveformSkipReason.NO_PATH: "No waveform path on the Track row",
    WaveformSkipReason.FILE_MISSING: "Waveform JSON file not found on disk",
    WaveformSkipReason.INVALID_JSON: "Waveform JSON could not be parsed",
    WaveformSkipReason.MISSING_ENVELOPE: "No envelope data in waveform JSON (pre-envelope-persistence file)",
    WaveformSkipReason.LENGTH_MISMATCH: "envelope_times/envelope_db length mismatch",
    WaveformSkipReason.NON_FINITE: "Non-finite (NaN/Infinity) value in envelope data",
    WaveformSkipReason.INSUFFICIENT_DATA: "Insufficient duration or time resolution to analyze",
}


@dataclass
class EnvelopeLoadResult:
    ok: bool
    times: List[float] = field(default_factory=list)
    envelope_db: List[float] = field(default_factory=list)
    duration: Optional[float] = None
    skip_reason: Optional[str] = None


def _all_finite_numbers(values):
    for v in values:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
        if not math.isfinite(v):
            return False
    return True


def load_envelope(waveform_path):
    """Read + validate one track's persisted waveform JSON. Read-only:
    never writes, never decodes audio, never touches anything but the
    one JSON file named by `waveform_path`. Returns an
    EnvelopeLoadResult -- always check `.ok` before touching
    `.times`/`.envelope_db`; a non-ok result carries a categorized
    `.skip_reason` (see WaveformSkipReason) instead of raising, so a
    caller scanning many tracks can just log-and-continue.

    Deliberately takes the path as a plain string derived by the
    CALLER from a Track row (see scan_for_hidden_tracks) -- this
    function itself does no DB lookup and has no notion of "which
    track," keeping it a pure, easily testable file-in/data-out
    function. Callers must never accept a path from an HTTP request."""
    if not waveform_path:
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.NO_PATH)

    path = Path(waveform_path)
    if not path.is_file():
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.FILE_MISSING)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.INVALID_JSON)

    if not isinstance(payload, dict):
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.INVALID_JSON)

    times = payload.get("envelope_times")
    envelope_db = payload.get("envelope_db")
    if not times or not envelope_db:
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.MISSING_ENVELOPE)
    if not isinstance(times, list) or not isinstance(envelope_db, list):
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.MISSING_ENVELOPE)
    if len(times) != len(envelope_db):
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.LENGTH_MISMATCH)
    if not _all_finite_numbers(times) or not _all_finite_numbers(envelope_db):
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.NON_FINITE)

    duration = payload.get("analysis_duration")
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        duration = times[-1] if times else None

    # Need at least a couple of windows and a positive duration to say
    # anything meaningful -- a single-window or zero-duration "track"
    # can't contain a qualifying gap under any settings.
    if len(times) < 2 or not duration or duration <= 0:
        return EnvelopeLoadResult(False, skip_reason=WaveformSkipReason.INSUFFICIENT_DATA)

    return EnvelopeLoadResult(True, times=times, envelope_db=envelope_db, duration=float(duration))


# =====================================================================
# Core detector -- pure function, no DB/filesystem access.
# =====================================================================

@dataclass
class HiddenTrackCandidate:
    """One qualifying internal gap within one track. Never persisted --
    constructed fresh per scan from live envelope data and returned
    straight to the view/template."""
    silence_start: float
    silence_end: float
    silence_duration: float
    resumed_start: float
    resumed_duration: float
    active_ratio: float          # 0..1, over the final (extended) resumed span
    representative_level_db: float  # median dB over the resumed span
    strength: str                # STRENGTH_HIGH / STRENGTH_MEDIUM


def _trailing_width(times, idx):
    """Estimated width (seconds) the window at `idx` represents, taken
    from local spacing -- `times[idx+1] - times[idx]` when a following
    window exists, else the preceding gap, else 0.0 for a single-window
    "track" (already filtered out by load_envelope's minimum-length
    check in real use, but kept safe here for direct unit-test calls)."""
    if idx + 1 < len(times):
        return times[idx + 1] - times[idx]
    if idx > 0:
        return times[idx] - times[idx - 1]
    return 0.0


def _span_extent(times, start_idx, end_idx):
    """Duration (seconds) a run of windows [start_idx..end_idx] actually
    covers. Each envelope window represents an INTERVAL of audio
    (compute_envelope's window_seconds), not a zero-width instant at its
    center timestamp -- so measuring a span as the bare center-to-center
    delta (times[end] - times[start]) systematically under-counts by
    about one window's width. Adding the trailing window's own width
    back in gives the true covered extent: a run of N windows at S
    seconds of spacing reads as very close to N*S, not (N-1)*S. This
    matters right at an operator-configured threshold boundary (e.g.
    exactly `min_silence_seconds` of real quiet audio must still
    qualify, not fall just short of it by one window's width) even
    though the correction is otherwise negligible against the
    multi-second thresholds this detector works with."""
    return (times[end_idx] - times[start_idx]) + _trailing_width(times, end_idx)


def _runs(flags):
    """Yield (start_idx, end_idx_inclusive, value) for each maximal run
    of equal booleans in `flags`."""
    if not flags:
        return
    start = 0
    current = flags[0]
    for i in range(1, len(flags)):
        if flags[i] != current:
            yield start, i - 1, current
            start = i
            current = flags[i]
    yield start, len(flags) - 1, current


def _find_quiet_spans(times, quiet_flags):
    """Merge quiet runs across short (<= MAX_SINGLE_EXCURSION_SECONDS)
    excursion runs into candidate spans, per the module docstring's
    tolerance policy. A long excursion is a hard split point -- never
    folded into a span's ratio. Returns a list of (start_idx, end_idx)
    inclusive index pairs, each a candidate "quiet span" BEFORE the
    MIN_SILENT_WINDOW_RATIO/min_silence_seconds/min_position checks are
    applied (those are the caller's job)."""
    run_list = list(_runs(quiet_flags))
    spans = []
    i = 0
    n_runs = len(run_list)
    while i < n_runs:
        start_idx, end_idx, is_quiet = run_list[i]
        if not is_quiet:
            i += 1
            continue
        span_start, span_end = start_idx, end_idx
        j = i + 1
        while j + 1 < n_runs:
            exc_start, exc_end, exc_is_quiet = run_list[j]
            if exc_is_quiet:
                break  # shouldn't happen (runs alternate), defensive
            exc_duration = _span_extent(times, exc_start, exc_end)
            if exc_duration > MAX_SINGLE_EXCURSION_SECONDS:
                break  # long excursion: hard split, stop extending this span
            # Short excursion -- fold it and the quiet run after it in,
            # then keep looking for another mergeable pair.
            next_quiet_start, next_quiet_end, next_is_quiet = run_list[j + 1]
            if not next_is_quiet:
                break  # defensive; shouldn't happen
            span_end = next_quiet_end
            j += 2
        spans.append((span_start, span_end))
        i = j if j > i + 1 else i + 1
    return spans


def _cumulative_active_seconds(times, envelope_db, resumed_audio_threshold_db):
    """Precompute, once (a single O(n) pass), a prefix-sum array where
    result[i] = total seconds of windows in [0, i) at/above the
    resumed-audio threshold -- the same "credible audio" bar used to
    validate a post-gap return, applied symmetrically to what came
    before a gap (see MIN_PRE_GAP_ACTIVE_SECONDS). Not a contiguous-run
    measure -- this only needs to establish that real program audio
    existed before a gap, not that it was unbroken -- so it simply sums
    each qualifying window's own estimated width (_trailing_width).

    A track can have many gaps (a long excursion forces a hard split --
    see _find_quiet_spans), and each one needs its own "how much
    credible audio came before this gap's start" answer. Answering that
    by rescanning from position 0 for every single gap is exactly the
    kind of per-gap linear-rescan that, summed across many gaps in a
    long track, becomes quadratic overall -- the same failure mode the
    resumed-audio search loop's shared attempt budget exists to avoid
    (see MAX_RESUMED_SEARCH_ATTEMPTS_TOTAL). A prefix sum answers every
    gap's query in O(1) off of one shared O(n) precompute instead."""
    n = len(times)
    cumulative = [0.0] * (n + 1)
    for i in range(n):
        width = _trailing_width(times, i) if envelope_db[i] >= resumed_audio_threshold_db else 0.0
        cumulative[i + 1] = cumulative[i] + width
    return cumulative


def classify_strength(silence_duration, resumed_duration, gap_median_db, resumed_level_db):
    """High: silence >= 30s AND resumed >= 30s AND resumed level clears
    the gap's own median level by >= HIGH_CONFIDENCE_MIN_SEPARATION_DB.
    Medium: meets the submitted minimums (the caller only calls this
    for an already-qualifying candidate) but not the stronger bar
    above. This is a detection-STRENGTH label, not a certainty claim --
    see the module/report UI copy."""
    separation = resumed_level_db - gap_median_db
    if (
        silence_duration >= HIGH_CONFIDENCE_MIN_SILENCE_SECONDS
        and resumed_duration >= HIGH_CONFIDENCE_MIN_RESUMED_SECONDS
        and separation >= HIGH_CONFIDENCE_MIN_SEPARATION_DB
    ):
        return STRENGTH_HIGH
    return STRENGTH_MEDIUM


def detect_hidden_track_candidates(
    times,
    envelope_db,
    *,
    silence_threshold_db,
    min_silence_seconds,
    resumed_audio_threshold_db,
    min_resumed_audio_seconds,
    required_active_ratio,
    min_position_seconds,
):
    """Pure detector: find every qualifying
    "credible audio -> quiet gap -> credible sustained return" pattern
    in one track's envelope. No DB/filesystem access; safe to call
    directly in a unit test with hand-built lists.

    `required_active_ratio` is a FRACTION in [0, 1] (the caller/view
    layer converts the operator-facing percentage before calling this).

    Returns a list of HiddenTrackCandidate, sorted strongest-first per
    the documented ranking rule (see module docstring / final report):
      1. Longer silent-gap duration
      2. Longer sustained post-gap audio duration
      3. Higher representative (median) resumed-audio level
      4. Later gap position in the track
    Empty list means no qualifying candidate was found -- this is the
    overwhelmingly common, correct result for an ordinary track."""
    n = len(times)
    if n == 0 or len(envelope_db) != n or n < 2:
        return []

    quiet_flags = [db <= silence_threshold_db for db in envelope_db]
    spans = _find_quiet_spans(times, quiet_flags)

    # O(1)-per-gap short circuit, computed once (a single O(n) pass)
    # up front: the rightmost index anywhere in the track where a
    # resumed-audio-credible crossing occurs, or None if there simply
    # isn't one at all. Any gap ending at or after this index can be
    # rejected immediately -- no crossing can possibly follow it -- so
    # the common "silence rides out to EOF" case never touches the
    # per-gap search loop below, regardless of how many separate
    # qualifying spans the track has. This is a pure optimization (it
    # can never change which candidates are found), not part of the
    # attempt budget.
    last_resumed_crossing_idx = None
    for i in range(n - 1, -1, -1):
        if envelope_db[i] >= resumed_audio_threshold_db:
            last_resumed_crossing_idx = i
            break

    # Also precomputed once, O(n) total, and reused by every gap below
    # via an O(1) lookup -- see _cumulative_active_seconds' own
    # docstring for why re-scanning from position 0 once per gap would
    # otherwise be a second, separate way this function could go
    # quadratic on a track with many gaps.
    cumulative_active_seconds = _cumulative_active_seconds(times, envelope_db, resumed_audio_threshold_db)

    candidates = []
    # Shared across EVERY gap below -- see MAX_RESUMED_SEARCH_ATTEMPTS_
    # TOTAL's docstring for why this must be a per-CALL budget, not
    # reset per gap: a track can have many disjoint qualifying spans
    # (a long excursion forces a hard split -- see _find_quiet_spans),
    # and without a shared cap, each span's own linear search-forward
    # would repeat the same work, summing to quadratic overall.
    resumed_search_attempts = 0
    for start_idx, end_idx in spans:
        span_duration = _span_extent(times, start_idx, end_idx)
        if span_duration < min_silence_seconds:
            continue
        if times[start_idx] < min_position_seconds:
            continue

        window_count = end_idx - start_idx + 1
        quiet_count = sum(1 for i in range(start_idx, end_idx + 1) if quiet_flags[i])
        if window_count == 0 or (quiet_count / window_count) < MIN_SILENT_WINDOW_RATIO:
            continue

        pre_gap_active = cumulative_active_seconds[start_idx]
        if pre_gap_active < MIN_PRE_GAP_ACTIVE_SECONDS:
            continue

        if last_resumed_crossing_idx is None or last_resumed_crossing_idx <= end_idx:
            continue  # nothing credible can follow this gap -- the fade-to-EOF case

        # Search forward for a credible SUSTAINED return, retrying past
        # any brief false start rather than giving up on the whole gap
        # the first time a crossing fails validation. Real hidden-track
        # gaps sometimes contain an isolated click/noise burst/false
        # start before the actual hidden song settles in:
        #   silence -> click -> silence -> hidden song
        # A click's own validation window has a LOW active_ratio (it's
        # mostly still quiet immediately around one brief spike) and is
        # rejected on its own merits below, exactly the same way an
        # ordinary section that merely falls short of the configured
        # required_active_ratio would be (e.g. 55% active against a 60%
        # requirement) -- there is no separate "is this a click"
        # detection; both simply fail the same active-ratio check and
        # the search moves on to look for a window that DOES clear it.
        # Neither a rejected click nor a rejected under-ratio section
        # is ever folded back into "silence" for this gap -- start_idx/
        # end_idx (and therefore silence_start/silence_end/
        # silence_duration below) are fixed once, before this search
        # begins, and are never adjusted by what happens here.
        #
        # Each attempt's own validation window [candidate_r0..
        # candidate_j] is fully consumed before the next search begins
        # at candidate_j + 1 -- no window is ever re-examined by a
        # later attempt WITHIN one gap. But a track can have many
        # disjoint gaps (see _find_quiet_spans' hard-split rule), and
        # without a budget SHARED across all of them, each gap's own
        # search-to-EOF repeats the same tail-of-track scan the next
        # gap will also have to do -- see MAX_RESUMED_SEARCH_ATTEMPTS_
        # TOTAL's docstring for why resumed_search_attempts is a
        # per-CALL counter, not reset here per gap.
        r0 = j = None
        search_from = end_idx + 1
        while search_from < n and resumed_search_attempts < MAX_RESUMED_SEARCH_ATTEMPTS_TOTAL:
            resumed_search_attempts += 1
            candidate_r0 = None
            for i in range(search_from, n):
                if envelope_db[i] >= resumed_audio_threshold_db:
                    candidate_r0 = i
                    break
            if candidate_r0 is None:
                break  # no further crossings before EOF -- silence rides out to EOF, correctly rejected

            candidate_j = candidate_r0
            while candidate_j + 1 < n and _span_extent(times, candidate_r0, candidate_j) < min_resumed_audio_seconds:
                candidate_j += 1
            if _span_extent(times, candidate_r0, candidate_j) < min_resumed_audio_seconds:
                # Not enough track remains from this crossing to prove
                # it out -- and strictly less remains from any LATER
                # crossing -- so no further attempt in this gap could
                # succeed either. Stop searching entirely.
                break

            active_count = sum(
                1 for i in range(candidate_r0, candidate_j + 1)
                if envelope_db[i] >= resumed_audio_threshold_db
            )
            total_count = candidate_j - candidate_r0 + 1
            if total_count > 0 and (active_count / total_count) >= required_active_ratio:
                r0, j = candidate_r0, candidate_j
                break  # found a credible sustained return

            # This crossing failed validation (click / noise burst /
            # a section that didn't clear the required ratio) --
            # resume searching strictly after its own validation
            # window; never re-examine these same windows again.
            search_from = candidate_j + 1

        if r0 is None:
            continue  # no credible sustained return anywhere in this gap

        # Extend the validated span forward while the cumulative active
        # ratio holds, for a more useful "approximate duration" than
        # the bare minimum window alone.
        end2 = j
        while end2 + 1 < n:
            candidate_total = total_count + 1
            candidate_active = active_count + (1 if envelope_db[end2 + 1] >= resumed_audio_threshold_db else 0)
            if (candidate_active / candidate_total) < required_active_ratio:
                break
            end2 += 1
            total_count = candidate_total
            active_count = candidate_active

        resumed_duration = _span_extent(times, r0, end2)
        final_active_ratio = active_count / total_count if total_count else 0.0
        resumed_level = median(envelope_db[r0:end2 + 1])
        gap_level = median(envelope_db[start_idx:end_idx + 1])
        strength = classify_strength(span_duration, resumed_duration, gap_level, resumed_level)

        candidates.append(HiddenTrackCandidate(
            silence_start=times[start_idx],
            silence_end=times[end_idx],
            silence_duration=span_duration,
            resumed_start=times[r0],
            resumed_duration=resumed_duration,
            active_ratio=final_active_ratio,
            representative_level_db=resumed_level,
            strength=strength,
        ))

    candidates.sort(key=lambda c: (
        -c.silence_duration,
        -c.resumed_duration,
        -c.representative_level_db,
        -c.silence_start,
    ))
    return candidates


# =====================================================================
# Scan orchestration -- the only layer that touches the DB/filesystem.
# =====================================================================

@dataclass
class DetectionSettings:
    """Everything the operator can edit on the report form. Percent is
    kept separate from the fraction the pure detector wants
    (`required_active_ratio`) so the form/UI can work in the more
    natural 0-100 unit while the detector's own signature stays a
    plain 0-1 fraction."""
    silence_threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB
    min_silence_seconds: float = DEFAULT_MIN_SILENCE_SECONDS
    resumed_audio_threshold_db: float = DEFAULT_RESUMED_AUDIO_THRESHOLD_DB
    min_resumed_audio_seconds: float = DEFAULT_MIN_RESUMED_AUDIO_SECONDS
    required_active_ratio_percent: float = DEFAULT_REQUIRED_ACTIVE_RATIO_PERCENT
    min_position_seconds: float = DEFAULT_MIN_POSITION_SECONDS

    @property
    def required_active_ratio(self):
        return self.required_active_ratio_percent / 100.0


# Bounded iteration chunk size -- .iterator(chunk_size=...) so a scan
# across the whole library never materializes the full queryset (or
# its waveform JSON) in memory at once. Independent of DB page size
# elsewhere in the project; picked to keep each round-trip's result set
# small without turning a big scan into thousands of tiny queries.
SCAN_CHUNK_SIZE = 500


def _process_track_rows(rows, settings):
    """Shared core: run the detector over an already-fetched iterable
    of (track_id, title, artist_name, album_title, duration_seconds,
    waveform_path) row tuples. No DB/queryset access of its own -- the
    caller (scan_for_hidden_tracks or scan_for_hidden_tracks_batch)
    owns fetching rows, so this function works identically whether
    those rows are the complete filtered queryset or one bounded
    pk-cursor batch of it. A single track's malformed/missing waveform,
    or an unexpected exception from the detector itself, is caught and
    counted -- never aborts the rest of the rows.

    Returns (results, considered, scanned, skipped_counts) -- unsorted
    and untimed; callers assemble the final summary dict and apply the
    strongest-first sort themselves (batch callers merge across
    batches before doing either)."""
    results = []
    considered = 0
    scanned = 0
    skipped_counts = {}

    for track_id, title, artist_name, album_title, duration_seconds, waveform_path in rows:
        considered += 1
        load_result = load_envelope(waveform_path)
        if not load_result.ok:
            skipped_counts[load_result.skip_reason] = skipped_counts.get(load_result.skip_reason, 0) + 1
            continue
        scanned += 1

        try:
            candidates = detect_hidden_track_candidates(
                load_result.times, load_result.envelope_db,
                silence_threshold_db=settings.silence_threshold_db,
                min_silence_seconds=settings.min_silence_seconds,
                resumed_audio_threshold_db=settings.resumed_audio_threshold_db,
                min_resumed_audio_seconds=settings.min_resumed_audio_seconds,
                required_active_ratio=settings.required_active_ratio,
                min_position_seconds=settings.min_position_seconds,
            )
        except Exception as exc:
            # Defense in depth against any per-track oddity the loader
            # itself didn't already categorize -- never let one track
            # abort the batch/scan. Logged operator-side only; never
            # surfaced to the browser (see the view).
            skipped_counts["detector_error"] = skipped_counts.get("detector_error", 0) + 1
            print(f"  [hidden_track_detection] track {track_id}: {exc}")
            continue

        if not candidates:
            continue

        primary = candidates[0]
        results.append({
            "track_id": track_id,
            "title": title,
            "artist": artist_name or "",
            "album": album_title or "",
            "duration_seconds": duration_seconds,
            "silence_start": primary.silence_start,
            "silence_end": primary.silence_end,
            "silence_duration": primary.silence_duration,
            "resumed_start": primary.resumed_start,
            "resumed_duration": primary.resumed_duration,
            "active_ratio": primary.active_ratio,
            "representative_level_db": primary.representative_level_db,
            "strength": primary.strength,
            "other_gap_count": len(candidates) - 1,
        })

    return results, considered, scanned, skipped_counts


def _sort_results_strongest_first(results):
    results.sort(key=lambda r: (
        r["strength"] != STRENGTH_HIGH,  # High first, then Medium
        -r["silence_duration"],
        -r["resumed_duration"],
        -r["representative_level_db"],
    ))
    return results


def scan_for_hidden_tracks(queryset, settings):
    """Run hidden-track detection over every Track in `queryset` --
    filtering (ready2air / category / a single track id) is entirely
    the CALLER's job; this function applies none of its own. Read-only
    throughout: no DB writes, no audio decode, no subprocess, no
    filesystem access beyond reading each track's own waveform JSON
    (path sourced from the Track row itself, never from a caller-
    supplied path).

    UNBOUNDED -- this walks the complete queryset in one call, which
    (see the final report / DEFAULT_BATCH_SIZE's docstring) is too
    slow for a single HTTP request against a large real library and
    can exceed the production gunicorn worker timeout. The web view
    uses scan_for_hidden_tracks_batch instead; this function remains
    for direct/offline/test use where an unbounded synchronous call is
    fine (it's what the test suite calls throughout, and would suit a
    future standalone management command).

    Memory/query-safety:
      - `.values_list(...).iterator(chunk_size=SCAN_CHUNK_SIZE)` streams
        rows from the DB in bounded chunks -- the full queryset and its
        waveform JSON are never materialized in memory together.
      - Exactly one query touches Track/Artist/Album (the values_list
        call itself, with select_related folded into the same SQL join)
        -- no N+1 regardless of queryset size.
      - Each track's waveform JSON is read one at a time, discarded
        before moving to the next -- at most one track's envelope
        arrays are resident in memory at once.

    Returns (results, summary):
      results: list of dicts, one per track with >=1 qualifying gap,
        sorted strongest-first -- each dict carries the STRONGEST
        candidate for that track (see detect_hidden_track_candidates'
        ranking rule) plus `other_gap_count` for any additional
        qualifying gaps found in the same track.
      summary: dict of scan counters + timing."""
    t0 = _time.monotonic()
    rows = (
        queryset
        .select_related("artist", "album")
        .values_list(
            "id", "title", "artist__name", "album__title",
            "duration_seconds", "waveform_path",
        )
        .iterator(chunk_size=SCAN_CHUNK_SIZE)
    )

    scan_start = _time.monotonic()
    results, considered, scanned, skipped_counts = _process_track_rows(rows, settings)
    scan_elapsed = _time.monotonic() - scan_start
    total_elapsed = _time.monotonic() - t0

    _sort_results_strongest_first(results)

    summary = {
        "tracks_considered": considered,
        "waveforms_scanned": scanned,
        "suspects_found": len(results),
        "skipped": {SKIP_REASON_LABELS.get(k, k): v for k, v in skipped_counts.items()},
        "skipped_total": sum(skipped_counts.values()),
        "scan_seconds": round(scan_elapsed, 3),
        "total_seconds": round(total_elapsed, 3),
        "avg_ms_per_scanned_track": round((scan_elapsed / scanned) * 1000, 2) if scanned else 0.0,
    }
    return results, summary


# Tracks per batch for scan_for_hidden_tracks_batch -- deliberately a
# fixed server-side constant, never client-supplied (a client could
# otherwise request an arbitrarily large batch and defeat the whole
# point of bounding each request). Sized against REAL measured
# production timing, not the earlier synthetic estimate: a full
# default-scope scan of this station's actual ~29.6k-track library
# (ready2air + music-kind, real waveform files, read-only) measured
# 49.1s wall-clock, ~7.4ms per SUCCESSFULLY SCANNED track (tracks
# skipped for missing/incompatible waveform data are cheaper and don't
# drive the bound). Assuming every track in a batch is the expensive
# kind and padding further for slower disks/cold cache/larger files
# (~15ms/track), 300 tracks costs at most ~4.5s -- comfortably inside
# gunicorn's own 30s default worker timeout (see deploy/isadoraair-
# gunicorn.service, which sets no --timeout override) with wide margin,
# not a near-miss.
DEFAULT_BATCH_SIZE = 300


def scan_for_hidden_tracks_batch(queryset, settings, cursor=None, batch_size=DEFAULT_BATCH_SIZE):
    """One bounded slice of scan_for_hidden_tracks, ordered by a stable
    primary-key cursor -- NOT offset/page-number pagination, which
    gets slower the deeper into a large queryset it's asked to skip;
    `pk__gt=cursor` costs the same regardless of how far into the
    library a batch starts, since Track.pk is already indexed.

    `queryset` must already be fully filtered/validated by the caller
    (same contract as scan_for_hidden_tracks) -- this function applies
    no filtering beyond the pk cursor and the batch-size slice, and in
    particular never accepts a track selection from the request beyond
    that one integer cursor.

    Returns (results, summary, next_cursor, is_last_batch):
      results/summary: same shape as scan_for_hidden_tracks's, scoped
        to just this batch (results NOT sorted here -- the caller
        accumulates batches and sorts once at the end, see
        _sort_results_strongest_first).
      next_cursor: the highest pk seen in this batch, to pass back in
        as `cursor` for the next call. None if this batch was empty.
      is_last_batch: True once fewer than `batch_size` rows came back
        (the queryset is exhausted) -- the caller should stop after
        this one, whether or not `results` is empty."""
    t0 = _time.monotonic()
    qs = queryset.order_by("pk")
    if cursor is not None:
        qs = qs.filter(pk__gt=cursor)

    # A single bounded batch (a few hundred rows) is small enough to
    # fetch directly -- .iterator() exists to avoid materializing an
    # UNBOUNDED queryset, which this already-sliced one isn't.
    rows = list(
        qs.select_related("artist", "album")
        .values_list("id", "title", "artist__name", "album__title", "duration_seconds", "waveform_path")
        [:batch_size]
    )

    results, considered, scanned, skipped_counts = _process_track_rows(rows, settings)
    elapsed = _time.monotonic() - t0

    # Sorted within THIS batch (cheap -- bounded to batch_size items).
    # NOT sufficient on its own for a multi-batch scan's overall order
    # (a later batch's strongest candidate can easily beat an earlier
    # batch's) -- the caller must still do one final sort over every
    # batch's results combined. Sorting here regardless keeps a
    # single-batch scan (the common case for a narrow filter) already
    # correctly ordered with no caller-side work required.
    _sort_results_strongest_first(results)

    next_cursor = rows[-1][0] if rows else cursor
    is_last_batch = len(rows) < batch_size

    summary = {
        "tracks_considered": considered,
        "waveforms_scanned": scanned,
        "suspects_found": len(results),
        "skipped": {SKIP_REASON_LABELS.get(k, k): v for k, v in skipped_counts.items()},
        "skipped_total": sum(skipped_counts.values()),
        "scan_seconds": round(elapsed, 3),
        "total_seconds": round(elapsed, 3),
        "avg_ms_per_scanned_track": round((elapsed / scanned) * 1000, 2) if scanned else 0.0,
    }
    return results, summary, next_cursor, is_last_batch
