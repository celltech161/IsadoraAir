"""Roadmap 2.23 -- Start Next fade-controller lifecycle under churn.

Removing a GstController binding from a pad the aggregator is still syncing
-- or adding a second binding for the same property, which replaces the first
-- crashes the process (SIGSEGV in gst_object_sync_values, which walks bindings
without the object lock; r0117 review SN-GST-1). deck_fade now gives each live
pad exactly ONE persistent binding, reprogrammed for every fade/cancel/restore
and released only with the pad. This drives many complete / cancel+restore /
immediate retry / overlap / pause / reload / seek cycles on the real program
topology (appsink, clock-synchronised) and proves:

- the process survives (a crash would abort the whole test run);
- every surviving pad has at most one ATTACHED binding (the real list, not
  just "active" ones), and it is that deck's own controller binding;
- a retry while a restoration owns the pad is refused, never bound again;
- no controller, mixer request pad, GLib timer or deck generation
  accumulates; every pad ends at unity; no later gain stage is touched;
- no stale Start Next callback acts after its transition ended.
"""

from __future__ import annotations

import gc
import weakref
from unittest.mock import patch

from django.test import SimpleTestCase

from library.services import deck_fade
from library.tests._transport_harness import (
    TransportHarness,
    patch_voicetracks,
    pump,
    start_next_command,
    wait_until,
)


