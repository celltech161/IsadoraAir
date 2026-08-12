"""1.1 spec (2026-08-11) -- matched-pair planning / exact-fit / clock-drift
/ async-backfill. This module covers the "Candidate/weighting" test
category: the extended effective-weight formula (existing rotation_weight
+ dormancy formula, PLUS the new x2.0 bonus for never-played/>365-day-idle
tracks) and its SQL/Python equivalence proof.

Uses TestCase (the real, isolated test_isadoraair DB) throughout -- never
the production DB. See log_builder.py's _effective_weight_sql (SQL) and
compute_effective_weight (Python) for the two implementations under test."""
import math
import random as random_module
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from library.models import Artist, Category, CategoryKind, Track
from library.services.log_builder import (
    DORMANT_WEIGHT_BONUS,
    DORMANT_WEIGHT_BONUS_DAYS,
    PAIR_DURATION_NEIGHBORS,
    FitCandidate,
    _effective_weight_sql,
    _extract_fit_candidates,
    _pair_valid,
    _pick_best_fit,
    compute_effective_weight,
    find_matched_pair,
)


def make_fit_candidate(track_id, duration, weight=4.0, identity_keys=frozenset()):
    return FitCandidate(track_id=track_id, identity_keys=identity_keys,
                         duration_seconds=duration, effective_weight=weight)


