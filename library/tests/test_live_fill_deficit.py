"""1.1 correction pass (2026-08-12): committed-runway-aware deficit
check for _try_extend_live_log_async.

Production reproduction: on 2026-08-12 the Wednesday 22:00 hour (the
Grateful Dead Hour rotation) built a correct 10-item log ending in a
~26.5-minute "1977 Part 2" segment. The instant Part 2 was dequeued
and started playing, self._queue_cursor reached the end of
self.log_items. _try_extend_live_log_async then fired on the very next
poll tick and computed "accumulated = 3600 - wall_seconds_left" -- pure
wall-clock elapsed, with zero awareness that the last dequeued item
was still going to run for another 26 minutes -- so
fill_remaining_hour perceived a ~26-minute deficit and dutifully
appended 50 consecutive PSA tracks. Rollover at ~30s before TOH
still cut over correctly (that was verified live and remains
untouched by this fix), but the "Coming Up" queue looked ridiculous
throughout Part 2.

The fix: compare wall-clock-remaining-in-hour against an authoritative
`_committed_future_runway_seconds()` (leading deck remaining playout +
effective airtime of every FUTURE queued item, cursor-aware -- never
counts already-played items, never counts a substitute the cursor
skipped past), and dispatch only when the deficit is genuinely
positive beyond DURATION_FIT_MARGIN. Also passes the deficit as
target_duration_seconds into fill_remaining_hour rather than a
hardcoded NOMINAL_HOUR_SECONDS.

Uses the same bare-stand-in + SimpleNamespace-fake-deck pattern as
test_engine_queue_eta.py's make_stand_in/make_fake_deck (no real
GStreamer pipeline needed for position math), extended to also drive
_try_extend_live_log_async's own state (running, current_log,
_live_fill_generation, etc.). Real DB-backed Track/LogItem rows
throughout, since fill_remaining_hour / _log_item_playable need
real ORM objects."""
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

from django.test.utils import CaptureQueriesContext
from django.db import connection

import library.services.engine as eng_module
from library.models import (
    Artist, Category, CategoryKind, LogFillConfig, LogItem, PlaylistLog, Track,
)
from monitoring.models import SystemEvent


