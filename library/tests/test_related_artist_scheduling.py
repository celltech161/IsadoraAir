"""Related Artists' integration with log_builder.py's artist-separation
machinery: the mutual identity-set semantics (primary artist + related
artists), applied consistently across recency history, same-build hard
exclusion, fixed rotation-slot contributions, playlist-fill
contributions, and the separation-loosening loop -- while leaving
title separation, duplicate-track exclusion, and pool-exhaustion
fallback behavior untouched.

Uses direct pick_track()/get_recent_exclusions() calls where that's
enough to isolate the behavior precisely, and real
_build_from_rotation()/_build_from_playlist() calls for the
slot/playlist-contribution tests, which need the real build loop."""
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from library.models import (
    Artist, Category, CategoryKind, Holiday, LogItem, Playlist, PlaylistItem,
    PlaylistLog, RecencyConfig, Rotation, RotationSlot, Track,
)
from library.services.log_builder import (
    TrackIdentityCache, _build_from_playlist, _build_from_rotation,
    _music_holiday_pool, get_recent_exclusions, pick_track,
)
from library.services.related_artists import track_identity_keys


def make_category(code, artist_separation=2.5, title_separation=8.0, kind_code="test-music-kind"):
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": "Test Music Kind"})
    return Category.objects.create(
        code=code, name=code, kind=kind,
        artist_separation=artist_separation, title_separation=title_separation,
    )


def make_track(title, artist_name, related_artists="", category=None, **overrides):
    artist, _ = Artist.get_or_create_ci(artist_name)
    defaults = dict(
        filepath=f"/tmp/does-not-exist/sched-{Track.objects.count()}.mp3",
        filename="track.mp3",
        title=title,
        artist=artist,
        related_artists=related_artists,
        category=category,
        ready2air=True,
        duration_seconds=180.0,
        next_start_seconds=175.0,
    )
    defaults.update(overrides)
    return Track.objects.create(**defaults)


class PickTrackIdentityConflictTests(TestCase):
    """Direct pick_track() calls with a controlled 2-candidate pool:
    one candidate conflicts with the banned identity set, one doesn't
    -- isolates the exclusion mechanism from recency-history/timing
    concerns entirely."""

    def setUp(self):
        self.category = make_category("TESTCAT")
        self.now = timezone.now()

    def test_primary_artist_blocks_candidate_that_lists_it_as_related(self):
        # Banned identity: "Pink Floyd" was just played as a plain
        # primary artist, no related_artists of its own.
        banned = track_identity_keys("Pink Floyd", "")
        conflicting = make_track("Some Song", "Someone Else", related_artists="Pink Floyd", category=self.category)
        safe = make_track("Other Song", "Third Person", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)
        self.assertNotEqual(picked.id, conflicting.id)

    def test_related_artist_blocks_its_primary_artist_track(self):
        # Banned identity: a track whose PRIMARY is someone else but
        # whose related_artists lists "Pink Floyd" was just played.
        banned = track_identity_keys("Someone Else", "Pink Floyd")
        conflicting = make_track("Some Song", "Pink Floyd", related_artists="", category=self.category)
        safe = make_track("Other Song", "Third Person", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)
        self.assertNotEqual(picked.id, conflicting.id)

    def test_two_tracks_sharing_same_related_artist_conflict(self):
        # Neither track's PRIMARY artist matches the other's -- the
        # only overlap is a shared related-artist entry.
        banned = track_identity_keys("Artist X", "Roger Waters")
        conflicting = make_track("Some Song", "Artist Y", related_artists="Roger Waters", category=self.category)
        safe = make_track("Other Song", "Artist Z", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)

    def test_comparison_is_case_insensitive(self):
        banned = track_identity_keys("PINK FLOYD", "")
        conflicting = make_track("Some Song", "Someone Else", related_artists="pink floyd", category=self.category)
        safe = make_track("Other Song", "Third Person", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)

    def test_same_build_hard_exclusion_applies_the_conflict(self):
        # hard_exclude_identity_keys must hold even on the FIRST
        # attempt (no loosening involved at all) -- "already picked
        # this identity earlier in the same build."
        hard_banned = track_identity_keys("Pink Floyd", "")
        conflicting = make_track("Some Song", "Someone Else", related_artists="Pink Floyd", category=self.category)
        safe = make_track("Other Song", "Third Person", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), set(), artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
            hard_exclude_identity_keys=hard_banned,
        )
        self.assertEqual(picked.id, safe.id)
        self.assertNotEqual(picked.id, conflicting.id)