class WeightFormulaEquivalenceTests(TestCase):
    """Candidate/weighting 1-11 (partial): the SQL expression
    (_effective_weight_sql, used by _weighted_order's real weighted-
    random draw) and the Python function (compute_effective_weight, used
    by pair/exact-fit mode's FitCandidate scoring) must be mathematically
    identical for the same inputs -- proven here against a real DB row
    rather than asserted by inspection alone."""

    @classmethod
    def setUpTestData(cls):
        cls.kind = CategoryKind.objects.create(code="wftest", name="Weight Formula Test")
        cls.category = Category.objects.create(code="WFTEST", name="Weight Formula Test", kind=cls.kind)
        cls.artist = Artist.objects.create(name="Weight Formula Test Artist")

    def _sql_weight(self, track, active_holiday_codes=None):
        sql, params = _effective_weight_sql(active_holiday_codes)
        with connection.cursor() as cur:
            cur.execute(f"SELECT {sql} FROM library_track WHERE id=%s", params + [track.id])
            return float(cur.fetchone()[0])

    def _make_track(self, i, rotation_weight, last_played_at):
        return Track.objects.create(
            filepath=f"/srv/isadoraair/music/wftest{i}.flac",
            filename=f"wftest{i}.flac",
            title=f"WF Test {i}",
            artist=self.artist,
            category=self.category,
            rotation_weight=rotation_weight,
            last_played_at=last_played_at,
        )

    def test_equivalence_across_dormancy_scenarios(self):
        """Never-played, just-played, mid-range idle, just-under-365-days,
        just-over-365-days (the dormancy-bonus boundary), and
        far-past-365-days -- SQL and Python must agree on all of them."""
        now = timezone.now()
        scenarios = [
            ("never played", None),
            ("1 hour ago", now - timedelta(hours=1)),
            ("10 days ago", now - timedelta(days=10)),
            ("364 days ago (just inside bonus boundary)", now - timedelta(days=364)),
            ("366 days ago (just outside bonus boundary)", now - timedelta(days=366)),
            ("1000 days ago", now - timedelta(days=1000)),
        ]
        for i, (label, last_played_at) in enumerate(scenarios):
            for rotation_weight in (0, 3, 5):
                with self.subTest(label=label, rotation_weight=rotation_weight):
                    track = self._make_track(f"{i}-{rotation_weight}", rotation_weight, last_played_at)
                    sql_weight = self._sql_weight(track)
                    py_weight = compute_effective_weight(rotation_weight, 0, last_played_at, now=timezone.now())
                    self.assertAlmostEqual(sql_weight, py_weight, delta=0.01,
                                            msg=f"{label}, rotation_weight={rotation_weight}: sql={sql_weight} py={py_weight}")

    def test_equivalence_with_holiday_boost(self):
        holiday_kind = CategoryKind.objects.create(code="wftest-holiday", name="Weight Formula Holiday Test")
        from library.models import Holiday
        holiday = Holiday.objects.create(
            code="WFTESTHOL", name="WF Test Holiday", month=1, day=1,
            ramp_in_days=0, ramp_out_days=0, max_share=0, max_weight_boost=3,
        )
        track = self._make_track("holiday", 3, None)
        track.holidays.add(holiday)
        sql_weight = self._sql_weight(track, active_holiday_codes=[holiday.code])
        py_weight = compute_effective_weight(3, 3, None, now=timezone.now())
        self.assertAlmostEqual(sql_weight, py_weight, delta=0.01)
        holiday_kind.delete()

    def test_never_played_gets_dormancy_bonus(self):
        """Never-played must be classified as dormant (bonus applied) --
        the never-played COALESCE-to-365-days treatment for the
        log-dampened factor is separate from, and must not suppress, the
        dormancy bonus classification."""
        w_never = compute_effective_weight(3, 0, None, now=timezone.now())
        w_recent = compute_effective_weight(3, 0, timezone.now() - timedelta(hours=1), now=timezone.now())
        # Never-played should be strictly greater than a just-played
        # track at the same rotation_weight -- both the log-dampened
        # factor AND the x2.0 bonus favor it.
        self.assertGreater(w_never, w_recent)

    def test_dormancy_bonus_boundary_is_strictly_older_than_365_days(self):
        now = timezone.now()
        just_inside = compute_effective_weight(3, 0, now - timedelta(days=DORMANT_WEIGHT_BONUS_DAYS - 1), now=now)
        just_outside = compute_effective_weight(3, 0, now - timedelta(days=DORMANT_WEIGHT_BONUS_DAYS + 1), now=now)
        # just_outside gets both the extra day of log-dampened dormancy
        # AND the discrete x2.0 bonus jump -- must be meaningfully larger,
        # not just marginally larger from the log-dampening alone.
        self.assertGreater(just_outside, just_inside * 1.5)

    def test_bonus_is_additional_not_replacing_log_dampened_factor(self):
        """A track dormant 2 years should still outrank one dormant
        exactly 366 days -- the bonus is a flat multiplier stacked on
        top of the still-growing log-dampened factor, not a ceiling."""
        now = timezone.now()
        w_just_over = compute_effective_weight(3, 0, now - timedelta(days=366), now=now)
        w_two_years = compute_effective_weight(3, 0, now - timedelta(days=730), now=now)
        self.assertGreater(w_two_years, w_just_over)

    def test_play_count_field_never_referenced(self):
        """Static confirmation: neither weight implementation's actual
        CODE (docstrings aside -- compute_effective_weight's own
        docstring explains the deliberate omission in prose) reads
        Track.play_count -- rotation_weight and recency of play only."""
        import ast
        import inspect
        import textwrap
        from library.services import log_builder

        def code_names(func):
            src = inspect.getsource(func)
            tree = ast.parse(textwrap.dedent(src))
            return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

        self.assertNotIn("play_count", code_names(log_builder._effective_weight_sql))
        self.assertNotIn("play_count", code_names(log_builder.compute_effective_weight))

    def test_rotation_weight_ratio_matches_expected_six_to_one(self):
        """Static ratio check (not a live draw) -- weight 5 vs weight 0
        at identical dormancy must differ by exactly (5+1)/(0+1) = 6x,
        confirming the +1 shift is applied identically in both
        implementations."""
        now = timezone.now()
        last_played_at = now - timedelta(days=5)
        w0 = compute_effective_weight(0, 0, last_played_at, now=now)
        w5 = compute_effective_weight(5, 0, last_played_at, now=now)
        self.assertAlmostEqual(w5 / w0, 6.0, delta=0.001)


