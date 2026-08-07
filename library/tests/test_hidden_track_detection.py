"""Hidden Track Detection -- Phase 1 (diagnostic only).

Covers the pure detector algorithm (library/services/
hidden_track_detection.py), waveform JSON validation, the report view/
form integration, permissions/CSRF, and query/memory-safety of the
scan. Uses only temporary, hand-built waveform JSON -- never production
waveform files."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from library.forms import HiddenTrackDetectionForm
from library.models import Artist, Category, CategoryKind, Track
from library.services.hidden_track_detection import (
    DetectionSettings,
    HiddenTrackCandidate,
    STRENGTH_HIGH,
    STRENGTH_MEDIUM,
    WaveformSkipReason,
    detect_hidden_track_candidates,
    load_envelope,
    scan_for_hidden_tracks,
    scan_for_hidden_tracks_batch,
)

DEFAULT_KWARGS = dict(
    silence_threshold_db=-50.0,
    min_silence_seconds=20.0,
    resumed_audio_threshold_db=-35.0,
    min_resumed_audio_seconds=15.0,
    required_active_ratio=0.60,
    min_position_seconds=60.0,
)


def build_envelope(spec, step=0.05):
    """spec: list of (duration_seconds, db_level) segments -> (times, envelope_db).
    Windows are evenly spaced `step` seconds apart, timestamps at each
    window's center -- matches compute_envelope's real output shape."""
    times, db = [], []
    t = step / 2.0
    for dur, level in spec:
        n = max(1, int(round(dur / step)))
        for _ in range(n):
            times.append(round(t, 6))
            db.append(level)
            t += step
    return times, db


def write_waveform_json(path, times, envelope_db, **extra):
    payload = {
        "track_id": 1,
        "samples_left": [], "samples_right": [],
        "next_start": None, "cue_in_seconds": None,
        "analysis_duration": times[-1] if times else None,
        "envelope_times": times,
        "envelope_db": envelope_db,
    }
    payload.update(extra)
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


# =====================================================================
# Detector fundamentals
# =====================================================================
class DetectorFundamentalsTests(TestCase):
    def test_normal_song_no_internal_silence(self):
        times, db = build_envelope([(120, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_fade_out_then_silence_through_eof(self):
        times, db = build_envelope([(90, -10.0), (30, -18.0), (60, -70.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_long_silence_then_sustained_audio_returns_result(self):
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], HiddenTrackCandidate)

    def test_exactly_at_threshold_silence_duration_accepted(self):
        times, db = build_envelope([(90, -10.0), (20.0, -70.0), (40, -10.0)])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(result[0].silence_duration, 20.0)

    def test_just_below_threshold_silence_duration_rejected(self):
        times, db = build_envelope([(90, -10.0), (19.9, -70.0), (40, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_resumed_audio_exactly_at_threshold(self):
        # Resumed section sits exactly ON the resumed-audio threshold --
        # >= is the accept condition throughout the detector.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -35.0)])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)

    def test_resumed_audio_below_threshold_rejected(self):
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -35.1)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_resumed_audio_shorter_than_required_duration_rejected(self):
        # Only 10s of loud audio after the gap, then EOF -- can't reach
        # the 15s minimum, and there's no more track to prove it out.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (10, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_short_click_after_long_silence_rejected(self):
        times, db = build_envelope([(90, -10.0), (25, -70.0), (0.3, -10.0), (60, -70.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_low_level_noise_after_long_silence_rejected(self):
        # Between the silence threshold (-50) and resumed threshold
        # (-35) -- never crosses the resumed-audio bar at all.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -42.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_credible_audio_required_before_gap(self):
        # Silent/malformed header -- no real program audio before the
        # "gap" for it to plausibly be a hidden-track pattern.
        times, db = build_envelope([(90, -70.0), (25, -70.0), (40, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_gap_before_minimum_position_rejected(self):
        times, db = build_envelope([(20, -10.0), (25, -70.0), (40, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_later_gap_after_minimum_position_accepted(self):
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
        self.assertGreaterEqual(times[0], 0)  # sanity: gap starts after 90s > 60s min position
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)


# =====================================================================
# Gap tolerance
# =====================================================================
class GapToleranceTests(TestCase):
    def test_one_brief_excursion_still_accepted(self):
        spec = [(90, -10.0), (10, -70.0), (0.5, -20.0), (12, -70.0), (40, -10.0)]
        times, db = build_envelope(spec)
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)

    def test_sustained_excursion_beyond_tolerance_splits_gap(self):
        # A 2s excursion (> MAX_SINGLE_EXCURSION_SECONDS=1.5) splits the
        # would-be 22.5s gap into two ~10s halves, neither reaching the
        # 20s minimum on its own.
        spec = [(90, -10.0), (10, -70.0), (2.0, -10.0), (12, -70.0), (40, -10.0)]
        times, db = build_envelope(spec)
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_gap_active_window_ratio_below_allowed_silence_ratio_rejected(self):
        # Scatter enough short excursions through a long span that the
        # overall quiet ratio drops below MIN_SILENT_WINDOW_RATIO
        # (0.92), even though no SINGLE excursion is long enough to
        # trigger the hard split on its own.
        spec = []
        for _ in range(10):
            spec.append((1.8, -70.0))
            spec.append((0.4, -10.0))  # ~18% excursion time -- well past the 8% tolerance
        times, db = build_envelope([(90, -10.0)] + spec + [(40, -10.0)])
        self.assertEqual(detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS), [])

    def test_irregular_timestamp_spacing_not_exactly_one_second(self):
        # A much coarser, deliberately non-1-second window spacing
        # (0.5s) -- the detector must derive durations from the actual
        # times array, not assume any particular spacing.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)], step=0.5)
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].silence_duration, 25.0, delta=0.5)

    def test_irregular_final_window_spacing_does_not_break_math(self):
        # Manually perturb the last window's spacing to something
        # different from the rest -- span/duration math must still be
        # based on real timestamps, not an assumed constant step, and
        # must not raise.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
        times[-1] = times[-2] + 0.37  # irregular final gap
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)  # doesn't raise, still finds the gap


# =====================================================================
# Failed-return retry: a quiet gap must not be abandoned outright the
# first time a crossing above resumed_audio_threshold_db fails
# sustained-audio validation -- the search continues past a brief
# false start (click/noise burst) looking for a LATER, credible,
# SUSTAINED return. Every excursion used here deliberately exceeds
# MAX_SINGLE_EXCURSION_SECONDS (1.5s in these defaults) so it forces a
# hard split in _find_quiet_spans and is evaluated as a genuine
# resumed-audio CANDIDATE by the retry loop -- a shorter excursion
# would instead be silently absorbed by the pre-existing gap-tolerance
# mechanism (see GapToleranceTests) before ever reaching this code
# path at all. Both mechanisms together cover "silence -> click ->
# silence -> hidden song" regardless of how long the click itself is.
# =====================================================================
class FailedReturnRetryTests(TestCase):
    def test_one_false_start_then_sustained_audio_is_found(self):
        # 3s false start (exceeds tolerance, forces a hard split) is
        # long enough to be evaluated as its own candidate, fails
        # sustained-audio validation on its own, and the search must
        # continue past it to the real return rather than giving up.
        times, db = build_envelope([
            (90, -10.0), (25, -70.0), (3.0, -10.0), (25, -70.0), (40, -10.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertGreaterEqual(len(result), 1)
        # At least one candidate must resolve to the REAL return, not
        # get stuck on (or silently vanish because of) the false start.
        real_return_time = 90 + 25 + 3.0 + 25
        self.assertTrue(
            any(abs(c.resumed_start - real_return_time) < 0.5 for c in result),
            f"no candidate resolved to the real return at ~{real_return_time}s: {result}",
        )

    def test_multiple_failed_crossings_before_sustained_audio_found(self):
        # Three separate false starts, each individually too short and
        # too isolated to pass validation, before the real return.
        times, db = build_envelope([
            (90, -10.0), (22, -70.0),
            (2.0, -10.0), (22, -70.0),
            (2.0, -10.0), (22, -70.0),
            (2.0, -10.0), (22, -70.0),
            (40, -10.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertGreaterEqual(len(result), 1)
        # 3 x (22s quiet + 2s blip), then one final 22s quiet, then audio.
        real_return_time = 90 + 3 * (22 + 2.0) + 22
        for c in result:
            self.assertAlmostEqual(c.resumed_start, real_return_time, delta=0.5)

    def test_repeated_clicks_with_no_sustained_audio_rejected(self):
        # Many short false starts, NEVER followed by anything that
        # actually passes sustained-audio validation -- the whole gap
        # (and every gap in this track) must end up rejected, not
        # crash, and not hang.
        spec = [(90, -10.0), (22, -70.0)]
        for _ in range(15):
            spec.append((2.0, -10.0))
            spec.append((5, -70.0))
        times, db = build_envelope(spec)
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(result, [])

    def test_sustained_active_section_ending_gap_is_not_skipped_indefinitely(self):
        # No false start at all -- the real return follows immediately.
        # Confirms the retry machinery doesn't change behavior (or add
        # unnecessary search cost) for the ordinary, most common case.
        times, db = build_envelope([(90, -10.0), (25, -70.0), (60, -10.0)])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].resumed_start, 115.0, delta=0.5)

    def test_reported_resumed_start_points_to_sustained_return_not_click(self):
        times, db = build_envelope([
            (90, -10.0), (25, -70.0), (3.0, -10.0), (25, -70.0), (45, -10.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertGreaterEqual(len(result), 1)
        primary = result[0]
        click_time = 90 + 25
        # Must not report the click's own position as the resumed-audio
        # start -- it must be well past it, at the real return.
        self.assertGreater(primary.resumed_start, click_time + 3.0)

    def test_reported_silence_duration_and_strength_remain_sensible(self):
        times, db = build_envelope([
            (90, -10.0), (35, -70.0), (3.0, -10.0), (35, -70.0), (35, -8.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertGreaterEqual(len(result), 1)
        primary = result[0]
        self.assertGreaterEqual(primary.silence_duration, DEFAULT_KWARGS["min_silence_seconds"])
        self.assertIn(primary.strength, (STRENGTH_HIGH, STRENGTH_MEDIUM))
        # A sane result never reports resumed audio starting before the
        # gap it supposedly follows.
        self.assertGreater(primary.resumed_start, primary.silence_end)

    def test_original_quiet_gap_start_preserved_for_reporting(self):
        # A 3s false start exceeds the excursion tolerance, so
        # _find_quiet_spans hard-splits this into TWO disjoint gaps:
        # (90s..115s) and (118s..143s) -- both legitimately reach the
        # same real return via the search, so both appear in the
        # result set (see MultipleGapsTests for the general multi-gap
        # case). What this test actually verifies: the FIRST gap's own
        # reported silence_start is its OWN start (90s) -- never
        # shifted forward to the false start's position (118s) or
        # anywhere else -- proving a failed attempt during the search
        # never mutates the gap bounds that were fixed before the
        # search began.
        times, db = build_envelope([
            (90, -10.0), (25, -70.0), (3.0, -10.0), (25, -70.0), (40, -10.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertGreaterEqual(len(result), 1)
        silence_starts = {round(c.silence_start) for c in result}
        self.assertIn(90, silence_starts)

    def test_runtime_near_linear_for_envelope_with_many_threshold_crossings(self):
        """Regression guard for a genuine quadratic-scan bug found and
        fixed during development: a track with many disjoint qualifying
        quiet spans (each separated by a brief, failing false-start
        crossing) must not cause per-span rescans of the remaining
        track. Confirms scaling stays close to linear, not quadratic,
        by comparing the cost of doubling the input twice in a row."""
        def make_envelope(n_blips):
            spec = [(90, -10.0)]
            for _ in range(n_blips):
                spec.append((22.0, -70.0))
                spec.append((2.0, -10.0))
            spec.append((22.0, -70.0))
            return build_envelope(spec)

        timings = []
        for n_blips in (200, 400, 800):
            times, db = make_envelope(n_blips)
            t0 = time.monotonic()
            detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
            timings.append(time.monotonic() - t0)

        # Quadratic scaling would roughly QUADRUPLE the time on each
        # doubling; linear scaling roughly doubles it. Allow generous
        # slack (6x, not 2x) for test-environment noise while still
        # firmly rejecting quadratic-or-worse growth (which would be
        # ~4x each step, ~16x over two doublings).
        self.assertLess(timings[1], timings[0] * 6 + 0.05)
        self.assertLess(timings[2], timings[1] * 6 + 0.05)


# =====================================================================
# Multiple gaps
# =====================================================================
class MultipleGapsTests(TestCase):
    def test_two_qualifying_gaps_choose_strongest_by_ranking(self):
        # Second gap (28s) is longer than the first (22s) -- ranking
        # rule #1 (longer silence wins) must pick it.
        times, db = build_envelope([
            (90, -10.0), (22, -70.0), (30, -10.0), (28, -70.0), (30, -10.0),
        ])
        result = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0].silence_duration, 28.0, delta=0.2)
        self.assertGreater(result[0].silence_duration, result[1].silence_duration)

    def test_qualifying_gap_count_reported_accurately_via_scan(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-music", defaults={"name": "Test"})
        category = Category.objects.create(code="HTMULTI", name="HT Multi", kind=kind)
        artist, _ = Artist.get_or_create_ci("Multi Gap Artist")
        times, db = build_envelope([
            (90, -10.0), (22, -70.0), (30, -10.0), (28, -70.0), (30, -10.0),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            wp = Path(tmp) / "1.json"
            write_waveform_json(wp, times, db)
            Track.objects.create(
                filepath="/tmp/does-not-exist/multi.flac", filename="multi.flac",
                title="Multi Gap Song", artist=artist, category=category,
                duration_seconds=times[-1], ready2air=True, waveform_path=str(wp),
            )
            results, summary = scan_for_hidden_tracks(Track.objects.all(), DetectionSettings())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["other_gap_count"], 1)

    def test_ranking_is_deterministic_across_repeated_calls(self):
        times, db = build_envelope([
            (90, -10.0), (22, -70.0), (30, -10.0), (28, -70.0), (30, -10.0),
        ])
        first = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        second = detect_hidden_track_candidates(times, db, **DEFAULT_KWARGS)
        self.assertEqual(
            [(c.silence_start, c.silence_duration) for c in first],
            [(c.silence_start, c.silence_duration) for c in second],
        )


# =====================================================================
# Waveform validation (load_envelope)
# =====================================================================
class WaveformValidationTests(TestCase):
    def test_missing_waveform_path(self):
        result = load_envelope("")
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.NO_PATH)

    def test_missing_file(self):
        result = load_envelope("/tmp/does-not-exist/nope-12345.json")
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.FILE_MISSING)

    def test_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not valid json", encoding="utf-8")
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.INVALID_JSON)

    def test_missing_envelope_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "old.json"
            p.write_text(json.dumps({"track_id": 1, "samples_left": [1, 2]}), encoding="utf-8")
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.MISSING_ENVELOPE)

    def test_mismatched_array_lengths(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mismatch.json"
            write_waveform_json(p, [0.1, 0.2, 0.3], [-10.0, -10.0])
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.LENGTH_MISMATCH)

    def test_empty_arrays(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.json"
            write_waveform_json(p, [], [])
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.MISSING_ENVELOPE)

    def test_non_finite_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "nonfinite.json"
            # json.dumps can't natively emit NaN/Infinity in strict
            # mode, but Python's json module accepts them on load by
            # default (non-standard extension) -- write it directly.
            p.write_text(
                '{"envelope_times": [0.1, 0.2, 0.3], "envelope_db": [-10.0, NaN, -10.0], '
                '"analysis_duration": 0.3}',
                encoding="utf-8",
            )
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.NON_FINITE)

    def test_insufficient_duration_or_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tiny.json"
            write_waveform_json(p, [0.025], [-10.0])
            result = load_envelope(str(p))
        self.assertFalse(result.ok)
        self.assertEqual(result.skip_reason, WaveformSkipReason.INSUFFICIENT_DATA)

    def test_valid_envelope_loads_ok(self):
        times, db = build_envelope([(30, -10.0)])
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "good.json"
            write_waveform_json(p, times, db)
            result = load_envelope(str(p))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.times), len(times))

    def test_individual_failure_does_not_abort_overall_scan(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-music2", defaults={"name": "Test"})
        category = Category.objects.create(code="HTBAD", name="HT Bad", kind=kind)
        artist, _ = Artist.get_or_create_ci("Mixed Batch Artist")
        with tempfile.TemporaryDirectory() as tmp:
            # Track 1: malformed waveform.
            bad_path = Path(tmp) / "bad.json"
            bad_path.write_text("not json", encoding="utf-8")
            Track.objects.create(
                filepath="/tmp/does-not-exist/bad.flac", filename="bad.flac", title="Bad",
                artist=artist, category=category, ready2air=True, waveform_path=str(bad_path),
            )
            # Track 2: valid waveform with a qualifying gap.
            times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
            good_path = Path(tmp) / "good.json"
            write_waveform_json(good_path, times, db)
            Track.objects.create(
                filepath="/tmp/does-not-exist/good.flac", filename="good.flac", title="Good",
                artist=artist, category=category, duration_seconds=times[-1],
                ready2air=True, waveform_path=str(good_path),
            )
            results, summary = scan_for_hidden_tracks(Track.objects.all(), DetectionSettings())
        self.assertEqual(summary["tracks_considered"], 2)
        self.assertEqual(summary["waveforms_scanned"], 1)
        self.assertEqual(summary["skipped_total"], 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Good")


# =====================================================================
# Report view / form
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class ReportViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("htstaff", "htstaff@example.invalid", "pw")

    def _make_track_with_gap(self, category=None, ready2air=True, tmp_dir=None):
        artist, _ = Artist.get_or_create_ci(f"Report Test Artist {Track.objects.count()}")
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
        wp = Path(tmp_dir) / f"{Track.objects.count()}.json"
        write_waveform_json(wp, times, db)
        return Track.objects.create(
            filepath=f"/tmp/does-not-exist/rvt-{Track.objects.count()}.flac",
            filename="rvt.flac", title="Report View Song", artist=artist,
            category=category, duration_seconds=times[-1], ready2air=ready2air,
            waveform_path=str(wp),
        )

    def test_existing_royalty_report_page_still_loads(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:reports"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Generate report", resp.content)
        self.assertIn(b"Past reports", resp.content)

    def test_hidden_track_detection_appears_in_reports_hub(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:reports"))
        self.assertIn(b"Hidden Track Detection", resp.content)

    def test_default_form_values_correct(self):
        form = HiddenTrackDetectionForm()
        self.assertEqual(form.fields["silence_threshold_db"].initial, -50.0)
        self.assertEqual(form.fields["min_silence_seconds"].initial, 20.0)
        self.assertEqual(form.fields["resumed_audio_threshold_db"].initial, -35.0)
        self.assertEqual(form.fields["min_resumed_audio_seconds"].initial, 15.0)
        self.assertEqual(form.fields["required_active_ratio_percent"].initial, 60.0)
        self.assertEqual(form.fields["min_position_seconds"].initial, 60.0)

    def test_help_text_present(self):
        form = HiddenTrackDetectionForm()
        self.assertIn("conservative starting point", form.fields["silence_threshold_db"].help_text)
        self.assertIn("20 seconds", form.fields["min_silence_seconds"].help_text)
        self.assertIn("above the silence threshold", form.fields["resumed_audio_threshold_db"].help_text)

    def test_invalid_thresholds_return_form_errors(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
            "silence_threshold_db": "not-a-number", "min_silence_seconds": "20",
            "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
            "required_active_ratio_percent": "60", "min_position_seconds": "60",
        })
        self.assertEqual(resp.status_code, 400)
        data = resp.json()
        self.assertFalse(data["ok"])
        self.assertIn("silence_threshold_db", data["errors"])

    def test_resumed_threshold_not_greater_than_silence_returns_error(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
            "silence_threshold_db": "-30", "min_silence_seconds": "20",
            "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
            "required_active_ratio_percent": "60", "min_position_seconds": "60",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("resumed_audio_threshold_db", resp.json()["errors"])

    def test_malformed_input_never_produces_unhandled_exception(self):
        self.client.force_login(self.staff)
        # Deliberately garbage-y values across the board.
        resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
            "silence_threshold_db": "abc", "min_silence_seconds": "-5",
            "resumed_audio_threshold_db": "xyz", "min_resumed_audio_seconds": "0",
            "required_active_ratio_percent": "150", "min_position_seconds": "-1",
            "track_id": "not-an-int",
        })
        self.assertEqual(resp.status_code, 400)  # never a 500

    def test_scan_is_post_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:api-hidden-track-scan"))
        self.assertEqual(resp.status_code, 405)

    def test_csrf_remains_active(self):
        from django.test import Client
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        resp = csrf_client.post(reverse("library:api-hidden-track-scan"), data={
            "silence_threshold_db": "-50", "min_silence_seconds": "20",
            "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
            "required_active_ratio_percent": "60", "min_position_seconds": "60",
        })
        self.assertEqual(resp.status_code, 403)  # rejected without a valid CSRF token

    def test_anonymous_user_rejected(self):
        # This project's LoginRequiredMiddleware (Django 5.1+) protects
        # every view by default and redirects an anonymous request to
        # login BEFORE the view (and _reports_permission_check) ever
        # runs -- 302, not 403, same as every other unauthenticated
        # request against a protected view in this project. Confirms
        # this new endpoint wasn't accidentally exempted from that
        # project-wide gate.
        resp = self.client.post(reverse("library:api-hidden-track-scan"), data={})
        self.assertEqual(resp.status_code, 302)

    def test_read_only_user_rejected(self):
        ro = User.objects.create_user("htro", "htro@example.invalid", "pw")
        Group.objects.get_or_create(name="remote_dj")[0].user_set.add(ro)
        self.client.force_login(ro)
        resp = self.client.post(reverse("library:api-hidden-track-scan"), data={})
        self.assertEqual(resp.status_code, 403)

    def test_result_links_through_real_track_detail_url_name(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            track = self._make_track_with_gap(tmp_dir=tmp)
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "ready2air": "yes", "track_id": str(track.id),
            })
        data = resp.json()
        expected_url = reverse("library:track-detail", args=[track.id])
        self.assertEqual(data["results"][0]["track_url"], expected_url)

    def test_no_track_fields_change_after_scan(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            track = self._make_track_with_gap(tmp_dir=tmp)
            before = Track.objects.get(pk=track.pk)
            before_values = {
                f: getattr(before, f) for f in
                ("next_start_seconds", "cue_out_seconds", "cue_in_seconds", "title", "related_artists")
            }
            self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "track_id": str(track.id),
            })
            after = Track.objects.get(pk=track.pk)
            for f, v in before_values.items():
                self.assertEqual(getattr(after, f), v, f"{f} changed after a scan")

    def test_no_database_rows_created_by_scan(self):
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            track = self._make_track_with_gap(tmp_dir=tmp)
            track_count_before = Track.objects.count()
            self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "track_id": str(track.id),
            })
            self.assertEqual(Track.objects.count(), track_count_before)

    def test_results_sorted_strongest_first(self):
        self.client.force_login(self.staff)
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-sort", defaults={"name": "Test"})
        category = Category.objects.create(code="HTSORT", name="HT Sort", kind=kind)
        with tempfile.TemporaryDirectory() as tmp:
            # Medium-strength candidate (25s/40s, not the 30/30 high bar).
            artist1, _ = Artist.get_or_create_ci("Sort Artist Medium")
            times1, db1 = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
            wp1 = Path(tmp) / "sort1.json"
            write_waveform_json(wp1, times1, db1)
            Track.objects.create(
                filepath="/tmp/does-not-exist/sort1.flac", filename="sort1.flac", title="Medium One",
                artist=artist1, category=category, duration_seconds=times1[-1],
                ready2air=True, waveform_path=str(wp1),
            )
            # High-strength candidate (35s/35s, strong separation).
            artist2, _ = Artist.get_or_create_ci("Sort Artist High")
            times2, db2 = build_envelope([(90, -10.0), (35, -70.0), (35, -8.0)])
            wp2 = Path(tmp) / "sort2.json"
            write_waveform_json(wp2, times2, db2)
            Track.objects.create(
                filepath="/tmp/does-not-exist/sort2.flac", filename="sort2.flac", title="High One",
                artist=artist2, category=category, duration_seconds=times2[-1],
                ready2air=True, waveform_path=str(wp2),
            )
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTSORT",
            })
        data = resp.json()
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["results"][0]["title"], "High One")
        self.assertEqual(data["results"][0]["strength"], STRENGTH_HIGH)
        self.assertEqual(data["results"][1]["strength"], STRENGTH_MEDIUM)

    def test_summary_counts_correct(self):
        self.client.force_login(self.staff)
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-summary", defaults={"name": "Test"})
        category = Category.objects.create(code="HTSUM", name="HT Summary", kind=kind)
        with tempfile.TemporaryDirectory() as tmp:
            # One track with a real qualifying gap.
            artist1, _ = Artist.get_or_create_ci("Summary Suspect")
            times1, db1 = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
            wp1 = Path(tmp) / "sum1.json"
            write_waveform_json(wp1, times1, db1)
            Track.objects.create(
                filepath="/tmp/does-not-exist/sum1.flac", filename="sum1.flac", title="Suspect",
                artist=artist1, category=category, duration_seconds=times1[-1],
                ready2air=True, waveform_path=str(wp1),
            )
            # One clean track, no gap.
            artist2, _ = Artist.get_or_create_ci("Summary Clean")
            times2, db2 = build_envelope([(120, -10.0)])
            wp2 = Path(tmp) / "sum2.json"
            write_waveform_json(wp2, times2, db2)
            Track.objects.create(
                filepath="/tmp/does-not-exist/sum2.flac", filename="sum2.flac", title="Clean",
                artist=artist2, category=category, duration_seconds=times2[-1],
                ready2air=True, waveform_path=str(wp2),
            )
            # One track with no waveform path at all.
            artist3, _ = Artist.get_or_create_ci("Summary Missing")
            Track.objects.create(
                filepath="/tmp/does-not-exist/sum3.flac", filename="sum3.flac", title="Missing WF",
                artist=artist3, category=category, ready2air=True, waveform_path="",
            )
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTSUM",
            })
        data = resp.json()
        s = data["summary"]
        self.assertEqual(s["tracks_considered"], 3)
        self.assertEqual(s["waveforms_scanned"], 2)
        self.assertEqual(s["suspects_found"], 1)
        self.assertEqual(s["skipped_total"], 1)

    def test_ready2air_filter_default_yes(self):
        self.client.force_login(self.staff)
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-r2a", defaults={"name": "Test"})
        category = Category.objects.create(code="HTR2A", name="HT R2A", kind=kind)
        with tempfile.TemporaryDirectory() as tmp:
            not_ready = self._make_track_with_gap(category=category, ready2air=False, tmp_dir=tmp)
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTR2A",
                # ready2air omitted -> defaults to "yes" per the form
            })
        data = resp.json()
        self.assertEqual(data["summary"]["tracks_considered"], 0)  # the not-ready track was excluded

    def test_category_filter_includes_additional_categories(self):
        self.client.force_login(self.staff)
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-cat2", defaults={"name": "Test"})
        primary = Category.objects.create(code="HTPRIMARY", name="HT Primary", kind=kind)
        secondary = Category.objects.create(code="HTSECOND", name="HT Secondary", kind=kind)
        with tempfile.TemporaryDirectory() as tmp:
            track = self._make_track_with_gap(category=primary, tmp_dir=tmp)
            track.additional_categories.add(secondary)
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTSECOND",
            })
        data = resp.json()
        self.assertEqual(data["summary"]["tracks_considered"], 1)

    def test_track_id_filter_scoped_to_same_staff_visibility(self):
        # Track ID filtering doesn't grant any access beyond what the
        # staff-only gate already allows -- there's no narrower
        # per-track ACL in this app for a staff/superuser to bypass.
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            track = self._make_track_with_gap(tmp_dir=tmp)
            resp = self.client.post(reverse("library:api-hidden-track-scan"), data={
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "track_id": str(track.id),
            })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["summary"]["tracks_considered"], 1)


# =====================================================================
# Performance / query behavior
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class PerformanceQueryTests(TestCase):
    def test_query_count_does_not_scale_with_track_count(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-perf", defaults={"name": "Test"})
        category = Category.objects.create(code="HTPERF", name="HT Perf", kind=kind)
        times, db = build_envelope([(120, -10.0)])  # no gap, keeps this fast
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(40):
                artist, _ = Artist.get_or_create_ci(f"Perf Artist {i}")
                wp = Path(tmp) / f"perf{i}.json"
                write_waveform_json(wp, times, db)
                Track.objects.create(
                    filepath=f"/tmp/does-not-exist/perf{i}.flac", filename=f"perf{i}.flac",
                    title=f"Perf Song {i}", artist=artist, category=category,
                    duration_seconds=times[-1], ready2air=True, waveform_path=str(wp),
                )

            with CaptureQueriesContext(connection) as ctx:
                results, summary = scan_for_hidden_tracks(
                    Track.objects.filter(category=category), DetectionSettings(),
                )
        self.assertEqual(summary["waveforms_scanned"], 40)
        # One query (values_list + select_related folded into one SQL
        # join) regardless of track count -- not 40, not 80.
        self.assertLessEqual(len(ctx.captured_queries), 5)

    def test_queryset_iteration_is_bounded_and_streamed(self):
        from library.services.hidden_track_detection import SCAN_CHUNK_SIZE
        self.assertGreater(SCAN_CHUNK_SIZE, 0)
        self.assertLess(SCAN_CHUNK_SIZE, 100000)  # sanity: genuinely bounded, not "load everything"

    def test_no_subprocess_or_decoder_invoked(self):
        # The whole detection path only ever touches JSON + Track
        # fields -- prove it by patching subprocess.run to explode if
        # called at all during a scan.
        import subprocess
        from unittest.mock import patch

        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-nosub", defaults={"name": "Test"})
        category = Category.objects.create(code="HTNOSUB", name="HT NoSub", kind=kind)
        artist, _ = Artist.get_or_create_ci("No Subprocess Artist")
        times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
        with tempfile.TemporaryDirectory() as tmp:
            wp = Path(tmp) / "nosub.json"
            write_waveform_json(wp, times, db)
            Track.objects.create(
                filepath="/tmp/does-not-exist/nosub.flac", filename="nosub.flac", title="No Sub",
                artist=artist, category=category, duration_seconds=times[-1],
                ready2air=True, waveform_path=str(wp),
            )
            with patch.object(subprocess, "run", side_effect=AssertionError("must not shell out")), \
                 patch.object(subprocess, "Popen", side_effect=AssertionError("must not shell out")):
                results, summary = scan_for_hidden_tracks(
                    Track.objects.filter(category=category), DetectionSettings(),
                )
        self.assertEqual(len(results), 1)

    def test_malformed_file_among_valid_files_does_not_stop_scan(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-mixed", defaults={"name": "Test"})
        category = Category.objects.create(code="HTMIXED", name="HT Mixed", kind=kind)
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                artist, _ = Artist.get_or_create_ci(f"Mixed Artist {i}")
                wp = Path(tmp) / f"mixed{i}.json"
                if i == 2:
                    wp.write_text("{broken", encoding="utf-8")
                else:
                    times, db = build_envelope([(120, -10.0)])
                    write_waveform_json(wp, times, db)
                Track.objects.create(
                    filepath=f"/tmp/does-not-exist/mixed{i}.flac", filename=f"mixed{i}.flac",
                    title=f"Mixed {i}", artist=artist, category=category,
                    ready2air=True, waveform_path=str(wp),
                )
            results, summary = scan_for_hidden_tracks(
                Track.objects.filter(category=category), DetectionSettings(),
            )
        self.assertEqual(summary["tracks_considered"], 5)
        self.assertEqual(summary["waveforms_scanned"], 4)
        self.assertEqual(summary["skipped_total"], 1)


class TimingSummaryTests(TestCase):
    def test_scan_records_timing_fields(self):
        kind, _ = CategoryKind.objects.get_or_create(code="test-ht-timing", defaults={"name": "Test"})
        category = Category.objects.create(code="HTTIME", name="HT Timing", kind=kind)
        artist, _ = Artist.get_or_create_ci("Timing Artist")
        times, db = build_envelope([(120, -10.0)])
        with tempfile.TemporaryDirectory() as tmp:
            wp = Path(tmp) / "t.json"
            write_waveform_json(wp, times, db)
            Track.objects.create(
                filepath="/tmp/does-not-exist/timing.flac", filename="timing.flac", title="Timing",
                artist=artist, category=category, duration_seconds=times[-1],
                ready2air=True, waveform_path=str(wp),
            )
            results, summary = scan_for_hidden_tracks(
                Track.objects.filter(category=category), DetectionSettings(),
            )
        for key in ("scan_seconds", "total_seconds", "avg_ms_per_scanned_track"):
            self.assertIn(key, summary)
            self.assertGreaterEqual(summary[key], 0)


# =====================================================================
# Batched scanning -- required because a real-library measurement (see
# the final report) showed a single unbounded scan can take far longer
# than the production gunicorn worker's default 30s timeout. The
# browser instead calls api_hidden_track_scan repeatedly with an
# advancing `cursor`, accumulating results client-side; every call is
# independently permission/CSRF/filter-validated (no server-side scan
# state persists between batches -- see the "no persistence" contract).
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class BatchedScanTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("batchstaff", "batch@example.invalid", "pw")
        self.kind, _ = CategoryKind.objects.get_or_create(code="test-ht-batch", defaults={"name": "Test"})
        self.category = Category.objects.create(code="HTBATCH", name="HT Batch", kind=self.kind)
        self.other_category = Category.objects.create(code="HTBATCHOTHER", name="HT Batch Other", kind=self.kind)

    def _make_track(self, tmp_dir, idx, category=None, with_gap=False, malformed=False, strength_high=False):
        artist, _ = Artist.get_or_create_ci(f"Batch Artist {idx}")
        wp = Path(tmp_dir) / f"batch{idx}.json"
        duration = None
        if malformed:
            wp.write_text("not json", encoding="utf-8")
        elif with_gap:
            if strength_high:
                times, db = build_envelope([(90, -10.0), (35, -70.0), (35, -8.0)])
            else:
                times, db = build_envelope([(90, -10.0), (25, -70.0), (40, -10.0)])
            write_waveform_json(wp, times, db)
            duration = times[-1]
        else:
            times, db = build_envelope([(120, -10.0)])
            write_waveform_json(wp, times, db)
            duration = times[-1]
        return Track.objects.create(
            filepath=f"/tmp/does-not-exist/batch{idx}.flac", filename=f"batch{idx}.flac",
            title=f"Batch Track {idx}", artist=artist, category=category or self.category,
            duration_seconds=duration, ready2air=True, waveform_path=str(wp),
        )

    def test_stable_cursor_progression_no_duplicates_or_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(25):
                self._make_track(tmp, i)

            settings = DetectionSettings()
            cursor = None
            total_considered = 0
            batch_count = 0
            seen_cursors = []
            while True:
                results, summary, next_cursor, is_last = scan_for_hidden_tracks_batch(
                    Track.objects.filter(category=self.category), settings,
                    cursor=cursor, batch_size=7,
                )
                batch_count += 1
                total_considered += summary["tracks_considered"]
                if next_cursor is not None:
                    if seen_cursors:
                        self.assertGreater(next_cursor, seen_cursors[-1])  # strictly increasing
                    seen_cursors.append(next_cursor)
                self.assertLessEqual(summary["tracks_considered"], 7)  # never exceeds batch_size
                if is_last:
                    break
                cursor = next_cursor
                self.assertLess(batch_count, 100)  # guard against a runaway loop bug

            self.assertGreater(batch_count, 1)  # actually required multiple batches
            # No duplicates AND no skips: the sum across every batch must
            # equal the total matching count exactly.
            self.assertEqual(total_considered, 25)

    def test_filter_consistency_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(10):
                self._make_track(tmp, i, category=self.category)
            for i in range(10, 20):
                self._make_track(tmp, i, category=self.other_category)

            settings = DetectionSettings()
            cursor = None
            total_considered = 0
            while True:
                results, summary, next_cursor, is_last = scan_for_hidden_tracks_batch(
                    Track.objects.filter(category=self.category), settings,
                    cursor=cursor, batch_size=3,
                )
                total_considered += summary["tracks_considered"]
                if is_last:
                    break
                cursor = next_cursor
            # Only the 10 tracks in self.category, never leaking the
            # other 10 in self.other_category -- the SAME filter is
            # reapplied (by the caller, mirroring the view) on every
            # single batch call.
            self.assertEqual(total_considered, 10)

    def test_permission_enforced_on_every_batch(self):
        ro = User.objects.create_user("batchro", "batchro@example.invalid", "pw")
        Group.objects.get_or_create(name="remote_dj")[0].user_set.add(ro)
        self.client.force_login(ro)
        base = {
            "silence_threshold_db": "-50", "min_silence_seconds": "20",
            "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
            "required_active_ratio_percent": "60", "min_position_seconds": "60",
        }
        # First batch (no cursor).
        resp1 = self.client.post(reverse("library:api-hidden-track-scan"), data=base)
        self.assertEqual(resp1.status_code, 403)
        # A LATER batch (with a cursor) must be rejected identically --
        # there is no way to "already be inside" an authorized scan.
        resp2 = self.client.post(reverse("library:api-hidden-track-scan"), data={**base, "cursor": "12345"})
        self.assertEqual(resp2.status_code, 403)

    def test_csrf_enforced_on_batch_with_cursor(self):
        from django.test import Client
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        resp = csrf_client.post(reverse("library:api-hidden-track-scan"), data={
            "silence_threshold_db": "-50", "min_silence_seconds": "20",
            "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
            "required_active_ratio_percent": "60", "min_position_seconds": "60",
            "cursor": "999",
        })
        self.assertEqual(resp.status_code, 403)

    def test_final_combined_result_ordering_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Interleave strengths so no single batch (size 2) is
            # internally pre-sorted the same way the FINAL combined
            # set must be.
            self._make_track(tmp, 0, with_gap=True, strength_high=False)  # medium
            self._make_track(tmp, 1, with_gap=False)                      # no candidate
            self._make_track(tmp, 2, with_gap=True, strength_high=True)   # high
            self._make_track(tmp, 3, with_gap=False)                      # no candidate
            self._make_track(tmp, 4, with_gap=True, strength_high=False)  # medium

            settings = DetectionSettings()
            all_results = []
            cursor = None
            while True:
                results, summary, next_cursor, is_last = scan_for_hidden_tracks_batch(
                    Track.objects.filter(category=self.category), settings,
                    cursor=cursor, batch_size=2,
                )
                all_results.extend(results)
                if is_last:
                    break
                cursor = next_cursor

            self.assertEqual(len(all_results), 3)  # the 3 with_gap=True tracks
            # Caller-side final sort (what the JS does after accumulating
            # every batch) must put the HIGH-strength candidate first.
            from library.services.hidden_track_detection import _sort_results_strongest_first
            _sort_results_strongest_first(all_results)
            self.assertEqual(all_results[0]["strength"], STRENGTH_HIGH)
            self.assertEqual(all_results[0]["title"], "Batch Track 2")
            self.assertEqual(all_results[1]["strength"], STRENGTH_MEDIUM)
            self.assertEqual(all_results[2]["strength"], STRENGTH_MEDIUM)

    def test_malformed_waveform_handling_across_different_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Malformed tracks deliberately land in DIFFERENT batches
            # (batch_size=3): track 1 in the first batch, track 7 in a
            # later one.
            for i in range(10):
                self._make_track(tmp, i, malformed=(i in (1, 7)))

            settings = DetectionSettings()
            cursor = None
            total_considered = 0
            total_skipped = 0
            batches = 0
            while True:
                results, summary, next_cursor, is_last = scan_for_hidden_tracks_batch(
                    Track.objects.filter(category=self.category), settings,
                    cursor=cursor, batch_size=3,
                )
                batches += 1
                total_considered += summary["tracks_considered"]
                total_skipped += summary["skipped_total"]
                if is_last:
                    break
                cursor = next_cursor

            self.assertGreater(batches, 1)
            self.assertEqual(total_considered, 10)
            self.assertEqual(total_skipped, 2)  # both malformed tracks counted, in whichever batch they fell into

    def test_empty_final_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(4):
                self._make_track(tmp, i)

            settings = DetectionSettings()
            # Exactly two full batches of 2. The count happens to divide
            # evenly, so the SECOND call (2 rows == batch_size) can't
            # yet tell it was the last one -- is_last_batch only goes
            # true once a call comes back SHORT of batch_size, per its
            # own documented contract. A caller (the JS loop) always
            # makes one further call in this shape; it must degrade
            # gracefully to a genuinely empty batch, not error.
            results1, summary1, cursor1, is_last1 = scan_for_hidden_tracks_batch(
                Track.objects.filter(category=self.category), settings, cursor=None, batch_size=2,
            )
            self.assertFalse(is_last1)
            results2, summary2, cursor2, is_last2 = scan_for_hidden_tracks_batch(
                Track.objects.filter(category=self.category), settings, cursor=cursor1, batch_size=2,
            )
            self.assertEqual(summary2["tracks_considered"], 2)
            self.assertFalse(is_last2)  # exactly batch_size rows -- not yet provably exhausted

            # The THIRD call is the genuinely empty final batch -- must
            # degrade gracefully: empty results, is_last_batch True, no
            # error, no exception from an empty `rows` list.
            results3, summary3, cursor3, is_last3 = scan_for_hidden_tracks_batch(
                Track.objects.filter(category=self.category), settings, cursor=cursor2, batch_size=2,
            )
            self.assertEqual(results3, [])
            self.assertEqual(summary3["tracks_considered"], 0)
            self.assertTrue(is_last3)
            self.assertEqual(cursor3, cursor2)  # cursor stays put when nothing new is found

    def test_retry_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                self._make_track(tmp, i, with_gap=(i == 2))

            settings = DetectionSettings()
            qs = Track.objects.filter(category=self.category)
            results1, summary1, cursor1, is_last1 = scan_for_hidden_tracks_batch(
                qs, settings, cursor=None, batch_size=10,
            )
            results2, summary2, cursor2, is_last2 = scan_for_hidden_tracks_batch(
                qs, settings, cursor=None, batch_size=10,
            )
            # A retried (e.g. network-blip) batch call is a pure read --
            # identical inputs must produce identical output, no side
            # effects, nothing to deduplicate. Excludes the timing-
            # derived summary fields (scan_seconds/total_seconds/
            # avg_ms_per_scanned_track), which legitimately vary by a
            # fraction of a millisecond between two real wall-clock
            # measurements -- idempotency is about the DATA, not
            # incidental timing noise.
            self.assertEqual(results1, results2)
            self.assertEqual(cursor1, cursor2)
            self.assertEqual(is_last1, is_last2)
            timing_fields = {"scan_seconds", "total_seconds", "avg_ms_per_scanned_track"}
            self.assertEqual(
                {k: v for k, v in summary1.items() if k not in timing_fields},
                {k: v for k, v in summary2.items() if k not in timing_fields},
            )

    def test_view_batch_loop_end_to_end_matches_unbounded_scan(self):
        """Full end-to-end check: driving api_hidden_track_scan through
        a cursor loop (as the browser does) must find exactly the same
        suspects as a single unbounded scan_for_hidden_tracks call."""
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(12):
                self._make_track(tmp, i, with_gap=(i % 3 == 0))

            reference_results, _ = scan_for_hidden_tracks(
                Track.objects.filter(category=self.category), DetectionSettings(),
            )
            reference_ids = sorted(r["track_id"] for r in reference_results)

            base = {
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTBATCH",
            }
            collected_ids = []
            cursor = None
            batches = 0
            while True:
                body = dict(base)
                if cursor is not None:
                    body["cursor"] = str(cursor)
                resp = self.client.post(reverse("library:api-hidden-track-scan"), data=body)
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                collected_ids.extend(r["track_id"] for r in data["results"])
                batches += 1
                self.assertLess(batches, 50)
                if data["is_last_batch"]:
                    break
                cursor = data["next_cursor"]

            self.assertEqual(sorted(collected_ids), reference_ids)

    # Fields snapshotted below are every column read/writeable by track
    # editing, cue-point picking, and playout -- not just the handful the
    # original (pre-batching) ReportViewTests.test_no_track_fields_change_
    # after_scan happened to check. waveform_path is the single most
    # important field here: a regression that let the scan ever rewrite or
    # re-point it would silently corrupt this feature's own data source
    # for every subsequent scan.
    _SNAPSHOT_FIELDS = (
        "filepath", "filename", "format", "title", "artist_id", "album_id",
        "category_id", "duration_seconds", "cue_in_seconds", "cue_out_seconds",
        "next_start_seconds", "intro_until_seconds", "sweep_start_seconds",
        "outro_starts_seconds", "hook_in_seconds", "hook_out_seconds",
        "ready2air", "play_count", "last_played_at", "waveform_path",
        "related_artists", "file_hash",
    )

    def test_no_track_fields_change_across_multi_batch_view_scan(self):
        """Regression test for item 5 of the hardening review: snapshot
        every relevant Track field before and after a real, successful,
        MULTI-BATCH scan driven through the actual view (not the bare
        service function), and prove none of them moved. Re-verifies the
        pre-existing single-call read-only guarantee still holds now that
        the view goes through scan_for_hidden_tracks_batch instead of the
        original unbounded scan_for_hidden_tracks.

        The view intentionally doesn't expose batch_size to the client
        (it's a server-fixed constant, not operator-controlled), so 9
        tracks against the real DEFAULT_BATCH_SIZE=300 would all fit in
        one call. A thin pass-through patch forces a small batch_size here
        so this test actually exercises multiple round-trips without
        needing hundreds of throwaway tracks."""
        self.client.force_login(self.staff)
        with tempfile.TemporaryDirectory() as tmp:
            track_ids = []
            for i in range(9):
                t = self._make_track(tmp, i, with_gap=(i % 3 == 0))
                track_ids.append(t.id)

            before = {
                t.id: {f: getattr(t, f) for f in self._SNAPSHOT_FIELDS}
                for t in Track.objects.filter(id__in=track_ids)
            }

            base = {
                "silence_threshold_db": "-50", "min_silence_seconds": "20",
                "resumed_audio_threshold_db": "-35", "min_resumed_audio_seconds": "15",
                "required_active_ratio_percent": "60", "min_position_seconds": "60",
                "category": "HTBATCH",
            }

            def _small_batch_scan(queryset, settings, cursor=None, batch_size=3):
                return scan_for_hidden_tracks_batch(queryset, settings, cursor=cursor, batch_size=3)

            cursor = None
            batches = 0
            with patch(
                "library.services.hidden_track_detection.scan_for_hidden_tracks_batch",
                side_effect=_small_batch_scan,
            ):
                while True:
                    body = dict(base)
                    if cursor is not None:
                        body["cursor"] = str(cursor)
                    resp = self.client.post(
                        reverse("library:api-hidden-track-scan"), data=body,
                    )
                    self.assertEqual(resp.status_code, 200)
                    data = resp.json()
                    batches += 1
                    self.assertLess(batches, 50)
                    if data["is_last_batch"]:
                        break
                    cursor = data["next_cursor"]
            self.assertGreater(batches, 1)  # confirm this actually exercised multiple batches

            after = {
                t.id: {f: getattr(t, f) for f in self._SNAPSHOT_FIELDS}
                for t in Track.objects.filter(id__in=track_ids)
            }
            self.assertEqual(before, after)
            # And, per the pre-existing guarantee, no rows were added or removed either.
            self.assertEqual(
                Track.objects.filter(id__in=track_ids).count(), len(track_ids),
            )


# =====================================================================
# UX / static-content regression checks (hardening review item 3)
#
# No JS runtime is available in this environment (no `node` binary), so
# these checks assert directly against the rendered template's HTML/
# inline-<script> content -- the same approach already used by
# test_hidden_track_detection_appears_in_reports_hub. Each assertion is
# tied to a literal source fragment from reports.html's runHiddenTrackScan()
# and its surrounding markup; a regression that removed or renamed the
# underlying behavior would break the matching assertion here.
# =====================================================================
@override_settings(SECURE_SSL_REDIRECT=False)
class HiddenTrackScanUXStaticContentTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("uxstaff", "ux@example.invalid", "pw")

    def _get_reports_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("library:reports"))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode("utf-8")

    def test_scan_button_disabled_immediately_and_duplicate_submit_guarded(self):
        html = self._get_reports_page()
        # Immediate disable, before any network round-trip.
        self.assertIn("btn.disabled = true;", html)
        # Explicit re-entrancy guard at the top of the handler.
        self.assertIn("if (btn.disabled) return;", html)

    def test_scanning_state_label_shown(self):
        html = self._get_reports_page()
        self.assertIn("Scanning…", html)

    def test_button_restored_in_finally_block(self):
        html = self._get_reports_page()
        # Restoration lives in a `finally` so it runs on both the success
        # and the error/exception paths, not just one or the other.
        self.assertIn("finally {", html)
        self.assertIn("btn.disabled = false;", html)
        self.assertIn("btn.textContent = originalLabel;", html)

    def test_error_paths_produce_useful_status_text(self):
        html = self._get_reports_page()
        self.assertIn("rpt-status err", html)
        self.assertIn("Fix the highlighted field(s) and try again.", html)
        self.assertIn("Network error: ", html)

    def test_batch_progress_display_present(self):
        html = self._get_reports_page()
        # The live "N of TOTAL tracks processed, M suspect(s) found so
        # far" progress line updated on every batch iteration.
        self.assertIn("tracks processed", html)
        self.assertIn("suspect(s) found so far", html)

    def test_unfiltered_scan_states_full_library_explicitly(self):
        html = self._get_reports_page()
        self.assertIn("the ENTIRE ready-to-air music library (no category filter set)", html)

    def test_final_results_rendered_only_after_last_batch_sorted(self):
        html = self._get_reports_page()
        # Confirms the accumulate-then-sort-once-at-the-end shape is still
        # in place (not a per-batch render/re-sort under the operator).
        self.assertIn("if (data.is_last_batch) break;", html)
        self.assertIn("allResults.sort(compareHiddenTrackResults);", html)

    def test_scan_form_and_results_panel_present_in_hub(self):
        html = self._get_reports_page()
        self.assertIn('id="htScanBtn"', html)
        self.assertIn('id="htStatus"', html)
        self.assertIn('id="htResultsPanel"', html)
