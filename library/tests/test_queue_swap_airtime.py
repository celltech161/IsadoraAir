"""Queue Swap and airtime continuity: a replacement changes only what the engine's authoritative committed
runway reads after its ~3 s queue reload. The established live fill (_try_extend_live_log_async) then
decides from that runway and the real wall-clock remainder -- a shorter replacement that leaves a real
shortage is filled by the normal policy, one that leaves enough runway is not, and the swap itself never
dispatches or installs anything (docs/QUEUE_SWAP.md, "Airtime")."""
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
from library.services import queue_replace

INSTANCE = "airtime-engine"


def fake_deck(log_item, *, next_start, duration, position):
    track = SimpleNamespace(next_start_seconds=next_start, duration_seconds=duration, cue_in_seconds=0.0)
    return SimpleNamespace(track=track, log_item=log_item, paused=False, paused_position=0.0,
                           silence_primed=True, started_at=time.time() - position)


class SwapAirtimeTests(TransactionTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        music, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.cat = Category.objects.create(code="AIRROCK", name="Air Rock", kind=music)
        self.artist, _ = Artist.get_or_create_ci("Airtime Artist")
        self.playing = self.track("Playing", 240.0, 238.0)
        self.long = self.track("Long", 300.0, 295.0)                    # effective airtime 295
        self.short = self.track("Short", 150.0, 116.0, cue_in=1.0)      # effective airtime 115 (not 150)
        self.after = self.track("After", 200.0, 196.0)
        self.now = timezone.make_aware(timezone.datetime(2027, 3, 1, 14, 51, 0))   # 540 s left in the hour
        self.log = PlaylistLog.objects.create(date=self.now.date(), hour=14, status="approved")
        self.items = [self.item(i, t) for i, t in enumerate((self.playing, self.long, self.after))]
        self.engine = self.stand_in()

    def track(self, title, duration, next_start, cue_in=0.0):
        path = Path(self._tmp.name) / f"{title}.mp3"
        path.touch()
        return Track.objects.create(filepath=str(path), filename=path.name, title=title, artist=self.artist,
                                    category=self.cat, ready2air=True, duration_seconds=duration,
                                    next_start_seconds=next_start, cue_in_seconds=cue_in)

    def item(self, position, track):
        return LogItem.objects.create(playlist_log=self.log, position=position, track=track, category=self.cat,
                                      scheduled_time=self.now + timedelta(minutes=position), track_title=track.title,
                                      origin=LogItem.ORIGIN_GENERATED_SCHEDULE)

    def stand_in(self):
        engine = object.__new__(eng_module.PlaybackEngine)
        engine.running = True
        engine._lock = threading.RLock()
        engine._live_fill_in_progress = False
        engine._live_fill_generation = 0
        engine._last_live_extend_attempt = 0.0
        engine._last_queue_reload = 0.0
        engine._poison_skip_identities = set()
        engine._forced_next_items = []
        engine.manual_mode = False
        engine.current_log = self.log
        engine.log_items = list(self.log.items.select_related("track").order_by("position"))
        engine._queue_cursor = 1
        # The first item is on air with 60 s of runway left before its handoff.
        engine.decks = {"A": fake_deck(engine.log_items[0], next_start=238.0, duration=240.0, position=178.0),
                        "B": None}
        return engine

    def swap(self, item, track):
        item = LogItem.objects.get(pk=item.pk)              # the committed track is the expected one
        state = {"timestamp": time.time(), "state_revision": {"instance": INSTANCE, "seq": 1},
                 "decks": {"A": {"log_item_id": self.items[0].id}}, "queue": [{"item_id": i.id} for i in self.items[1:]]}
        outcome = queue_replace.replace_upcoming_item(item_id=item.id, expected_track_id=item.track_id,
                                                      track_id=track.id, engine_instance=INSTANCE, state=state)
        self.assertTrue(outcome.changed, outcome.reason)

    def reload(self):
        self.engine._last_queue_reload = 0.0
        self.engine._reload_queue_if_changed()

    def dispatch(self, at=None):
        self.engine._last_live_extend_attempt = 0.0
        thread = MagicMock()
        with patch.object(eng_module.timezone, "localtime", return_value=at or self.now), \
                patch.object(eng_module.threading, "Thread", thread):
            self.engine._try_extend_live_log_async()
        return thread

    def test_the_runway_drops_by_the_effective_airtime_difference_only_after_the_reload(self):
        before = self.engine._committed_future_runway_seconds()
        self.assertAlmostEqual(before, 60 + 295 + 196, delta=1.0)
        self.swap(self.items[1], self.short)
        self.assertAlmostEqual(self.engine._committed_future_runway_seconds(), before, delta=1.0)   # not yet seen
        self.reload()
        after = self.engine._committed_future_runway_seconds()
        self.assertAlmostEqual(before - after, 295 - 115, delta=1.0)    # next_start - cue_in, not 300 - 150
        self.assertEqual(self.engine.log_items[1].track_id, self.short.id)
        self.assertEqual(self.engine._queue_cursor, 1)

    def test_a_shorter_swap_that_leaves_a_real_shortage_gets_the_normal_live_fill_for_that_deficit(self):
        self.assertFalse(self.dispatch().called)          # 540 s left, ~551 s committed: nothing to fill
        self.swap(self.items[1], self.short)
        self.reload()
        thread = self.dispatch()
        self.assertTrue(thread.called)
        args = thread.call_args.kwargs["args"]
        log_id, _generation, existing, count, accumulated, _hour_start, start_position, target = args
        self.assertEqual((log_id, count, accumulated, start_position), (self.log.id, 3, 0.0, 3))
        self.assertAlmostEqual(target, 540 - (60 + 115 + 196), delta=2.0)   # the real deficit, ~169 s
        self.assertEqual(existing[1]["track"].id, self.short.id)              # the worker sees the swap
        self.assertEqual(existing[1]["category"].id, self.cat.id)             # slot provenance unchanged

    def test_a_shorter_swap_with_enough_runway_left_adds_nothing(self):
        self.swap(self.items[1], self.short)
        self.reload()
        later = self.now.replace(minute=55)                # 300 s left, ~371 s committed
        self.assertFalse(self.dispatch(at=later).called)
        self.assertFalse(self.engine._live_fill_in_progress)

    def test_a_longer_swap_creates_no_deficit(self):
        self.swap(self.items[2], self.long)
        self.reload()
        self.assertFalse(self.dispatch().called)

    def test_the_swap_itself_never_dispatches_or_installs_fill(self):
        count = LogItem.objects.filter(playlist_log=self.log).count()
        with patch.object(eng_module.threading, "Thread") as thread:
            self.swap(self.items[1], self.short)
        self.assertFalse(thread.called)
        self.assertEqual(LogItem.objects.filter(playlist_log=self.log).count(), count)

    def test_a_causal_refill_that_owns_the_hour_still_blocks_legacy_fill(self):
        self.swap(self.items[1], self.short)
        self.reload()
        self.engine._refill_coordinator = SimpleNamespace(blocks_legacy=lambda log_id, items: True)
        self.assertFalse(self.dispatch().called)
        self.assertFalse(self.engine._live_fill_in_progress)

    def test_a_small_end_of_hour_shortage_gets_closing_fill_unless_a_later_hour_was_adopted(self):
        self.swap(self.items[1], self.short)
        self.reload()
        runway = self.engine._committed_future_runway_seconds()
        # Wall clock leaving a ~25 s shortage: above the 20 s closing minimum, within the 30 s fit margin.
        left = runway + 25
        at = self.now.replace(minute=0, second=0) + timedelta(seconds=3600 - left)
        control = self.dispatch(at=at)
        self.assertTrue(control.called)                                   # closing fill for the real shortage
        self.assertIsNotNone(control.call_args.kwargs["kwargs"].get("closing_until"))
        self.engine._live_fill_in_progress = False
        self.engine.current_log = PlaylistLog.objects.create(date=self.now.date(), hour=15, status="approved")
        self.assertFalse(self.dispatch(at=at).called)                     # a later hour is already on air