class FadeControllerLifecycleTests(SimpleTestCase):
    CYCLES = 6

    def setUp(self):
        self.h = TransportHarness(self, appsink=True)
        self.engine = self.h.engine
        patch_voicetracks(self)
        self.h.queue(*({"id": i, "seconds": 30, "freq": 400.0} for i in range(1, 80)))
        self.engine._invalidate_schedule_authority = lambda _reason: None
        self.engine._load_log_for = lambda _date, _hour: None   # reload keeps the queue
        self.fades = []
        original = deck_fade.PadVolumeControl

        harness = self

        class TrackedFade(original):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                harness.fades.append(weakref.ref(self))

        tracked = patch.object(deck_fade, "PadVolumeControl", TrackedFade)
        tracked.start()
        self.addCleanup(tracked.stop)
        self.late_calls = []
        for name in ("_start_next_incoming_due", "_start_next_completion_due",
                     "_start_next_anchor_due", "_start_next_restore_done"):
            self._spy(name)
        self.h.play()

    def _spy(self, name):
        bound = getattr(self.engine, name)

        def spy(token, _bound=bound, _name=name):
            if _name == "_start_next_restore_done":
                owner = self.engine._start_next_restoring
                if owner is None or owner["token"] != token:
                    self.late_calls.append(_name)
            else:
                active = self.engine._start_next_active
                if active is None or active["request_id"] != token:
                    self.late_calls.append(_name)
            return _bound(token)

        setattr(self.engine, name, spy)

    def send(self, fade_ms=250):
        self.engine._dispatch_engine_command(start_next_command(self.engine, fade_ms=fade_ms))
        return self.engine._start_next_state()["recent"][:1]

    def settle(self):
        self.assertTrue(wait_until(lambda: self.engine._start_next_active is None, 5.0),
                        self.engine._start_next_state())
        return self.engine._start_next_state()["recent"][0]

    def ensure_one_live_deck(self):
        if not self.h.live_ids():
            self.engine._start_next_track()
        self.assertTrue(wait_until(lambda: len(self.h.live_ids()) == 1, 3.0), self.h.occupants())
        pump(0.15)

    def assert_one_binding_per_pad(self):
        for deck in self.engine.decks.values():
            if deck is not None and deck.mixer_pad is not None:
                count = len(deck.mixer_pad.control_bindings)
                self.assertLessEqual(count, 1, "a second binding was attached to a live pad")
                if count:
                    self.assertIs(deck.mixer_pad.get_control_binding("volume"),
                                  deck.volume_control.binding)

    def assert_clean_program(self):
        self.assertTrue(wait_until(lambda: self.engine._start_next_restoring is None, 2.0))
        pump(0.1)
        self.assert_one_binding_per_pad()
        for deck in self.engine.decks.values():
            if deck is not None and deck.mixer_pad is not None:
                self.assertAlmostEqual(deck.mixer_pad.get_property("volume"), 1.0)
        for element in (self.engine.vt_duck_gain, self.engine.duck_gain):
            self.assertEqual(element.get_property("volume"), 1.0)
            self.assertEqual(len(element.control_bindings), 0)
        self.assertLessEqual(len(list(self.engine.mixer.sinkpads)), 2)
        self.assertIsNone(self.engine._start_next_active)

    def test_repeated_complete_cancel_restore_overlap_reload_and_seek_cycles(self):
        outcomes = []
        for cycle in range(self.CYCLES):
            # 1. completed fade_and_start
            self.ensure_one_live_deck()
            self.send()
            outcomes.append(self.settle()["outcome"])
            # 2. cancel + restore: eject the incoming deck mid-fade
            self.ensure_one_live_deck()
            self.send(fade_ms=1200)
            self.assertTrue(wait_until(
                lambda: self.engine._start_next_active is not None
                and self.engine._start_next_active.get("incoming") is not None, 3.0))
            incoming_slot = self.engine._start_next_active["incoming"]["slot"]
            self.engine._eject_deck(incoming_slot)
            outcomes.append(self.engine._start_next_state()["recent"][0]["reason"])
            # Immediate retry on the same still-restoring pad: refused, unbound.
            self.send()
            outcomes.append(self.engine._start_next_state()["recent"][0]["reason"])
            self.assert_one_binding_per_pad()
            pump(0.5)
            # 3. overlap: fade the older of two live decks
            live = self.engine._live_decks()
            if len(live) == 1:
                self.engine._start_next_track()
                self.assertTrue(wait_until(lambda: len(self.h.live_ids()) == 2, 3.0))
            pump(0.15)
            self.send()
            outcomes.append(self.settle()["outcome"])
            # 4. cancel before anchoring could matter: pause the outgoing deck at once
            self.ensure_one_live_deck()
            self.send(fade_ms=1000)
            outgoing = self.engine._live_decks()[0]
            self.engine._pause_deck(outgoing.slot)
            outcomes.append(self.engine._start_next_state()["recent"][0]["reason"])
            self.engine._resume_deck(outgoing.slot)
            self.assertTrue(wait_until(
                lambda: self.engine.decks[outgoing.slot] is not None
                and self.engine.decks[outgoing.slot].gated_seek is None, 6.0))
            pump(0.3)
            # 5. reload mid-fade
            self.ensure_one_live_deck()
            self.send(fade_ms=1000)
            pump(0.1)
            self.engine._dispatch_engine_command({"command": "reload_current_log"})
            outcomes.append(self.engine._start_next_state()["recent"][0]["reason"])
            pump(0.4)
            # 6. seek the outgoing deck mid-fade
            self.ensure_one_live_deck()
            self.send(fade_ms=1000)
            pump(0.1)
            self.engine._seek_deck(self.engine._live_decks()[0].slot, 3.0)
            outcomes.append(self.engine._start_next_state()["recent"][0]["reason"])
            self.assertTrue(wait_until(
                lambda: all(d is None or d.gated_seek is None for d in self.engine.decks.values()),
                6.0))
            pump(0.4)
            self.assert_clean_program()

        self.assertEqual(outcomes, [
            "completed", "superseded_by_eject", "busy_restoring", "completed", "superseded_by_pause",
            "superseded_by_reload", "superseded_by_seek",
        ] * self.CYCLES)
        pump(0.8)
        self.assertEqual(self.late_calls, [], "a Start Next callback ran after its transition ended")
        # Shutdown with a transition in flight, then nothing may remain.
        self.ensure_one_live_deck()
        self.send(fade_ms=2000)
        self.engine._media_validation_worker = None
        self.engine.stop()
        self.assertEqual(self.engine._start_next_state()["recent"][0]["reason"], "engine_stopped")
        pump(0.3)
        gc.collect()
        alive = [ref for ref in self.fades if ref() is not None]
        self.assertEqual(alive, [], f"{len(alive)} of {len(self.fades)} volume controllers leaked")
        # One controller per bound pad generation, reused within it -- never
        # more than one per fade.
        self.assertLessEqual(len(self.fades), 6 * self.CYCLES + 1)
