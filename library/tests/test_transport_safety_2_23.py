"""Roadmap 2.23 Phase 2a -- Eject and Voice Tracking safety (D9).

Real GStreamer decks (decodebin, silence prime, audiomixer request pads,
clock-synchronised sink) and the real queue/VT state machine; see
library/tests/_transport_harness.py. Each class pins one row of the
START-NEXT-CONTRACT Phase 2a table (E1-E9, stale VT timers, intro-only VT).
"""

from __future__ import annotations

import time
from unittest.mock import patch

from django.test import SimpleTestCase

from library.tests._transport_harness import (
    TransportHarness,
    fake_voicetrack,
    patch_voicetracks,
    pump,
    wait_until,
)


def _start(engine, slot):
    engine._start_next_track(slot=slot)
    return engine.decks[slot]


class EjectOverlapTests(SimpleTestCase):
    """E1-E4, E6, E9: Eject never consumes an extra occurrence while the
    next item is already entering the program."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        self.h.queue(
            {"id": 1, "seconds": 8}, {"id": 2, "seconds": 8},
            {"id": 3, "seconds": 8}, {"id": 4, "seconds": 8},
        )
        self.h.play()

    def _crossfade(self, outgoing_slot="A"):
        incoming_slot = "B" if outgoing_slot == "A" else "A"
        _start(self.engine, outgoing_slot)
        pump(0.4)
        _start(self.engine, incoming_slot)
        pump(0.4)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [1, 2])
        return outgoing_slot, incoming_slot

    def test_eject_outgoing_during_crossfade_starts_no_third_item(self):
        outgoing, incoming = self._crossfade("A")
        self.engine._eject_deck(outgoing)
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2], "Eject consumed an extra queue occurrence")
        self.assertEqual(self.h.occupants(), {outgoing: None, incoming: 2})
        self.assertEqual(self.engine._queue_cursor, 2)

    def test_outgoing_role_follows_occurrence_order_not_slot_letter(self):
        # Outgoing in B, incoming in A: _leading_deck() would pick A.
        outgoing, incoming = self._crossfade("B")
        self.engine._eject_deck(outgoing)
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.occupants(), {"A": 2, "B": None})

    def test_eject_incoming_during_crossfade_replaces_it_exactly_once(self):
        outgoing, incoming = self._crossfade("A")
        self.engine._eject_deck(incoming)
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2, 3])
        self.assertEqual(self.h.live_ids(), [1, 3])

    def test_eject_only_deck_advances_once(self):
        _start(self.engine, "A")
        pump(0.4)
        self.engine._eject_deck("A")
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [2])

    def test_delayed_eject_of_an_empty_slot_next_to_a_live_deck_is_a_noop(self):
        _start(self.engine, "B")
        pump(0.4)
        self.engine._eject_deck("A")
        pump(0.6)
        self.assertEqual(self.h.claims, [1], "a stale Eject started a second program item")
        self.assertEqual(self.h.occupants(), {"A": None, "B": 1})

    def test_eject_on_an_idle_console_still_starts_playback(self):
        self.engine._eject_deck("A")
        pump(0.4)
        self.assertEqual(self.h.claims, [1])

    def test_eject_records_eject_as_the_segment_termination_reason(self):
        _start(self.engine, "A")
        pump(0.4)
        self.engine._eject_deck("A")
        pump(0.4)
        closes = [c for c in self.h.duration_closes if c[0] == 1]
        self.assertEqual(len(closes), 1)
        self.assertEqual(closes[0][2], "eject")

    def test_eject_bound_to_a_superseded_occurrence_is_a_noop(self):
        _start(self.engine, "A")
        pump(0.4)
        self.engine._eject_deck("A", expected_log_item_id=999)
        pump(0.4)
        self.assertEqual(self.h.claims, [1])
        self.assertEqual(self.h.live_ids(), [1])
        self.engine._eject_deck("A", expected_log_item_id=1)
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2])

    def test_eject_command_carries_the_expected_occurrence_through_dispatch(self):
        _start(self.engine, "A")
        pump(0.4)
        self.engine._dispatch_engine_command(
            {"command": "deck_eject", "slot": "A", "expected_log_item_id": 7}
        )
        pump(0.3)
        self.assertEqual(self.h.live_ids(), [1])
        # A malformed expectation is ignored (r0116 callers send none).
        self.engine._dispatch_engine_command(
            {"command": "deck_eject", "slot": "A", "expected_log_item_id": "x"}
        )
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2])


class EjectVoiceTrackTests(SimpleTestCase):
    """E5, E7, E8: Eject cannot strand, bypass or duplicate a VT transition."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        outro = fake_voicetrack(self.h.media("outro-vt", seconds=1.0, freq=900), 1.0)
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.6, freq=700), 0.6)
        patch_voicetracks(self, outro=outro, intro=intro)
        self.h.queue(
            {"id": 1, "seconds": 8, "outro_starts": 1.0},
            {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8},
        )
        self.h.play()

    def _enter_vt(self):
        deck = _start(self.engine, "A")
        pump(0.4)
        self.assertTrue(self.engine._vt_maybe_enter(deck))
        self.assertEqual(self.engine._vt["phase"], "outro_playing")
        self.assertTrue(wait_until(
            lambda: self.engine.vt_duck_gain.get_property("volume") < 0.6, 1.0))
        return deck

    def _assert_vt_finished_cleanly(self):
        self.assertTrue(
            wait_until(lambda: self.engine._vt.get("phase") == "idle", 6.0),
            f"VT stranded in {self.engine._vt.get('phase')!r}",
        )
        self.assertTrue(wait_until(
            lambda: abs(self.engine.vt_duck_gain.get_property("volume") - 1.0) < 1e-6, 1.0),
            "program left VT-ducked")

    def test_eject_vt_outgoing_runs_the_vt_transition_exactly_once(self):
        self._enter_vt()
        self.engine._eject_deck("A")
        pump(0.2)
        # The incoming item waits for the outro VT, gap and intro VT.
        self.assertEqual(self.h.claims, [1], "Eject bypassed the VT transition")
        self._assert_vt_finished_cleanly()
        pump(0.5)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [2])

    def test_eject_of_an_empty_slot_during_the_vt_gap_does_not_double_start(self):
        self._enter_vt()
        self.engine._eject_deck("A")
        self.assertTrue(wait_until(
            lambda: self.engine._vt.get("phase") in ("outro_tail", "inter_gap"), 2.0))
        self.engine._eject_deck("B")
        self.engine._eject_deck("A")
        self._assert_vt_finished_cleanly()
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2], "VT timer and Eject both started an item")

    def test_eject_of_the_incoming_deck_during_its_intro_vt_releases_the_duck(self):
        # Characterization (E8, unchanged): the replacement starts; the intro VT
        # completes the sequence and releases the duck when it ends.
        self._enter_vt()
        self.engine._eject_deck("A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 6.0))
        incoming_slot = next(s for s, d in self.engine.decks.items() if d is not None)
        if self.engine._vt.get("phase") == "intro_playing":
            self.engine._eject_deck(incoming_slot)
        self._assert_vt_finished_cleanly()
        pump(0.5)
        self.assertLessEqual(len(self.h.live_ids()), 1)


class VoiceTrackNaturalTransitionTests(SimpleTestCase):
    """A VT transition after the outgoing deck ends NATURALLY, with the
    engine's periodic teardown tick running (as run() installs it): the
    tick's empty-console restart (5ecbfe3) must not start the incoming item
    during the outro tail / gap, and the VT must start it exactly once."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        outro = fake_voicetrack(self.h.media("outro-vt", seconds=1.2, freq=900), 1.2)
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.6, freq=700), 0.6)
        patch_voicetracks(self, outro=outro, intro=intro, gap_ms=300)
        self.h.queue(
            {"id": 1, "seconds": 1.0, "outro_starts": 0.2},
            {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8},
        )
        self.h.play()

    def test_the_teardown_tick_cannot_bypass_or_duplicate_the_vt_incoming_start(self):
        deck = _start(self.engine, "A")
        pump(0.35)
        self.assertTrue(self.engine._vt_maybe_enter(deck))
        self.assertTrue(wait_until(lambda: self.engine.decks["A"] is None, 3.0))
        self.assertIn(self.engine._vt["phase"], ("outro_tail", "inter_gap"))
        # Outro VT still talking (outro_tail), then the gap: no music yet.
        pump(0.5)
        self.assertEqual(self.h.claims, [1], "music started under the outro VT / gap")
        self.assertTrue(wait_until(lambda: self.engine._vt.get("phase") == "idle", 6.0))
        pump(0.8)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [2])
        self.assertAlmostEqual(self.engine.vt_duck_gain.get_property("volume"), 1.0)


class VoiceTrackWatchdogTests(SimpleTestCase):
    """A stuck VT outgoing deck retired by the stuck-deck watchdog hands over
    to the VT state machine exactly like its natural EOS."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        outro = fake_voicetrack(self.h.media("outro-vt", seconds=0.8, freq=900), 0.8)
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.4, freq=700), 0.4)
        patch_voicetracks(self, outro=outro, intro=intro)
        self.h.queue({"id": 1, "seconds": 8, "outro_starts": 1.0},
                     {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        self.engine._persist_media_incident = lambda _evidence: None
        for mocked in (
            patch("library.services.engine.DECK_STUCK_TIMEOUT_SECONDS", 0.05),
            patch("library.services.engine.capture_deck_evidence", lambda *a, **k: {}),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.h.play()

    def test_watchdog_retirement_of_the_vt_outgoing_deck_keeps_the_vt_transition(self):
        deck = _start(self.engine, "A")
        pump(0.3)
        self.assertTrue(self.engine._vt_maybe_enter(deck))
        deck.mark_milestone("A_DECODER_AUDIO_EOS")   # an EOS that was never handled
        pump(0.1)
        self.engine._check_stuck_decks()
        pump(0.3)
        self.assertEqual(self.h.claims, [1], "the watchdog bypassed the VT transition")
        self.assertEqual(self.h.occupants(), {"A": None, "B": None})
        self.assertTrue(wait_until(lambda: self.engine._vt.get("phase") == "idle", 6.0),
                        f"VT stranded in {self.engine._vt.get('phase')!r}")
        pump(0.6)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertAlmostEqual(self.engine.vt_duck_gain.get_property("volume"), 1.0)


class VoiceTrackTimerIdentityTests(SimpleTestCase):
    """Stale VT timers and fires are bound to their own sequence."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        outro = fake_voicetrack(self.h.media("outro-vt", seconds=0.5, freq=900), 0.5)
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.8, freq=700), 0.8)
        self.config = patch_voicetracks(self, outro=outro, intro=intro, gap_ms=400)
        self.items = self.h.queue(
            {"id": 1, "seconds": 8, "outro_starts": 1.0},
            {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8},
        )
        self.replacement = self.h.queue({"id": 10, "seconds": 8}, {"id": 11, "seconds": 8})
        self.engine.log_items = self.items
        self.engine._invalidate_schedule_authority = lambda _reason: None

        def load_replacement(_date, _hour):
            self.engine.log_items = list(self.replacement)
            self.engine._queue_cursor = 0

        self.engine._load_log_for = load_replacement
        self.h.play()

    def test_reload_during_the_vt_gap_cannot_fire_a_stale_vt_or_extra_item(self):
        deck = _start(self.engine, "A")
        pump(0.3)
        self.assertTrue(self.engine._vt_maybe_enter(deck))
        self.engine._eject_deck("A")   # outgoing ends -> outro_tail -> inter_gap
        self.assertTrue(wait_until(lambda: self.engine._vt.get("phase") == "inter_gap", 2.0))
        self.engine._dispatch_engine_command({"command": "reload_current_log"})
        pump(2.0)
        self.assertEqual(self.h.claims, [1, 10], "a stale VT timer started an extra item")
        self.assertEqual(self.engine._vt.get("phase"), "idle")
        self.assertAlmostEqual(self.engine.vt_duck_gain.get_property("volume"), 1.0)

    def test_reload_during_the_outro_releases_the_vt_and_its_duck(self):
        deck = _start(self.engine, "A")
        pump(0.3)
        self.assertTrue(self.engine._vt_maybe_enter(deck))
        pump(0.2)
        self.engine._dispatch_engine_command({"command": "reload_current_log"})
        pump(1.5)
        self.assertEqual(self.engine._vt.get("phase"), "idle")
        self.assertAlmostEqual(self.engine.vt_duck_gain.get_property("volume"), 1.0)
        self.assertEqual(self.h.claims, [1, 10])
        self.assertEqual(self.h.live_ids(), [10])

    def test_a_fire_eos_from_another_sequence_cannot_complete_this_one(self):
        with self.engine._vt_lock:
            self.engine._vt = {
                "phase": "intro_playing", "incoming_vt_fire_id": 5,
                "outgoing_vt_fire_id": None, "incoming_started": True,
            }
        self.engine._vt_on_fire_eos("intro", 99)
        self.assertEqual(self.engine._vt["phase"], "intro_playing")
        self.engine._vt_on_fire_eos("intro", 5)
        self.assertEqual(self.engine._vt["phase"], "idle")

    def test_a_newer_duck_ramp_supersedes_an_older_one(self):
        self.engine._vt_ramp_duck_to_db(-6.0, 300)
        pump(0.05)
        self.engine._vt_ramp_duck_to_db(0.0, 40)
        pump(0.6)
        self.assertAlmostEqual(self.engine.vt_duck_gain.get_property("volume"), 1.0)


class IntroOnlyVoiceTrackCharacterizationTests(SimpleTestCase):
    """2.25 hypothesis, characterized only (no behaviour change in 2.23):
    VT entry requires the OUTGOING track's outro marker, so an incoming-only
    intro VT never airs after a track without one."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        intro = fake_voicetrack(self.h.media("intro-vt", seconds=0.5, freq=700), 0.5)
        patch_voicetracks(self, outro=None, intro=intro)
        for name in ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
                     "_sweep_active_message_queue", "_warm_track_cache"):
            setattr(self.engine, name, lambda *_args, **_kwargs: None)
        self.h.play()

    def _poll_at(self, deck, position):
        deck.started_at = time.time() - 60.0
        with patch.object(type(self.engine), "_get_deck_position", lambda _self, _deck: position):
            self.engine._poll_position()

    def test_intro_only_vt_is_never_entered_without_an_outgoing_outro_marker(self):
        self.h.queue({"id": 1, "seconds": 30, "outro_starts": None, "next_start": 25},
                     {"id": 2, "seconds": 8})
        deck = _start(self.engine, "A")
        with patch.object(type(self.engine), "_vt_maybe_enter",
                          side_effect=AssertionError("entered")) as entered:
            self._poll_at(deck, 20.0)
        entered.assert_not_called()
        self.assertEqual(self.engine._vt["phase"], "idle")

    def test_vt_is_entered_once_the_outgoing_has_an_outro_marker(self):
        self.h.queue({"id": 1, "seconds": 30, "outro_starts": 18.0, "next_start": 25},
                     {"id": 2, "seconds": 8})
        deck = _start(self.engine, "A")
        self._poll_at(deck, 20.0)
        self.assertEqual(self.engine._vt["phase"], "outro_playing")
        self.assertTrue(self.engine._next_triggered)


class OrdinaryAutomaticTransitionRegressionTests(SimpleTestCase):
    """The natural trigger/EOS paths still advance exactly once."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        patch_voicetracks(self)
        for name in ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
                     "_sweep_active_message_queue", "_warm_track_cache"):
            setattr(self.engine, name, lambda *_args, **_kwargs: None)
        self.h.queue({"id": 1, "seconds": 30, "next_start": 25}, {"id": 2, "seconds": 8},
                     {"id": 3, "seconds": 8})
        self.h.play()

    def _poll_at(self, deck, position):
        deck.started_at = time.time() - 60.0
        with patch.object(type(self.engine), "_get_deck_position", lambda _self, _deck: position):
            self.engine._poll_position()

    def test_trigger_starts_exactly_one_item_and_repeated_polls_add_none(self):
        deck = _start(self.engine, "A")
        for _ in range(4):
            self._poll_at(deck, 26.0)
            pump(0.05)
        self.assertEqual(self.h.claims, [1, 2])

    def test_manual_hold_defers_then_auto_release_starts_once(self):
        deck = _start(self.engine, "A")
        self.engine.manual_mode = True
        self._poll_at(deck, 26.0)
        self.assertTrue(self.engine._manual_hold_pending)
        self.assertEqual(self.h.claims, [1])
        self.engine._set_manual_mode(False)
        pump(0.3)
        self.assertEqual(self.h.claims, [1, 2])
        self.engine._set_manual_mode(False)
        pump(0.3)
        self.assertEqual(self.h.claims, [1, 2])

    def test_natural_end_of_only_deck_advances_once(self):
        self.h.queue({"id": 1, "seconds": 0.5}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        deck = _start(self.engine, "A")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 4.0),
                        f"claims={self.h.claims} milestones={sorted(deck.eos_milestones)} "
                        f"decks={self.h.occupants()} closes={self.h.duration_closes}")
        pump(0.5)
        self.assertEqual(self.h.claims, [1, 2])


def _post_stream_error(deck):
    """What wavparse posts when a push fails not-linked: a STREAM error from an
    element inside this exact generation's bin (routed by _deck_bin_map)."""
    from gi.repository import GLib, Gst
    element = deck.pipeline.iterate_elements().next()[1]
    error = GLib.Error.new_literal(Gst.StreamError.quark(), "Internal data stream error.",
                                   int(Gst.StreamError.FAILED))
    element.post_message(Gst.Message.new_error(element, error, "streaming stopped, reason not-linked (-1)"))


class NonProgramGenerationErrorTests(SimpleTestCase):
    """r0116 Pause/Resume hazard found under full-suite load: Pause unlinks
    the deck before PAUSED, so a buffer in flight can fail not-linked and the
    bin posts a stream error. That generation contributes no program audio
    (paused) or has already been superseded in its slot (the paused/seeked
    predecessor a gated seek keeps until its successor prerolls); its error
    must not complete the occurrence and start the next item -- observed: the
    resumed item AND the next item both live."""

    def setUp(self):
        self.h = TransportHarness(self)
        self.engine = self.h.engine
        self.engine._persist_media_incident = lambda _evidence: None
        patch_voicetracks(self)
        self.h.queue({"id": 1, "seconds": 30}, {"id": 2, "seconds": 30}, {"id": 3, "seconds": 30})
        self.h.play()

    def test_an_error_from_a_paused_deck_does_not_start_the_next_item(self):
        deck = _start(self.engine, "A")
        pump(0.4)
        self.engine._pause_deck("A")
        _post_stream_error(deck)
        pump(0.6)
        self.assertEqual(self.h.claims, [1], "Pause turned into a skip")
        self.assertIs(self.engine.decks["A"], deck)
        self.assertTrue(deck.paused)
        self.engine._resume_deck("A")
        self.assertTrue(wait_until(
            lambda: self.engine.decks["A"] is not None and self.engine.decks["A"].gated_seek is None, 6.0))
        pump(0.4)
        self.assertEqual(self.h.live_ids(), [1])

    def test_an_error_from_a_seek_replaced_predecessor_does_not_start_another_item(self):
        paused = _start(self.engine, "A")
        pump(0.4)
        self.engine._pause_deck("A")
        self.engine._resume_deck("A")
        self.assertIsNot(self.engine.decks["A"], paused)      # successor installed, predecessor kept
        _post_stream_error(paused)
        self.assertTrue(wait_until(
            lambda: self.engine.decks["A"] is not None and self.engine.decks["A"].gated_seek is None, 6.0))
        pump(0.6)
        self.assertNotIn(2, self.h.claims, "a superseded generation's error started the next item")
        self.assertEqual(self.h.live_ids(), [1])

    def test_an_error_on_the_playing_deck_still_advances_as_before(self):
        deck = _start(self.engine, "A")
        pump(0.4)
        _post_stream_error(deck)
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2], 3.0), self.h.claims)
        pump(0.4)
        self.assertEqual(self.h.live_ids(), [2])