def make_stand_in():
    """Same pattern as test_async_live_fill.make_stand_in + test_engine_queue_eta.
    make_stand_in -- combined here because these tests exercise both
    _try_extend_live_log_async's dispatch logic AND the new
    _committed_future_runway_seconds helper it calls, which needs the
    deck-position machinery."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._live_fill_in_progress = False
    obj._live_fill_generation = 0
    obj._last_live_extend_attempt = 0.0
    obj.current_log = None
    obj.log_items = []
    obj._queue_cursor = 0
    obj._forced_next_items = []
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    obj._start_next_track = MagicMock()
    return obj


def make_fake_deck(next_start_seconds, duration_seconds, position, cue_in_seconds=0.0, paused=False):
    """Copy of test_engine_queue_eta.make_fake_deck (silence_primed=True
    routes _get_deck_position through wall-clock, sidestepping GStreamer)."""
    track = SimpleNamespace(
        next_start_seconds=next_start_seconds, duration_seconds=duration_seconds,
        cue_in_seconds=cue_in_seconds,
    )
    return SimpleNamespace(
        track=track, paused=paused, paused_position=position if paused else 0.0,
        silence_primed=True, started_at=time.time() - position,
    )


class LiveFillDeficitFixtureMixin:
    def setUp(self):
        super().setUp()
        self.kind_music = CategoryKind.objects.create(code="deficit-music", name="Music")
        self.kind_spot = CategoryKind.objects.create(code="deficit-spot", name="Spot")
        # The GDead segment lives in its own music-kind category; PSA is
        # a spot-kind fill category. Same shape as production.
        self.cat_gdead = Category.objects.create(code="GDEADTEST", name="GDead Test", kind=self.kind_music)
        self.cat_psa = Category.objects.create(code="PSATEST", name="PSA Test", kind=self.kind_spot)
        self.artist_gdead, _ = Artist.get_or_create_ci("Grateful Dead Hour Test")
        self.artist_psa, _ = Artist.get_or_create_ci("PSA Test Artist")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

        # A generous PSA pool so if a false-deficit-driven live fill
        # DID trigger, it would have plenty of eligible content to
        # append -- the fix's job is to make sure it doesn't.
        for i in range(80):
            self._make_track(f"psa-{i:03d}.mp3", self.cat_psa, self.artist_psa,
                              title=f"PSA Track {i}", duration_seconds=30.0,
                              next_start_seconds=29.0)

        LogFillConfig.objects.update_or_create(
            pk=1, defaults={"strategy": "fixed_category", "fallback_category": self.cat_psa},
        )

    def _make_track(self, filename, category, artist, *, title, duration_seconds, next_start_seconds, cue_in_seconds=0.0):
        path = Path(self._tmpdir.name) / filename
        path.touch()
        return Track.objects.create(
            filepath=str(path), filename=filename, title=title, artist=artist,
            category=category, ready2air=True, duration_seconds=duration_seconds,
            next_start_seconds=next_start_seconds, cue_in_seconds=cue_in_seconds,
        )

    def _make_gdead_hour_log(self, hour):
        """Reproduces the exact production 22:00 shape: 10 items, last
        one a ~26.5-minute mixshow segment. Uses today's date so
        _try_extend_live_log_async's own current-hour bookkeeping is
        unconfused by a date mismatch (its wall_now check is separately
        pinned in each test)."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=hour).delete()
        log = PlaylistLog.objects.create(date=today, hour=hour, status="approved")
        items = []
        # 9 short items (~4-60s each) representing legal ID + intro +
        # announcements + drops + weather + underwriter + part 1 + local
        # drop + wxtemp -- exact production shape.
        short_specs = [
            (0, "Legal ID KOGR-LP", 4.7, 4.6),
            (1, "1977 Intro", 60.0, 48.7),
            (2, "Announcements 08/10/2026", 30.5, 26.9),
            (3, "Thursday at 9pm", 46.4, 43.5),
            (4, "Current Observation (Max)", 57.4, 57.2),
            (5, "Underwriter thank you", 31.3, 31.2),
            (6, "1977 Part 1", 1795.2, 1792.8),
            (7, "Sundays 5 to 9am", 46.6, 43.3),
            (8, "Current Temp (Max)", 8.8, 8.7),
        ]
        for pos, title, dur, nxt in short_specs:
            t = self._make_track(f"h{hour}-item{pos}.mp3", self.cat_gdead, self.artist_gdead,
                                  title=title, duration_seconds=dur, next_start_seconds=nxt)
            items.append(LogItem.objects.create(
                playlist_log=log, position=pos, scheduled_time=timezone.now(),
                track=t, category=self.cat_gdead,
            ))
        # Position 9 -- the mixshow segment: ~26.5 min.
        part2 = self._make_track(f"h{hour}-gdh_part2.mp3", self.cat_gdead, self.artist_gdead,
                                  title="1977 Part 2", duration_seconds=1589.7, next_start_seconds=1587.5)
        items.append(LogItem.objects.create(
            playlist_log=log, position=9, scheduled_time=timezone.now(),
            track=part2, category=self.cat_gdead,
        ))
        return log, items, part2


