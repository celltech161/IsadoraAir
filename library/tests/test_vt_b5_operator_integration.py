"""Roadmap 2.25 B5 -- operator controls, live talent and transport holds on
the integrated VT transition path.

The real engine transition path (production 250 ms ``_poll_position``,
``_start_next_track`` -> claim -> ``_vtt_admission_for_claim``, B2 scheduled
decks and fires, the one duck curve), driven by the REAL operator
authorities: ``_dispatch_engine_command`` (Start Next), ``_set_manual_mode``,
``_set_mic_ptt``, ``_remote_dj_set_gate``, ``_pause_deck`` / ``_resume_deck``
/ ``_seek_deck``, ``_eject_deck`` and ``_reload_and_restart_current_log``,
measured in rendered program PCM. The last class runs a Manual hold against
PostgreSQL (claim kept, played_at / PlayEvent only at the real admission).

Signals: 400 Hz outgoing, 600 Hz incoming music, 1400 Hz incoming vocal,
2200 / 2600 Hz VT head / tail markers, 1000 Hz intro speech, 900 Hz outro
speech, 300 Hz third item / urgent item.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import vt_transition
from library.services.vt_audio import FireState
from library.tests._transport_harness import start_next_command
from library.tests._vt_b0_lab import Gst
from library.tests._vt_integration_lab import (
    HEAD, MUSIC, NS, OUT, OUTRO_SPEECH, SPEECH, TAIL, TOL, URGENT, IntegrationLab, fake_voicetrack,
    pump, report, wait_until,
)
from library.tests.test_vt_b4_intro_integration import RAMP, _IntroBase, music

FADE_LIMIT = 0.07          # ~50 ms speech fade + one 10 ms analysis bin + scheduling
THIRD = URGENT             # 300 Hz: an item after the incoming


def _urgent(lab, item_id=50, seconds=4.0):
    item = lab.item(item_id, [(seconds, URGENT)], name=f"urgent-{item_id}-{seconds}")
    item.category_id = 77
    item.category = SimpleNamespace(code="WxAlert")
    return item


def _insert_urgent(engine, item):
    """The r0137 insertion's queue effect (at the cursor) plus its B3/B4 hooks
    (the DB splice itself is exercised by the r0137 suites)."""
    engine.log_items.insert(engine._queue_cursor, item)
    engine._vtt_mark_urgent(item)
    engine._vtt_queue_changed("urgent_insert")


class _B5Base(_IntroBase):
    def start_next(self, lab, **kwargs):
        command = start_next_command(lab.engine, **kwargs)
        lab.engine._dispatch_engine_command(command)
        return command

    def last_start_next(self, lab):
        recent = lab.engine._start_next_state()["recent"]
        return recent[0] if recent else None

    def settle_start_next(self, lab, timeout=6.0):
        self.assertTrue(wait_until(lambda: lab.engine.__dict__.get("_start_next_active") is None, timeout))
        return self.last_start_next(lab)

    def air_starts(self, lab, item_id):
        return [a for a in lab.air_starts if a["log_item_id"] == item_id]

    def ducking(self, lab, *, auto_manual=True):
        """The real mic / Remote DJ authorities read DuckingConfig (patched
        here, after the lab's own patch) and ramp duck_gain."""
        cfg = SimpleNamespace(ptt_auto_manual_enabled=auto_manual, enabled=False, duck_level_db=-10.0)
        lab.engine.__dict__.setdefault("_duck_ramp_source_id", None)
        mocked = patch.object(eng_module.DuckingConfig, "load", new=lambda: cfg)
        mocked.start()
        self.addCleanup(mocked.stop)
        return cfg

    def speech_end(self, tr, freq, command_at):
        """End of speech that was audible when the command was given."""
        return tr.end(freq, after=command_at - 0.12)

    def underlap(self, **kwargs):
        """Long intro VT: speech at A_seg, music held U = 1.65 s (W 1.5, V 3.0)."""
        return self.build(in_cue=0.25, in_intro=1.75, intro_vt=3.0, **kwargs)


# ---------------------------------------------------------------------------
# D8: Start Next during a VT transition
# ---------------------------------------------------------------------------
class StartNextDuringVtTests(_B5Base, SimpleTestCase):
    def test_held_claimed_occurrence_is_admitted_at_the_fade_end_not_a_second_item(self):
        lab, out, inc, deck, zero = self.underlap(extra=())
        third = lab.item(3, [(5.0, THIRD)], name="b5-third")
        lab.engine.log_items.append(third)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        held = lab.deck_of(2)
        self.assertEqual(held.admission_state, "scheduled", "music still held for the long intro VT")
        command_at = lab.now()
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(1.5)
        tr = lab.trace()
        speech_end = self.speech_end(tr, SPEECH, command_at)
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B5-start-next-held", mode=outcome["mode"], speech_end_after_command=speech_end - command_at,
               music_vs_fade_end=music_on - (command_at + 0.4), claims=lab.claims)
        self.assertEqual((outcome["outcome"], outcome["mode"]), ("completed", "overlap"))
        self.assertLess(speech_end - command_at, FADE_LIMIT, "VT speech cancelled (D8)")
        self.assertLess(abs(music_on - (command_at + 0.4)), 0.03, "held song at the fade end")
        self.assertNotIn(3, lab.claims, "no second queue item consumed (no skip, no double advance)")
        self.assertEqual(set(lab.claims), {1, 2})
        self.assertEqual(len(self.air_starts(lab, 2)), 1, "one air start, at the real admission")
        self.assertIsNone(lab.record())
        self.assertIn("start_next", lab.event_names())
        self.assertLess(tr.level(OUT, command_at + 0.5, command_at + 0.6), 0.02, "outgoing faded")
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)

    def test_after_the_outgoing_ended_the_held_song_is_admitted_at_once(self):
        # Outro speech outlasts the outgoing (D3/D9): only the held deck is live.
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        lab, out, inc, deck, zero = _OutroBase.scenario(self, out_len=9.0, next_start=8.5, outro_at=8.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 9.2, 12.0))
        self.assertEqual([d.log_item.id for d in lab.engine._live_decks()], [2])
        command_at = lab.now()
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(1.2)
        tr = lab.trace()
        speech_end = self.speech_end(tr, OUTRO_SPEECH, command_at)
        music_on = tr.onset(MUSIC, after=zero + 1.0)
        report("B5-start-next-vt-admit", mode=outcome["mode"], speech_end_after_command=speech_end - command_at,
               music_after_command=music_on - command_at)
        self.assertEqual((outcome["outcome"], outcome["mode"]), ("completed", "vt_admit"))
        self.assertLess(speech_end - command_at, FADE_LIMIT)
        self.assertLess(abs(music_on - (command_at + vt_transition.START_NEXT_ADMIT_LEAD_S)), 0.03)
        self.assertEqual(set(lab.claims), {1, 2})

    def test_audible_incoming_keeps_the_established_contract_and_its_intro_speech_stops(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)          # U = 0: music with speech
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        self.assertEqual(lab.deck_of(2).admission_state, "audible")
        command_at = lab.now()
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(0.8)
        tr = lab.trace()
        speech_end = self.speech_end(tr, SPEECH, command_at)
        self.assertEqual(outcome["mode"], "overlap")
        self.assertLess(speech_end - command_at, FADE_LIMIT)
        self.assertGreater(tr.present(MUSIC, command_at + 0.1, command_at + 0.8), 0.6, "incoming keeps playing")
        self.assertEqual(set(lab.claims), {1, 2})

    def test_planned_outro_before_the_claim_is_cancelled_and_the_next_item_starts_once(self):
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        lab, out, inc, deck, zero = _OutroBase.scenario(self, outro_at=5.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.5, 10.0))
        record = lab.record()
        self.assertFalse(record.committed)
        command_at = lab.now()
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(1.0)
        tr = lab.trace()
        self.assertEqual((outcome["outcome"], outcome["mode"]), ("completed", "fade_and_start"))
        self.assertLess(self.speech_end(tr, OUTRO_SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertEqual(lab.claims, [1, 2])
        self.assertTrue(record.done)

    def test_repeated_and_closely_spaced_presses_advance_exactly_once(self):
        lab, out, inc, deck, zero = self.underlap()
        third = lab.item(3, [(5.0, THIRD)], name="b5-third-rep")
        lab.engine.log_items.append(third)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        first = start_next_command(lab.engine, fade_ms=400)
        second = start_next_command(lab.engine, fade_ms=400)
        lab.engine._dispatch_engine_command(first)
        lab.engine._dispatch_engine_command(second)
        lab.engine._dispatch_engine_command(first)          # the same file again
        self.settle_start_next(lab)
        pump(1.0)
        recent = lab.engine._start_next_state()["recent"]
        outcomes = {r["request_id"]: (r["outcome"], r.get("reason")) for r in recent}
        report("B5-start-next-repeat", outcomes=outcomes)
        self.assertEqual(outcomes[second["request_id"]], ("rejected", "busy_start_next"))
        self.assertEqual(outcomes[first["request_id"]][0], "completed")
        self.assertNotIn(3, lab.claims)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_command_at_the_admission_boundary_never_regenerates_an_audible_deck(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg - 0.07, 10.0))
        held = lab.deck_of(2)
        generation = held.generation
        self.start_next(lab, fade_ms=400)
        self.settle_start_next(lab)
        pump(1.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B5-start-next-boundary", music_vs_segue=music_on - a_seg)
        self.assertEqual(lab.deck_of(2).generation, generation, "on air: never regenerated")
        self.assertLess(abs(music_on - a_seg), TOL, "lands at its own admission")
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 0.2), "unadmitted speech cancelled")
        self.assertEqual(set(lab.claims), {1, 2})

    def test_command_after_an_eos_boundary_with_stale_identity_is_refused_then_honoured(self):
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        lab, out, inc, deck, zero = _OutroBase.scenario(self, out_len=9.0, next_start=8.5, outro_at=8.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 8.9, 12.0))
        stale = start_next_command(lab.engine, fade_ms=400)
        self.assertEqual([d["log_item_id"] for d in stale["expected"]["live_decks"]], [1])
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 9.15, 3.0))
        lab.engine._dispatch_engine_command(stale)
        self.assertEqual(self.last_start_next(lab)["reason"], "target_changed")
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        self.assertEqual(outcome["mode"], "vt_admit")
        pump(0.6)
        self.assertEqual(set(lab.claims), {1, 2})