class RecentHistoryExclusionTests(TestCase):
    """get_recent_exclusions() itself -- confirms recently-PLAYED
    history (a real LogItem with played_at set) contributes both the
    primary artist AND every related-artist entry to the returned
    identity-key set, and that title separation (exclude_track_ids)
    is unaffected by any of this -- still purely track-id/scheduled_
    time based."""

    def setUp(self):
        self.category = make_category("HISTCAT")
        self.now = timezone.now()

    def _make_played_log_item(self, track, minutes_ago):
        log = PlaylistLog.objects.create(date=self.now.date(), hour=self.now.hour, status="approved")
        scheduled = self.now - timedelta(minutes=minutes_ago)
        return LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=scheduled,
            track=track, track_title=track.title, track_artist=track.artist.name,
            category=self.category, played_at=scheduled,
        )

    def test_recently_played_history_applies_the_conflict(self):
        played_track = make_track("Aired Song", "Someone Else", related_artists="Pink Floyd", category=self.category)
        self._make_played_log_item(played_track, minutes_ago=30)

        _exclude_tracks, exclude_identity_keys = get_recent_exclusions(
            self.now, artist_sep_hours=2.5, title_sep_hours=8.0,
            already_picked_tracks=[], already_picked_identity_keys=set(),
        )
        self.assertIn("pink floyd", exclude_identity_keys)
        self.assertIn("someone else", exclude_identity_keys)

    def test_title_separation_still_purely_track_id_based(self):
        """A same-titled, different-artist/related_artists track is
        still excluded by TRACK ID (title_sep), completely independent
        of the identity-key machinery -- confirms that refactor didn't
        touch this."""
        played_track = make_track("Shared Title", "Artist One", related_artists="Some Related", category=self.category)
        self._make_played_log_item(played_track, minutes_ago=30)

        exclude_tracks, _exclude_identity_keys = get_recent_exclusions(
            self.now, artist_sep_hours=2.5, title_sep_hours=8.0,
            already_picked_tracks=[], already_picked_identity_keys=set(),
        )
        self.assertIn(played_track.id, exclude_tracks)


class SeparationLooseningTests(TestCase):
    """Related-artist exclusion must loosen at the same point and by
    the same amount as ordinary artist separation -- both are folded
    into the exact same artist_sep-halving loop in pick_track."""

    def setUp(self):
        self.category = make_category("LOOSENCAT", artist_separation=2.0, title_separation=0.0)
        self.now = timezone.now()

    def test_related_artist_exclusion_loosens_with_artist_separation(self):
        # Only ONE track in the pool -- it conflicts with a play 1.5h
        # ago via a shared related artist. At full artist_sep=2.0h,
        # that play is inside the window (excluded, no candidate
        # survives). After one halving (artist_sep -> 1.0h), the
        # cutoff moves to now-1.0h -- a play at now-1.5h falls OUTSIDE
        # that narrower window, so the track becomes pickable again.
        only_track = make_track("Only Song", "Solo Artist", related_artists="Shared Name", category=self.category)
        # Deliberately NOT filed under self.category -- it must only
        # matter as PLAY HISTORY here, not as a second pool candidate
        # (title_sep=0 means it would otherwise become pickable again
        # itself once loosened, muddying which track the test is
        # actually proving survives).
        played_track = make_track("Aired Song", "Other Artist", related_artists="Shared Name", category=None)
        log = PlaylistLog.objects.create(date=self.now.date(), hour=self.now.hour, status="approved")
        scheduled = self.now - timedelta(hours=1.5)
        LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=scheduled,
            track=played_track, track_title=played_track.title, track_artist=played_track.artist.name,
            category=self.category, played_at=scheduled,
        )

        exclude_tracks, exclude_identity_keys = get_recent_exclusions(
            self.now, artist_sep_hours=2.0, title_sep_hours=0.0,
            already_picked_tracks=[], already_picked_identity_keys=set(),
        )
        picked = pick_track(
            self.category, exclude_tracks, exclude_identity_keys,
            artist_sep=2.0, title_sep=0.0, target_datetime=self.now, max_loosening=1,
        )
        # Succeeds only via the loosening pass -- proves the identity
        # exclusion window narrows in lockstep with artist_sep.
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, only_track.id)


