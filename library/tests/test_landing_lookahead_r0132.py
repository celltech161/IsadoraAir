"""r0132 Package A: fair multi-track end-of-hour lookahead.

Pure-helper tests (_landing_quality, _landing_lookahead_table,
_choose_landing_candidate) plus walk tests through the real
_build_from_rotation()/_plan_hour(). Web Requests and operator replacement of
lookahead-built items live in webrequests/tests/test_matched_pair_replaceability.py."""
import random
import time
from collections import Counter
from datetime import date
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone as django_timezone

from library.models import Artist, Category, CategoryKind, Holiday, LogItem, Rotation, RotationSlot, Track
from library.services import log_builder
from library.services.log_builder import (
    DURATION_FIT_MARGIN,
    FitCandidate,
    SegmentPlan,
    _build_from_rotation,
    _choose_landing_candidate,
    _landing_lookahead_table,
    _landing_quality,
    _landing_weights,
    _plan_hour,
    effective_airtime_seconds,
    find_matched_pair,
)
from monitoring.models import SystemEvent

from .test_rotation_pair_integration import _find_healthy_diagnostics_print


def fc(track_id, duration, weight=10.0, artist=None):
    return FitCandidate(track_id=track_id, identity_keys=frozenset({artist or f"artist {track_id}"}),
                        duration_seconds=float(duration), effective_weight=weight)


def probabilities(candidates, remaining, lookup, more_slots_follow=False):
    """Exact selection probabilities of _choose_landing_candidate."""
    admissible, weights = _landing_weights(candidates, remaining, lookup, more_slots_follow)
    total = sum(weights)
    return {c.track_id: w / total for c, w in zip(admissible, weights)}


class LandingQualityTests(SimpleTestCase):
    def test_overrun_beyond_margin_is_never_acceptable(self):
        self.assertEqual(_landing_quality(-DURATION_FIT_MARGIN - 0.01), 0.0)
        self.assertGreater(_landing_quality(-DURATION_FIT_MARGIN), 0.0)
        for within in (-5, 0, 5):
            self.assertEqual(_landing_quality(within), 1.0)
        self.assertLess(_landing_quality(-20), _landing_quality(20))     # overrun decays faster than shortfall
        self.assertLess(_landing_quality(200), 1e-6)


