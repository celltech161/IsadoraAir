"""Roadmap 2.25 B3 -- independent outgoing outro VT playback, integrated.

The real engine transition path (production 250 ms ``_poll_position`` tick,
``_start_next_track`` -> ``_create_deck``, the B3 transition coordinator, the
B2 scheduled fires / scheduled decks / duck curve) measured in rendered
program PCM (sample-accurate onset and end of tone-identified signals).

Signals: 400 Hz outgoing music, 600 Hz incoming music, 2200 Hz VT head
marker, 900 Hz outro speech, 2600 Hz VT tail marker, 300 Hz urgent item.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import vt_transition
from library.services.vt_audio import FireState
from library.services.vt_transition import Reason, StartKind
from library.tests._vt_integration_lab import (
    CONFIGS, HEAD, MUSIC, NS, OUT, OUTRO_SPEECH, TAIL, TOL, URGENT, IntegrationLab, fake_voicetrack,
    pump, report, unplayable_voicetrack, wait_until,
)

V = 1.6            # outro VT length (head 0.15, speech 1.3, tail 0.15)
RAMP = 0.100       # duck_ramp_ms 100


class _OutroBase:
    def scenario(self, *, rate=44100, fmt="wav", outro_at=5.0, next_start=8.0, out_len=9.0, vt_len=V,
                 incoming_cue=0.0, incoming_role=None, voicetrack="ok", gap_ms=200, out_item=1, poll=True):
        lab = IntegrationLab(self, rate=rate, ramp_ms=int(RAMP * 1000), gap_ms=gap_ms)
        out = lab.item(out_item, [(out_len, OUT)], next_start=next_start, outro_starts=outro_at,
                       name=f"out-{out_len}-{rate}")
        segs = ([(incoming_cue, 0)] if incoming_cue else []) + [(6.0, URGENT if incoming_role else MUSIC)]
        inc = lab.item(2, segs, cue_in=incoming_cue, name=f"in-{incoming_cue}-{incoming_role}-{rate}")
        if incoming_role == "urgent":
            inc.category_id = 77
            inc.category = SimpleNamespace(code="WxAlert")
        lab.queue(out, inc)
        if voicetrack == "ok":
            path = lab.vt(f"outro-vt-{vt_len}-{fmt}-{rate}", vt_len, fmt=fmt, speech=OUTRO_SPEECH)
            lab.attach(out, "outro", fake_voicetrack(path, vt_len))
        elif voicetrack == "unplayable":
            lab.attach(out, "outro", unplayable_voicetrack())
        elif voicetrack == "corrupt":
            corrupt = lab.root / "corrupt-outro.flac"
            corrupt.write_bytes(b"\x00\xffnot audio\x13\x37" * 4096)
            lab.attach(out, "outro", fake_voicetrack(corrupt, vt_len))
        if poll:
            lab.poll()
        lab.play()
        deck = lab.start("A")
        return lab, out, inc, deck, lab.media_zero(deck)

    def wait_claims(self, lab, claims, timeout=16.0):
        self.assertTrue(wait_until(lambda: lab.claims == claims, timeout), f"claims {lab.claims}")

    def duck_edges(self, trace, after, before):
        """(attack start, release end) of the 400 Hz music bed in 10 ms bins."""
        series = [(t, a) for t, a in trace.coarse(OUT) if after <= t < before]
        attack = next((t for t, a in series if a < 0.37), None)
        release = None
        if attack is not None:
            low = [t for t, a in series if t > attack and a < 0.22]
            if low:
                release = next((t for t, a in series if t > low[-1] and a > 0.37), None)
        return attack, release


class OutroTimingTests(_OutroBase, SimpleTestCase):
    """Outro VT only, ending before the segue: exact onset at Outro Starts,
    the whole recording, a speech-owned duck, and the r0144 ordinary segue."""

    def _run(self, rate, fmt):
        lab, out, inc, deck, zero = self.scenario(rate=rate, fmt=fmt)
        self.wait_claims(lab, [1, 2])
        pump(1.5)
        tr = lab.trace()
        o_s, o_end, seg = zero + 5.0, zero + 5.0 + V, zero + 8.0
        head_on = tr.onset(HEAD, after=o_s - 0.5)
        tail_end = tr.end(TAIL, after=o_end - 0.1)
        music_on = tr.onset(MUSIC, after=seg - 0.5)
        attack, release = self.duck_edges(tr, o_s - 1.0, seg)
        record = lab.last_record()
        fire = record.outro_fire
        report("B3-outro", rate=rate, fmt=fmt, onset_error=head_on - o_s, end_error=tail_end - o_end,
               vt_rendered=tail_end - head_on, head=tr.present(HEAD, o_s - 0.1, o_s + 0.3),
               tail=tr.present(TAIL, o_end - 0.3, o_end + 0.1),
               duck_attack_vs_speech=attack - o_s, duck_release_end_vs_speech_end=release - o_end,
               ducked_level=tr.level(OUT, o_s + 0.05, o_end - 0.05),
               segue_vs_next_start=music_on - seg)
        self.assertEqual(fire.state, FireState.ENDED, fire.history)
        self.assertLess(abs(head_on - o_s), TOL, "outro VT onset at Outro Starts")
        self.assertLess(abs(tail_end - o_end), TOL, "whole recording rendered (F14)")
        self.assertGreaterEqual(tr.present(HEAD, o_s - 0.1, o_s + 0.3), 0.13)
        self.assertGreaterEqual(tr.present(TAIL, o_end - 0.3, o_end + 0.1), 0.13)
        # D12 / F5: the duck is owned by speech -- attack one ramp before it,
        # release at its end; full level before and after.
        self.assertLess(abs(attack - (o_s - RAMP)), 0.025)
        self.assertLess(abs(release - (o_end + RAMP)), 0.035)
        self.assertGreater(tr.level(OUT, zero + 1.0, o_s - RAMP - 0.05), 0.37)
        self.assertLess(abs(tr.level(OUT, o_s + 0.05, o_end - 0.05) - 0.2), 0.03)
        self.assertGreater(tr.level(OUT, o_end + RAMP + 0.05, seg - 0.05), 0.37)
        # No hold, no ownership: the r0144 ordinary trigger (primed deck,
        # late by its prime + poll jitter, R0/E01 baseline) starts the incoming.
        self.assertIsNone(lab.deck_of(2).scheduled_admission_ns)
        self.assertTrue(0.25 < music_on - seg < 1.2, music_on - seg)
        self.assertEqual(lab.engine._vtt_outro_delivery_state(1), "aired")
        self.assertIn(StartKind.AUTOMATIC, [k for _t, k in lab.start_calls])
        self.assertEqual(lab.engine._vtt_observed_phase(), "idle")
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)

    def test_outro_44100_wav(self):
        self._run(*CONFIGS[0])

    def test_outro_44100_flac(self):
        self._run(*CONFIGS[1])

    def test_outro_48000_wav(self):
        self._run(*CONFIGS[2])

    def test_outro_48000_flac(self):
        self._run(*CONFIGS[3])


class OutroHoldTests(_OutroBase, SimpleTestCase):
    """D3: outro speech past the segue holds the incoming until it ends."""

    def test_hold_admits_the_incoming_at_the_end_of_outro_speech(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=7.0, incoming_cue=0.25)
        self.wait_claims(lab, [1, 2])
        pump(2.0)
        tr = lab.trace()
        o_s, o_end, seg = zero + 7.0, zero + 7.0 + V, zero + 8.0
        music_on = tr.onset(MUSIC, after=seg - 1.0)
        head_on = tr.onset(HEAD, after=o_s - 0.5)
        tail_end = tr.end(TAIL, after=o_end - 0.1)
        start = next(a for a in lab.air_starts if a["log_item_id"] == 2)
        report("B3-hold", onset_error=head_on - o_s, end_error=tail_end - o_end, music_vs_outro_end=music_on - o_end,
               air_start_vs_admission=start["running"] - (start["scheduled"] / NS))
        self.assertLess(abs(head_on - o_s), TOL)
        self.assertLess(abs(tail_end - o_end), TOL)
        self.assertLess(abs(music_on - o_end), TOL, "music admitted exactly at O_end (Cue In 0.25 accounted)")
        self.assertGreater(tr.onset(MUSIC, after=zero + 1.0), o_end - 0.005,
                           "no incoming music before speech ends (first sustained onset)")
        self.assertEqual(lab.claims, [1, 2], "one claim, no extra occurrence")
        self.assertIsNotNone(start["scheduled"], "incoming opened in scheduled admission")
        self.assertLess(abs(start["running"] - start["scheduled"] / NS), 0.02,
                        "air start dispatched at the scheduled admission (media 0, like r0144)")
        self.assertIn("committed", lab.event_names())

    def test_outro_outlasting_the_outgoing_plays_dry_then_admits_the_incoming_at_its_end(self):
        lab, out, inc, deck, zero = self.scenario(out_len=9.0, next_start=8.5, outro_at=8.0)
        self.wait_claims(lab, [1, 2])
        pump(2.0)
        tr = lab.trace()
        o_s, o_end, eos = zero + 8.0, zero + 8.0 + V, zero + 9.0
        tail_end = tr.end(TAIL, after=o_end - 0.1)
        music_on = tr.onset(MUSIC, after=zero + 1.0)
        report("B3-dry-outro", tail_end_error=tail_end - o_end, music_vs_outro_end=music_on - o_end,
               dry_speech_seconds=o_end - eos)
        self.assertLess(abs(tail_end - o_end), TOL, "whole outro, continuing after the outgoing ended")
        self.assertLess(abs(music_on - o_end), TOL, "music at O_end (contract section 4, case F)")
        self.assertGreater(tr.present(OUTRO_SPEECH, eos + 0.02, o_end - 0.17), 0.3, "dry speech, not dead air")
        self.assertEqual(lab.claims, [1, 2])

    def test_urgent_next_suppresses_an_outro_that_would_hold(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=7.0, incoming_role="urgent")
        self.wait_claims(lab, [1, 2])
        pump(1.0)
        tr = lab.trace()
        seg = zero + 8.0
        urgent_on = tr.onset(URGENT, after=seg - 1.0)
        report("B3-urgent-next", urgent_vs_next_start=urgent_on - seg)
        self.assertLess(tr.present(OUTRO_SPEECH, zero, zero + 12), 0.011, "no outro speech")
        self.assertEqual(lab.engine._vtt_outro_delivery_state(1), None, "never prepared")
        self.assertEqual(lab.engine.__dict__.get("_vtt_seq", 0), 0, "no transition record churn")
        self.assertIsNone(lab.deck_of(2).scheduled_admission_ns, "urgent starts by the ordinary path")
        self.assertTrue(0.25 < urgent_on - seg < 1.2)

    def test_urgent_inserted_while_outro_is_audible_limits_it_at_the_segue(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=7.0)
        urgent = lab.item(50, [(4.0, URGENT)], name="urgent")
        urgent.category_id = 77
        urgent.category = SimpleNamespace(code="WxAlert")
        o_s, seg = zero + 7.0, zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= o_s + 0.3, 12.0))
        engine = lab.engine
        engine.log_items.insert(engine._queue_cursor, urgent)     # _apply_log_item_insert at the cursor
        engine._vtt_mark_urgent(urgent)
        engine._vtt_queue_changed("urgent_insert")
        self.wait_claims(lab, [1, 50])
        pump(1.5)
        tr = lab.trace()
        speech_end = tr.end(OUTRO_SPEECH, after=o_s + 0.4)
        urgent_on = tr.onset(URGENT, after=seg - 0.5)
        report("B3-urgent-insert", speech_end_vs_segue=speech_end - seg, urgent_vs_segue=urgent_on - seg)
        self.assertLess(abs(speech_end - seg), 0.07, "audible outro limited at the ordinary segue")
        self.assertLess(urgent_on - seg, 1.2, "urgent never held for the outro")
        self.assertEqual(lab.last_record().outro_fire.state, FireState.CANCELLED)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)


class OutroEligibilityTests(_OutroBase, SimpleTestCase):
    def test_d7_marker_at_or_after_the_segue_is_suppressed_without_delay(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=8.2)
        def side():
            return lab.engine._vtt_outro_side(deck, eng_module._vtt_track_markers(out.track), None,
                                              lab.engine._vtt_live_context(), lab.now_ns())
        # 2.25 B6: the lookup worker answers asynchronously (pending first).
        self.assertTrue(wait_until(lambda: side()[3] != vt_transition.LOOKUP_PENDING, 2.0))
        self.assertEqual(side()[3], Reason.OUTRO_MARKER_AT_OR_AFTER_SEGUE)
        self.wait_claims(lab, [1, 2])
        pump(1.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=zero + 7.0)
        self.assertLess(tr.present(OUTRO_SPEECH, zero, zero + 12), 0.011)
        self.assertIsNone(lab.last_record(), "no transition created")
        self.assertTrue(0.25 < music_on - (zero + 8.0) < 1.2, "ordinary segue unchanged (R6 inverted)")

    def test_outro_first_seen_after_its_marker_is_suppressed_not_fired_late(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=3.0, poll=False)
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 3.6, 8.0))
        lab.poll()
        self.wait_claims(lab, [1, 2])
        pump(0.5)
        tr = lab.trace()
        self.assertLess(tr.present(OUTRO_SPEECH, zero, lab.now()), 0.011)
        self.assertIsNone(lab.last_record(), "outro_preparation_missed: never fired late and out of place")
        self.assertTrue(0.25 < tr.onset(MUSIC, after=zero + 7.0) - (zero + 8.0) < 1.2)

    def _never_owns(self, **kwargs):
        lab, out, inc, deck, zero = self.scenario(**kwargs)
        self.wait_claims(lab, [1, 2])
        pump(0.5)
        tr = lab.trace()
        self.assertIsNone(lab.last_record())
        self.assertIsNone(lab.engine.__dict__.get("_vt2_duck"), "duck never engaged (F6)")
        self.assertGreater(tr.level(OUT, zero + 1.0, zero + 7.9), 0.37, "no speechless duck")
        self.assertTrue(0.25 < tr.onset(MUSIC, after=zero + 7.0) - (zero + 8.0) < 1.2)

    def test_missing_marker_never_takes_ownership(self):
        self._never_owns(outro_at=None)

    def test_attached_but_unplayable_media_never_takes_ownership(self):
        self._never_owns(voicetrack="unplayable")

    def test_corrupt_outro_media_fails_bounded_and_releases_everything(self):
        lab, out, inc, deck, zero = self.scenario(outro_at=7.0, voicetrack="corrupt")
        self.wait_claims(lab, [1, 2])
        pump(1.0)
        tr = lab.trace()
        record = lab.last_record()
        seg = zero + 8.0
        music_on = tr.onset(MUSIC, after=seg - 1.0)
        report("B3-corrupt-outro", fire_reason=record.outro_fire.reason, music_vs_next_start=music_on - seg)
        self.assertEqual(record.outro_fire.state, FireState.FAILED)
        self.assertTrue(record.done)
        self.assertTrue(0.25 < music_on - seg < 1.2, "hold released: ordinary segue, no dead air (R4a inverted)")
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)
        self.assertGreater(tr.level(OUT, zero + 7.2, zero + 7.9), 0.37, "program back at unity")
        self.assertEqual(lab.engine._vtt_observed_phase(), "idle")


class OutroOperatorTests(_OutroBase, SimpleTestCase):
    def test_d9_eject_of_the_outgoing_during_its_outro_completes_the_outro_then_the_incoming(self):
        lab, out, inc, deck, zero = self.scenario()
        o_s, o_end = zero + 5.0, zero + 5.0 + V
        self.assertTrue(wait_until(lambda: lab.now() >= o_s + 0.4, 10.0))
        lab.engine._eject_deck("A")
        self.wait_claims(lab, [1, 2], 6.0)
        pump(1.5)
        tr = lab.trace()
        tail_end = tr.end(TAIL, after=o_end - 0.1)
        music_on = tr.onset(MUSIC, after=o_s)
        kinds = [k for _t, k in lab.start_calls]
        report("B3-eject-outgoing", tail_end_error=tail_end - o_end, music_vs_outro_end=music_on - o_end)
        self.assertLess(abs(tail_end - o_end), TOL, "the outro VT completes (D9)")
        self.assertLess(abs(music_on - o_end), TOL, "then the incoming")
        self.assertIn(StartKind.EJECT_REPLACEMENT, kinds)
        self.assertEqual(lab.claims, [1, 2])

    def test_mic_live_fades_audible_outro_speech_and_releases_the_vt_duck(self):
        lab, out, inc, deck, zero = self.scenario()
        o_s = zero + 5.0
        self.assertTrue(wait_until(lambda: lab.now() >= o_s + 0.5, 10.0))
        command = lab.now()
        lab.mic(True)
        pump(1.0)
        tr = lab.trace()
        speech_end = tr.end(OUTRO_SPEECH, after=o_s + 0.3)
        report("B3-mic-fade", speech_end_after_command=speech_end - command)
        self.assertLess(speech_end - command, 0.07, "audible speech faded (D6)")
        self.assertEqual(lab.last_record().outro_fire.state, FireState.CANCELLED)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)
        self.assertTrue(lab.engine.manual_mode, "mic hold: Manual (existing r0144 policy)")
        self.assertGreater(tr.level(OUT, command + RAMP + 0.1, command + 0.9), 0.37)

    def test_seek_of_the_outgoing_cancels_its_outro_without_replay(self):
        lab, out, inc, deck, zero = self.scenario(out_len=14.0, next_start=13.0, outro_at=5.0)
        o_s = zero + 5.0
        self.assertTrue(wait_until(lambda: lab.now() >= o_s + 0.5, 10.0))
        command = lab.now()
        lab.engine._seek_deck("A", 2.0)      # back before Outro Starts
        self.assertTrue(wait_until(lambda: lab.engine.decks["A"] is not None
                                   and lab.engine.decks["A"].generation != deck.generation, 5.0))
        pump(5.5)                             # re-crosses the marker
        tr = lab.trace()
        speech_end = tr.end(OUTRO_SPEECH, after=o_s + 0.3)
        self.assertLess(speech_end - command, 0.07)
        self.assertLess(tr.present(HEAD, command + 0.2, lab.now()), 0.011, "no replay on re-crossing (F11)")
        self.assertEqual(lab.engine._vtt_outro_delivery_state(1), "aired")


class OutroResumeTests(_OutroBase, SimpleTestCase):
    """D13: a resumed occurrence never replays an aired or uncertain outro;
    a new occurrence of the same Track is independently eligible."""

    def _resume(self, position, age, *, outro_at=9.0):
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000))
        out = lab.item(1, [(14.0, OUT)], next_start=13.0, outro_starts=outro_at, name="resume-out")
        inc = lab.item(2, [(6.0, MUSIC)], name="resume-in")
        lab.queue(out, inc)
        path = lab.vt("resume-outro-vt", V, speech=OUTRO_SPEECH)
        lab.attach(out, "outro", fake_voicetrack(path, V))
        engine = lab.engine
        engine._resume_item = out
        engine._resume_hint = {"track_id": out.track.id, "log_item_id": out.id, "position": position,
                               "saved_position": position, "state_age_seconds": age}
        lab.poll()
        lab.play()
        with engine._vtt_starting(None):
            engine._start_next_track(slot="A")
        self.assertTrue(wait_until(lambda: engine.decks["A"] is not None
                                   and engine.decks["A"].gated_seek is None
                                   and engine.decks["A"].continuation_reason == "auto_resume", 8.0))
        return lab, engine.decks["A"]

    def test_resume_well_before_the_marker_with_fresh_evidence_fires_once(self):
        lab, deck = self._resume(3.0, 1.0)
        self.assertTrue(wait_until(lambda: lab.engine._vtt_outro_delivery_state(1) == "aired", 9.0))
        pump(2.0)
        tr = lab.trace()
        self.assertGreater(tr.present(HEAD, 0, lab.now()), 0.13)
        record = lab.last_record()
        report("B3-resume-eligible", position=3.0, age=1.0,
               onset_estimate_error=(tr.onset(HEAD) - record.outro_fire.admission_ns / NS))

    def _uncertain(self, position, age):
        lab, deck = self._resume(position, age)
        pump(max(1.0, 9.6 - position))
        tr = lab.trace()
        self.assertLess(tr.present(HEAD, 0, lab.now()), 0.011, "no replay (R9 inverted)")
        self.assertIsNone(lab.engine._vtt_outro_delivery_state(1))
        reason = lab.engine._vtt_outro_side(deck, eng_module._vtt_track_markers(deck.track), None,
                                            lab.engine._vtt_live_context(), lab.now_ns())[3]
        self.assertEqual(reason, Reason.OUTRO_DELIVERY_UNCERTAIN)

    def test_resume_where_saved_position_plus_staleness_reaches_the_marker_is_suppressed(self):
        self._uncertain(5.0, 2.0)

    def test_resume_past_the_marker_is_suppressed(self):
        self._uncertain(10.5, 1.0)

    def test_two_occurrences_of_the_same_track_are_independent(self):
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000))
        first = lab.item(1, [(7.0, OUT)], track_id=4242, next_start=6.0, outro_starts=3.0, name="same-a")
        second = lab.item(3, [(7.0, OUT)], track_id=4242, next_start=6.0, outro_starts=3.0, name="same-a")
        tail = lab.item(4, [(6.0, MUSIC)], name="same-tail")
        lab.queue(first, second, tail)
        path = lab.vt("same-outro-vt", 1.0, speech=OUTRO_SPEECH)
        lab.attach(first, "outro", fake_voicetrack(path, 1.0))
        lab.poll()
        lab.play()
        lab.start("A")
        self.assertTrue(wait_until(lambda: lab.claims == [1, 3, 4], 20.0), lab.claims)
        pump(0.5)
        delivered = lab.engine.__dict__["_vtt_outro_delivery"]
        self.assertEqual(delivered.get(1), "aired")
        self.assertEqual(delivered.get(3), "aired", "a new occurrence of the same Track is eligible (F12)")
        self.assertEqual(sum(1 for r in lab.history() if r.outro_fire is not None), 2)


class LegacyRetirementTests(_OutroBase, SimpleTestCase):
    def test_legacy_vt_machine_is_deleted_and_never_competes(self):
        # 2.25 B6: the r0144 entry, phases and duck stepper are deleted (B3
        # retired them; B6 removes them). Only the occurrence-owned lifecycle
        # plans this transition and only its curve writes vt_duck_gain.
        lab, out, inc, deck, zero = self.scenario()
        engine = lab.engine
        for name in ("_vt_maybe_enter", "_vt_ramp_duck_to_db", "_vt_on_fire_eos"):
            self.assertFalse(hasattr(type(engine), name), name)
        self.assertNotIn("_vt", engine.__dict__)
        self.wait_claims(lab, [1, 2])
        self.assertEqual(len(list(engine.vt_duck_gain.control_bindings)), 1)

    def test_poisoned_vt_teardown_worker_suspends_vt_preparation_with_one_event(self):
        events = []
        lab, out, inc, deck, zero = self.scenario(poll=False)
        with patch.object(eng_module, "emit_event", new=lambda **kwargs: events.append(kwargs)):
            coordinator = lab.engine._vt2_teardown_coordinator()
            with coordinator._condition:
                coordinator._poisoned = True       # as after a NULL that outlived its bound
            lab.poll()
            self.wait_claims(lab, [1, 2])
            pump(0.5)
        tr = lab.trace()
        self.assertLess(tr.present(OUTRO_SPEECH, zero, lab.now()), 0.011)
        self.assertIsNone(lab.last_record())
        suspended = [e for e in events if e.get("dedupe_key") == "engine|vt-teardown-unavailable"]
        self.assertEqual(len(suspended), 1)
        self.assertTrue(0.25 < tr.onset(MUSIC, after=zero + 7.0) - (zero + 8.0) < 1.2, "ordinary playout continues")