class PoolExhaustionAndDuplicateBehaviorTests(TestCase):
    """Existing invariants (unrelated to related_artists) must still
    hold: duplicate-track exclusion is stronger than artist separation
    (yielded first), and the final pool-exhaustion fallback still lets
    a pool-of-1 category re-pick its only track."""

    def setUp(self):
        self.category = make_category("POOLCAT")
        self.now = timezone.now()

    def test_pool_of_one_survives_via_exhaustion_fallback(self):
        only_track = make_track("Only Song", "Solo Artist", category=self.category)
        # hard_exclude_track_ids holds the only track -- must still be
        # returned via the pool-exhaustion fallback (drops hard track
        # exclusion as an absolute last resort).
        picked = pick_track(
            self.category, set(), set(), artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
            hard_exclude_track_ids={only_track.id},
        )
        self.assertEqual(picked.id, only_track.id)

    def test_hard_track_exclusion_outlives_hard_identity_exclusion(self):
        """Same invariant as before this feature: the final pass drops
        history + hard artist/identity separation but KEEPS hard track
        exclusion -- "don't play the same artist too close together"
        yields before "don't repeat the exact same track.\""""
        t1 = make_track("Song One", "Artist A", category=self.category)
        t2 = make_track("Song Two", "Artist A", category=self.category)  # same artist, different track
        # Hard-exclude BOTH the track and its identity; only t2 remains
        # as a same-identity alternative, which should be preferred
        # over re-exhausting all the way to re-picking t1.
        picked = pick_track(
            self.category, set(), set(), artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
            hard_exclude_track_ids={t1.id},
            hard_exclude_identity_keys=track_identity_keys("Artist A", ""),
        )
        # t2 shares the identity, so it's excluded on the normal pass
        # too -- exhaustion drops IDENTITY exclusion in the "final
        # pass" (see pick_track), landing on t2, not t1 (t1's TRACK id
        # stays hard-excluded even in that fallback).
        self.assertEqual(picked.id, t2.id)