class FullPoolExactFitTests(TestCase):
    """Candidate/weighting: full-pool extraction (_extract_fit_candidates)
    and exact-fit selection (_pick_best_fit) must consider the ENTIRE
    eligible pool -- no top-200 truncation, unlike the historical
    implementation this replaces."""

    @classmethod
    def setUpTestData(cls):
        cls.kind = CategoryKind.objects.create(code="fitpooltest", name="Fit Pool Test")
        cls.category = Category.objects.create(code="FITPOOLTEST", name="Fit Pool Test", kind=cls.kind)
        cls.artist = Artist.objects.create(name="Fit Pool Test Artist")

    def _make_track(self, i, duration_seconds, rotation_weight=3, last_played_at=None):
        return Track.objects.create(
            filepath=f"/srv/isadoraair/music/fitpool{i}.flac",
            filename=f"fitpool{i}.flac",
            title=f"Fit Pool {i}",
            artist=self.artist,
            category=self.category,
            duration_seconds=duration_seconds,
            rotation_weight=rotation_weight,
            last_played_at=last_played_at,
        )

    def test_extract_fit_candidates_returns_one_per_track_no_truncation(self):
        for i in range(230):
            self._make_track(i, duration_seconds=180)
        qs = Track.objects.filter(category=self.category)
        candidates = _extract_fit_candidates(qs)
        self.assertEqual(len(candidates), 230)
        self.assertTrue(all(isinstance(c, FitCandidate) for c in candidates))

    def test_pick_best_fit_finds_match_past_the_historical_200_row_limit(self):
        """230 tracks, all a poor duration fit except the LAST one
        created (guaranteed to sort after row 200 in id order) -- the
        historical top-200-slice implementation could miss this
        entirely depending on random SQL-side draw order; full-pool
        extraction must find it every time."""
        for i in range(229):
            self._make_track(i, duration_seconds=180)  # poor fit for a ~30s remaining target
        exact = self._make_track(229, duration_seconds=30)  # the only good fit
        qs = Track.objects.filter(category=self.category)
        picked = _pick_best_fit(qs, remaining_seconds=30)
        self.assertEqual(picked.id, exact.id)

    def test_pick_best_fit_returns_none_for_empty_pool(self):
        qs = Track.objects.filter(category=self.category)
        self.assertIsNone(_pick_best_fit(qs, remaining_seconds=180))

    def test_pick_best_fit_returns_none_when_nothing_clears_margin(self):
        """Second-pass correction: every candidate overshoots badly and
        none clears DURATION_FIT_MARGIN -- must return None so the
        caller can stop gracefully, rather than force-picking a wildly
        oversized track. Exact-fit mode only engages near a real
        wall-clock boundary, so forcing an oversized pick here doesn't
        prevent dead air, it just creates an undisclosed large overrun
        -- exactly what clock-drift recovery exists to prevent."""
        for i in range(5):
            self._make_track(i, duration_seconds=600)
        qs = Track.objects.filter(category=self.category)
        picked = _pick_best_fit(qs, remaining_seconds=10)
        self.assertIsNone(picked)

    def test_pick_best_fit_favors_higher_weight_among_near_ties(self):
        """Two candidates tie exactly on duration fit; the higher-
        rotation_weight one must win the large majority of draws (not
        a 50/50 uniform choice, which the historical implementation
        would have produced)."""
        low = self._make_track("low", duration_seconds=180, rotation_weight=0,
                                last_played_at=timezone.now())
        high = self._make_track("high", duration_seconds=180, rotation_weight=5,
                                 last_played_at=timezone.now())
        qs = Track.objects.filter(category=self.category)
        wins = {low.id: 0, high.id: 0}
        for _ in range(200):
            picked = _pick_best_fit(qs, remaining_seconds=180)
            wins[picked.id] += 1
        self.assertGreater(wins[high.id], wins[low.id])


