"""1.1 spec second-pass correction (listener-audible-start semantics) --
regression coverage for effective_airtime_seconds(), the single
authoritative definition of a track's listener-facing broadcast-clock
contribution, and for its consistent use across every scheduling-
duration call site in log_builder.py.

Background: a full engine trace (see PROJECT_NOTES.md / the session
that produced this correction) established that _create_deck never
seeks to cue_in_seconds on a normal start -- every track plays from
real file position 0, and a track's own audible content only begins
cue_in_seconds after its deck was created. Stacking successive tracks'
spans end-to-end (exactly what every accumulation loop in log_builder.py
does) means each track's correct per-track contribution is
next_start_seconds - cue_in_seconds (falling back to duration_seconds
- cue_in_seconds when next_start_seconds is unset), NOT raw
next_start_seconds alone -- the previously-deployed 1.1 formula
over-counted every track with a nonzero cue-in by exactly its own
cue_in_seconds.

This module covers: the helper's own edge-case correctness (explicit
`is not None` handling -- next_start_seconds=0.0 is a real, meaningful
value, never "missing"), cross-path agreement (candidate-fit ==
rotation == playlist == fill == preview, for the same track metadata),
concrete proof that matched-pair landing and exact-fit selection now
score against effective airtime rather than raw next_start_seconds,
and an explicit, documented demonstration of the one case this
per-track helper does NOT capture (the engine's 10-second
trigger-point clamp, which needs look-ahead to the successor track)."""
import random
from datetime import date, time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone as django_timezone

from library.models import (
    Artist, Category, CategoryKind, LogItem, Playlist, PlaylistItem,
    Rotation, RotationSlot, ScheduleBlock, Track,
)
from library.services.log_builder import (
    DURATION_FIT_MARGIN,
    _build_from_playlist,
    _build_from_rotation,
    _effective_airtime,
    _extract_fit_candidates,
    _pick_best_fit,
    effective_airtime_seconds,
    fill_remaining_hour,
    preview_hour_log,
)
from monitoring.models import SystemEvent


def make_category(code, kind_code="test-airtime-music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": "Test Airtime Music"})
    return Category.objects.create(code=code, name=code, kind=kind, artist_separation=0, title_separation=0)