class LookaheadFairnessTests(SimpleTestCase):
    def test_preference_is_the_best_completion_never_the_number_of_completions(self):
        """X leaves 100 s that fifty later tracks can fill; Y leaves 200 s that exactly
        one later track can fill. Equal weights, equally landable: equal chances."""
        x, y = fc(1, 300), fc(2, 200)
        later = [fc(100 + i, 100) for i in range(50)] + [fc(999, 200)]
        lookup = _landing_lookahead_table([later], 400)
        self.assertEqual(lookup(100), lookup(200))
        self.assertEqual(probabilities([x, y], 400, lookup), {1: 0.5, 2: 0.5})
        rng = random.Random(3)
        counts = Counter(_choose_landing_candidate([x, y], 400, lookup, rng=rng).track_id for _ in range(4000))
        self.assertLess(abs(counts[1] - counts[2]), 300)

    def test_equal_weights_and_equal_landing_opportunity_give_equal_chances(self):
        pool = [fc(i, 140 + i) for i in range(30)]
        lookup = _landing_lookahead_table([pool, pool, pool], 600)
        probs = probabilities(pool, 600, lookup)
        self.assertEqual(len(set(round(p, 12) for p in probs.values())), 1)
        rng = random.Random(11)
        counts = Counter(_choose_landing_candidate(pool, 600, lookup, rng=rng).track_id for _ in range(30000))
        for tid in range(30):
            self.assertLess(abs(counts[tid] - 1000), 150, tid)       # ~4.7 sigma

    def test_the_window_is_a_maximum_and_shorter_sequences_are_not_favored(self):
        """Window of four: 300+300 lands in two picks, 150 x 4 in four. Both land
        equally well, so equal weights give equal first-pick chances -- neither the
        two-track finish nor the four-track one is preferred."""
        pool = [fc(i, 300) for i in range(5)] + [fc(10 + i, 150) for i in range(5)]
        lookup = _landing_lookahead_table([pool, pool, pool], 600)
        probs = probabilities(pool, 600, lookup)
        self.assertEqual(len(set(round(p, 12) for p in probs.values())), 1)
        self.assertEqual(lookup(300), lookup(450))
        self.assertEqual(lookup(0), lookup(300))     # landing now == landing two tracks later

    def test_rotation_weight_keeps_its_meaning(self):
        pool = [fc(1, 150, weight=30.0), fc(2, 150, weight=10.0)]
        lookup = _landing_lookahead_table([pool], 300)
        self.assertAlmostEqual(probabilities(pool, 300, lookup)[1], 0.75)

    def test_candidate_and_pool_order_never_change_a_seeded_result(self):
        pool = [fc(i, 120 + 3 * i, weight=5 + i % 7) for i in range(40)]
        shuffled = pool[:]
        random.Random(1).shuffle(shuffled)
        a = _landing_lookahead_table([pool, pool], 500)
        b = _landing_lookahead_table([shuffled, list(reversed(pool))], 500)
        self.assertEqual([a(r) for r in range(0, 520, 7)], [b(r) for r in range(0, 520, 7)])
        r1, r2 = random.Random(42), random.Random(42)
        self.assertEqual([_choose_landing_candidate(pool, 500, a, rng=r1).track_id for _ in range(200)],
                         [_choose_landing_candidate(shuffled, 500, b, rng=r2).track_id for _ in range(200)])

    def _ordinary_pool_with_one_long_track(self):
        rng = random.Random(5)
        pool = [fc(i, rng.uniform(120, 210)) for i in range(1000)]
        return pool + [fc(5000, 496)]

    def test_one_long_track_in_a_large_ordinary_pool_gets_its_share_not_dominance(self):
        pool = self._ordinary_pool_with_one_long_track()
        lookup = _landing_lookahead_table([pool, pool, pool], 700)
        p_long = probabilities(pool, 700, lookup)[5000]
        fair = 1 / len(pool)
        self.assertLess(p_long, 2 * fair)
        self.assertGreater(p_long, 0.5 * fair)      # not penalized for its length
        # The r0131 two-track solver, same pool and runway, picks it nearly always.
        rng = random.Random(9)
        hits = sum(5000 in (r.candidate_a.track_id, r.candidate_b.track_id)
                   for r in (find_matched_pair(pool, pool, 700, rng=rng) for _ in range(50)))
        self.assertGreaterEqual(hits, 45)

    def test_a_long_recording_is_chosen_when_it_is_the_appropriate_landing(self):
        """Two-slot tail at 700 s: only the long track plus a ~204 s partner lands, in
        either order. The two-pick sequence contains it almost always."""
        pool = self._ordinary_pool_with_one_long_track()
        rng = random.Random(8)
        hits = 0
        for _ in range(200):
            first = _choose_landing_candidate(pool, 700, _landing_lookahead_table([pool], 700), rng=rng)
            rest = [c for c in pool if c.track_id != first.track_id]
            runway = 700 - first.duration_seconds
            second = _choose_landing_candidate(rest, runway, _landing_lookahead_table([], runway), rng=rng)
            hits += 5000 in (first.track_id, getattr(second, "track_id", None))
        self.assertGreaterEqual(hits, 180)

    def test_long_only_pool_still_produces_long_admissible_picks(self):
        rng = random.Random(2)
        pool = [fc(i, rng.uniform(380, 600)) for i in range(40)]
        lookup = _landing_lookahead_table([pool], 700)
        for _ in range(50):
            chosen = _choose_landing_candidate(pool, 700, lookup, rng=rng)
            self.assertIsNotNone(chosen)
            self.assertLessEqual(chosen.duration_seconds, 700 + DURATION_FIT_MARGIN)

    def test_no_length_bias_until_a_landing_is_within_reach_of_the_window(self):
        short_items = [fc(i, 20 if i % 2 else 40) for i in range(20)]
        lookup = _landing_lookahead_table([short_items] * 3, 700)
        follow = probabilities(short_items, 700, lookup, more_slots_follow=True)
        self.assertAlmostEqual(min(follow.values()), max(follow.values()))
        # The same pool when the window is the end of the rotation: judged, best effort.
        final = probabilities(short_items, 700, lookup, more_slots_follow=False)
        self.assertGreater(final[0], final[1])      # 40 s item leaves less shortfall