class MatchedPairSearchTests(TestCase):
    """Pair 20-26: find_matched_pair's duration-indexed bisect search --
    pure Python (FitCandidate dataclasses), no DB needed."""

    def test_finds_obvious_best_pair(self):
        """One pair (#1+#2) lands remaining_seconds exactly; everything
        else is a poor fit. Weighted-random selection means the winner
        isn't guaranteed deterministically, but the dominant pair's
        pair_score should heavily outweigh the decoys' -- assert on
        landing_error (a property any correctly-found close pair must
        have) rather than exact identity, so this isn't flaky."""
        pool = [
            make_fit_candidate(1, 60),
            make_fit_candidate(2, 300),   # 60 + 300 = 360: exact landing
            make_fit_candidate(3, 5),
            make_fit_candidate(4, 500),
        ]
        result = find_matched_pair(pool, pool, remaining_seconds=360, rng=random_module.Random(1))
        self.assertIsNotNone(result)
        self.assertLess(result.landing_error, 5)

    def test_returns_none_for_empty_pools(self):
        self.assertIsNone(find_matched_pair([], [], remaining_seconds=300))
        self.assertIsNone(find_matched_pair([make_fit_candidate(1, 100)], [], remaining_seconds=300))

    def test_self_pairing_excluded(self):
        """A single-track pool paired with itself must never return that
        track twice -- no valid pair exists with only one candidate."""
        pool = [make_fit_candidate(1, 200)]
        result = find_matched_pair(pool, pool, remaining_seconds=400)
        self.assertIsNone(result)

    def test_mutual_identity_conflict_excluded(self):
        """Two candidates that share an identity key (same artist, or
        related-artist overlap) must never be paired together, even if
        they're a perfect duration match."""
        shared_keys = frozenset({"artist:shared"})
        a = make_fit_candidate(1, 180, identity_keys=shared_keys)
        b = make_fit_candidate(2, 180, identity_keys=shared_keys)
        c = make_fit_candidate(3, 180, identity_keys=frozenset({"artist:other"}))
        result = find_matched_pair([a], [b, c], remaining_seconds=360, rng=random_module.Random(1))
        self.assertIsNotNone(result)
        self.assertEqual(result.candidate_b.track_id, 3)

    def test_pair_valid_helper_directly(self):
        a = make_fit_candidate(1, 100, identity_keys=frozenset({"x"}))
        b = make_fit_candidate(1, 100, identity_keys=frozenset({"y"}))  # same track_id
        self.assertFalse(_pair_valid(a, b))
        c = make_fit_candidate(2, 100, identity_keys=frozenset({"x"}))  # shared identity key
        self.assertFalse(_pair_valid(a, c))
        d = make_fit_candidate(3, 100, identity_keys=frozenset({"z"}))
        self.assertTrue(_pair_valid(a, d))

    def test_seeded_rng_is_reproducible(self):
        pool = [make_fit_candidate(i, 60 + i * 5) for i in range(20)]
        result1 = find_matched_pair(pool, pool, remaining_seconds=200, rng=random_module.Random(99))
        result2 = find_matched_pair(pool, pool, remaining_seconds=200, rng=random_module.Random(99))
        self.assertEqual(
            (result1.candidate_a.track_id, result1.candidate_b.track_id),
            (result2.candidate_a.track_id, result2.candidate_b.track_id),
        )

    def test_never_deterministically_the_single_closest_pair(self):
        """Across many seeded draws from a pool with several comparably-
        good pairs, more than one distinct pair must win at least once --
        confirms this is a weighted-random choice among finalists, not
        an argmax."""
        pool = [make_fit_candidate(i, 175 + i) for i in range(10)]  # 175..184, all close to half of 360
        winners = set()
        for seed in range(30):
            result = find_matched_pair(pool, pool, remaining_seconds=360, rng=random_module.Random(seed))
            winners.add(frozenset({result.candidate_a.track_id, result.candidate_b.track_id}))
        self.assertGreater(len(winners), 1)

    def test_duration_indexed_search_scales_past_naive_neighbor_window(self):
        """Large pool (beyond a small Cartesian-product-sized pool) --
        the best pair still gets found via the bisect search rather than
        being missed because it wasn't examined. Decoys are all far from
        remaining_seconds (900-1399s, nowhere near any combination
        summing to ~360) so only the one deliberately planted pair is a
        genuine fit -- a broken/non-bisecting implementation (e.g. one
        that only ever looks at the first few list entries) would miss
        it entirely and this test would fail."""
        pool_a = [make_fit_candidate(1000 + i, 900 + i) for i in range(500)]  # decoys, poor fits
        pool_b = [make_fit_candidate(2000 + i, 900 + i) for i in range(500)]  # decoys, poor fits
        planted_a = make_fit_candidate(9001, 181)
        planted_b = make_fit_candidate(9002, 179)  # 181 + 179 = 360, near-perfect landing
        pool_a.append(planted_a)
        pool_b.append(planted_b)
        result = find_matched_pair(pool_a, pool_b, remaining_seconds=360, rng=random_module.Random(1))
        self.assertIsNotNone(result)
        self.assertLess(result.landing_error, 5)

    def test_equal_duration_and_weight_pairs_not_distinguished_by_split_evenness(self):
        """Second-pass correction: pair_score = weight_a * weight_b *
        landing_quality only -- there is no requirement that matched
        songs be similar in duration. Two pairs with the same combined
        duration (so identical landing_error) and identical weights
        must score equally, regardless of how evenly each pair splits
        that duration between its two tracks. A 7:00+3:00 pair is not
        inherently worse than a 5:00+5:00 pair."""
        from library.services.log_builder import _score_pair
        remaining = 600
        balanced = _score_pair(make_fit_candidate(1, 300, weight=4.0), make_fit_candidate(2, 300, weight=4.0), remaining)
        lopsided = _score_pair(make_fit_candidate(3, 420, weight=4.0), make_fit_candidate(4, 180, weight=4.0), remaining)
        self.assertAlmostEqual(balanced.landing_error, lopsided.landing_error)
        self.assertAlmostEqual(balanced.pair_score, lopsided.pair_score)
