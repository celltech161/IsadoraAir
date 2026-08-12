"""1.1 spec (2026-08-11) -- Rotation integration tests for the selection-
mode wiring in _build_from_rotation: normal/landing/exact-fit dispatch,
the direct-track-slot-blocks-pairing invariant, landing pairs consuming
two rotation slots in one step, and the SelectionDiagnostics SystemEvent.

Exhaustive coverage of all 83 enumerated spec tests lives across this
file and its siblings (test_log_builder_selection.py for pure-unit
candidate/weight/pair-search coverage); this file covers the "Rotation
integration" category specifically -- the real _build_from_rotation()
walk, not the algorithms in isolation."""
import random
from datetime import date

from django.test import TestCase
from django.utils import timezone as django_timezone

from library.models import (
    Artist, Category, CategoryKind, LogItem, PlaylistLog, Rotation, RotationSlot, Track,
)
from library.services.log_builder import (
    EXACT_FIT_THRESHOLD_SECONDS,
    PAIR_LANDING_THRESHOLD_SECONDS,
    _build_from_rotation,
    _pick_best_fit,
    fill_remaining_hour,
)
from monitoring.models import SystemEvent


def make_category(code, kind_code="test-pair-music"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": "Test Pair Music"})
    return Category.objects.create(code=code, name=code, kind=kind, artist_separation=0, title_separation=0)


def make_track(title, artist_name, category, duration, **overrides):
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(
        filepath=f"/tmp/does-not-exist/pairint-{Track.objects.count()}.mp3",
        filename="track.mp3", title=title, artist=artist, category=category,
        ready2air=True, duration_seconds=duration, next_start_seconds=duration,
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


class DirectTrackBlocksPairingTests(TestCase):
    """Invariant: a direct-track RotationSlot must never be paired,
    reordered, or treated as a candidate pool -- pairing falls back to
    sequential single-track selection whenever the immediately-next slot
    is direct-track."""

    def setUp(self):
        self.category = make_category("PAIRBLOCK")
        # Direct-track's own Track.category is DELIBERATELY a separate
        # category from the filler pool -- matching how a real rotation
        # is configured (a Legal ID direct-insert isn't itself a member
        # of the surrounding music category's pool) and eliminating any
        # chance that an earlier category-random slot coincidentally
        # draws this exact track before the direct-track slot inserts
        # it explicitly, which would be a pre-existing rotation-model
        # characteristic unrelated to pairing and would make this test
        # non-deterministic for the wrong reason.
        self.direct_category = make_category("PAIRBLOCK-LEGALID", kind_code="test-pair-legalid")
        self.direct_artist, _ = Artist.get_or_create_ci("Direct Insert Artist")
        self.direct_track = make_track("Legal ID", "Direct Insert Artist", self.direct_category, 15)
        for i in range(6):
            make_track(f"Filler {i}", f"Filler Artist {i}", self.category, 180)

    def test_direct_track_slot_appears_unmodified_and_pairing_skipped(self):
        rotation = Rotation.objects.create(name="Block Pairing Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)
        RotationSlot.objects.create(rotation=rotation, position=1, track=self.direct_track)
        RotationSlot.objects.create(rotation=rotation, position=2, category=self.category)

        random.seed(12345)
        # target_duration_seconds chosen so slot 0's remaining lands
        # squarely in the landing zone -- if pairing incorrectly reached
        # past the direct-track slot, it would try to pair slot 0 with
        # slot 2 instead of falling back, which this test would catch
        # via the direct track's position.
        log, error = _build_from_rotation(date(2027, 4, 1), 5, rotation, target_duration_seconds=500)

        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        direct_positions = [i.position for i in items if i.track_id == self.direct_track.id]
        self.assertEqual(len(direct_positions), 1, "direct track must appear exactly once, unmodified")
        # It must be the SECOND item (position 1) -- proves slot order
        # was preserved, not reordered around the pairing attempt.
        self.assertEqual(direct_positions[0], 1)


class LandingPairConsumesTwoSlotsTests(TestCase):
    """A rotation with two consecutive category-random slots, entered
    with remaining_seconds in the landing zone, must have BOTH slots
    filled by a single joint pair decision -- not one slot filled and
    the other left to fill_remaining_hour's fallback strategy."""

    def setUp(self):
        self.category = make_category("LANDINGPAIR")
        # One excellent pair (200+200=400, exact landing) among poor-fit
        # decoys, mirroring the pure-unit pair test's design.
        self.track_a = make_track("Landing A", "Artist A", self.category, 200)
        self.track_b = make_track("Landing B", "Artist B", self.category, 200)
        make_track("Decoy Short", "Artist C", self.category, 20)
        make_track("Decoy Long", "Artist D", self.category, 900)

    def test_two_slot_rotation_filled_by_one_pair(self):
        rotation = Rotation.objects.create(name="Landing Pair Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category)

        random.seed(42)
        log, error = _build_from_rotation(date(2027, 4, 2), 6, rotation, target_duration_seconds=400)

        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).order_by("position"))
        # Exactly two items -- both rotation slots consumed by the pair,
        # and fill_remaining_hour found nothing worth adding afterward
        # (a good landing should leave well under DURATION_FIT_MARGIN).
        self.assertEqual(len(items), 2)
        total_duration = sum(i.track.duration_seconds for i in items)
        self.assertLess(abs(400 - total_duration), 30)
        # Both tracks distinct -- never the same track played twice as
        # "a pair".
        self.assertNotEqual(items[0].track_id, items[1].track_id)


class SelectionDiagnosticsEventTests(TestCase):
    """_build_from_rotation must emit a SystemEvent summarizing which
    selection modes ran -- the forward-compatible diagnostics extension
    point, not a full advanced-rule surface."""

    def setUp(self):
        self.category = make_category("DIAGEVENT")
        for i in range(4):
            make_track(f"Diag Track {i}", f"Diag Artist {i}", self.category, 190)

    def test_diagnostics_event_emitted_with_expected_shape(self):
        rotation = Rotation.objects.create(name="Diagnostics Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)

        random.seed(7)
        target_date, hour = date(2027, 4, 3), 7
        log, error = _build_from_rotation(target_date, hour, rotation, target_duration_seconds=3600)
        self.assertIsNone(error)

        event = SystemEvent.objects.get(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}")
        self.assertEqual(event.category, "library")
        for key in (
            "normal_picks", "landing_pairs", "landing_pair_fallbacks", "exact_fit_picks",
            "direct_track_inserts", "pool_exhausted_picks", "landing_errors",
            "target_duration_seconds", "late_offset_seconds",
        ):
            self.assertIn(key, event.detail)
        self.assertEqual(event.detail["target_duration_seconds"], 3600)
        self.assertEqual(event.detail["late_offset_seconds"], 0)

    def test_late_offset_seconds_reflects_shortened_target(self):
        rotation = Rotation.objects.create(name="Diagnostics Rotation Late")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)

        random.seed(7)
        target_date, hour = date(2027, 4, 4), 8
        _build_from_rotation(target_date, hour, rotation, target_duration_seconds=3400)

        event = SystemEvent.objects.get(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}")
        self.assertEqual(event.detail["late_offset_seconds"], 200)


class ModeThresholdBoundaryTests(TestCase):
    """Time-boundary: remaining_seconds exactly at PAIR_LANDING_
    THRESHOLD_SECONDS/EXACT_FIT_THRESHOLD_SECONDS resolves to the
    documented side of each boundary (normal mode is remaining >
    threshold, i.e. the threshold value itself is NOT normal mode)."""

    def setUp(self):
        self.category = make_category("BOUNDARYTEST")
        for i in range(6):
            make_track(f"Boundary Track {i}", f"Boundary Artist {i}", self.category, 180)

    def test_exactly_at_landing_threshold_is_not_normal_mode(self):
        """remaining == PAIR_LANDING_THRESHOLD_SECONDS must NOT take the
        normal-mode branch (normal is remaining > threshold, strictly)."""
        rotation = Rotation.objects.create(name="Boundary Rotation A")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category)

        random.seed(3)
        target_date, hour = date(2027, 4, 5), 9
        _build_from_rotation(target_date, hour, rotation, target_duration_seconds=PAIR_LANDING_THRESHOLD_SECONDS)

        event = SystemEvent.objects.get(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}")
        self.assertEqual(event.detail["normal_picks"], 0)

    def test_exactly_at_exact_fit_threshold_is_exact_fit_mode(self):
        """remaining == EXACT_FIT_THRESHOLD_SECONDS must take the
        exact-fit branch (exact-fit is remaining <= threshold)."""
        rotation = Rotation.objects.create(name="Boundary Rotation B")
        RotationSlot.objects.create(rotation=rotation, position=0, category=self.category)

        random.seed(3)
        target_date, hour = date(2027, 4, 6), 10
        _build_from_rotation(target_date, hour, rotation, target_duration_seconds=EXACT_FIT_THRESHOLD_SECONDS)

        event = SystemEvent.objects.get(dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}")
        self.assertEqual(event.detail["landing_pairs"], 0)
        self.assertEqual(event.detail["normal_picks"], 0)


class FillRemainingHourPairingTests(TestCase):
    """1.1 spec problem #3/#5 ("fill_remaining_hour() doesn't jointly
    optimize fill pairs"): the fallback-category fill loop must also use
    landing-mode two-track pairing when entering the landing zone, not
    just single-track exact-fit picks."""

    def setUp(self):
        self.category = make_category("FILLPAIRTEST")

    def test_landing_zone_fill_uses_a_pair_not_two_separate_single_picks(self):
        # One excellent pair (200+200=400) among poor-fit decoys, same
        # shape as LandingPairConsumesTwoSlotsTests but exercised
        # through fill_remaining_hour directly rather than rotation slots.
        make_track("Fill Landing A", "Fill Artist A", self.category, 200)
        make_track("Fill Landing B", "Fill Artist B", self.category, 200)
        make_track("Fill Decoy Short", "Fill Artist C", self.category, 20)
        make_track("Fill Decoy Long", "Fill Artist D", self.category, 900)

        seed_pick = make_track("Seed", "Seed Artist", self.category, 3200)
        picks = [{"position": 0, "scheduled_time": None, "track": seed_pick, "category": self.category}]

        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 7, 5, 0))
        random.seed(42)
        all_picks, accumulated = fill_remaining_hour(
            picks, accumulated_seconds=3200, target_datetime=target_datetime, target_duration_seconds=3600,
        )

        new_picks = all_picks[1:]
        self.assertEqual(len(new_picks), 2, "landing pair should fill both slots in one step")
        self.assertLess(abs(3600 - accumulated), 30)
        self.assertNotEqual(new_picks[0]["track"].id, new_picks[1]["track"].id)