class RotationAndPlaylistContributionTests(TestCase):
    """Fixed rotation-slot tracks and playlist items must contribute
    their identity sets to LATER picks in the same build -- a direct
    track insert bypasses recency separation for its OWN insertion,
    but everything picked after it must still respect its identity."""

    def setUp(self):
        self.category = make_category("ROTCAT")

    def test_fixed_rotation_track_blocks_later_category_pick(self):
        fixed_track = make_track(
            "Fixed Insert", "Pink Floyd", related_artists="David Gilmour",
            category=self.category,
        )
        conflicting = make_track("Conflict Song", "David Gilmour", related_artists="", category=self.category)
        safe = make_track("Safe Song", "Totally Different Artist", related_artists="", category=self.category)

        rotation = Rotation.objects.create(name="Test Rotation")
        RotationSlot.objects.create(rotation=rotation, position=0, track=fixed_track)
        RotationSlot.objects.create(rotation=rotation, position=1, category=self.category)

        target_date = timezone.localdate()
        log, error = _build_from_rotation(target_date, 10, rotation)
        self.assertIsNone(error, error)

        items = list(log.items.order_by("position"))
        self.assertEqual(items[0].track_id, fixed_track.id)
        # The second slot's pick must not be the identity-conflicting
        # track -- "David Gilmour" was already inserted via the fixed
        # slot and must block it.
        self.assertNotEqual(items[1].track_id, conflicting.id)

    def test_playlist_items_contribute_identity_to_fill_picks(self):
        # Playlist is short (one item), forcing fill_remaining_hour to
        # top up the rest of the hour from the fallback category --
        # the playlist item's identity must still block a matching
        # fill candidate.
        from library.models import LogFillConfig

        # Leaves only ~55s remaining -- one fill pick's duration (180s
        # default) overshoots past DURATION_FIT_MARGIN and the loop
        # stops, so exactly ONE fill pick happens. Keeps this test
        # about "does the FIRST fill pick respect the playlist item's
        # identity", not about pool-exhaustion behavior with only two
        # candidates (a separate, already-covered invariant).
        playlist_track = make_track(
            "Playlist Song", "Pink Floyd", related_artists="David Gilmour",
            duration_seconds=3545.0, next_start_seconds=3545.0,
        )
        conflicting = make_track("Conflict Fill", "David Gilmour", related_artists="", category=self.category)
        safe = make_track("Safe Fill", "Totally Different Artist", related_artists="", category=self.category)
        # Only these two fill candidates exist; if the conflicting one
        # is correctly excluded, the one fill pick can only land on
        # `safe`.

        LogFillConfig.objects.all().delete()
        LogFillConfig.objects.create(strategy="fixed_category", fallback_category=self.category)

        playlist = Playlist.objects.create(name="Test Playlist")
        PlaylistItem.objects.create(playlist=playlist, position=0, track=playlist_track)

        target_date = timezone.localdate()
        log, error = _build_from_playlist(target_date, 11, playlist)
        self.assertIsNone(error, error)

        fill_track_ids = set(log.items.exclude(track_id=playlist_track.id).values_list("track_id", flat=True))
        self.assertNotIn(conflicting.id, fill_track_ids)