# ---------------------------------------------------------------------------
# Manual and the mic hold: a claimed, held song is never admitted automatically
# ---------------------------------------------------------------------------
class ManualHoldTests(_B5Base, SimpleTestCase):
    def _manual_at(self, offset, **build):
        lab, out, inc, deck, zero = self.underlap(**build)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + offset, 10.0))
        command_at = lab.now()
        lab.engine._set_manual_mode(True)
        return lab, a_seg, command_at

    def test_manual_during_held_underlap_holds_the_claimed_song_until_auto(self):
        lab, a_seg, command_at = self._manual_at(0.4)
        engine = lab.engine
        held = engine.__dict__.get("_vtt_held_occurrence")
        self.assertIsNotNone(held)
        self.assertEqual(held["log_item"].id, 2)
        self.assertIsNone(lab.deck_of(2), "the never-audible generation is withdrawn")
        self.assertTrue(engine._manual_hold_pending)
        pump(2.5)
        tr = lab.trace()
        speech_end = self.speech_end(tr, SPEECH, command_at)
        self.assertLess(speech_end - command_at, FADE_LIMIT, "intro of a held song never continues")
        self.assertIsNone(tr.onset(MUSIC, after=a_seg - 0.5), "no automatic admission in Manual")
        self.assertEqual(self.air_starts(lab, 2), [])
        self.assertEqual(engine._next_queue_item_id(), 2, "the claimed occurrence stays next")
        release_at = lab.now()
        engine._set_manual_mode(False)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=release_at)
        report("B5-manual-hold", speech_end_after_manual=speech_end - command_at,
               music_after_release=music_on - release_at, claims=lab.claims)
        self.assertIsNotNone(music_on)
        self.assertLess(music_on - release_at, 0.8, "the deferred handoff starts it at once")
        self.assertIsNone(tr.onset(HEAD, after=release_at), "deferred handoff: no intro VT (D5)")
        self.assertEqual(set(lab.claims), {1, 2}, "the SAME claimed occurrence, no other item")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertIsNone(engine.__dict__.get("_vtt_held_occurrence"))

    def test_manual_just_before_admission_holds_and_just_after_admission_airs(self):
        for offset, held in ((1.15, True), (1.55, False)):
            with self.subTest(offset=offset):
                lab, a_seg, command_at = self._manual_at(offset)
                pump(1.2)
                tr = lab.trace()
                music_on = tr.onset(MUSIC, after=a_seg - 0.5)
                report("B5-manual-boundary", offset=offset, held=held,
                       music_vs_admission=None if music_on is None else music_on - (a_seg + 1.65))
                if held:
                    self.assertIsNone(music_on)
                    self.assertIsNotNone(lab.engine.__dict__.get("_vtt_held_occurrence"))
                else:
                    self.assertLess(abs(music_on - (a_seg + 1.65)), TOL, "already on air: never interrupted")
                    self.assertIsNone(lab.engine.__dict__.get("_vtt_held_occurrence"))
                lab.lab.stop()
                lab.engine.running = False

    def test_start_next_in_manual_starts_the_held_occurrence_once(self):
        lab, a_seg, command_at = self._manual_at(0.4)
        third = lab.item(3, [(5.0, THIRD)], name="b5-third-manual")
        lab.engine.log_items.append(third)
        pump(1.5)
        self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(1.0)
        self.assertEqual(outcome["outcome"], "completed")
        self.assertNotIn(3, lab.claims)
        self.assertEqual(lab.deck_of(2) is not None, True)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_held_occurrence_that_becomes_unplayable_is_skipped_by_the_safe_policy(self):
        lab, a_seg, command_at = self._manual_at(0.4)
        third = lab.item(3, [(5.0, THIRD)], name="b5-third-unplayable")
        lab.engine.log_items.append(third)
        held = lab.engine._vtt_held_occurrence["log_item"]
        Path(held.track.filepath).rename(Path(held.track.filepath).with_suffix(".gone"))
        events = []
        with patch.object(eng_module, "emit_event", new=lambda **kw: events.append(kw)):
            release_at = lab.now()
            lab.engine._set_manual_mode(False)
            pump(1.2)
        tr = lab.trace()
        self.assertIn("Skipped held log item", [e.get("title") for e in events])
        self.assertIsNotNone(tr.onset(THIRD, after=release_at), "the next item instead")
        self.assertEqual(self.air_starts(lab, 2), [])

    def test_mic_hold_holds_and_mic_release_starts_the_same_occurrence(self):
        lab, out, inc, deck, zero = self.underlap()
        self.ducking(lab, auto_manual=True)
        engine = lab.engine
        engine.mic_ptt_volume = Gst.ElementFactory.make("volume", None)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        command_at = lab.now()
        engine._set_mic_ptt(True)              # the real studio PTT authority
        self.assertTrue(engine.manual_mode)
        self.assertIsNotNone(engine.__dict__.get("_vtt_held_occurrence"))
        pump(1.5)
        tr = lab.trace()
        self.assertLess(self.speech_end(tr, SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertIsNone(tr.onset(MUSIC, after=a_seg - 0.5))
        release_at = lab.now()
        engine._set_mic_ptt(False)
        pump(1.2)
        tr = lab.trace()
        self.assertFalse(engine.manual_mode)
        music_on = tr.onset(MUSIC, after=release_at)
        report("B5-mic-hold", music_after_release=music_on - release_at)
        self.assertLess(music_on - release_at, 0.8)
        self.assertIsNone(tr.onset(HEAD, after=release_at))
        self.assertEqual(set(lab.claims), {1, 2})

    def test_auto_talk_over_keeps_admission_authorized_without_speech(self):
        lab, out, inc, deck, zero = self.underlap()
        self.ducking(lab, auto_manual=False)
        engine = lab.engine
        engine.mic_ptt_volume = Gst.ElementFactory.make("volume", None)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        command_at = lab.now()
        engine._set_mic_ptt(True)
        pump(1.2)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B5-mic-auto", music_after_mic=music_on - command_at)
        self.assertFalse(engine.manual_mode, "the mic never changes the transport mode itself")
        self.assertLess(self.speech_end(tr, SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertLess(music_on - command_at, 0.45, "held music re-admitted at once (Auto)")
        self.assertIsNone(tr.onset(SPEECH, after=command_at + FADE_LIMIT))


# ---------------------------------------------------------------------------
# Remote DJ gate (the real _remote_dj_set_gate authority)
# ---------------------------------------------------------------------------
class RemoteDjTests(_B5Base, SimpleTestCase):
    def remote(self, engine):
        gate = Gst.ElementFactory.make("volume", None)
        gate.set_property("volume", 0.0)
        session = SimpleNamespace(slot_id=0, gate_desired=False, connection_attempt=None, emergency_latch=None)
        engine.dj_slots = [SimpleNamespace(remote_gate=gate, session=session)]
        engine.remote_dj_session = session
        return session, gate

    def test_gate_on_fades_outro_speech_and_reconnect_with_gate_off_keeps_vt_eligible(self):
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        lab, out, inc, deck, zero = _OutroBase.scenario(self, outro_at=5.0)
        self.ducking(lab, auto_manual=False)
        engine = lab.engine
        self.remote(engine)               # connected, mic initially Off
        self.assertFalse(engine._vtt_live_context().remote_dj_live)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.5, 10.0))
        self.assertEqual(lab.record().outro_fire.state, FireState.ADMITTED, "gate Off: VT unaffected")
        command_at = lab.now()
        engine._remote_dj_set_gate(True)
        pump(0.6)
        tr = lab.trace()
        report("B5-remote-gate", speech_end_after_gate=self.speech_end(tr, OUTRO_SPEECH, command_at) - command_at)
        self.assertLess(self.speech_end(tr, OUTRO_SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertFalse(engine.manual_mode)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3, msg="VT duck released; mic duck independent")

    def test_rapid_gate_changes_never_let_recorded_speech_compete(self):
        lab, out, inc, deck, zero = self.underlap()
        self.ducking(lab, auto_manual=True)
        engine = lab.engine
        self.remote(engine)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg - 0.3, 10.0))
        command_at = lab.now()
        for state in (True, False, True, False, True):
            engine._remote_dj_set_gate(state)
        self.assertTrue(engine.manual_mode)
        pump(2.0)
        engine._remote_dj_set_gate(False)       # gate off: the mic hold releases
        pump(1.2)
        tr = lab.trace()
        report("B5-remote-rapid", speech=tr.present(SPEECH, command_at, lab.now()),
               head=tr.present(HEAD, command_at, lab.now()))
        # Each gate-off released the mic hold (existing policy): the claimed
        # song starts exactly once, as the deferred handoff -- never with VT
        # speech, never a second claim.
        self.assertIsNone(tr.onset(HEAD, after=command_at), "no recorded speech competes with the gate")
        self.assertIsNotNone(tr.onset(MUSIC, after=command_at))
        self.assertEqual(set(lab.claims), {1, 2})
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_session_loss_closes_the_gate_and_vt_stays_suppressed_for_that_transition(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        self.ducking(lab, auto_manual=False)
        engine = lab.engine
        session, gate = self.remote(engine)
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 6.0, 10.0))
        engine._remote_dj_set_gate(True)          # live through the claim
        self.wait_claims(lab, [1, 2])
        # Involuntary loss: the recovery path forces the physical gate to 0
        # (preserving gate_desired) without a mic-hold re-evaluation.
        gate.set_property("volume", 0.0)
        engine._apply_talk_ducking()
        self.assertFalse(engine._vtt_live_context().remote_dj_live)
        pump(2.5)
        tr = lab.trace()
        report("B5-remote-loss", music_after_segue=tr.onset(MUSIC, after=a_seg - 1.0) - a_seg)
        self.assertIsNotNone(tr.onset(MUSIC, after=a_seg - 1.0), "the transition proceeds")
        self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0),
                          "a VT ruled out by the live gate at the claim is not resurrected")
        self.assertFalse(engine.manual_mode)