class LookaheadConstraintTests(SimpleTestCase):
    def test_overshoot_limit_and_no_admissible_candidate(self):
        pool = [fc(1, 150), fc(2, 90)]
        lookup = _landing_lookahead_table([pool], 100)
        rng = random.Random(1)
        self.assertEqual({_choose_landing_candidate(pool, 100, lookup, rng=rng).track_id for _ in range(50)}, {2})
        self.assertIsNone(_choose_landing_candidate([fc(3, 140)], 100, lookup, rng=rng))

    def test_tight_remaining_window_with_nothing_short_enough(self):
        pool = [fc(i, 86 + i) for i in range(20)]
        lookup = _landing_lookahead_table([pool], 40)
        self.assertIsNone(_choose_landing_candidate(pool, 40, lookup))

    def test_cost_is_bounded_by_bins_not_pool_size(self):
        rng = random.Random(4)
        big = [fc(i, rng.uniform(5, 900)) for i in range(20000)]
        started = time.perf_counter()
        lookup = _landing_lookahead_table([big, big, big], 720)
        self.assertIsNotNone(_choose_landing_candidate(big, 720, lookup, rng=rng))
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_deadline_returns_none(self):
        pool = [fc(i, 150) for i in range(10)]
        self.assertIsNone(_landing_lookahead_table([pool], 600, deadline=time.monotonic() - 1))


def music_category(code):
    kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
    return Category.objects.create(code=code, name=code, kind=kind, artist_separation=0, title_separation=0)


def make_track(title, artist_name, category, duration, **overrides):
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(filepath=f"/tmp/does-not-exist/r0132-{Track.objects.count()}-{title}.mp3", filename="t.mp3",
                    title=title, artist=artist, category=category, ready2air=True,
                    duration_seconds=duration, next_start_seconds=duration)
    defaults.update(overrides)
    return Track.objects.create(**defaults)


def rotation_of(name, *slots):
    rotation = Rotation.objects.create(name=name)
    for position, slot in enumerate(slots):
        if isinstance(slot, Track):
            RotationSlot.objects.create(rotation=rotation, position=position, track=slot)
        else:
            RotationSlot.objects.create(rotation=rotation, position=position, category=slot)
    return rotation