class WeightedAndDurationFitTests(TestCase):
    """Proves related-artist exclusion is applied server-side (SQL WHERE,
    before _weighted_order's ORDER BY / _pick_best_fit's 200-row LIMIT)
    rather than as a post-hoc Python-side filter over an already-sampled
    candidate set. If it were the latter, a category dominated by
    conflicting tracks could hide a valid candidate that exists but
    doesn't happen to land in a small random sample -- these tests
    construct exactly that shape and confirm the valid candidate is
    still found, deterministically."""

    def setUp(self):
        self.category = make_category("WEIGHTCAT")
        self.now = timezone.now()

    def test_weighted_selection_excludes_conflicting_candidates_regardless_of_weight(self):
        # Conflicting candidates carry the highest possible weight (5);
        # the only valid candidate has the lowest (0). If exclusion
        # weren't a real SQL WHERE clause, a weight-5 conflicting track
        # would dominate the weighted draw. Since the conflicting rows
        # never enter the query's result set at all, the weight-0 safe
        # track is the ONLY row pick_track can return -- deterministic,
        # not "usually."
        banned = track_identity_keys("Dominant Artist", "")
        for i in range(10):
            make_track(
                f"Conflict {i}", "Dominant Artist", related_artists="",
                category=self.category, rotation_weight=5,
            )
        safe = make_track(
            "Underdog Song", "Nobody Artist", related_artists="",
            category=self.category, rotation_weight=0,
        )

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)

    def test_more_than_200_conflicting_candidates_leaves_valid_pick_in_fit_mode(self):
        # 205 conflicting rows (> _pick_best_fit's 200-row candidate
        # slice) plus exactly one valid candidate. This is only
        # reliably deterministic if the conflicting rows are excluded
        # in SQL before the LIMIT 200 -- if exclusion happened AFTER
        # sampling 200 rows out of 206 total, the single safe row would
        # still usually (but not always) appear by chance, making this
        # a flaky, weak test; instead it PASSES DETERMINISTICALLY
        # because the base query itself only ever returns 1 row.
        conflicting_artist, _ = Artist.get_or_create_ci("Extremely Popular Artist")
        bulk = [
            Track(
                filepath=f"/tmp/does-not-exist/bulk-conflict-{i}.mp3",
                filename="track.mp3", title=f"Conflict {i}",
                artist=conflicting_artist, related_artists="",
                category=self.category, ready2air=True,
                duration_seconds=180.0, next_start_seconds=175.0,
            )
            for i in range(205)
        ]
        Track.objects.bulk_create(bulk)
        safe = make_track(
            "Only Safe Song", "Totally Different Artist", related_artists="",
            category=self.category, duration_seconds=195.0, next_start_seconds=190.0,
        )
        banned = track_identity_keys("Extremely Popular Artist", "")

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
            remaining_seconds=200,  # < DURATION_FIT_THRESHOLD (480) -> fit mode
        )
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, safe.id)

    def test_duration_fit_selects_among_valid_candidates_never_the_conflicting_one(self):
        # The conflicting candidate is a PERFECT duration fit (200s for
        # 200s remaining) -- if identity filtering only ran after
        # duration-fit scoring picked a winner, this track could still
        # win on fit quality alone. It must never be returned at all;
        # the pick must always land on one of the two valid candidates
        # regardless of their own relative fit.
        banned = track_identity_keys("Dominant Artist", "")
        conflicting = make_track(
            "Perfect Fit But Conflicts", "Dominant Artist", related_artists="",
            category=self.category, duration_seconds=200.0, next_start_seconds=200.0,
        )
        valid_a = make_track(
            "Valid Fit A", "Artist B", related_artists="",
            category=self.category, duration_seconds=150.0, next_start_seconds=150.0,
        )
        valid_b = make_track(
            "Valid Fit B", "Artist C", related_artists="",
            category=self.category, duration_seconds=195.0, next_start_seconds=195.0,
        )
        valid_ids = {valid_a.id, valid_b.id}

        for _ in range(5):
            picked = pick_track(
                self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
                target_datetime=self.now, max_loosening=0,
                remaining_seconds=200,
            )
            self.assertIn(picked.id, valid_ids)
            self.assertNotEqual(picked.id, conflicting.id)


class DynamicFilterInteractionTests(TestCase):
    """Identity-conflict exclusion is layered into _build_qs() alongside
    every other pre-existing filter -- neither bypasses the other."""

    def setUp(self):
        self.category = make_category("DYNCAT")
        self.now = timezone.now()

    def test_blocked_slot_and_identity_conflict_both_enforced(self):
        # Three tracks: one is identity-conflicting (excluded regardless
        # of blocked_slots), one is blocked for the exact target hour x
        # weekday slot (excluded regardless of identity), one is
        # neither -- only the third can survive both filters at once.
        target_datetime = self.now.replace(minute=0, second=0, microsecond=0)
        slot = target_datetime.weekday() * 24 + target_datetime.hour

        banned = track_identity_keys("Dominant Artist", "")
        make_track("Conflict", "Dominant Artist", related_artists="", category=self.category)
        make_track("Blocked", "Other Artist", related_artists="", category=self.category, blocked_slots=[slot])
        safe = make_track("Safe", "Third Artist", related_artists="", category=self.category)

        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=target_datetime, max_loosening=0,
        )
        self.assertEqual(picked.id, safe.id)

    def test_holiday_pool_override_still_enforces_identity_conflict(self):
        # pool_override_qs (the station-wide holiday pool, bypassing
        # the slot's normal category) must still have identity-
        # conflicting candidates excluded -- the override swaps WHICH
        # base pool is used, not whether exclusion applies to it.
        holiday_category = make_category("HOLCAT", kind_code="music")
        holiday, _ = Holiday.objects.get_or_create(
            code="TESTXMAS", defaults=dict(name="Test Christmas", month=12, day=25),
        )
        banned = track_identity_keys("Dominant Artist", "")
        conflicting = make_track(
            "Holiday Conflict", "Dominant Artist", related_artists="", category=holiday_category,
        )
        conflicting.holidays.add(holiday)
        safe = make_track("Holiday Safe", "Other Artist", related_artists="", category=holiday_category)
        safe.holidays.add(holiday)

        pool_qs = _music_holiday_pool(["TESTXMAS"], self.now)
        picked = pick_track(
            self.category, set(), banned, artist_sep=2.5, title_sep=8.0,
            target_datetime=self.now, max_loosening=0,
            pool_override_qs=pool_qs, pool_key=("holiday", ("TESTXMAS",)),
        )
        self.assertEqual(picked.id, safe.id)


