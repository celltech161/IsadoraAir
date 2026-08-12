"""1.1 airtime-correction follow-up -- regression coverage for
_compute_queue_eta_state() (library/services/engine.py), which
_write_state calls to build engine_state.json's live "queue" array
(the dashboard Coming Up list's "Starts at"/eta_seconds, and
webrequests.services.estimate_air_time's live source via
_live_eta_datetime).

Background: this loop previously computed the correct, cue-in-adjusted
"airtime_seconds" DISPLAY value per queued item, but advanced its
running eta accumulator with the raw, UNADJUSTED next_start_seconds
instead -- silently overcounting every future queued track's
contribution to the live "Starts at" estimate by its own cue_in_seconds,
even though the number sitting right next to it in the same payload was
already correct.

The fix distinguishes two fundamentally different offsets:
  - the FIRST offset (the currently-in-progress leading deck's own
    remaining runway) comes from _leading_deck_eta_seconds, measuring
    forward from the deck's CURRENT source position -- its own cue_in
    has already happened and must NOT be subtracted again;
  - every offset AFTER that is a full future track that hasn't started
    yet, and uses log_builder.py's own authoritative
    effective_airtime_seconds() (next_start_seconds - cue_in_seconds),
    imported directly rather than re-implemented, so the two can never
    silently drift apart again.

Uses the same bare-stand-in + SimpleNamespace-fake-deck technique as
test_clock_drift_recovery.py's own make_stand_in/make_fake_deck (no
real GStreamer pipeline needed for position math), combined with real
DB-backed Track/LogItem rows for queued items, since
_get_upcoming_preview/_log_item_playable need real ORM objects and a
real file on disk."""
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track


def make_stand_in():
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.decks = {"A": None, "B": None}
    obj._lock = threading.RLock()
    obj.log_items = []
    obj._queue_cursor = 0
    obj._forced_next_items = []
    return obj


def make_fake_deck(next_start_seconds, duration_seconds, position, cue_in_seconds=0.0, paused=False):
    """Mirrors test_clock_drift_recovery.py's own make_fake_deck --
    silence_primed=True routes _get_deck_position through its wall-
    clock branch (time.time() - started_at) rather than needing a real
    GStreamer pipeline; started_at is backed out from the desired
    `position` at construction time. cue_in_seconds is populated (even
    though _leading_deck_eta_seconds never reads it) specifically to
    prove the "not double-subtracted" invariant structurally -- if
    something regressed to read it, these tests would catch the wrong
    eta value immediately."""
    track = SimpleNamespace(
        next_start_seconds=next_start_seconds, duration_seconds=duration_seconds,
        cue_in_seconds=cue_in_seconds,
    )
    return SimpleNamespace(
        track=track, paused=paused, paused_position=position if paused else 0.0,
        silence_primed=True, started_at=time.time() - position,
    )