def make_track(title, artist_name, category, **overrides):
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(
        filepath=f"/tmp/does-not-exist/airtime-{Track.objects.count()}.mp3",
        filename="track.mp3", title=title, artist=artist, category=category,
        ready2air=True, duration_seconds=250.0,
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


class EffectiveAirtimeSecondsTests(TestCase):
    """Pure-value tests -- effective_airtime_seconds/_effective_airtime
    only touch attribute access, so a lightweight SimpleNamespace
    stand-in is enough and keeps these fast, no DB needed."""

    def _track(self, next_start=None, duration=None, cue_in=0.0):
        return SimpleNamespace(next_start_seconds=next_start, duration_seconds=duration, cue_in_seconds=cue_in)

    def test_next_start_and_cue_in_both_set(self):
        """next_start=238, cue_in=8, duration=250 -> 230 (the reference
        worked example from the correction review)."""
        track = self._track(next_start=238.0, duration=250.0, cue_in=8.0)
        self.assertEqual(effective_airtime_seconds(track), 230.0)

    def test_next_start_none_falls_back_to_duration_minus_cue_in(self):
        """next_start=None, cue_in=8, duration=250 -> 242."""
        track = self._track(next_start=None, duration=250.0, cue_in=8.0)
        self.assertEqual(effective_airtime_seconds(track), 242.0)

    def test_explicit_zero_next_start_zero_cue_in(self):
        """next_start=0.0, cue_in=0.0, duration=250 -> 0.0."""
        track = self._track(next_start=0.0, duration=250.0, cue_in=0.0)
        self.assertEqual(effective_airtime_seconds(track), 0.0)

    def test_explicit_zero_next_start_beats_nonzero_duration(self):
        """The core zero-regression proof: an explicit 0.0
        next_start_seconds (a deliberately near-instant crossfade
        point, e.g. a very short sweeper/ID) must NOT be silently
        replaced by a much larger duration_seconds, the way the
        previously-deployed `next_start_seconds or duration_seconds`
        chain would have."""
        track = self._track(next_start=0.0, duration=600.0, cue_in=0.0)
        self.assertEqual(effective_airtime_seconds(track), 0.0)

    def test_both_endpoint_fields_unavailable_returns_zero(self):
        track = self._track(next_start=None, duration=None, cue_in=5.0)
        self.assertEqual(effective_airtime_seconds(track), 0.0)

    def test_malformed_endpoint_before_cue_in_clamps_to_zero(self):
        """Malformed/inconsistent metadata (cue_in accidentally past the
        auto-mix point) must never produce a negative scheduling
        duration."""
        track = self._track(next_start=5.0, duration=250.0, cue_in=20.0)
        self.assertEqual(effective_airtime_seconds(track), 0.0)

    def test_cue_in_none_treated_as_zero(self):
        """cue_in_seconds is never None at the DB level (default=0, not
        nullable), but the helper defends against a caller/stand-in
        that doesn't enforce that constraint."""
        track = self._track(next_start=100.0, duration=200.0, cue_in=None)
        self.assertEqual(effective_airtime_seconds(track), 100.0)

    def test_duration_zero_with_no_next_start(self):
        track = self._track(next_start=None, duration=0.0, cue_in=0.0)
        self.assertEqual(effective_airtime_seconds(track), 0.0)

    def test_raw_values_core_matches_object_wrapper(self):
        """effective_airtime_seconds is a thin wrapper around
        _effective_airtime -- the two must always agree, since
        _extract_fit_candidates calls the raw-values core directly
        (it reads scalar columns via .values_list(), not full Track
        objects) while every other call site uses the object wrapper."""
        track = self._track(next_start=238.0, duration=250.0, cue_in=8.0)
        self.assertEqual(_effective_airtime(238.0, 250.0, 8.0), effective_airtime_seconds(track))


class ClampLimitationDocumentedTests(TestCase):
    """Documents, rather than fixes, the one case effective_airtime_
    seconds does NOT capture: the engine's 10-second crossfade
    trigger-point clamp (_poll_position, engine.py). That clamp
    correction is pairwise -- it depends on the SUCCESSOR track's own
    cue_in_seconds -- and is deliberately out of scope for a per-track
    helper (see the correction review). This is NOT a failing
    regression; it's a known, documented limitation."""

    def test_clamp_case_helper_value_differs_from_real_engine_spacing(self):
        """A next_start=12, A cue_in=2, B cue_in=6.

        Helper says: 12 - 2 = 10.

        Real engine: trigger_point = next_start_A - cue_in_B = 12 - 6 = 6,
        which is < 10.0 -- the engine's clamp discards the cue_in
        adjustment entirely and uses trigger_point = next_start_A = 12
        unmodified (see _poll_position, engine.py). B's own deck is
        therefore created 12s (not 6s) after A's, and B's own audible
        content arrives cue_in_B=6s after THAT -- real audible A->B
        spacing = 12 - 2 + 6 = 16, not the 10 the per-track helper
        reports."""
        track_a = SimpleNamespace(next_start_seconds=12.0, duration_seconds=20.0, cue_in_seconds=2.0)
        cue_in_b = 6.0

        helper_value = effective_airtime_seconds(track_a)
        self.assertEqual(helper_value, 10.0)

        # Mirrors _poll_position's own trigger_point clamp (engine.py) --
        # not a live call into the engine, just the same documented
        # arithmetic, to make the discrepancy concrete and numeric.
        trigger_point = track_a.next_start_seconds - cue_in_b
        if trigger_point < 10.0:
            trigger_point = track_a.next_start_seconds
        real_audible_spacing = trigger_point + cue_in_b - track_a.cue_in_seconds

        self.assertEqual(real_audible_spacing, 16.0)
        self.assertNotEqual(
            helper_value, real_audible_spacing,
            "documented known limitation: the per-track helper cannot see the "
            "successor's cue_in, so it under-reports spacing by exactly cue_in_b "
            "whenever the engine's 10-second trigger-point clamp engages",
        )


class CrossPathAirtimeConsistencyTests(TestCase):
    """For the SAME track metadata, _extract_fit_candidates, rotation
    accumulation, playlist accumulation, fill_remaining_hour, and
    preview timing must all agree on exactly how much broadcast-clock
    time the track consumes."""

    @classmethod
    def setUpTestData(cls):
        cls.category = make_category("AIRTIMECONSISTENCY")
        # cue_in=8.0, next_start=238.0 -> effective airtime 230.0,
        # deliberately far from raw next_start_seconds so any call site
        # still reading the raw field instead of the helper would be
        # caught immediately (a 8-second discrepancy is easy to miss;
        # this one is not).
        cls.ref_track = make_track(
            "Reference Track", "Reference Artist", cls.category,
            cue_in_seconds=8.0, next_start_seconds=238.0, duration_seconds=250.0,
        )
        cls.marker_track = make_track(
            "Marker Track", "Marker Artist", cls.category,
            cue_in_seconds=0.0, next_start_seconds=999.0, duration_seconds=999.0,
        )
        cls.expected_airtime = 230.0

    def test_extract_fit_candidates_uses_effective_airtime(self):
        qs = Track.objects.filter(id=self.ref_track.id)
        candidates = _extract_fit_candidates(qs)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].duration_seconds, self.expected_airtime)

    def test_rotation_accumulation_uses_effective_airtime(self):
        rotation = Rotation.objects.create(name="Airtime Consistency Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, track=self.ref_track)
        RotationSlot.objects.create(rotation=rotation, position=1, track=self.marker_track)

        log, error = _build_from_rotation(date(2027, 5, 10), 6, rotation, target_duration_seconds=1229.0)
        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 2)
        delta = (items[1].scheduled_time - items[0].scheduled_time).total_seconds()
        self.assertAlmostEqual(delta, self.expected_airtime, places=3)

    def test_playlist_accumulation_uses_effective_airtime(self):
        playlist = Playlist.objects.create(name="Airtime Consistency Playlist")
        PlaylistItem.objects.create(playlist=playlist, position=0, track=self.ref_track)
        PlaylistItem.objects.create(playlist=playlist, position=1, track=self.marker_track)

        log, error = _build_from_playlist(date(2027, 5, 11), 7, playlist, target_duration_seconds=1229.0)
        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 2)
        delta = (items[1].scheduled_time - items[0].scheduled_time).total_seconds()
        self.assertAlmostEqual(delta, self.expected_airtime, places=3)

    def test_fill_remaining_hour_uses_effective_airtime(self):
        seed = make_track("Seed", "Seed Artist", self.category, next_start_seconds=3000.0, cue_in_seconds=0.0)
        picks = [{"position": 0, "scheduled_time": None, "track": seed, "category": self.category}]
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 5, 12, 8, 0))

        random.seed(1)
        all_picks, accumulated = fill_remaining_hour(
            picks, accumulated_seconds=3000.0, target_datetime=target_datetime,
            target_duration_seconds=3000.0 + self.expected_airtime,
        )
        new_picks = all_picks[1:]
        self.assertEqual(len(new_picks), 1)
        self.assertEqual(new_picks[0]["track"].id, self.ref_track.id)
        self.assertAlmostEqual(accumulated, 3000.0 + self.expected_airtime, places=3)

    def test_preview_uses_effective_airtime(self):
        rotation = Rotation.objects.create(name="Airtime Consistency Preview Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, track=self.ref_track)
        RotationSlot.objects.create(rotation=rotation, position=1, track=self.marker_track)
        ScheduleBlock.objects.create(
            specific_date=date(2027, 5, 13), start_time=time(9, 0), end_time=time(10, 0), rotation=rotation,
        )

        result, error = preview_hour_log(date(2027, 5, 13), 9, target_duration_seconds=1229.0)
        self.assertIsNone(error)
        items = result["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["duration"], self.expected_airtime)
        t0 = django_timezone.datetime.fromisoformat(items[0]["scheduled_time"])
        t1 = django_timezone.datetime.fromisoformat(items[1]["scheduled_time"])
        self.assertAlmostEqual((t1 - t0).total_seconds(), self.expected_airtime, places=3)


class ZeroRegressionEndToEndTests(TestCase):
    """Beyond the centralized helper-level zero-value coverage (see
    EffectiveAirtimeSecondsTests), one real end-to-end path proof: a
    track with an explicit next_start_seconds=0.0 must contribute
    ZERO seconds to the accumulated schedule, not its (much larger)
    duration_seconds -- through the actual _build_from_rotation walk,
    not just the helper in isolation."""

    def test_explicit_zero_next_start_contributes_zero_through_rotation_build(self):
        category = make_category("ZEROAIRTIME")
        zero_track = make_track(
            "Instant Crossfade", "Zero Artist", category,
            next_start_seconds=0.0, cue_in_seconds=0.0, duration_seconds=600.0,
        )
        marker_track = make_track("Marker", "Marker Artist Zero", category, next_start_seconds=50.0, cue_in_seconds=0.0)

        rotation = Rotation.objects.create(name="Zero Airtime Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, track=zero_track)
        RotationSlot.objects.create(rotation=rotation, position=1, track=marker_track)

        log, error = _build_from_rotation(date(2027, 5, 14), 10, rotation, target_duration_seconds=50.0)
        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 2)
        delta = (items[1].scheduled_time - items[0].scheduled_time).total_seconds()
        self.assertEqual(delta, 0.0, "next_start_seconds=0.0 must not be silently replaced by duration_seconds=600.0")


class MatchedPairLandingUsesEffectiveAirtimeTests(TestCase):
    """Deterministic pair example where raw next_start values would
    produce a materially worse landing than effective airtime values --
    proving the pair solver scores/lands using effective airtime
    (230 + 200 = 430), not raw next_start (238 + 205 = 443)."""

    def test_pair_lands_against_effective_airtime_total_not_raw_next_start(self):
        category = make_category("PAIRAIRTIME")
        track_a = make_track(
            "Pair A", "Pair Airtime Artist A", category,
            cue_in_seconds=8.0, next_start_seconds=238.0, duration_seconds=250.0,
        )
        track_b = make_track(
            "Pair B", "Pair Airtime Artist B", category,
            cue_in_seconds=5.0, next_start_seconds=205.0, duration_seconds=215.0,
        )
        # Effective airtimes: A=230.0, B=200.0 -- A+B=430.0 exactly.
        # Raw next_start_seconds: A=238.0, B=205.0 -- A+B=443.0, a
        # pairing the corrected code must NOT treat as a perfect landing.
        self.assertEqual(effective_airtime_seconds(track_a), 230.0)
        self.assertEqual(effective_airtime_seconds(track_b), 200.0)

        make_track("Decoy Short", "Pair Airtime Decoy C", category, next_start_seconds=20.0, cue_in_seconds=0.0)
        make_track("Decoy Long", "Pair Airtime Decoy D", category, next_start_seconds=900.0, cue_in_seconds=0.0)

        rotation = Rotation.objects.create(name="Pair Airtime Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=category)
        RotationSlot.objects.create(rotation=rotation, position=1, category=category)

        random.seed(42)
        target_date, hour = date(2027, 5, 15), 11
        # target_duration_seconds = 430 = A+B's EFFECTIVE total exactly.
        # If the search were still scoring against raw next_start
        # (238+205=443), this same target would land 13s off instead of
        # ~0s off.
        #
        # A landing_error this small is a HEALTHY build (well under
        # DURATION_FIT_MARGIN -- see the 1.1 Monitoring-noise-
        # suppression follow-up), so no SystemEvent is created; the
        # full diagnostics payload is still always printed, which is
        # where this test reads landing_errors from.
        with patch("library.services.log_builder.print") as mock_print:
            log, error = _build_from_rotation(target_date, hour, rotation, target_duration_seconds=430)
        self.assertIsNone(error)
        self.assertFalse(
            SystemEvent.objects.filter(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}").exists(),
        )

        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        self.assertEqual(len(items), 2)
        item_ids = {i.track_id for i in items}
        self.assertEqual(item_ids, {track_a.id, track_b.id}, "the near-perfect-landing pair must win over the poor-fit decoys")

        detail = None
        for call in mock_print.call_args_list:
            if len(call.args) == 2 and "selection diagnostics" in call.args[0] and "healthy" in call.args[0]:
                detail = call.args[1]
        self.assertIsNotNone(detail, "no healthy selection-diagnostics print call found")
        landing_errors = detail["landing_errors"]
        self.assertEqual(len(landing_errors), 1)
        # Landing against the EFFECTIVE total (430) is essentially
        # perfect. Had the search still used raw next_start (443
        # total), this same pair's landing_error against a 430 target
        # would have been 13.0, not ~0 -- this assertion is the
        # concrete proof the fix changed real search behavior, not
        # just an isolated formula.
        self.assertLess(landing_errors[0], 1.0)


class ExactFitUsesEffectiveAirtimeTests(TestCase):
    """Proves full-pool exact-fit (_pick_best_fit, fed by
    _extract_fit_candidates) compares remaining_seconds against
    effective airtime, not raw file-relative next_start_seconds."""

    def test_pick_best_fit_matches_on_effective_airtime_not_raw_next_start(self):
        category = make_category("EXACTFITAIRTIME")
        # cue_in=35 makes the raw-vs-effective GAP (35s) exceed
        # DURATION_FIT_MARGIN (30s) -- under the previously-deployed
        # raw-next_start formula, this track could never have matched
        # remaining_seconds=165 at all (|165-200|=35 > 30, excluded
        # entirely). Under the corrected effective-airtime formula
        # (200-35=165), it's a PERFECT match (diff=0).
        track = make_track(
            "Exact Fit Airtime Track", "Exact Fit Airtime Artist", category,
            cue_in_seconds=35.0, next_start_seconds=200.0, duration_seconds=210.0,
        )
        self.assertEqual(effective_airtime_seconds(track), 165.0)
        self.assertGreater(
            abs(165.0 - track.next_start_seconds), DURATION_FIT_MARGIN,
            "test setup sanity check: the raw next_start value must fall outside "
            "the margin, so this is a real behavior change, not a coincidence",
        )

        qs = Track.objects.filter(id=track.id)
        picked = _pick_best_fit(qs, remaining_seconds=165.0)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, track.id)