# ---------------------------------------------------------------------------
# Pause / resume / seek (F11): exact timing from the confirmed media position
# ---------------------------------------------------------------------------
class TransportPolicyTests(_B5Base, SimpleTestCase):
    def outro_scenario(self, **kwargs):
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        return _OutroBase.scenario(self, **kwargs)

    def test_forward_seek_before_the_marker_fires_the_outro_exactly_from_the_confirmed_position(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=20.0, next_start=19.0, outro_at=12.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 2.0, 6.0))
        lab.engine._seek_deck("A", 8.0)
        self.assertTrue(wait_until(lambda: lab.engine.decks["A"] is not None
                                   and lab.engine.decks["A"].generation != deck.generation
                                   and lab.engine.decks["A"].gated_seek is None, 5.0))
        new = lab.engine.decks["A"]
        zero_new = lab.engine._vtt_media_zero_ns(new)
        self.assertIsNotNone(zero_new, "a confirmed seek has an exact mapping")
        o_s = zero_new / NS + 12.0
        self.assertTrue(wait_until(lambda: lab.now() > o_s + 2.0, 8.0))
        tr = lab.trace()
        head = tr.onset(HEAD, after=o_s - 1.0)
        report("B5-seek-outro", onset_error=head - o_s)
        self.assertLess(abs(head - o_s), TOL, "outro at the marker, from the authoritative position")

    def test_resume_before_the_marker_fires_the_outro_exactly(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=20.0, next_start=19.0, outro_at=8.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 3.0, 6.0))
        lab.engine._pause_deck("A")
        pump(0.8)
        lab.engine._resume_deck("A")
        self.assertTrue(wait_until(lambda: lab.engine.decks["A"] is not None
                                   and not lab.engine.decks["A"].paused
                                   and lab.engine.decks["A"].gated_seek is None, 5.0))
        new = lab.engine.decks["A"]
        o_s = lab.engine._vtt_media_zero_ns(new) / NS + 8.0
        self.assertTrue(wait_until(lambda: lab.now() > o_s + 2.0, 10.0))
        head = lab.trace().onset(HEAD, after=o_s - 1.5)
        report("B5-resume-outro", onset_error=head - o_s)
        self.assertLess(abs(head - o_s), TOL)

    def test_forward_seek_across_the_marker_never_inserts_the_outro(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=20.0, next_start=19.0, outro_at=8.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 2.0, 6.0))
        seek_at = lab.now()
        lab.engine._seek_deck("A", 10.0)
        pump(3.0)
        self.assertIsNone(lab.trace().onset(HEAD, after=seek_at), "crossing a marker by seeking airs nothing")

    def test_pause_of_the_outgoing_during_an_outro_hold_withdraws_the_held_song_until_it_is_due(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=12.0, next_start=8.0, outro_at=7.0)
        self.wait_claims(lab, [1, 2])
        o_s = zero + 7.0
        self.assertTrue(wait_until(lambda: lab.now() >= o_s + 0.6, 10.0))
        command_at = lab.now()
        lab.engine._pause_deck("A")
        held = lab.engine.__dict__.get("_vtt_held_occurrence")
        self.assertIsNotNone(held)
        pump(2.0)
        tr = lab.trace()
        self.assertLess(self.speech_end(tr, OUTRO_SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertIsNone(tr.onset(MUSIC, after=zero + 1.0), "no admission during the transport hold")
        resume_at = lab.now()
        lab.engine._resume_deck("A")
        self.assertTrue(wait_until(lambda: lab.trace().onset(MUSIC, after=resume_at) is not None, 10.0))
        tr = lab.trace()
        self.assertIsNone(tr.onset(HEAD, after=resume_at), "no outro replay (D13), no intro (claimed)")
        self.assertEqual(set(lab.claims), {1, 2})
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_backward_seek_never_replays_an_aired_outro(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=16.0, next_start=15.0, outro_at=5.0)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 7.0, 10.0))
        seek_at = lab.now()
        lab.engine._seek_deck("A", 2.0)
        pump(5.0)
        self.assertIsNone(lab.trace().onset(HEAD, after=seek_at))
        self.assertEqual(lab.engine._vtt_outro_delivery_state(1), "aired")

    def test_unconfirmed_mapping_suppresses_vt_timing_authority(self):
        lab, out, inc, deck, zero = self.outro_scenario(out_len=20.0, next_start=19.0, outro_at=8.0, poll=False)
        deck.silence_primed = False                   # no exact mapping for this generation
        lab.engine._vtt_poll(deck)
        self.assertIsNone(lab.record(), "no plan from an estimate")