class IdentityCacheReuseTests(TestCase):
    """TrackIdentityCache must load a given pool's identity data ONCE
    per build (per distinct pool_key), not once per pick_track() call
    and never once per candidate -- the N+1 pattern this cache exists
    to avoid. An empty banned-identity set short-circuits without
    loading the pool at all (see conflicting_track_ids), so these tests
    use a non-empty but non-matching banned set to force the real load
    path and actually exercise the cache."""

    def setUp(self):
        self.category = make_category("CACHECAT")
        self.now = timezone.now()
        self.ghost_banned = track_identity_keys("Nonexistent Ghost Artist", "")

    def test_pool_loaded_once_across_repeated_picks_same_pool_key(self):
        for i in range(15):
            make_track(f"Song {i}", f"Artist {i}", category=self.category)

        cache = TrackIdentityCache()
        pool_key = ("category", self.category.id)
        picked_ids = set()
        for _ in range(5):
            track = pick_track(
                self.category, set(), self.ghost_banned, artist_sep=2.5, title_sep=8.0,
                target_datetime=self.now, max_loosening=0,
                hard_exclude_track_ids=picked_ids,
                identity_cache=cache, pool_key=pool_key,
            )
            self.assertIsNotNone(track)
            picked_ids.add(track.id)

        # Loaded exactly once for this pool_key, no matter how many
        # pick_track() calls shared it.
        self.assertEqual(len(cache._pools), 1)
        self.assertIn(pool_key, cache._pools)
        self.assertEqual(len(cache._pools[pool_key]), 15)

    def test_query_count_does_not_scale_with_pick_count_for_same_pool(self):
        """Regression guard: 5 picks sharing one pool/cache must not
        cost anywhere near what per-call reloading (let alone
        per-candidate querying) of a 15-track pool would cost."""
        for i in range(15):
            make_track(f"Song {i}", f"Artist {i}", category=self.category)

        cache = TrackIdentityCache()
        pool_key = ("category", self.category.id)
        picked_ids = set()

        with CaptureQueriesContext(connection) as ctx:
            for _ in range(5):
                track = pick_track(
                    self.category, set(), self.ghost_banned, artist_sep=2.5, title_sep=8.0,
                    target_datetime=self.now, max_loosening=0,
                    hard_exclude_track_ids=picked_ids,
                    identity_cache=cache, pool_key=pool_key,
                )
                self.assertIsNotNone(track)
                picked_ids.add(track.id)

        # ~1 pool load + ~1 SELECT per pick is the expected cost (well
        # under 20); O(picks) reloading or O(candidates) per-pick
        # querying against a 15-track pool would blow well past it.
        self.assertLess(len(ctx.captured_queries), 20)
        self.assertEqual(len(cache._pools), 1)