class GDeadReproductionTests(LiveFillDeficitFixtureMixin, TransactionTestCase):
    """The literal production reproduction the fix targets."""

    def test_mixshow_final_item_playing_does_not_append_psa_block(self):
        """Cursor has reached the end of log_items because the last item
        (Part 2, 26.5 min) is now on deck and playing. Wall clock is
        early in Part 2 (:34 of the hour, ~1560s left, ~1550s of Part 2
        left). Committed runway = ~1550 (deck only, since queue is
        empty), deficit = 1560 - 1550 = ~10s -- well UNDER
        DURATION_FIT_MARGIN. Must NOT dispatch, must NOT append PSAs,
        must NOT emit "Live log extension activated"."""
        stand_in = make_stand_in()
        log, items, part2 = self._make_gdead_hour_log(22)
        stand_in.current_log = log
        stand_in.log_items = items
        # Cursor at end -- Part 2 was the last item dequeued.
        stand_in._queue_cursor = len(items)
        # Part 2 is on deck A, near its start (10s in of a 1589s track).
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=1587.5, duration_seconds=1589.7, position=10.0,
        )

        # Wall clock: :34:10, i.e. ~1550s left in the hour.
        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 22, 34, 10))
        before_count = LogItem.objects.filter(playlist_log=log).count()
        before_events = SystemEvent.objects.filter(
            category="engine", title="Live log extension activated",
        ).count()

        with patch.object(eng_module.timezone, "localtime", return_value=fake_now):
            result = stand_in._try_extend_live_log_async()

        # Give any spuriously-dispatched worker time to run + install
        time.sleep(0.5)

        self.assertFalse(result)
        self.assertEqual(
            LogItem.objects.filter(playlist_log=log).count(), before_count,
            "committed-runway check should have suppressed dispatch -- no PSAs should have been appended",
        )
        self.assertEqual(
            SystemEvent.objects.filter(
                category="engine", title="Live log extension activated",
            ).count(),
            before_events,
            "no 'Live log extension activated' event should have been emitted",
        )
        self.assertFalse(stand_in._live_fill_in_progress,
                          "in-progress guard must have been cleared, not held indefinitely")

    def test_repeated_polling_during_mixshow_stays_stable(self):
        """The proactive call site (from _poll_position) fires every
        ~250ms while the queue looks exhausted. Even with the throttle
        bypassed each cycle, ten repeated invocations across the
        entire course of the mixshow's playback must produce zero
        appends."""
        stand_in = make_stand_in()
        log, items, part2 = self._make_gdead_hour_log(22)
        stand_in.current_log = log
        stand_in.log_items = items
        stand_in._queue_cursor = len(items)
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=1587.5, duration_seconds=1589.7, position=100.0,
        )
        before_count = LogItem.objects.filter(playlist_log=log).count()

        # Every 3 minutes across ~24 minutes of Part 2 playback.
        for elapsed_minutes in (34, 37, 40, 43, 46, 49, 52, 55, 58):
            stand_in._last_live_extend_attempt = 0.0  # bypass throttle for the test
            # Advance the fake deck's position too so the runway shrinks
            # in lockstep with wall clock, matching real playback.
            stand_in.decks["A"] = make_fake_deck(
                next_start_seconds=1587.5, duration_seconds=1589.7,
                position=100.0 + (elapsed_minutes - 34) * 60,
            )
            fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 22, elapsed_minutes, 10))
            with patch.object(eng_module.timezone, "localtime", return_value=fake_now):
                stand_in._try_extend_live_log_async()
            time.sleep(0.1)  # let any (wrongly-dispatched) worker have a chance

        self.assertEqual(
            LogItem.objects.filter(playlist_log=log).count(), before_count,
            "repeated polling during a natural-carry mixshow must never accumulate fill",
        )


class GenuineDeficitStillFillsTests(LiveFillDeficitFixtureMixin, TransactionTestCase):
    """The opposite case -- a genuinely short final item that leaves a
    real gap -- must still receive normal live fill."""

    def test_short_final_item_at_end_of_hour_triggers_normal_fill(self):
        """Simple 2-item log ending in a short 30s item. Wall clock at
        :30:00 (1800s left). Deck has 20s remaining on that short item.
        Committed runway = 20 (no future queued items). Deficit = 1780,
        well above DURATION_FIT_MARGIN -- dispatch must proceed. Split
        the check across the two halves the async-live-fill test suite
        already uses (dispatch decision via captured worker args, then
        install-time via a direct call) rather than trying to run a
        real GLib main loop under test to bridge them: this test is
        specifically about the DISPATCH DECISION being right, and the
        install-time contract already has its own dedicated coverage in
        test_async_live_fill.InstallLiveFillValidProposalTests."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        t = self._make_track("short-final.mp3", self.cat_gdead, self.artist_gdead,
                              title="Short Final", duration_seconds=30.0, next_start_seconds=29.0)
        items = [LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now(),
            track=t, category=self.cat_gdead,
        )]

        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = items
        stand_in._queue_cursor = len(items)
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=29.0, duration_seconds=30.0, position=9.0,  # 20s left
        )

        captured = {}
        def fake_worker(*args, **kwargs):
            captured["args"] = args

        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 10, 30, 0))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(stand_in, "_live_fill_worker", side_effect=fake_worker):
            stand_in._try_extend_live_log_async()

        deadline = time.time() + 5
        while "args" not in captured and time.time() < deadline:
            time.sleep(0.02)

        self.assertIn("args", captured,
                       "genuine ~1780s deficit must still trigger a dispatch")
        (_log_id, _gen, _picks, _orig, _accum, _hour_start,
         _start_pos, target_duration_seconds) = captured["args"]
        # ~1780s deficit -- well above DURATION_FIT_MARGIN. The exact
        # value depends on committed_runway's use of live wall-clock
        # deck position, so allow a comfortable band around 1780.
        self.assertGreater(target_duration_seconds, 1700)
        self.assertLess(target_duration_seconds, 1850)

    def test_dispatch_targets_wall_remaining_not_hardcoded_3600(self):
        """The old code called fill_remaining_hour(..., accumulated=...,
        target_duration_seconds default NOMINAL_HOUR_SECONDS) with
        accumulated derived from wall clock. Combined that made the
        effective target inadvertently correct-ish when the queue was
        genuinely empty, but wrong when it wasn't. The new code passes
        target_duration_seconds = deficit directly, so
        fill_remaining_hour never sees a 3600. Verified by inspecting
        the args handed to the worker."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        t = self._make_track("shortish.mp3", self.cat_gdead, self.artist_gdead,
                              title="Shortish", duration_seconds=60.0, next_start_seconds=58.0)
        items = [LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now(),
            track=t, category=self.cat_gdead,
        )]
        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = items
        stand_in._queue_cursor = len(items)
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=58.0, duration_seconds=60.0, position=10.0,  # 48s left
        )

        captured = {}

        def fake_worker(*args, **kwargs):
            captured["args"] = args

        # :10:00 -> 3000s remaining. Committed = 48s. Deficit = ~2952s.
        fake_now = timezone.make_aware(timezone.datetime(2027, 3, 1, 10, 10, 0))
        with patch.object(eng_module.timezone, "localtime", return_value=fake_now), \
             patch.object(stand_in, "_live_fill_worker", side_effect=fake_worker):
            stand_in._try_extend_live_log_async()

        deadline = time.time() + 3
        while "args" not in captured and time.time() < deadline:
            time.sleep(0.02)

        self.assertIn("args", captured)
        (_log_id, _gen, _picks, _orig, accumulated, _hour_start,
         _start_pos, target_duration_seconds) = captured["args"]
        self.assertEqual(accumulated, 0.0)
        self.assertLess(target_duration_seconds, 3000,
                        "target must reflect the DEFICIT (~2952s), not the wall-clock remaining (3000s) and not a flat 3600")
        self.assertGreater(target_duration_seconds, 2900)