# ---------------------------------------------------------------------------
# Urgent delivery: recorded speech never extends the boundary ahead of it
# ---------------------------------------------------------------------------
class UrgentUnderlapTests(_B5Base, SimpleTestCase):
    def test_urgent_during_held_underlap_releases_the_song_at_once(self):
        lab, out, inc, deck, zero = self.underlap()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        urgent = _urgent(lab)                  # media built before the command instant
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        command_at = lab.now()
        _insert_urgent(lab.engine, urgent)
        pump(1.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B5-urgent-underlap", speech_end_after=self.speech_end(tr, SPEECH, command_at) - command_at,
               music_after=music_on - command_at, saved=(a_seg + 1.65) - music_on)
        self.assertLess(self.speech_end(tr, SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertLess(music_on - command_at, 0.5, "no unnecessary underlap (fade, deck lead, Cue In)")
        self.assertEqual(set(lab.claims), {1, 2}, "the claimed song airs first (r0137 order)")
        self.assertEqual(lab.engine._next_queue_item_id(), 50, "the urgent item keeps its queue position")

    def test_urgent_during_preparation_suppresses_the_intro_and_keeps_the_segue(self):
        lab, out, inc, deck, zero = self.underlap()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertLess(lab.now(), a_seg - 0.2)
        _insert_urgent(lab.engine, _urgent(lab, 51))
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.0, 8.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B5-urgent-preparation", music_vs_segue=music_on - a_seg)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "no intro VT ahead of an urgent item")
        self.assertLess(abs(music_on - a_seg), TOL, "music at the ordinary segue, never earlier")

    def test_urgent_during_an_outro_hold_past_the_segue_fades_it_and_releases_the_song(self):
        from library.tests.test_vt_b3_outro_integration import _OutroBase
        lab, out, inc, deck, zero = _OutroBase.scenario(self, outro_at=7.0, incoming_cue=0.25)
        self.wait_claims(lab, [1, 2])
        seg = zero + 8.0
        urgent = _urgent(lab, 52)
        self.assertTrue(wait_until(lambda: lab.now() >= seg + 0.1, 10.0))
        command_at = lab.now()
        _insert_urgent(lab.engine, urgent)
        pump(1.0)
        tr = lab.trace()
        self.assertLess(self.speech_end(tr, OUTRO_SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertLess(tr.onset(MUSIC, after=zero + 1.0) - command_at, 0.5)

    def test_urgent_with_the_claimed_song_audible_changes_nothing(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        _insert_urgent(lab.engine, _urgent(lab, 53))
        pump(1.3)
        tr = lab.trace()
        tail_on = tr.onset(TAIL, after=a_seg + 0.5)
        tail = tr.end(TAIL, after=tail_on + 0.05)
        report("B5-urgent-audible", tail_end_error=tail - (a_seg + 1.2))
        self.assertLess(abs(tail - (a_seg + 1.2)), TOL, "intro speech over audible music completes")


# ---------------------------------------------------------------------------
# Consecutive short items and the unplanned-start latency
# ---------------------------------------------------------------------------
class ConsecutiveTransitionTests(_B5Base, SimpleTestCase):
    def _short_chain(self, id_next):
        """out (A_seg1 = 5.5) -> short ID with a 1.0 s intro VT -> song with a
        1.4 s intro VT at A_seg2 = A_seg1 + id_next - 0.1 (Cue In 0.1)."""
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000), gap_ms=200)
        out = lab.item(1, [(6.0, OUT)], next_start=5.5, name="b5-short-out")
        short = lab.item(2, music(0.1, 1.6, after=1.4), cue_in=0.1, intro_until=1.6, next_start=id_next,
                         name=f"b5-short-id-{id_next}")
        song = lab.item(3, music(0.1, 2.5, after=2.0, freq=THIRD), cue_in=0.1, intro_until=2.5,
                        name="b5-short-song")
        lab.attach(short, "intro", fake_voicetrack(lab.vt("b5-short-vt-a", 1.0), 1.0))
        lab.attach(song, "intro", fake_voicetrack(lab.vt("b5-short-vt-b", 1.4, speech=OUTRO_SPEECH), 1.4))
        lab.queue(out, short, song)
        lab.poll()
        lab.play()
        deck = lab.start("A")
        zero = lab.media_zero(deck)
        self.assertTrue(wait_until(lambda: lab.claims[:3] == [1, 2, 3], 14.0), lab.claims)
        pump(3.0)
        a_seg1 = zero + 5.5
        return lab, a_seg1, a_seg1 + id_next - 0.1

    def test_short_item_draining_speech_never_blocks_or_overlaps_the_next_intro_vt(self):
        lab, a_seg1, a_seg2 = self._short_chain(1.35)            # 0.25 s after the first VT ends
        tr = lab.trace()
        head1 = tr.onset(HEAD, after=a_seg1 - 0.5)
        tail1_on = tr.onset(TAIL, after=head1 + 0.5)
        end1 = tr.end(TAIL, after=tail1_on + 0.05)
        head2 = tr.onset(HEAD, after=end1 + 0.02)
        song_on = tr.onset(THIRD, after=a_seg2 - 0.5)
        report("B5-consecutive", first_onset_error=head1 - a_seg1, first_end=end1 - a_seg1,
               second_onset_error=head2 - a_seg2, gap=head2 - end1, song_vs_segue=song_on - a_seg2)
        self.assertLess(abs(head1 - a_seg1), TOL)
        self.assertLess(abs(head2 - a_seg2), TOL, "the next occurrence's intro VT is not blocked")
        self.assertGreaterEqual(head2 - end1, 0.2 - TOL, "no overlapping recorded voices (gap kept)")
        self.assertLess(abs(song_on - a_seg2), TOL)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)

    def test_next_intro_vt_that_would_overlap_draining_speech_is_suppressed_not_delayed(self):
        lab, a_seg1, a_seg2 = self._short_chain(1.2)             # only 0.1 s after the first VT ends
        tr = lab.trace()
        song_on = tr.onset(THIRD, after=a_seg2 - 0.5)
        report("B5-consecutive-tight", song_vs_segue=song_on - a_seg2,
               second_speech=tr.present(OUTRO_SPEECH, a_seg2 - 0.5, lab.now()))
        self.assertIsNone(tr.onset(OUTRO_SPEECH, after=a_seg2 - 0.5), "suppressed, never overlapping")
        self.assertLess(abs(song_on - a_seg2), 0.03, "ordinary progression is not delayed by speech")

    def test_unplanned_start_latency_is_measured_separately_from_scheduling_accuracy(self):
        """A start the look-ahead did not plan: the decision is made at the
        claim; speech and music then land EXACTLY where that decision put
        them (scheduling accuracy), max(A_seg, claim + UNPLANNED_LEAD_S).
        The delay after A_seg is preparation latency: how late the ordinary
        trigger claimed (poll phase, the r0144 short-item rule) plus the lead."""
        lead = vt_transition.UNPLANNED_LEAD_S
        for label, out_len, out_next, cue in (("short_trigger", 9.0, 8.0, 0.25), ("long_trigger", 20.0, 19.0, 0.5)):
            with self.subTest(label):
                lab, out, inc, deck, zero = self.build(out_len=out_len, out_next=out_next, in_cue=cue,
                                                       in_intro=cue + 4.0, intro_vt=1.2, poll=False)
                engine = lab.engine
                # The look-ahead read the recording (2.25 B6 cache) but could not
                # plan the transition: the claim decides it (unplanned start).
                engine._vtt_config()
                engine._vtt_cached_voicetrack(inc, "intro")
                self.assertTrue(wait_until(lambda: engine._vtt_cached_voicetrack(inc, "intro") is not None
                                           and engine._vtt_config() is not None, 2.0))
                engine._vtt_plan = lambda *_a, **_k: None
                lab.poll()
                a_seg = zero + out_next
                self.wait_claims(lab, [1, 2], timeout=out_len + 6.0)
                self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.0, 6.0))
                tr = lab.trace()
                head = tr.onset(HEAD, after=a_seg - 1.0)
                music_on = tr.onset(MUSIC, after=a_seg - 1.0)
                record = lab.last_record()
                claim_t = next(t for t, name, _d in record.events if name == "unplanned")
                decided = record.plan.intro_start      # the claim's decision (lead, deck lead, Cue In)
                report("B5-unplanned", case=label, claim_vs_segue=claim_t - a_seg, vt_after_segue=head - a_seg,
                       scheduling_error=head - decided, music_vs_vt=music_on - head,
                       recorded_delay=record.unplanned_delay_s)
                self.assertLess(abs(head - decided), TOL, "speech exactly where the claim decided")
                self.assertLess(abs(music_on - head), TOL, "speech and music still land together (D1)")
                bound = max(lead, cue + vt_transition.DECK_MIN_LEAD_S) + eng_module.POSITION_POLL_MS / 1000.0
                self.assertLess(head - a_seg, bound + 0.05, "bounded: lead or Cue In + deck lead, + one poll")
                lab.lab.stop()
                lab.engine.running = False