class QueueEtaFixtureMixin:
    def setUp(self):
        super().setUp()
        self.kind = CategoryKind.objects.create(code="queue-eta-test", name="Queue ETA Test")
        self.category = Category.objects.create(code="QUEUEETA", name="Queue ETA", kind=self.kind)
        self.artist, _ = Artist.get_or_create_ci("Queue ETA Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 6, 1), hour=10, status="approved")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self._counter = 0

    def make_queued_item(self, next_start_seconds=None, cue_in_seconds=0.0, duration_seconds=250.0, position=None):
        self._counter += 1
        real_path = Path(self._tmpdir.name) / f"queue-eta-{self._counter}.mp3"
        real_path.touch()
        track = Track.objects.create(
            filepath=str(real_path), filename=real_path.name, title=f"Queue Track {self._counter}",
            artist=self.artist, category=self.category, ready2air=True,
            duration_seconds=duration_seconds, next_start_seconds=next_start_seconds,
            cue_in_seconds=cue_in_seconds,
        )
        return LogItem.objects.create(
            playlist_log=self.log, position=position if position is not None else self._counter,
            scheduled_time=timezone.now(), track=track, category=self.category,
        )


class ComputeQueueEtaStateTests(QueueEtaFixtureMixin, TestCase):
    def test_worked_example_current_deck_runway_then_full_track_airtimes(self):
        """Track A (currently playing): current source position=100,
        next_start=238, cue_in=8. Remaining runway to the next audible
        handoff = 238-100=138 -- NOT 238-8-100=130. The leading deck's
        own cue_in has already happened and played out; subtracting it
        again would double-count it.

        Queued B: cue_in=7.5, next_start=205 -> effective airtime 197.5.
        Queued C: cue_in=5, next_start=220 -> effective airtime 215.

        Expected: B starts at now+138; C starts at B_start+197.5; the
        next item after C would start at C_start+215."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(
            next_start_seconds=238.0, duration_seconds=250.0, position=100.0, cue_in_seconds=8.0,
        )

        item_b = self.make_queued_item(next_start_seconds=205.0, cue_in_seconds=7.5, position=1)
        item_c = self.make_queued_item(next_start_seconds=220.0, cue_in_seconds=5.0, position=2)
        stand_in.log_items = [item_b, item_c]

        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)

        self.assertEqual(len(queue), 2)
        self.assertAlmostEqual(queue[0]["eta_seconds"], 138.0, delta=0.5)
        self.assertAlmostEqual(queue[0]["airtime_seconds"], 197.5, delta=0.05)
        self.assertAlmostEqual(queue[1]["eta_seconds"], 138.0 + 197.5, delta=0.5)
        self.assertAlmostEqual(queue[1]["airtime_seconds"], 215.0, delta=0.05)

    def test_no_active_deck_eta_baseline_is_zero(self):
        stand_in = make_stand_in()
        item = self.make_queued_item(next_start_seconds=100.0, cue_in_seconds=10.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["eta_seconds"], 0.0)
        self.assertEqual(queue[0]["airtime_seconds"], 90.0)

    def test_single_queued_item(self):
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=50.0, duration_seconds=60.0, position=20.0)
        item = self.make_queued_item(next_start_seconds=100.0, cue_in_seconds=0.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(len(queue), 1)
        self.assertAlmostEqual(queue[0]["eta_seconds"], 30.0, delta=0.5)

    def test_multiple_queued_items_chain_correctly(self):
        stand_in = make_stand_in()
        items = [
            self.make_queued_item(next_start_seconds=100.0, cue_in_seconds=0.0, position=1),
            self.make_queued_item(next_start_seconds=200.0, cue_in_seconds=10.0, position=2),
            self.make_queued_item(next_start_seconds=50.0, cue_in_seconds=5.0, position=3),
        ]
        stand_in.log_items = items
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(len(queue), 3)
        self.assertEqual(queue[0]["eta_seconds"], 0.0)
        self.assertEqual(queue[1]["eta_seconds"], 100.0)
        self.assertEqual(queue[2]["eta_seconds"], 100.0 + 190.0)  # 200-10

    def test_next_start_none_falls_back_to_duration_minus_cue_in(self):
        stand_in = make_stand_in()
        item = self.make_queued_item(next_start_seconds=None, cue_in_seconds=10.0, duration_seconds=180.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(queue[0]["airtime_seconds"], 170.0)

    def test_explicit_zero_next_start_not_replaced_by_duration(self):
        stand_in = make_stand_in()
        item = self.make_queued_item(next_start_seconds=0.0, cue_in_seconds=0.0, duration_seconds=500.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(queue[0]["airtime_seconds"], 0.0)

    def test_cue_in_zero(self):
        stand_in = make_stand_in()
        item = self.make_queued_item(next_start_seconds=200.0, cue_in_seconds=0.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(queue[0]["airtime_seconds"], 200.0)

    def test_malformed_missing_duration_metadata_returns_zero_not_negative(self):
        stand_in = make_stand_in()
        item = self.make_queued_item(next_start_seconds=None, cue_in_seconds=5.0, duration_seconds=None)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(queue[0]["airtime_seconds"], 0.0)

    def test_crossfade_overlap_eta_baseline_uses_the_finishing_deck(self):
        """Two decks occupied simultaneously (crossfade in progress) --
        the eta baseline must come from the deck actually governing the
        next handoff (the one about to finish), not whichever slot
        happens to be encountered first. This is _leading_deck_eta_
        seconds's own established behavior (see test_clock_drift_
        recovery.py); this test only confirms _compute_queue_eta_state
        correctly passes the snapshot through and uses that value as
        its starting point."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=300.0, duration_seconds=310.0, position=0.0)  # incoming
        stand_in.decks["B"] = make_fake_deck(next_start_seconds=10.0, duration_seconds=20.0, position=0.0)  # outgoing, almost done
        item = self.make_queued_item(next_start_seconds=50.0, cue_in_seconds=0.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertAlmostEqual(queue[0]["eta_seconds"], 10.0, delta=0.5)

    def test_all_decks_paused_eta_baseline_is_zero(self):
        """A frozen/paused deck's remaining time is not a trustworthy
        live prediction -- _leading_deck_eta_seconds already excludes
        paused decks entirely and returns 0.0 when none qualify. This
        confirms _compute_queue_eta_state inherits that fail-safe
        rather than working around it."""
        stand_in = make_stand_in()
        stand_in.decks["A"] = make_fake_deck(next_start_seconds=200.0, duration_seconds=210.0, position=50.0, paused=True)
        item = self.make_queued_item(next_start_seconds=100.0, cue_in_seconds=0.0)
        stand_in.log_items = [item]
        queue = stand_in._compute_queue_eta_state(snapshot=stand_in.decks)
        self.assertEqual(queue[0]["eta_seconds"], 0.0)