class CommittedRunwayCorrectnessTests(LiveFillDeficitFixtureMixin, TransactionTestCase):
    """Direct tests of the _committed_future_runway_seconds helper --
    the primitive both the deficit check and any future safety-fill
    consumer will depend on."""

    def test_cursor_skips_already_played_items(self):
        """A cursor at position N means items[0..N-1] have already been
        dequeued/played (or substituted/skipped past by
        _next_queue_item). They must NOT be counted toward committed
        future runway -- doing so is the exact bug _build_from_rotation
        already goes to lengths to avoid on the build side."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        items = []
        for i, dur in enumerate([100.0, 200.0, 300.0, 400.0]):
            t = self._make_track(f"cur-{i}.mp3", self.cat_gdead, self.artist_gdead,
                                  title=f"T{i}", duration_seconds=dur, next_start_seconds=dur - 1)
            items.append(LogItem.objects.create(
                playlist_log=log, position=i, scheduled_time=timezone.now(),
                track=t, category=self.cat_gdead,
            ))

        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = items
        # Cursor at 2 -- items 0 and 1 have already played.
        stand_in._queue_cursor = 2
        # No deck playing (between-tracks moment).
        runway = stand_in._committed_future_runway_seconds()
        # Only items[2] + items[3] should count: 299 + 399 = 698.
        self.assertAlmostEqual(runway, 698.0, delta=0.1)

    def test_leading_deck_remaining_plus_future_queue_sums_correctly(self):
        """Leading deck: 250s track, 50s in, next_start=245 => 195s left.
        Future queue: two items, effective airtime 100s + 200s.
        Expected total: 195 + 100 + 200 = 495s."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        t1 = self._make_track("rw-1.mp3", self.cat_gdead, self.artist_gdead,
                               title="T1", duration_seconds=105.0, next_start_seconds=100.0)
        t2 = self._make_track("rw-2.mp3", self.cat_gdead, self.artist_gdead,
                               title="T2", duration_seconds=205.0, next_start_seconds=200.0)
        # The cursor-at-0 item is the one currently on deck; the two future
        # items live at positions 1 and 2. The stand-in's log_items must
        # include the current one too, since cursor semantics count from
        # position 0 forward.
        current_placeholder = self._make_track(
            "rw-0.mp3", self.cat_gdead, self.artist_gdead,
            title="Currently Playing", duration_seconds=250.0, next_start_seconds=245.0,
        )
        items = [
            LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now(),
                                    track=current_placeholder, category=self.cat_gdead),
            LogItem.objects.create(playlist_log=log, position=1, scheduled_time=timezone.now(),
                                    track=t1, category=self.cat_gdead),
            LogItem.objects.create(playlist_log=log, position=2, scheduled_time=timezone.now(),
                                    track=t2, category=self.cat_gdead),
        ]

        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = items
        # The currently-playing item has been dequeued, cursor moved past it.
        stand_in._queue_cursor = 1
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=245.0, duration_seconds=250.0, position=50.0,
        )

        runway = stand_in._committed_future_runway_seconds()
        self.assertAlmostEqual(runway, 195.0 + 100.0 + 200.0, delta=1.0)

    def test_helper_issues_zero_db_queries_across_every_population_path(self):
        """_committed_future_runway_seconds runs on the GLib main
        thread from _try_extend_live_log_async. It MUST be strictly
        in-memory arithmetic -- a lazy Django query issued from GLib
        blocks the audio callback loop. This test exercises every
        distinct way self.log_items gets populated in production
        (_load_log_for's select_related path AND _apply_log_item_insert's
        LogItem-created-with-Track-instance path -- the two structurally
        different cache mechanisms) and asserts zero queries during the
        helper's own execution. Verified via CaptureQueriesContext on
        the real connection, not by descriptor introspection."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")

        # Path A: _load_log_for-style select_related. Create fixture
        # LogItems + Tracks, then reload via the same select_related
        # chain the real code uses -- this is what production
        # self.log_items looks like most of the time.
        for i, dur in enumerate([100.0, 200.0, 300.0]):
            t = self._make_track(f"nq-a-{i}.mp3", self.cat_gdead, self.artist_gdead,
                                  title=f"A{i}", duration_seconds=dur, next_start_seconds=dur - 1)
            LogItem.objects.create(
                playlist_log=log, position=i, scheduled_time=timezone.now(),
                track=t, category=self.cat_gdead,
            )
        reloaded = list(
            log.items
            .select_related("track", "track__artist", "track__album",
                             "track__category", "track__category__kind",
                             "category", "category__kind")
            .order_by("position")
        )

        # Path B: LogItem-constructed-with-Track-instance (the
        # _splice_log_item_db_at + _apply_log_item_insert pattern).
        spliced_track = self._make_track(
            "nq-b-spliced.mp3", self.cat_gdead, self.artist_gdead,
            title="Spliced", duration_seconds=400.0, next_start_seconds=399.0,
        )
        spliced_item = LogItem.objects.create(
            playlist_log=log, position=99, scheduled_time=timezone.now(),
            track=spliced_track, category=self.cat_gdead,
        )

        stand_in = make_stand_in()
        stand_in.current_log = log
        # Mixed queue: 2 select_related items + the spliced item, then
        # a deck playing a Track loaded via a third path.
        stand_in.log_items = reloaded + [spliced_item]
        stand_in._queue_cursor = 1  # first item already played
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=100.0, duration_seconds=105.0, position=25.0,
        )

        # THE assertion: the entire helper runs without touching the DB.
        # If ANY population path silently leaves .track uncached, this
        # fails immediately and loudly -- the exact failure mode the
        # GLib-thread contract needs to catch.
        with CaptureQueriesContext(connection) as ctx:
            runway = stand_in._committed_future_runway_seconds()

        self.assertEqual(
            len(ctx.captured_queries), 0,
            f"_committed_future_runway_seconds must issue zero DB queries "
            f"(GLib main thread contract) -- got {len(ctx.captured_queries)}: "
            f"{[q['sql'] for q in ctx.captured_queries]}",
        )
        # Sanity: the value it computed is nonzero (helper actually ran,
        # didn't short-circuit somewhere that would mask the query-count check).
        self.assertGreater(runway, 0)

    def test_deleted_track_ghost_contributes_zero(self):
        """A LogItem whose Track FK was NULLed (library deletion) is
        skipped by _next_queue_item's _log_item_playable check and
        contributes zero airtime. _committed_future_runway_seconds
        must handle it without crashing and without over-counting."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        t = self._make_track("ghost-real.mp3", self.cat_gdead, self.artist_gdead,
                              title="Real", duration_seconds=100.0, next_start_seconds=95.0)
        real_item = LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now(),
            track=t, category=self.cat_gdead,
        )
        # Simulate a ghost: LogItem row exists but Track FK is NULL.
        ghost_item = LogItem.objects.create(
            playlist_log=log, position=1, scheduled_time=timezone.now(),
            track=None, category=self.cat_gdead,
        )

        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = [real_item, ghost_item]
        stand_in._queue_cursor = 0

        runway = stand_in._committed_future_runway_seconds()
        self.assertAlmostEqual(runway, 95.0, delta=0.1)