# ---------------------------------------------------------------------------
# Reload / Eject races: no obsolete callback reopens anything
# ---------------------------------------------------------------------------
class CommandRaceTests(_B5Base, SimpleTestCase):
    def test_reload_during_a_held_incoming_never_reopens_it(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        replacement = lab.item(9, [(5.0, THIRD)], name="b5-reload")
        engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                            setattr(engine, "_queue_cursor", 0))
        command_at = lab.now()
        engine._reload_and_restart_current_log()
        pump(3.0)
        tr = lab.trace()
        self.assertLess(self.speech_end(tr, SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertIsNone(tr.onset(MUSIC, after=a_seg - 0.5), "the replaced program's held song never airs")
        self.assertEqual(self.air_starts(lab, 2), [])
        self.assertIsNotNone(tr.onset(THIRD, after=command_at))
        self.assertIsNone(lab.record())

    def test_manual_then_start_next_then_auto_within_one_transition(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        third = lab.item(3, [(5.0, THIRD)], name="b5-multi")
        engine.log_items.append(third)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        engine._set_manual_mode(True)
        self.start_next(lab, fade_ms=400)
        self.settle_start_next(lab)
        engine._set_manual_mode(False)
        pump(2.0)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims, "Auto release after Start Next starts nothing extra")


# ---------------------------------------------------------------------------
# PostgreSQL: a Manual hold keeps the claim; played_at only at real admission
# ---------------------------------------------------------------------------
from library.tests.test_vt_b4_intro_integration import IntegratedAccountingDbTests  # noqa: E402


class ManualHoldAccountingDbTests(IntegratedAccountingDbTests):
    test_swap_adopted_at_claim_and_played_at_only_at_the_admission = None

    def test_manual_hold_keeps_the_claim_and_records_play_only_after_auto(self):
        from library.models import LogItem, PlayEvent, VoiceTrackConfig
        VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 100,
                                                                 "min_gap_ms": 200})
        lab = self.real_lab()
        out_track = self.track(lab, "b5db-out", [(9.0, OUT)], next_start_seconds=8.0)
        song = self.track(lab, "b5db-song", music(0.25, 1.75), cue_in_seconds=0.25, intro_until_seconds=1.75)
        self.voicetrack(lab, song, "b5db-vt", 3.0, SPEECH)
        out_item, in_item = self.item(out_track), self.item(song)
        engine = lab.engine
        engine.log_items = [out_item, in_item]
        engine._queue_cursor = 0
        lab.play()
        engine._start_next_track(slot="A")
        zero = engine._vtt_media_zero_ns(engine.decks["A"]) / NS
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 12.0))
        claimed = LogItem.objects.get(pk=in_item.pk)
        self.assertIsNotNone(claimed.playback_claimed_at)
        engine._set_manual_mode(True)
        pump(2.0)
        row = LogItem.objects.get(pk=in_item.pk)
        self.assertIsNone(row.played_at, "held in Manual: never played")
        self.assertFalse(PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).exists())
        wall0, run0 = time.time(), lab.now()
        engine._set_manual_mode(False)
        self.assertTrue(wait_until(lambda: PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).exists(), 6.0))
        pump(1.5)
        row = LogItem.objects.get(pk=in_item.pk)
        music_on = lab.trace().onset(MUSIC, after=run0)
        report("B5-db-manual-hold", play_events=PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).count(),
               played_at_vs_music=row.played_at.timestamp() - (wall0 + (music_on - 0.25 - run0)))
        self.assertEqual(row.playback_claimed_at, claimed.playback_claimed_at, "the claim is never recreated")
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).count(), 1)
        self.assertGreater(row.played_at.timestamp(), wall0 - 0.01, "played only after the Auto release")
        self.assertIsNone(lab.trace().onset(HEAD, after=run0), "no intro VT on the deferred handoff (D5)")
        lab.stop()
