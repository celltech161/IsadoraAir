"""Roadmap 2.23 Phase 2b -- Start Next engine state machine.

Real GStreamer decks and the real queue path (library/tests/_transport_harness);
every assertion is on engine-observable state: which occurrences got a deck
(claims), which decks are live, the published start_next outcome, the
closing segment's termination reason, Auto/Manual ownership and the gain of
every stage. The rows map to START-NEXT-CONTRACT sections 3.1-3.8.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst
from django.test import SimpleTestCase

from library.tests._transport_harness import (
    TransportHarness,
    fake_voicetrack,
    patch_voicetracks,
    pump,
    running_seconds,
    attached_volume_bindings,
    start_next_command,
    wait_until,
)


def _start(engine, slot):
    engine._start_next_track(slot=slot)
    return engine.decks[slot]


class _StartNextBase(SimpleTestCase):
    appsink = False

    def setUp(self):
        self.h = TransportHarness(self, appsink=self.appsink)
        self.engine = self.h.engine
        patch_voicetracks(self)

    def send(self, **kwargs):
        command = start_next_command(self.engine, **kwargs)
        self.engine._dispatch_engine_command(command)
        return command

    def settle(self, timeout=6.0):
        self.assertTrue(
            wait_until(lambda: getattr(self.engine, "_start_next_active", None) is None, timeout),
            f"Start Next never reached a terminal outcome: {self.engine._start_next_state()}",
        )
        return self.last()

    def last(self):
        recent = self.engine._start_next_state()["recent"]
        self.assertTrue(recent, "no Start Next outcome recorded")
        return recent[0]

    def closes_for(self, item_id):
        return [c for c in self.h.duration_closes if c[0] == item_id]

    def assert_untouched_program_gains(self):
        for element in (self.engine.vt_duck_gain, self.engine.duck_gain):
            self.assertEqual(element.get_property("volume"), 1.0, element.get_name())
            self.assertEqual(len(element.control_bindings), 0, element.get_name())


class StartNextModeTests(_StartNextBase):
    """3.2 decision table."""

    def setUp(self):
        super().setUp()
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        self.h.play()

    def test_deck_a_live_fades_a_and_starts_the_next_item_once(self):
        _start(self.engine, "A")
        pump(0.5)
        self.send()
        self.assertEqual(self.engine._start_next_state()["active"]["phase"], "fading")
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["mode"], "fade_and_start")
        self.assertEqual(record["outgoing"]["log_item_id"], 1)
        self.assertEqual(record["incoming"], {"slot": "B", "log_item_id": 2})
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.occupants(), {"A": None, "B": 2})
        self.assertEqual(self.closes_for(1)[-1][2], "start_next")
        self.assertFalse(self.engine._next_triggered)
        self.assert_untouched_program_gains()

    def test_deck_b_live_fades_b_and_starts_the_next_item_in_a(self):
        _start(self.engine, "B")
        pump(0.5)
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["incoming"], {"slot": "A", "log_item_id": 2})
        self.assertEqual(self.h.occupants(), {"A": 2, "B": None})

    def _overlap(self, outgoing_slot):
        incoming_slot = "B" if outgoing_slot == "A" else "A"
        _start(self.engine, outgoing_slot)
        pump(0.4)
        _start(self.engine, incoming_slot)
        pump(0.4)
        return incoming_slot

    def test_overlap_in_auto_fades_only_the_older_deck_and_starts_nothing(self):
        incoming_slot = self._overlap("A")
        incoming_pad = self.engine.decks[incoming_slot].mixer_pad
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["mode"], "overlap")
        self.assertEqual(record["outgoing"]["log_item_id"], 1)
        self.assertEqual(self.h.claims, [1, 2], "Start Next consumed a third occurrence")
        self.assertEqual(self.h.occupants(), {"A": None, "B": 2})
        self.assertEqual(incoming_pad.get_property("volume"), 1.0)
        self.assertEqual(attached_volume_bindings(incoming_pad), (0, None))   # never bound

    def test_overlap_in_manual_is_identical_and_keeps_manual(self):
        incoming_slot = self._overlap("A")
        self.engine.manual_mode = True
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2])
        self.assertTrue(self.engine.manual_mode)
        self.assertFalse(self.engine._manual_hold_pending)
        self.assertEqual(self.h.live_ids(), [2])
        self.assertIsNotNone(self.engine.decks[incoming_slot])

    def test_overlap_roles_follow_the_occurrence_not_the_slot_letter(self):
        self._overlap("B")
        self.send()
        record = self.settle()
        self.assertEqual(record["outgoing"], {"slot": "B", "log_item_id": 1, "generation": 1})
        self.assertEqual(self.h.occupants(), {"A": 2, "B": None})

    def test_only_a_paused_deck_is_retired_and_the_next_item_starts(self):
        _start(self.engine, "A")
        pump(0.4)
        self.engine._pause_deck("A")
        self.send(expected={"live_log_item_ids": []})
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["mode"], "start_only")
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [2])
        self.assertEqual(self.closes_for(1)[-1][2], "start_next")

    def test_a_live_deck_next_to_a_paused_deck_is_refused_unchanged(self):
        _start(self.engine, "A")
        pump(0.3)
        _start(self.engine, "B")
        pump(0.3)
        self.engine._pause_deck("A")
        self.send()
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("rejected", "paused_deck_occupies_slot"))
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.engine.decks["B"].mixer_pad.get_property("volume"), 1.0)

    def test_stopped_console_starts_the_next_item_immediately(self):
        self.send(expected={"live_log_item_ids": []})
        record = self.settle()
        self.assertEqual((record["outcome"], record["mode"]), ("completed", "start_only"))
        self.assertEqual(self.h.claims, [1])

    def test_stopped_console_with_nothing_queued_is_refused(self):
        self.h.queue()
        self.send(expected={"live_log_item_ids": []})
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("rejected", "nothing_to_start"))
        self.assertEqual(self.h.on_log_exhausted_calls, [])

    def test_next_hour_log_is_the_eligible_next_item_at_the_boundary(self):
        nxt = self.h.queue({"id": 10, "seconds": 8}, {"id": 11, "seconds": 8})
        self.h.queue({"id": 1, "seconds": 8})
        log = SimpleNamespace(id=77, date="2026-10-23", hour=11)

        def roll_over():
            self.engine.current_log, self.engine.log_items = log, list(nxt)
            self.engine._queue_cursor = 0
            return True

        self.engine._peek_next_hour = lambda: (log, list(nxt))
        self.engine._roll_over_to_next_hour = roll_over
        _start(self.engine, "A")
        pump(0.4)
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 10])


class StartNextEmptyQueueTests(_StartNextBase):
    """D4: fade to silence, consume nothing, no immediate restart."""

    def setUp(self):
        super().setUp()
        self.h.queue({"id": 1, "seconds": 8})
        self.h.play()
        self.deck = _start(self.engine, "A")
        pump(0.4)

    def test_auto_fades_to_silence_and_hands_over_to_end_of_queue_authority(self):
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed_to_silence")
        self.assertEqual(self.h.claims, [1])
        self.assertEqual(self.h.live_ids(), [])
        self.assertEqual(self.h.on_log_exhausted_calls, ["A"])
        self.assertFalse(self.engine.manual_mode)
        self.assertEqual(self.closes_for(1)[-1][2], "start_next")
        pump(0.6)
        self.assertEqual(self.h.claims, [1], "the faded item restarted")

    def test_manual_fades_to_silence_and_arms_the_ordinary_hold(self):
        self.engine.manual_mode = True
        self.send(release_manual=True)
        record = self.settle()
        self.assertEqual(record["outcome"], "completed_to_silence")
        self.assertFalse(record["manual_released"])
        self.assertTrue(self.engine.manual_mode)
        self.assertTrue(self.engine._manual_hold_pending)
        self.assertEqual(self.h.on_log_exhausted_calls, [])
        # Auto release: the withheld handoff runs the established path once.
        self.engine._set_manual_mode(False)
        self.assertEqual(len(self.h.on_log_exhausted_calls), 1)
        self.assertEqual(self.h.claims, [1])

    def test_an_item_queued_during_the_fade_is_started_at_its_end(self):
        self.send(fade_ms=600)
        pump(0.2)
        self.h.queue({"id": 5, "seconds": 8})
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 5])


class StartNextManualPolicyTests(_StartNextBase):
    """D8 and the pending-hold double-start hazard."""

    def setUp(self):
        super().setUp()
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        self.h.play()
        _start(self.engine, "A")
        pump(0.4)
        self.engine.manual_mode = True

    def test_manual_is_preserved_by_default(self):
        self.send()
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertTrue(self.engine.manual_mode)
        self.assertFalse(record["manual_released"])
        self.assertEqual(self.h.claims, [1, 2])

    def test_operator_owned_manual_is_released_when_enabled(self):
        self.send(release_manual=True)
        record = self.settle()
        self.assertTrue(record["manual_released"])
        self.assertFalse(self.engine.manual_mode)
        pump(0.4)
        self.assertEqual(self.h.claims, [1, 2], "releasing Manual started an extra item")

    def test_a_studio_mic_hold_is_never_released(self):
        self.engine._manual_from_mic = True
        self.engine.mic_live = True
        self.send(release_manual=True)
        record = self.settle()
        self.assertFalse(record["manual_released"])
        self.assertTrue(self.engine.manual_mode)
        self.assertTrue(self.engine._manual_from_mic)

    def test_a_live_remote_mic_blocks_release_even_for_operator_manual(self):
        self.engine._remote_dj_gate_open = lambda: True
        self.send(release_manual=True)
        record = self.settle()
        self.assertFalse(record["manual_released"])
        self.assertTrue(self.engine.manual_mode)

    def test_remote_dj_reconnect_grace_blocks_release(self):
        self.engine._remote_dj_reconnecting = lambda: True
        self.send(release_manual=True)
        record = self.settle()
        self.assertFalse(record["manual_released"])
        self.assertTrue(self.engine.manual_mode)

    def test_a_withheld_handoff_is_satisfied_and_auto_release_starts_nothing_extra(self):
        self.engine._manual_hold_pending = True
        self.send()
        self.settle()
        self.assertFalse(self.engine._manual_hold_pending)
        self.engine._set_manual_mode(False)
        pump(0.4)
        self.assertEqual(self.h.claims, [1, 2])


class StartNextIdentityTests(_StartNextBase):
    """3.1: freshness, replay, duplicates, restart, target binding."""

    def setUp(self):
        super().setUp()
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        self.h.play()
        _start(self.engine, "A")
        pump(0.4)

    def assert_rejected(self, reason):
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("rejected", reason))
        self.assertIsNone(getattr(self.engine, "_start_next_active", None))
        self.assertEqual(self.h.claims, [1])
        self.assertEqual(self.engine.decks["A"].mixer_pad.get_property("volume"), 1.0)

    def test_stale_and_future_commands_are_refused(self):
        self.send(issued_at=time.time() - 6)
        self.assert_rejected("stale_command")
        self.send(issued_at=time.time() + 3)
        self.assert_rejected("stale_command")

    def test_a_command_from_before_an_engine_restart_is_refused(self):
        self.send(engine_instance="previous-engine-instance")
        self.assert_rejected("engine_restarted")

    def test_malformed_values_are_refused(self):
        for overrides in ({"fade_ms": 100}, {"fade_ms": 20000}, {"fade_ms": "2000"},
                          {"fade_ms": True}, {"release_manual": "yes"}, {"issued_at": "now"}):
            with self.subTest(**{k: repr(v) for k, v in overrides.items()}):
                command = start_next_command(self.engine)
                command.update(overrides)
                self.engine._dispatch_engine_command(command)
                self.assert_rejected("malformed")
        self.engine._dispatch_engine_command({"command": "start_next"})
        self.assertEqual(self.last()["reason"], "malformed")

    def test_a_target_the_operator_did_not_see_is_refused(self):
        self.send(expected={"live_log_item_ids": [99]})
        self.assert_rejected("target_changed")

    def test_a_duplicate_command_file_is_ignored_silently(self):
        command = self.send(request_id="same-request")
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        count = len(self.engine._start_next_state()["recent"])
        self.engine._dispatch_engine_command(dict(command, issued_at=time.time()))
        self.assertEqual(len(self.engine._start_next_state()["recent"]), count)
        self.assertEqual(self.h.claims, [1, 2])

    def test_a_retried_client_request_is_refused_as_a_duplicate(self):
        first = self.send(client_request_id="click-1")
        self.send(client_request_id="click-1")
        record = self.last()
        self.assertEqual(record["reason"], "duplicate_request")
        self.assertEqual(record["duplicate_of"], first["request_id"])
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2])

    def test_the_same_client_id_from_another_user_is_independent(self):
        self.send(client_request_id="click-1", user_id=1)
        self.settle()
        self.send(client_request_id="click-1", user_id=2)
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2, 3])

    def test_double_click_and_a_delayed_click_advance_exactly_once(self):
        expected = {"live_log_item_ids": [1]}
        self.send(expected=expected)
        self.send(expected=expected)
        self.assertEqual(self.last()["reason"], "busy_start_next")
        self.assertEqual(self.settle()["outcome"], "completed")
        self.send(expected=expected)   # delayed duplicate of the same click
        self.assertEqual(self.last()["reason"], "target_changed")
        pump(0.5)
        self.assertEqual(self.h.claims, [1, 2])

    def test_a_queued_backlog_after_restart_cannot_replay(self):
        command = start_next_command(self.engine, issued_at=time.time() - 30,
                                     engine_instance="previous-engine-instance")
        self.engine._dispatch_engine_command(command)
        self.assertIn(self.last()["reason"], {"engine_restarted", "stale_command"})
        self.assertEqual(self.h.claims, [1])


class StartNextRefusalTests(_StartNextBase):
    """D6 and seek safety."""

    def setUp(self):
        super().setUp()
        outro = fake_voicetrack(self.h.media("outro", seconds=1.0, freq=900), 1.0)
        patch_voicetracks(self, outro=outro)
        self.h.queue({"id": 1, "seconds": 8, "outro_starts": 1.0}, {"id": 2, "seconds": 8})
        self.h.play()
        self.deck = _start(self.engine, "A")
        pump(0.4)

    def test_an_active_voice_track_transition_is_refused(self):
        self.assertTrue(self.engine._vt_maybe_enter(self.deck))
        self.send()
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("rejected", "busy_voice_track"))
        self.assertEqual(self.h.claims, [1])

    def test_a_seek_in_flight_is_refused(self):
        self.deck.gated_seek = {"phase": "seeking"}
        try:
            self.send()
        finally:
            self.deck.gated_seek = None
        self.assertEqual(self.last()["reason"], "busy_seek")

    def test_a_pending_vt_is_not_entered_during_the_fade(self):
        for name in ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
                     "_sweep_active_message_queue", "_warm_track_cache"):
            setattr(self.engine, name, lambda *_a, **_k: None)
        self.send(fade_ms=800)
        self.deck.started_at = time.time() - 60
        with patch.object(type(self.engine), "_get_deck_position", lambda _s, _d: 3.0):
            self.engine._poll_position()
        self.assertEqual(self.engine._vt["phase"], "idle")
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2])


class StartNextInterruptionTests(_StartNextBase):
    """3.7: operator commands, EOS, faults, shutdown, watchdog."""

    def setUp(self):
        super().setUp()
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        self.h.play()

    def test_eject_of_the_outgoing_deck_supersedes_and_advances_once(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=1500)
        pump(0.2)
        self.engine._eject_deck("A")
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("cancelled", "superseded_by_eject"))
        pump(2.0)
        self.assertEqual(self.h.claims, [1, 2], "Start Next's timer and Eject both advanced")

    def test_pause_of_the_outgoing_deck_cancels_without_starting_anything(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=1500)
        pump(0.2)
        self.engine._pause_deck("A")
        self.assertEqual(self.last()["reason"], "superseded_by_pause")
        pump(2.0)
        self.assertEqual(self.h.claims, [1])
        self.assertTrue(self.engine.decks["A"].paused)
        self.assertFalse(self.engine._next_triggered)

    def test_seek_of_the_outgoing_deck_cancels_and_the_new_generation_is_unfaded(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=1500)
        pump(0.3)
        self.engine._seek_deck("A", 2.0)
        self.assertEqual(self.last()["reason"], "superseded_by_seek")
        settled = wait_until(
            lambda: self.engine.decks["A"] is not None
            and self.engine.decks["A"].gated_seek is None
            and self.engine.decks["A"].mixer_pad is not None, 6.0)
        deck = self.engine.decks["A"]
        self.assertTrue(settled, (
            deck and deck.generation, deck and deck.gated_seek and deck.gated_seek.get("phase"),
            deck and sorted(deck.eos_milestones), self.h.claims, self.h.occupants()))
        pump(2.0)
        self.assertNotIn(2, self.h.claims)
        pad = self.engine.decks["A"].mixer_pad
        self.assertEqual(pad.get_property("volume"), 1.0)
        self.assertEqual(attached_volume_bindings(pad), (0, None))   # a new generation, never bound

    def test_eject_of_the_incoming_deck_restores_the_outgoing_deck(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=2000)
        self.assertTrue(wait_until(lambda: self.engine.decks["B"] is not None, 3.0))
        self.engine._eject_deck("B")
        self.assertEqual(self.last()["reason"], "superseded_by_eject")
        pump(0.6)
        deck = self.engine.decks["A"]
        self.assertEqual(deck.mixer_pad.get_property("volume"), 1.0)
        self.assertEqual(attached_volume_bindings(deck.mixer_pad), (1, deck.volume_control.binding))
        self.assertIsNone(self.engine._start_next_restoring)
        self.assertEqual(self.h.claims, [1, 2, 3])
        self.assertEqual(self.h.live_ids(), [1, 3])

    def test_a_play_now_reload_supersedes_the_transition(self):
        self.engine._invalidate_schedule_authority = lambda _reason: None
        replacement = []

        def load(_date, _hour):
            replacement[:] = self.h.queue({"id": 20, "seconds": 8})
            self.engine._queue_cursor = 0

        self.engine._load_log_for = load
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=1500)
        pump(0.2)
        self.engine._dispatch_engine_command({"command": "reload_current_log"})
        self.assertEqual(self.last()["reason"], "superseded_by_reload")
        pump(2.0)
        self.assertEqual(self.h.claims, [1, 20])

    def test_outgoing_eos_before_the_fade_ends_starts_the_incoming_once(self):
        self.h.queue({"id": 1, "seconds": 0.8}, {"id": 2, "seconds": 8}, {"id": 3, "seconds": 8})
        _start(self.engine, "A")
        pump(0.3)
        self.send(fade_ms=3000)
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        pump(0.5)
        self.assertEqual(self.h.claims, [1, 2])
        self.assertEqual(self.h.live_ids(), [2])

    def test_an_incoming_item_shorter_than_the_fade_hands_over_naturally(self):
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 0.3, "cue_in": 1.6},
                     {"id": 3, "seconds": 8})
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=2000)
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertTrue(wait_until(lambda: self.h.claims == [1, 2, 3], 2.0), self.h.claims)

    def test_an_incoming_item_shorter_than_the_fade_in_manual_arms_the_hold(self):
        self.h.queue({"id": 1, "seconds": 8}, {"id": 2, "seconds": 0.3, "cue_in": 1.6},
                     {"id": 3, "seconds": 8})
        _start(self.engine, "A")
        pump(0.4)
        self.engine.manual_mode = True
        self.send(fade_ms=2000)
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertTrue(self.engine._manual_hold_pending)
        self.assertEqual(self.h.claims, [1, 2])

    def test_unopenable_next_items_fail_and_restore_the_outgoing_deck(self):
        original = self.engine._claim_playback_occurrence
        self.engine._claim_playback_occurrence = (
            lambda item: False if item.id in (2, 3) else original(item))
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=800)
        record = self.settle()
        self.assertEqual((record["outcome"], record["reason"]), ("failed", "incoming_open_failed"))
        pump(0.6)
        self.assertEqual(self.h.live_ids(), [1])
        deck = self.engine.decks["A"]
        self.assertEqual(deck.mixer_pad.get_property("volume"), 1.0)
        self.assertEqual(attached_volume_bindings(deck.mixer_pad), (1, deck.volume_control.binding))
        self.assertFalse(self.engine._next_triggered)

    def test_one_unopenable_item_is_skipped_as_ever(self):
        original = self.engine._claim_playback_occurrence
        self.engine._claim_playback_occurrence = (
            lambda item: False if item.id == 2 else original(item))
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=600)
        record = self.settle()
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["incoming"]["log_item_id"], 3)
        self.assertEqual(self.h.claims, [1, 3])

    def test_shutdown_during_the_fade_is_recorded(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=2000)
        self.engine._media_validation_worker = None
        self.engine.stop()
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("cancelled", "engine_stopped"))
        self.assertIsNone(getattr(self.engine, "_start_next_active", None))

    def test_lost_timers_are_bounded_by_the_watchdog(self):
        _start(self.engine, "A")
        pump(0.4)
        self.send(fade_ms=2000)
        active = self.engine._start_next_active
        for source_id in active["timers"]:
            GLib.source_remove(source_id)
        active["timers"] = []
        pump(0.3)
        active["accepted_monotonic"] -= 60
        self.engine._start_next_watchdog()
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("failed", "watchdog_timeout"))
        pump(0.5)
        deck = self.engine.decks["A"]
        self.assertEqual(deck.mixer_pad.get_property("volume"), 1.0)
        self.assertEqual(attached_volume_bindings(deck.mixer_pad), (1, deck.volume_control.binding))
        self.assertFalse(self.engine._next_triggered)


class StartNextStatusTests(_StartNextBase):
    """3.8: the published block is bounded, identity-free and JSON."""

    def setUp(self):
        super().setUp()
        self.h.queue(*({"id": i, "seconds": 8} for i in range(1, 14)))
        self.h.play()

    def test_the_status_block_is_bounded_json_without_user_identity(self):
        _start(self.engine, "A")
        pump(0.3)
        self.send(fade_ms=300)
        state = self.engine._start_next_state()
        active = state["active"]
        self.assertEqual(set(active), {
            "request_id", "client_request_id", "phase", "mode", "outgoing", "incoming",
            "fade_ms", "accepted_at", "fade_ends_at",
        })
        json.dumps(state)
        self.settle()
        for _ in range(10):
            self.send(fade_ms=250)
            self.settle()
        state = self.engine._start_next_state()
        self.assertEqual(len(state["recent"]), 8)
        self.assertIsNone(state["active"])
        encoded = json.dumps(state)
        self.assertNotIn("user_id", encoded)
        self.assertEqual(state["version"], 1)


class StartNextAudioTests(_StartNextBase):
    """Audio-level proof on the program output (appsink): the equal-power
    fade on the outgoing pad only, D7 cue-in landing and its fallback, and
    no change to the other deck or any later gain stage."""

    appsink = True
    A_HZ = 400.0
    B_HZ = 1000.0

    def _level_at(self, freq, t, width=0.03):
        return self.h.tone_level(t - width / 2, t + width / 2, freq)

    def _first_time(self, freq, predicate, after):
        for t, level in self.h.tone_series(freq):
            if t >= after and predicate(level):
                return t
        return None

    def test_the_outgoing_fade_follows_the_equal_power_curve(self):
        self.h.queue({"id": 1, "seconds": 8, "freq": self.A_HZ})
        self.h.play()
        _start(self.engine, "A")
        pump(1.0)
        t0 = running_seconds(self.engine)
        self.send(fade_ms=1000)
        record = self.settle()
        self.assertEqual(record["outcome"], "completed_to_silence")
        pump(0.3)
        reference = self.h.tone_level(t0 - 0.5, t0 - 0.05, self.A_HZ)
        self.assertGreater(reference, 0.3)
        import math
        for fraction in (0.25, 0.5, 0.75, 0.9):
            expected = reference * math.cos(math.pi / 2 * fraction)
            with self.subTest(fraction=fraction):
                self.assertAlmostEqual(
                    self._level_at(self.A_HZ, t0 + fraction), expected,
                    delta=0.06 * reference + 0.02,
                )
        self.assertLess(self.h.tone_level(t0 + 1.05, t0 + 1.25, self.A_HZ), 0.01)
        self.assert_untouched_program_gains()

    def test_the_incoming_cue_in_lands_at_the_end_of_the_fade(self):
        self.h.queue(
            {"id": 1, "seconds": 8, "freq": self.A_HZ},
            {"id": 2, "seconds": 4, "freq": self.B_HZ, "lead_silence": 0.5, "cue_in": 0.5},
        )
        self.h.play()
        _start(self.engine, "A")
        pump(1.0)
        t0 = running_seconds(self.engine)
        self.send(fade_ms=1500)
        self.assertEqual(self.settle()["outcome"], "completed")
        pump(0.6)
        reference = self.h.tone_level(t0 - 0.5, t0 - 0.05, self.A_HZ)
        fade_end = self._first_time(self.A_HZ, lambda level: level < 0.02 * reference, t0)
        onset = self._first_time(self.B_HZ, lambda level: level > 0.2, t0)
        self.assertIsNotNone(fade_end)
        self.assertIsNotNone(onset)
        self.assertAlmostEqual(fade_end - t0, 1.5, delta=0.08)
        self.assertAlmostEqual(onset, fade_end, delta=0.08,
                               msg=f"cue-in {onset - t0:.3f}s vs fade end {fade_end - t0:.3f}s")

    def test_a_cue_in_longer_than_the_fade_starts_the_incoming_at_once(self):
        self.h.queue(
            {"id": 1, "seconds": 8, "freq": self.A_HZ},
            {"id": 2, "seconds": 4, "freq": self.B_HZ, "lead_silence": 1.2, "cue_in": 1.2},
        )
        self.h.play()
        _start(self.engine, "A")
        pump(1.0)
        t0 = running_seconds(self.engine)
        self.send(fade_ms=800)
        self.assertEqual(self.settle()["outcome"], "completed")
        pump(1.2)
        onset = self._first_time(self.B_HZ, lambda level: level > 0.2, t0)
        self.assertIsNotNone(onset)
        # Created at fade start: 0.3 s silence prime + 1.2 s cue-in.
        self.assertAlmostEqual(onset - t0, 1.5, delta=0.1)

    def test_in_an_overlap_only_the_outgoing_deck_is_attenuated(self):
        self.h.queue({"id": 1, "seconds": 8, "freq": self.A_HZ},
                     {"id": 2, "seconds": 8, "freq": self.B_HZ})
        self.h.play()
        _start(self.engine, "A")
        pump(0.5)
        _start(self.engine, "B")
        pump(0.8)
        t0 = running_seconds(self.engine)
        self.send(fade_ms=1000)
        self.assertEqual(self.settle()["outcome"], "completed")
        pump(0.3)
        before = self.h.tone_level(t0 - 0.4, t0 - 0.05, self.B_HZ)
        during = self.h.tone_level(t0 + 0.2, t0 + 0.9, self.B_HZ)
        after = self.h.tone_level(t0 + 1.1, t0 + 1.3, self.B_HZ)
        self.assertGreater(before, 0.3)
        self.assertAlmostEqual(during, before, delta=0.03 * before)
        self.assertAlmostEqual(after, before, delta=0.03 * before)
        self.assertLess(self.h.tone_level(t0 + 1.05, t0 + 1.3, self.A_HZ), 0.01)
        self.assert_untouched_program_gains()


class StartNextBindingLifecycleTests(_StartNextBase):
    """r0117 review SN-GST-1: one volume binding per live pad, reused for
    every fade/cancel/restore, never removed/replaced/disabled; restoration is
    owned until complete; stale callbacks cannot touch a newer fade."""

    def setUp(self):
        super().setUp()
        self.h.queue(*({"id": i, "seconds": 30} for i in range(1, 30)))
        self.h.play()
        self.deck = _start(self.engine, "A")
        pump(0.4)

    def fade(self, fade_ms=3000):
        command = self.send(fade_ms=fade_ms)
        self.assertTrue(wait_until(lambda: self.engine._start_next_active
                                   and self.engine._start_next_active.get("fade_anchored"), 2.0))
        pump(0.15)
        return command

    def cancel(self):
        active = self.engine._start_next_active
        self.engine._start_next_cancel(active["request_id"], "test_cancel", restore=True)
        self.assertEqual(self.last()["outcome"], "cancelled")

    def binding(self):
        count, binding = attached_volume_bindings(self.deck.mixer_pad)
        self.assertEqual(count, 1, "exactly one attached binding on the surviving pad")
        self.assertIs(binding, self.deck.volume_control.binding)
        return binding

    def wait_restored(self):
        self.assertTrue(wait_until(lambda: self.engine._start_next_restoring is None, 2.0))
        pump(0.1)
        self.assertAlmostEqual(self.deck.mixer_pad.get_property("volume"), 1.0)

    def test_same_pad_cancel_restore_retry_before_and_after_restore_reuses_one_binding(self):
        self.fade()
        first = self.binding()
        self.assertLess(self.deck.mixer_pad.get_property("volume"), 1.0)
        self.cancel()
        # Immediately, while the restoration owns the pad: refused, nothing added.
        self.send()
        self.assertEqual(self.last()["reason"], "busy_restoring")
        self.assertIs(self.binding(), first)
        self.assertEqual(self.engine._deck_volume_control(self.deck).control_points()[-1][1], 1.0)
        self.wait_restored()
        # After restoration: the same pad fades again on the SAME binding.
        self.fade(fade_ms=400)
        self.assertIs(self.binding(), first)
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2])
        self.assertIsNone(self.deck.volume_control)            # released with its pad
        self.assertIsNone(self.deck.mixer_pad)

    def test_a_stale_restore_callback_cannot_touch_a_newer_fade(self):
        self.fade()
        self.cancel()
        old_token = self.engine._start_next_restoring["token"]
        self.wait_restored()
        self.fade()
        before = self.engine._deck_volume_control(self.deck).control_points()
        self.engine._start_next_restore_done(old_token)        # a late, stale completion
        pump(0.3)
        self.assertEqual(self.engine._deck_volume_control(self.deck).control_points(), before)
        self.assertEqual(before[-1][1], 0.0)                    # still the newer fade's curve
        earlier = self.deck.mixer_pad.get_property("volume")
        pump(0.4)
        self.assertLess(self.deck.mixer_pad.get_property("volume"), earlier)   # still fading down
        self.assertLess(earlier, 1.0)
        self.assertIsNotNone(self.engine._start_next_active)

    def test_cancellation_during_the_restore_is_deterministic(self):
        self.fade()
        self.cancel()
        self.engine._pause_deck("A")                           # the deck leaves during its restore
        self.assertIsNone(self.deck.volume_control)
        self.send(expected={"live_log_item_ids": []})                 # paused only -> start_only
        self.assertNotEqual(self.last()["reason"], "busy_restoring")
        self.assertIsNone(self.engine._start_next_restoring)
        self.assertEqual(self.settle()["outcome"], "completed")

    def test_shutdown_invalidates_a_pending_restore(self):
        self.fade()
        self.cancel()
        source_id = self.engine._start_next_restoring["source_id"]
        self.engine._media_validation_worker = None
        self.engine.stop()
        self.assertIsNone(self.engine._start_next_restoring)
        from gi.repository import GLib
        self.assertIsNone(GLib.MainContext.default().find_source_by_id(source_id))

    def test_a_foreign_volume_binding_is_never_replaced(self):
        gi.require_version("GstController", "1.0")
        from gi.repository import GstController
        foreign = GstController.DirectControlBinding.new_absolute(
            self.deck.mixer_pad, "volume", GstController.InterpolationControlSource())
        self.deck.mixer_pad.add_control_binding(foreign)
        self.send()
        self.assertEqual(self.last()["reason"], "fade_unsupported")
        self.assertEqual(attached_volume_bindings(self.deck.mixer_pad), (1, foreign))
        self.assertEqual(self.h.claims, [1])

    def test_repeated_same_pad_fade_cancel_restore_cycles(self):
        identities = set()
        for cycle in range(12):
            self.fade()
            identities.add(id(self.binding()))
            self.cancel()
            if cycle % 2 == 0:                                 # immediate retry: refused
                self.send()
                self.assertEqual(self.last()["reason"], "busy_restoring")
            self.wait_restored()
            identities.add(id(self.binding()))
        self.assertEqual(len(identities), 1)
        self.assertEqual(self.h.claims, [1])                    # cancels consumed nothing
        self.fade(fade_ms=300)
        self.assertEqual(self.settle()["outcome"], "completed")
        self.assertEqual(self.h.claims, [1, 2])