class LookaheadWalkTests(TestCase):
    def setUp(self):
        self.pool = music_category("R0132POOL")
        for i in range(30):
            make_track(f"Pool {i}", f"Pool Artist {i}", self.pool, 100 + i * 4)

    def build(self, rotation, target, day=1, hour=6):
        target_date = date(2027, 6, day)
        with patch("library.services.log_builder.print") as mock_print:
            log, error = _build_from_rotation(target_date, hour, rotation, target_duration_seconds=target)
        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).select_related("track", "category").order_by("position"))
        event = SystemEvent.objects.filter(
            dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}").first()
        detail = event.detail if event else _find_healthy_diagnostics_print(mock_print)
        return log, items, detail, event

    def assert_timeline(self, log, items, offset=0.0):
        start = django_timezone.make_aware(django_timezone.datetime(log.date.year, log.date.month, log.date.day, log.hour))
        elapsed = offset
        for item in items:
            self.assertEqual(item.scheduled_time, start + django_timezone.timedelta(seconds=elapsed))
            elapsed += effective_airtime_seconds(item.track)
        return elapsed

    def test_two_three_and_four_slot_tails_land_with_independent_items(self):
        for day, (slots, target) in enumerate(((2, 300), (3, 450), (4, 600)), start=1):
            random.seed(day)
            rotation = rotation_of(f"Tail {slots}", *[self.pool] * slots)
            log, items, detail, event = self.build(rotation, target, day=day)
            self.assertIsNone(event, detail)
            self.assertLessEqual(len(items), slots)
            total = self.assert_timeline(log, items)
            self.assertLessEqual(abs(total - target), DURATION_FIT_MARGIN)
            self.assertEqual(len({i.track_id for i in items}), len(items))
            self.assertTrue(all(i.category_id == self.pool.id for i in items))
            self.assertGreaterEqual(detail["lookahead_picks"], 1)
            self.assertEqual((detail["pool_exhausted_picks"], detail["landing_pairs"]), (0, 0))

    def test_planner_finishes_early_without_forcing_extra_tracks(self):
        longs = music_category("R0132TWO")
        for i in range(6):
            make_track(f"Two {i}", f"Two Artist {i}", longs, 300)
        rotation = rotation_of("Two of four", *[longs] * 4)
        random.seed(5)
        log, items, detail, event = self.build(rotation, 600)
        self.assertIsNone(event, detail)
        self.assertEqual(len(items), 2)                # two slots simply stay unused
        self.assertEqual(self.assert_timeline(log, items), 600)
        self.assertEqual(detail["pool_exhausted_picks"], 0)

    def test_mixed_categories_fill_each_slot_from_its_own_pool(self):
        shorts, longs = music_category("R0132SHORT"), music_category("R0132LONG")
        for i in range(12):
            make_track(f"Short {i}", f"Short Artist {i}", shorts, 100 + i * 3)
            make_track(f"Long {i}", f"Long Artist {i}", longs, 250 + i * 5)
        rotation = rotation_of("Mixed", shorts, longs, shorts)
        random.seed(4)
        log, items, detail, event = self.build(rotation, 520)
        self.assertIsNone(event, detail)
        self.assertEqual([i.category_id for i in items], [shorts.id, longs.id, shorts.id][:len(items)])
        self.assertTrue(all(i.track.category_id == i.category_id for i in items))
        self.assertLessEqual(abs(self.assert_timeline(log, items) - 520), DURATION_FIT_MARGIN)

    def test_direct_track_stops_the_window_and_keeps_its_position(self):
        ident = music_category("R0132ID")
        direct = make_track("Station ID", "Station", ident, 30)
        rotation = rotation_of("Direct", self.pool, self.pool, direct, self.pool, self.pool)
        for seed in range(5):
            random.seed(seed)
            log, items, detail, event = self.build(rotation, 600, day=seed + 1)
            positions = [i.position for i in items if i.track_id == direct.id]
            self.assertEqual(positions, [2])
            self.assertEqual(items[2].category_id, ident.id)
            self.assertEqual(detail["direct_track_inserts"], 1)

    def test_artist_separation_holds_across_lookahead_picks(self):
        shared = music_category("R0132SHARED")
        for i in range(12):
            make_track(f"Shared {i}", f"Shared Artist {i % 6}", shared, 150)
        rotation = rotation_of("Shared", *[shared] * 4)
        for seed in range(5):
            random.seed(seed)
            _log, items, _detail, _event = self.build(rotation, 600, day=seed + 1)
            self.assertEqual(len(items), 4)
            self.assertEqual(len({i.track.artist_id for i in items}), 4)

    def test_holiday_dice_roll_once_per_slot_at_its_own_turn(self):
        day = date(2027, 12, 25)
        Holiday.objects.create(code="R0132XMAS", name="R0132 Christmas", month=12, day=25,
                               ramp_in_days=0, ramp_out_days=0, max_share=1, max_weight_boost=0)
        holiday = Holiday.objects.get(code="R0132XMAS")
        for i in range(10):
            make_track(f"Carol {i}", f"Carol Artist {i}", self.pool, 150).holidays.add(holiday)
        rotation = rotation_of("Holiday", *[self.pool] * 3)
        rolls = []
        real_random = random.random

        def counted():
            rolls.append(1)
            return real_random()
        random.seed(8)
        with patch("library.services.log_builder.random.random", side_effect=counted), \
                patch("library.services.log_builder.print"):
            log, error = _build_from_rotation(day, 6, rotation, target_duration_seconds=450)
        self.assertIsNone(error)
        items = list(LogItem.objects.filter(playlist_log=log).select_related("track"))
        self.assertEqual(len(rolls), len(items))                       # one roll per visited slot
        self.assertTrue(all(i.track.holidays.filter(code="R0132XMAS").exists() for i in items))

    def test_empty_pool_is_still_a_genuine_warning(self):
        empty = music_category("R0132EMPTY")
        rotation = rotation_of("Empty", empty, self.pool)
        random.seed(1)
        _log, items, detail, event = self.build(rotation, 400)
        self.assertIsNotNone(event)
        self.assertEqual(detail["pool_exhausted_picks"], 1)

    def test_slots_left_after_landing_within_tolerance_do_not_warn(self):
        even = music_category("R0132EVEN")
        for i in range(10):
            make_track(f"Even {i}", f"Even Artist {i}", even, 147)
        rotation = rotation_of("Landed", *[even] * 6)
        random.seed(2)
        _log, items, detail, event = self.build(rotation, 600)
        self.assertIsNone(event)                     # r0131 counted these slots as exhausted and warned
        self.assertEqual(len(items), 4)
        self.assertEqual(detail["pool_exhausted_picks"], 0)
        self.assertEqual(detail["landed_unused_slots"], 2)
        self.assertEqual(detail["landing_errors"], [12.0])

    def test_segment_boundary_is_the_landing_target(self):
        first = rotation_of("Segment 1", *[self.pool] * 20)
        second = rotation_of("Segment 2", *[self.pool] * 20)
        random.seed(6)
        plans = [SegmentPlan(kind="rotation", source=first, start_minute=0),
                 SegmentPlan(kind="rotation", source=second, start_minute=30)]
        with patch("library.services.log_builder.print"):
            ctx, records, error = _plan_hour(date(2027, 6, 9), 6, plans, 3600)
        self.assertIsNone(error)
        self.assertLessEqual(abs(records[0]["actual_end_seconds"] - 1800), DURATION_FIT_MARGIN)
        self.assertGreaterEqual(ctx.diagnostics.lookahead_picks, 2)

    def test_seeded_builds_are_reproducible(self):
        rotation = rotation_of("Repro", *[self.pool] * 6)
        runs = []
        for day in (1, 2):
            random.seed(77)
            _log, items, _detail, _event = self.build(rotation, 700, day=day)
            runs.append([i.track_id for i in items])
        self.assertEqual(runs[0], runs[1])

    def test_planning_budget_exhaustion_falls_back_to_a_duration_aware_pick(self):
        rotation = rotation_of("Budget", *[self.pool] * 4)
        random.seed(3)
        with patch.object(log_builder, "PAIR_SOLVER_BUDGET_MS", -1000):
            log, items, detail, event = self.build(rotation, 600)
        self.assertEqual(detail["lookahead_picks"], 0)
        self.assertGreaterEqual(detail["exact_fit_picks"], 1)
        self.assertLessEqual(self.assert_timeline(log, items) - 600, DURATION_FIT_MARGIN)