class StaleProposalProtectionsIntactTests(LiveFillDeficitFixtureMixin, TransactionTestCase):
    """The full-suite _install_live_fill staleness contract
    (test_async_live_fill.InstallLiveFillStaleProposalTests) is
    unaffected by this fix -- but explicit coverage that a proposal
    which somehow escapes the new suppression gate ALSO passes the
    downstream install-time re-validation keeps the belts-and-suspenders
    architecture provable end-to-end."""

    def test_proposal_still_re_checks_current_log_id_at_install_time(self):
        """Even if a worker's proposal DID get generated (older code
        path, or a dispatch that raced right before a rollover),
        _install_live_fill must still discard it if current_log has
        changed by the time the callback fires."""
        stand_in = make_stand_in()
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour__in=[10, 11]).delete()
        log_a = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        log_b = PlaylistLog.objects.create(date=today, hour=11, status="approved")
        item_a = LogItem.objects.create(
            playlist_log=log_a, position=0, scheduled_time=timezone.now(),
            track=self._make_track("stale-a.mp3", self.cat_gdead, self.artist_gdead,
                                    title="A", duration_seconds=100, next_start_seconds=95),
            category=self.cat_gdead,
        )
        item_b = LogItem.objects.create(
            playlist_log=log_b, position=0, scheduled_time=timezone.now(),
            track=self._make_track("stale-b.mp3", self.cat_gdead, self.artist_gdead,
                                    title="B", duration_seconds=100, next_start_seconds=95),
            category=self.cat_gdead,
        )
        # Engine has already rolled over to hour 11.
        stand_in.current_log = log_b
        stand_in.log_items = [item_b]
        stand_in._live_fill_generation = 1

        picks = [{"track": item_a.track, "category": self.cat_gdead,
                  "scheduled_time": timezone.now()}]
        before_a = LogItem.objects.filter(playlist_log=log_a).count()

        # Proposal targets hour 10 (log_a.id), but current_log is log_b.
        result = stand_in._install_live_fill(log_a.id, 1, picks, 1, 1)

        self.assertFalse(result)
        self.assertEqual(LogItem.objects.filter(playlist_log=log_a).count(), before_a,
                          "install-time re-check must still block a stale proposal even after the new dispatch-time suppression")