class SmallGapDurationAwareFallbackTests(TestCase):
    """1.1 spec second-pass correction, item 3: when a live request,
    skip, eject, or DJ substitution leaves only a small real wall-clock
    gap before the top of the hour, the fallback system must make
    sensible, duration-aware choices -- never silently degrade to
    ordinary unconstrained weighted selection just because no perfect
    candidate exists, and never force a wildly oversized pick onto a
    small gap either. Covers both _pick_best_fit directly (single-track
    exact-fit) and the full fill_remaining_hour landing-zone-pair-
    search-failed fallback path (force_fit_mode)."""

    def setUp(self):
        self.category = make_category("SMALLGAPTEST")

    def test_small_gap_prefers_short_candidate_not_high_weight_long_track(self):
        """~2:05 (125s) remaining; two short, sensible candidates (118s,
        131s) sit alongside two much longer ones (410s, 500s) given
        enormous rotation weight. The long tracks overshoot
        DURATION_FIT_MARGIN and must be structurally excluded from
        exact-fit selection -- rotation weight can never buy them a
        win the way it could in ordinary unconstrained weighted mode."""
        short_a = make_track("Short A", "Artist SA", self.category, 118, rotation_weight=1)
        short_b = make_track("Short B", "Artist SB", self.category, 131, rotation_weight=1)
        make_track("Long A", "Artist LA", self.category, 410, rotation_weight=1000)
        make_track("Long B", "Artist LB", self.category, 500, rotation_weight=1000)

        qs = Track.objects.filter(category=self.category)
        for _ in range(30):
            picked = _pick_best_fit(qs, remaining_seconds=125)
            self.assertIn(picked.id, (short_a.id, short_b.id))

    def test_small_gap_all_candidates_over_five_minutes_returns_none(self):
        """125s remaining, every candidate well over five minutes --
        must stop gracefully (None) rather than knowingly create a
        gross top-of-hour overrun just to ensure another song starts."""
        for i, dur in enumerate((310, 400, 480)):
            make_track(f"Overlong {i}", f"Overlong Artist {i}", self.category, dur)

        qs = Track.objects.filter(category=self.category)
        self.assertIsNone(_pick_best_fit(qs, remaining_seconds=125))

    def test_landing_zone_pair_search_fails_duration_aware_fallback_bounded_by_runway(self):
        """~9 minutes (540s) remain and no valid matched pair exists --
        the pool's only two tracks share an artist, so _pair_valid
        blocks the sole possible combination (and self-pairing is
        always invalid). The single-track fallback must still be
        duration-aware (force_fit_mode) rather than degrade to
        ordinary unconstrained weighted selection: a wildly oversized
        decoy carrying enormous rotation weight must not win merely
        because pairing failed."""
        same_artist, _ = Artist.get_or_create_ci("Shared Pairing Artist")
        good_fit = Track.objects.create(
            filepath="/tmp/does-not-exist/pairfallback-good.mp3", filename="good.mp3",
            title="Good Fit", artist=same_artist, category=self.category,
            ready2air=True, duration_seconds=530, next_start_seconds=530, rotation_weight=1,
        )
        Track.objects.create(
            filepath="/tmp/does-not-exist/pairfallback-decoy.mp3", filename="decoy.mp3",
            title="Oversized Decoy", artist=same_artist, category=self.category,
            ready2air=True, duration_seconds=1200, next_start_seconds=1200, rotation_weight=1000,
        )

        seed_pick = make_track("Seed", "Seed Artist", self.category, 3060)
        picks = [{"position": 0, "scheduled_time": None, "track": seed_pick, "category": self.category}]
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 8, 6, 0))

        random.seed(99)
        all_picks, accumulated = fill_remaining_hour(
            picks, accumulated_seconds=3060, target_datetime=target_datetime, target_duration_seconds=3600,
        )

        new_picks = all_picks[1:]
        self.assertEqual(len(new_picks), 1)
        self.assertEqual(new_picks[0]["track"].id, good_fit.id)
        self.assertLess(abs(3600 - accumulated), 30)

    def test_remainder_recalculated_each_iteration_determines_next_pick(self):
        """After a landing-zone fallback pick lands short of a perfect
        fit, the freshly recalculated remainder -- not the original,
        now-stale remaining_seconds -- must govern the very next
        selection. An overwhelming-weight track dominates the first
        (landing-zone) pick; the second pick's pool then has only one
        candidate that clears the margin against the NEW, much smaller
        remainder, proving the loop re-derives `remaining` on every
        iteration rather than reusing the value it started with."""
        same_artist, _ = Artist.get_or_create_ci("Remainder Mode Artist")
        big = Track.objects.create(
            filepath="/tmp/does-not-exist/remaindermode-big.mp3", filename="big.mp3",
            title="Big", artist=same_artist, category=self.category,
            ready2air=True, duration_seconds=550, next_start_seconds=550,
            rotation_weight=1000, last_played_at=None,
        )
        small = Track.objects.create(
            filepath="/tmp/does-not-exist/remaindermode-small.mp3", filename="small.mp3",
            title="Small", artist=same_artist, category=self.category,
            ready2air=True, duration_seconds=45, next_start_seconds=45,
            rotation_weight=1, last_played_at=django_timezone.now(),
        )

        seed_pick = make_track("Seed", "Seed Artist", self.category, 3000)
        picks = [{"position": 0, "scheduled_time": None, "track": seed_pick, "category": self.category}]
        target_datetime = django_timezone.make_aware(django_timezone.datetime(2027, 4, 9, 7, 0))

        random.seed(11)
        all_picks, accumulated = fill_remaining_hour(
            picks, accumulated_seconds=3000, target_datetime=target_datetime, target_duration_seconds=3600,
        )

        new_picks = all_picks[1:]
        self.assertEqual(len(new_picks), 2, "big landing-zone pick, then small exact-fit pick against the new remainder")
        self.assertEqual(new_picks[0]["track"].id, big.id)
        self.assertEqual(new_picks[1]["track"].id, small.id)
        self.assertLess(abs(3600 - accumulated), 30)