class GenuineExtensionStillEmitsEventTests(LiveFillDeficitFixtureMixin, TransactionTestCase):
    """When live extension IS the right call (genuine deficit), the
    existing '/reports/'-visible SystemEvent must still fire -- the fix
    suppresses false-positive dispatches, not legitimate ones."""

    def test_install_live_fill_still_emits_live_log_extension_activated(self):
        """The event fires from _install_live_fill on a successful DB
        append -- unchanged by this fix. Reproduced directly to prove
        the event pipeline still works end-to-end after the dispatch-
        gate changes above, without needing a real GLib main loop to
        bridge worker -> install in test."""
        today = timezone.localtime().date()
        PlaylistLog.objects.filter(date=today, hour=10).delete()
        log = PlaylistLog.objects.create(date=today, hour=10, status="approved")
        t = self._make_track("genuine.mp3", self.cat_gdead, self.artist_gdead,
                              title="Short", duration_seconds=30.0, next_start_seconds=29.0)
        items = [LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now(),
            track=t, category=self.cat_gdead,
        )]

        stand_in = make_stand_in()
        stand_in.current_log = log
        stand_in.log_items = list(items)
        stand_in._live_fill_generation = 1
        # Not idle -- suppresses the _start_next_track auto-start branch
        # so we can assert the event fires without needing any real
        # engine plumbing beyond the event emission itself.
        stand_in.decks = {"A": object(), "B": None}

        picks = [
            {"track": t, "category": self.cat_psa, "scheduled_time": timezone.now()},
        ]
        before_events = SystemEvent.objects.filter(
            category="engine", title="Live log extension activated",
        ).count()

        result = stand_in._install_live_fill(log.id, 1, picks, 1, 1)

        self.assertFalse(result)  # one-shot idle callback returns False
        self.assertEqual(
            SystemEvent.objects.filter(
                category="engine", title="Live log extension activated",
            ).count(),
            before_events + 1,
            "genuine live fill must still emit its Recent Event",
        )
