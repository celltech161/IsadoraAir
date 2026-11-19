"""Roadmap 2.25 B7 -- final engineering qualification: recovery, operator
policy boundaries, late Swap, combined holds, reload and urgent delivery.

The real engine transition path (production 250 ms ``_poll_position``,
``_start_next_track`` -> claim, B2 scheduled decks and fires, the one duck
curve), driven by the REAL operator authorities and measured in rendered
program PCM (tone-identified onsets/ends, 10 ms Goertzel bins).

Recovery (B7 correction C1): a scheduled incoming generation that misses its
readiness falls back to an ordinary primed start of the SAME claimed
occurrence. Until that start happens the occurrence is the B5 held claim with
a token-guarded deferred start, so every transport authority (Manual, the mic
hold, Start Next, Eject, reload, urgent insertion, Auto talk-over) acts on it
exactly once and no stale callback can open it.

Late Swap (B7 correction C2): a Track adopted at the claim gets the ordinary
approved intro eligibility (underlap, missing Intro Until) whenever enough
preparation lead remains; only a deadline miss suppresses it.

Signals: 400 Hz outgoing, 600 Hz incoming music, 1400 Hz incoming vocal,
2200 / 2600 Hz VT head / tail markers, 1000 Hz intro speech, 900 Hz outro
speech, 300 Hz third item / urgent item, 1700 Hz an adopted Track's intro VT,
800 Hz a dedication follow-up.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import vt_transition
from library.services.vt_audio import FireState
from library.tests._vt_b0_lab import GLib, Gst
from library.tests._vt_integration_lab import (
    DEDICATION, FOREIGN, HEAD, MUSIC, NS, OUT, OUTRO_SPEECH, SPEECH, TAIL, TOL, URGENT, fake_voicetrack,
    pump, report, wait_until,
)
from library.tests.test_vt_b3_outro_integration import _OutroBase
from library.tests.test_vt_b4_intro_integration import M, music
from library.tests import test_vt_b5_operator_integration as _b5
from library.tests.test_vt_b5_operator_integration import FADE_LIMIT, THIRD, _B5Base, _insert_urgent, _urgent


def _stall_first_readiness(engine):
    """The next scheduled deck never reports readiness (its decode stalls):
    the B2 readiness deadline fails it -- the R01 condition, forced."""
    real = engine._vt2_on_scheduled_deck_ready
    dropped = []

    def stall(slot, generation):
        if not dropped:
            dropped.append((slot, generation))
            return False
        return real(slot, generation)
    engine._vt2_on_scheduled_deck_ready = stall
    return dropped


def _after_fallback(lab, action, delay_s=0.0):
    """Run ``action`` once, right after the first fallback decision
    (``_vtt_fallback_ordinary`` returned), or ``delay_s`` later."""
    engine = lab.engine
    original = engine._vtt_fallback_ordinary
    fired = []

    def wrapped(record, deck, reason):
        original(record, deck, reason)
        if fired:
            return
        fired.append(lab.now())
        if delay_s <= 0:
            action()
        else:
            GLib.timeout_add(int(delay_s * 1000), lambda: (action(), False)[1])
    engine._vtt_fallback_ordinary = wrapped
    return fired


# ---------------------------------------------------------------------------
# C1: bounded, ownership-safe recovery of a failed scheduled admission
# ---------------------------------------------------------------------------
class FallbackRecoveryTests(_B5Base, SimpleTestCase):
    """Outro hold (D3): outro VT 8.0 .. 10.6 past the segue (8.5), the claimed
    song held until 10.6; its scheduled deck (opened at 10.0) never becomes
    ready, fails at ~10.1 and falls back to an ordinary primed start due at
    10.3 (music at 10.6). The outgoing plays until 10.4. A third item follows."""

    def outro_hold(self, **kwargs):
        lab, out, inc, deck, zero = _OutroBase.scenario(self, out_len=10.4, next_start=8.5, outro_at=8.0,
                                                        vt_len=2.6, poll=False, **kwargs)
        lab.engine.log_items.append(lab.item(3, [(5.0, THIRD)], name="b7-third"))
        _stall_first_readiness(lab.engine)
        return lab, out, inc, deck, zero

    def music_plan(self, zero):
        return zero + 10.6

    def finish(self, lab, seconds=2.0):
        pump(seconds)
        return lab.trace()

    def test_deferred_fallback_lands_at_the_plan_and_is_one_occurrence(self):
        lab, out, inc, deck, zero = self.outro_hold()
        fired = _after_fallback(lab, lambda: None)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 11.5, 14.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        record = lab.last_record()
        names = lab.event_names(record)
        report("B7-fallback-deferred", failed_at=fired[0] - zero, music_vs_plan=music_on - self.music_plan(zero),
               claims=lab.claims)
        self.assertIn("fallback_deferred", names)
        self.assertIn("fallback_ordinary", names)
        self.assertLess(abs(music_on - self.music_plan(zero)), 0.05, "after the outro, as planned")
        self.assertEqual(lab.claims, [1, 2, 2], "same occurrence (claim idempotent); no other item")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertIsNone(lab.engine.__dict__.get("_vtt_held_occurrence"))

    def test_outgoing_eject_during_a_pending_fallback_never_starts_a_second_item(self):
        # The outgoing finishing while the fallback waits (Eject here; natural
        # EOS and the watchdog use the same _vtt_outgoing_finished hand-over).
        lab, out, inc, deck, zero = self.outro_hold()
        _after_fallback(lab, lambda: lab.engine._eject_deck("A"))
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 12.0, 14.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7-fallback-eject", music_vs_plan=None if music_on is None else music_on - self.music_plan(zero),
               claims=lab.claims, third=tr.onset(THIRD, after=zero + 9.0))
        self.assertNotIn(3, lab.claims, "no second queue item while the claimed song waits")
        self.assertIsNone(tr.onset(THIRD, after=zero + 9.0))
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertLess(abs(music_on - self.music_plan(zero)), 0.05, "D9: the outro completes, then the song")

    def test_outgoing_finished_during_a_pending_fallback_is_owned_by_the_transition(self):
        lab, out, inc, deck, zero = self.outro_hold()
        seen = {}

        def probe():
            record = lab.record()
            seen["pending"] = record.fallback_pending
            seen["owns"] = lab.engine._vtt_owns_next_start()
            seen["finished_owned"] = lab.engine._vtt_outgoing_finished(
                record.out_deck, vt_transition.StartKind.AUTOMATIC)
        _after_fallback(lab, probe)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 11.5, 14.0))
        self.assertEqual(seen, {"pending": True, "owns": True, "finished_owned": True})
        self.assertNotIn(3, lab.claims)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_manual_during_a_pending_fallback_holds_the_claimed_occurrence_until_auto(self):
        lab, out, inc, deck, zero = self.outro_hold()
        engine = lab.engine
        _after_fallback(lab, lambda: engine._set_manual_mode(True))
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 11.6, 14.0))
        held = engine.__dict__.get("_vtt_held_occurrence")
        self.assertIsNotNone(held)
        self.assertEqual(held["log_item"].id, 2)
        state = engine._vtt_state()["held_occurrence"]
        self.assertFalse(state["automatic_start_pending"], "published: waits for an authorized start")
        tr = lab.trace()
        self.assertIsNone(tr.onset(MUSIC, after=zero + 9.0), "no automatic start in Manual")
        self.assertEqual(self.air_starts(lab, 2), [])
        release_at = lab.now()
        engine._set_manual_mode(False)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=release_at)
        report("B7-fallback-manual", music_after_release=music_on - release_at, claims=lab.claims)
        self.assertLess(music_on - release_at, 0.8)
        self.assertIsNone(tr.onset(HEAD, after=release_at), "deferred handoff: no intro VT (D5)")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims)

    def test_mic_hold_during_a_pending_fallback_holds_until_the_mic_is_released(self):
        lab, out, inc, deck, zero = self.outro_hold()
        engine = lab.engine
        self.ducking(lab, auto_manual=True)
        _after_fallback(lab, lambda: lab.mic(True))
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 11.6, 14.0))
        self.assertTrue(engine.manual_mode, "the mic hold")
        self.assertIsNone(lab.trace().onset(MUSIC, after=zero + 9.0))
        release_at = lab.now()
        lab.mic(False)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=release_at)
        report("B7-fallback-mic-hold", music_after_release=music_on - release_at)
        self.assertLess(music_on - release_at, 0.8)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims)

    def test_auto_talk_over_during_a_pending_fallback_starts_it_without_speech(self):
        lab, out, inc, deck, zero = self.outro_hold()
        self.ducking(lab, auto_manual=False)
        at = {}
        _after_fallback(lab, lambda: (at.setdefault("t", lab.now()), lab.mic(True)))
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 11.5, 14.0))
        tr = lab.trace()
        command_at = at["t"]
        speech_end = self.speech_end(tr, OUTRO_SPEECH, command_at)
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7-fallback-talk-over", speech_end_after=speech_end - command_at,
               music_after_command=music_on - command_at)
        self.assertFalse(lab.engine.manual_mode)
        self.assertLess(speech_end - command_at, FADE_LIMIT, "no recorded speech over live talent")
        self.assertLess(music_on - command_at, 0.15 + SILENCE_PRIME, "the song no longer waits for the outro")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_reload_during_a_pending_fallback_never_opens_the_replaced_programs_song(self):
        lab, out, inc, deck, zero = self.outro_hold()
        engine = lab.engine
        replacement = lab.item(9, [(5.0, DEDICATION)], name="b7-reload")

        def reload():
            engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                                setattr(engine, "_queue_cursor", 0))
            engine._reload_and_restart_current_log()
        at = _after_fallback(lab, reload)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        pump(2.0)
        tr = lab.trace()
        report("B7-fallback-reload", claims=lab.claims, replacement_after=tr.onset(DEDICATION, after=at[0]) - at[0])
        self.assertIsNone(tr.onset(MUSIC, after=zero + 9.0), "the stale deferred start never fires")
        self.assertEqual(self.air_starts(lab, 2), [])
        self.assertIsNotNone(tr.onset(DEDICATION, after=at[0]))
        self.assertIsNone(engine.__dict__.get("_vtt_held_occurrence"))

    def test_start_next_during_a_pending_fallback_starts_the_claimed_occurrence_once(self):
        lab, out, inc, deck, zero = self.outro_hold()
        pressed = {}

        def press():
            pressed["t"] = lab.now()
            self.start_next(lab, fade_ms=250)
        _after_fallback(lab, press, delay_s=0.05)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: "t" in pressed, 14.0))
        outcome = self.settle_start_next(lab)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7-fallback-start-next", outcome=outcome["outcome"], mode=outcome["mode"],
               reason=outcome.get("reason"), music_after_press=music_on - pressed["t"], claims=lab.claims)
        self.assertEqual(outcome["outcome"], "completed", outcome)
        self.assertNotEqual(outcome.get("reason"), "busy_voice_track")
        self.assertEqual(len(self.air_starts(lab, 2)), 1, "the claimed song, exactly once")
        self.assertNotIn(3, lab.claims, "Start Next starts the held claim, not a second item")
        self.assertLess(music_on - pressed["t"], 0.8)

    def test_start_next_refusal_window_after_a_failure_is_bounded(self):
        """Presses every 20 ms from the failure until one is accepted: the
        maximum refusal window, and only bounded recovery reasons."""
        lab, out, inc, deck, zero = self.outro_hold()
        engine = lab.engine
        log = {"presses": 0, "accepted_at": None, "failed_at": None}

        def press_loop():
            if log["accepted_at"] is not None:
                return False
            if engine.__dict__.get("_start_next_active") is not None:
                log["accepted_at"] = lab.now()
                return False
            log["presses"] += 1
            self.start_next(lab, fade_ms=250)
            return True

        def begin():
            log["failed_at"] = lab.now()
            press_loop()
            GLib.timeout_add(20, press_loop)
        _after_fallback(lab, begin)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: log["accepted_at"] is not None, 14.0))
        self.settle_start_next(lab)
        pump(1.2)
        recent = engine._start_next_state()["recent"]
        refusals = [r.get("reason") for r in recent if r.get("outcome") == "rejected"]
        window = log["accepted_at"] - log["failed_at"]
        report("B7-start-next-refusal-window", window=window, presses=log["presses"],
               refusal_reasons=",".join(sorted(set(filter(None, refusals)))) or "none")
        self.assertLess(window, 0.6, "a short, strictly bounded refusal")
        self.assertTrue(set(filter(None, refusals)) <= {"busy_transition_recovery", "slot_unavailable"}, refusals)
        self.assertNotIn("busy_voice_track", refusals)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims)

    def test_urgent_during_a_pending_fallback_releases_the_held_song_and_keeps_claim_order(self):
        lab, out, inc, deck, zero = self.outro_hold()
        engine = lab.engine
        urgent = _urgent(lab)
        at = _after_fallback(lab, lambda: _insert_urgent(engine, urgent))
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: 50 in lab.claims, 14.0))
        pump(0.5)
        tr = lab.trace()
        command_at = at[0]
        speech_end = self.speech_end(tr, OUTRO_SPEECH, command_at)
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7-fallback-urgent", speech_end_after=speech_end - command_at,
               music_after_urgent=music_on - command_at, saved=self.music_plan(zero) - music_on, claims=lab.claims)
        self.assertLess(speech_end - command_at, FADE_LIMIT, "recorded speech never extends the boundary")
        self.assertLess(music_on - command_at, 0.15 + SILENCE_PRIME)
        self.assertLess(lab.claims.index(2), lab.claims.index(50), "r0137 claim order: the urgent item follows")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims[:lab.claims.index(50)])

    def test_failed_incoming_in_a_long_underlap_lands_at_the_segue_without_dead_air(self):
        # The intro speech never became audible (it is cancelled with its
        # deck): nothing justifies holding the music U = 1.65 s.
        lab, out, inc, deck, zero = self.underlap(poll=False)
        _stall_first_readiness(lab.engine)
        failed = _after_fallback(lab, lambda: None)
        lab.poll()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.2, 12.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        # The miss is only known at the readiness deadline (~0.5 s before the
        # segue); an ordinary primed start then needs prime + Cue In. That is
        # preparation latency, reported separately from scheduling accuracy.
        earliest = failed[0] + SILENCE_PRIME + 0.25
        expected = max(a_seg, earliest)
        gap = music_on - (zero + 9.0)          # outgoing audio ends at 9.0
        report("B7-fallback-underlap", detected_before_segue=a_seg - failed[0], music_vs_segue=music_on - a_seg,
               music_vs_earliest=music_on - expected, dead_air_after_outgoing=max(0.0, gap),
               b6_would_land_at_segue_plus=1.65)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "no intro speech without viability")
        self.assertLess(abs(music_on - expected), 0.05, "as soon as an ordinary start can land, not A_seg + U")
        self.assertLess(music_on - a_seg, 0.12, "no underlap-length dead air for speech that never comes")
        self.assertEqual(max(0.0, gap), 0.0, "the outgoing still plays: no dead air")
        self.assertEqual(lab.claims, [1, 2, 2])


SILENCE_PRIME = eng_module.SILENCE_PRIME_SECONDS


def _any_record(lab):
    """The current, draining or most recent transition record."""
    engine = lab.engine
    for record in (engine.__dict__.get("_vtt"), engine.__dict__.get("_vtt_draining")):
        if record is not None:
            return record
    history = lab.history()
    return history[-1] if history else None


# ---------------------------------------------------------------------------
# C2: late Swap -- ordinary eligibility when the lead allows it
# ---------------------------------------------------------------------------
class LateSwapTests(_B5Base, SimpleTestCase):
    """A Swap committed after the engine's queue copy was read is adopted at
    the claim (~0.6 s before the segue): the look-ahead never saw that
    Track's intro VT, so its authoritative answer arrives after the claim."""

    def swap(self, *, intro_until, vt, resolver_delay=0.0, cue=0.25):
        lab, out, inc, deck, zero = self.build(intro_vt=1.0, poll=False)     # the predicted Track's VT
        segments = music(cue, intro_until, after=1.0) if intro_until else [(cue, 0), (5.0, MUSIC)]
        adopted = lab.item(2, segments, track_id=7007, name=f"adopted-{intro_until}-{vt}", cue_in=cue,
                           intro_until=intro_until or 0.0)
        path = lab.vt(f"adopted-vt-{vt}", vt, speech=FOREIGN)
        lab.attach(adopted, "intro", fake_voicetrack(path, vt))
        engine = lab.engine
        claim = engine._claim_playback_occurrence

        def adopt_at_claim(item):
            if item.id == 2 and item.track.id != 7007:
                item.track = adopted.track
            return claim(item)
        engine._claim_playback_occurrence = adopt_at_claim
        if resolver_delay:
            resolve = engine._vtt_resolve_lookup

            def slow(key, payload):
                if key[0] == "vt" and key[2] == 7007:
                    time.sleep(resolver_delay)
                return resolve(key, payload)
            engine._vtt_resolve_lookup = slow
            service = engine.__dict__.get("_vtt_lookup_service")
            if service is not None:
                service._resolve = slow
        lab.poll()
        return lab, zero + 8.0

    def test_swapped_track_with_lead_gets_its_underlap_intro_vt(self):
        # W = 1.5, V = 3.0 -> U = 3.0 - 1.5 + 0.15 = 1.65 s.
        lab, a_seg = self.swap(intro_until=1.75, vt=3.0)
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 3.6, 12.0))
        tr = lab.trace()
        head = tr.onset(HEAD, after=a_seg - 1.0)
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        record = _any_record(lab)
        report("B7-late-swap-underlap", vt_onset_error=None if head is None else head - a_seg,
               music_vs_plan=None if music_on is None else music_on - (a_seg + 1.65),
               events=",".join(n for n in lab.event_names(record) if "late" in n or n == "readmit"))
        self.assertIn("late_intro_upgrade", lab.event_names(record))
        self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0), "never the predicted Track's VT")
        self.assertLess(abs(head - a_seg), TOL, "the swapped Track's VT at the segue")
        self.assertLess(abs(music_on - (a_seg + 1.65)), TOL, "underlap applies (music re-admitted later)")
        self.assertGreater(tr.present(FOREIGN, a_seg + 0.2, a_seg + 2.8), 2.4, "its whole recording")
        self.assertEqual(set(lab.claims), {1, 2}, "the same claimed occurrence (idempotent re-claim)")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_swapped_track_without_intro_until_uses_the_approved_fallback(self):
        # No Intro Until: U = V + M (music after the speech and the margin).
        lab, a_seg = self.swap(intro_until=None, vt=1.0)
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.0, 12.0))
        tr = lab.trace()
        head = tr.onset(HEAD, after=a_seg - 1.0)
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        report("B7-late-swap-no-intro-until", vt_onset_error=head - a_seg,
               music_vs_plan=music_on - (a_seg + 1.0 + M))
        self.assertLess(abs(head - a_seg), TOL)
        self.assertLess(abs(music_on - (a_seg + 1.0 + M)), TOL)

    def test_swap_answer_after_the_deadline_suppresses_only_the_vt(self):
        lab, a_seg = self.swap(intro_until=1.75, vt=3.0, resolver_delay=1.2)
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.5, 12.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        record = _any_record(lab)
        report("B7-late-swap-deadline", intro_reason=record.intro_reason, music_vs_segue=music_on - a_seg)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "too late to prepare: no speech")
        self.assertLess(abs(music_on - a_seg), TOL, "the music plan never moves for a missed deadline")
        self.assertEqual(lab.claims, [1, 2])


# ---------------------------------------------------------------------------
# Combined holds: releasing one hold never admits while another applies
# ---------------------------------------------------------------------------
class CombinedHoldTests(_B5Base, SimpleTestCase):
    def held(self, lab):
        held = lab.engine.__dict__.get("_vtt_held_occurrence")
        return None if held is None else held["log_item"].id

    def prepare(self):
        lab, out, inc, deck, zero = self.underlap()
        self.ducking(lab, auto_manual=True)
        _b5.RemoteDjTests.remote(self, lab.engine)    # connected Remote DJ, gate Off
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        return lab, a_seg

    def gate(self, lab, live):
        lab.engine._remote_dj_set_gate(live)       # the real Remote DJ gate authority

    def assert_still_held(self, lab, after, label):
        pump(0.6)
        self.assertEqual(self.held(lab), 2, label)
        self.assertIsNone(lab.trace().onset(MUSIC, after=after), label)

    def released(self, lab, release_at):
        pump(1.2)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=release_at)
        self.assertIsNotNone(music_on)
        self.assertLess(music_on - release_at, 0.8)
        self.assertIsNone(tr.onset(HEAD, after=release_at), "immediate deferred handoff: no intro VT")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertEqual(set(lab.claims), {1, 2})
        return music_on - release_at

    def test_operator_manual_then_mic_then_mic_release_stays_held_until_auto(self):
        lab, a_seg = self.prepare()
        engine = lab.engine
        engine._set_manual_mode(True)
        self.assertEqual(self.held(lab), 2)
        lab.mic(True)
        lab.mic(False)
        self.assertTrue(engine.manual_mode, "the operator's Manual remains")
        self.assert_still_held(lab, a_seg - 0.5, "mic release under an operator Manual")
        release_at = lab.now()
        engine._set_manual_mode(False)
        report("B7-hold-manual-mic", music_after_auto=self.released(lab, release_at))

    def test_mic_hold_then_operator_manual_then_mic_release_stays_held(self):
        lab, a_seg = self.prepare()
        engine = lab.engine
        lab.mic(True)
        self.assertEqual(self.held(lab), 2)
        engine._set_manual_mode(True)          # the operator takes ownership of Manual
        lab.mic(False)
        self.assertTrue(engine.manual_mode)
        self.assert_still_held(lab, a_seg - 0.5, "mic release after an operator Manual")
        release_at = lab.now()
        engine._set_manual_mode(False)
        report("B7-hold-mic-manual", music_after_auto=self.released(lab, release_at))

    def test_studio_mic_and_remote_gate_release_only_after_both(self):
        lab, a_seg = self.prepare()
        engine = lab.engine
        lab.mic(True)
        self.gate(lab, True)
        self.assertEqual(self.held(lab), 2)
        lab.mic(False)
        self.assertTrue(engine.manual_mode, "the Remote DJ gate still holds")
        self.assert_still_held(lab, a_seg - 0.5, "studio mic released, gate live")
        release_at = lab.now()
        self.gate(lab, False)
        self.assertFalse(engine.manual_mode)
        report("B7-hold-mic-gate", music_after_last_release=self.released(lab, release_at))

    def test_mic_hold_in_auto_releases_on_its_own(self):
        lab, a_seg = self.prepare()
        engine = lab.engine
        lab.mic(True)
        self.assertTrue(engine.manual_mode)
        self.assert_still_held(lab, a_seg - 0.5, "mic hold")
        release_at = lab.now()
        lab.mic(False)
        self.assertFalse(engine.manual_mode)
        report("B7-hold-mic-only", music_after_release=self.released(lab, release_at))


# ---------------------------------------------------------------------------
# Operator commands on both sides of the actual admission boundary
# ---------------------------------------------------------------------------
class AdmissionBoundaryTests(_B5Base, SimpleTestCase):
    """Underlap: the held deck's admission (media 0) is A_seg + 1.4, its Cue
    In (first real audio) at A_seg + 1.65. B7 supplemental C7: a waiting deck
    is on air only when the deck mixer has pushed output to within
    MIXER_RECALL_GUARD_S of its admission (measured irreversible point
    ~10-20 ms before admission), not within a fixed 120 ms."""

    OFFSETS = (-0.200, -0.135, -0.105, -0.060, 0.060)

    def at_offset(self, offset, action):
        lab, out, inc, deck, zero = self.underlap(extra=())
        lab.engine.log_items.append(lab.item(3, [(5.0, THIRD)], name=f"b7-boundary-{offset}"))
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.deck_of(2) is not None
                                   and lab.deck_of(2).admission_state == "scheduled", 6.0))
        admission = lab.deck_of(2).scheduled_admission_ns / NS
        hit = {}

        def fire():
            hit["t"] = lab.now()
            deck = lab.deck_of(2)
            hit["state"] = getattr(deck, "admission_state", None)
            hit["on_air"] = deck is None or lab.engine._vtt_incoming_on_air(deck)
            action(lab)
        lab.lab.at(admission + offset, fire)
        self.assertTrue(wait_until(lambda: "t" in hit, 12.0))
        return lab, a_seg, admission, hit

    def test_manual_on_both_sides_of_the_admission_boundary(self):
        rows = []
        for offset in self.OFFSETS:
            with self.subTest(offset=offset):
                lab, a_seg, admission, hit = self.at_offset(offset, lambda lab: lab.engine._set_manual_mode(True))
                pump(1.0)
                tr = lab.trace()
                music_on = tr.onset(MUSIC, after=a_seg - 0.5)
                lead = admission - hit["t"]
                on_air = hit["on_air"]          # the engine's own evidence at the command
                rows.append(f"{offset:+.3f}:lead={lead:.3f}:{'air' if music_on is not None else 'held'}")
                if lead >= 0.055:
                    self.assertFalse(on_air, "far from the irreversible point: Manual must hold it (C7)")
                if on_air:
                    self.assertIsNotNone(music_on, "within the boundary it is on air: never interrupted")
                    self.assertLess(abs(music_on - (a_seg + 1.65)), TOL)
                    self.assertEqual(len(self.air_starts(lab, 2)), 1)
                else:
                    self.assertIsNone(music_on, "recallable: held, not one sample rendered")
                    self.assertLess(lab.trace().present(MUSIC, admission - 0.05, admission + 0.6), 0.005)
                    self.assertEqual(lab.engine.__dict__["_vtt_held_occurrence"]["log_item"].id, 2)
                    self.assertEqual(self.air_starts(lab, 2), [])
                lab.lab.stop()
                lab.engine.running = False
        report("B7-boundary-manual", rows=" ".join(rows))

    def test_start_next_on_both_sides_never_skips_the_claimed_occurrence(self):
        rows = []
        for offset in self.OFFSETS:
            with self.subTest(offset=offset):
                lab, a_seg, admission, hit = self.at_offset(offset, lambda lab: self.start_next(lab, fade_ms=300))
                outcome = self.settle_start_next(lab)
                pump(1.2)
                tr = lab.trace()
                music_on = tr.onset(MUSIC, after=a_seg - 0.5)
                rows.append(f"{offset:+.3f}:{outcome['mode']}:{outcome.get('vt_admission', {}).get('outcome')}"
                            f":music+{(music_on - hit['t']) if music_on else float('nan'):.3f}")
                self.assertEqual(outcome["outcome"], "completed", outcome)
                self.assertEqual(len(self.air_starts(lab, 2)), 1, "exactly once")
                if hit["t"] < admission:
                    # Never audible yet (recallable, or already irreversible but not
                    # yet admitted): it is the operator's next -- it airs.
                    self.assertIsNotNone(music_on)
                    self.assertNotIn(3, lab.claims, "never skipped in favour of the next item")
                else:
                    # Admitted (on air, played_at recorded): the established
                    # 2.23 contract -- Start Next advances past what is on air.
                    self.assertEqual(outcome["mode"], "fade_and_start")
                    self.assertIn(3, lab.claims)
                lab.lab.stop()
                lab.engine.running = False
        report("B7-boundary-start-next", rows=" ".join(rows))

    def test_delayed_decoding_of_a_regenerated_deck_falls_back_to_the_same_occurrence(self):
        # Start Next regenerates the held deck (150 ms lead); its decode is
        # delayed past READY_LEAD_S: the bounded fallback starts the SAME
        # occurrence ordinarily (never skipped, one air start).
        lab, out, inc, deck, zero = self.underlap(extra=())
        lab.engine.log_items.append(lab.item(3, [(5.0, THIRD)], name="b7-delayed-decode"))
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        original = engine._create_deck

        def delayed(slot, log_item, *args, **kwargs):
            if kwargs.get("reopen") and kwargs.get("scheduled_admission_ns"):
                def slow_first(pad, info):
                    time.sleep(0.140)           # streaming-thread decode delay
                    return Gst.PadProbeReturn.REMOVE
                kwargs["before_sync"] = lambda d: d.seek_gate_pad.add_probe(Gst.PadProbeType.BUFFER, slow_first)
            return original(slot, log_item, *args, **kwargs)
        engine._create_deck = delayed
        pressed = lab.now()
        self.start_next(lab, fade_ms=300)
        self.settle_start_next(lab)
        pump(1.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        names = lab.event_names(lab.history()[-1]) if lab.history() else []
        report("B7-delayed-decode", music_after_press=music_on - pressed, claims=lab.claims,
               fallback="fallback_ordinary" in names or 2 in lab.claims[2:])
        self.assertIsNotNone(music_on)
        self.assertLess(music_on - pressed, 1.0, "bounded recovery")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertNotIn(3, lab.claims)


# ---------------------------------------------------------------------------
# Reload semantics and urgent ownership of held claims
# ---------------------------------------------------------------------------
class ReloadAndUrgentOwnershipTests(_B5Base, SimpleTestCase):
    def wx_decision(self, held_id):
        decision = SimpleNamespace(allowed=True, generation=7, published_at=None, reason="")
        return (
            patch.object(eng_module.wx_alert_playout, "current_decision", new=lambda: (decision, None)),
            patch.object(eng_module.wx_alert_playout, "audio_track", new=lambda: SimpleNamespace(id=5050)),
            patch.object(eng_module.wx_alert_playout, "generation_delivery",
                         new=lambda _track_id, _published: (False, {held_id})),
        )

    def test_a_held_claimed_alert_is_owned_never_retried_twice_and_retried_after_reload(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        engine._pending_wx_alert_occurrence = lambda _track: None
        for mocked in self.wx_decision(2):
            mocked.start()
            self.addCleanup(mocked.stop)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        engine._set_manual_mode(True)
        self.assertEqual(engine.__dict__["_vtt_held_occurrence"]["log_item"].id, 2)
        held_answer = engine._wx_alert_track_to_queue()
        replacement = lab.item(9, [(5.0, THIRD)], name="b7-wx-reload")
        engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                            setattr(engine, "_queue_cursor", 0))
        engine._set_manual_mode(False)
        engine._set_manual_mode(True)          # keep it held through the reload below
        engine._reload_and_restart_current_log()
        pump(0.5)
        after_reload = engine._wx_alert_track_to_queue()
        report("B7-wx-held", while_held=held_answer, after_reload=getattr(after_reload, "id", None))
        self.assertIsNone(held_answer, "a held claim is owned: no duplicate delivery")
        self.assertEqual(getattr(after_reload, "id", None), 5050,
                         "discarded with the replaced program: the undelivered alert is retried")

    def test_reload_keeps_forced_follow_ups_and_reports_the_discarded_held_claim(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        follow_up = lab.item(61, [(4.0, DEDICATION)], name="b7-follow-up")
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        engine._set_manual_mode(True)
        engine._forced_next_items.append(follow_up)        # a pending dedication follow-up
        replacement = lab.item(9, [(5.0, THIRD)], name="b7-reload-forced")
        engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                            setattr(engine, "_queue_cursor", 0))
        engine._set_manual_mode(False)
        engine._reload_and_restart_current_log()
        pump(1.0)
        tr = lab.trace()
        report("B7-reload-forced", claims=lab.claims)
        self.assertIsNone(engine.__dict__.get("_vtt_held_occurrence"))
        self.assertEqual(self.air_starts(lab, 2), [], "the replaced program's held claim never airs")
        self.assertIn(61, lab.claims, "the forced follow-up is not lost by reload")
        self.assertIsNotNone(tr.onset(DEDICATION, after=a_seg))


# ---------------------------------------------------------------------------
# Urgent arrival matrix (section 7) not covered by B5
# ---------------------------------------------------------------------------
class UrgentArrivalMatrixTests(_B5Base, SimpleTestCase):
    def test_urgent_before_the_claim_is_next_without_a_hold_or_intro_vt(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.0, 8.0))
        urgent = _urgent(lab)
        _insert_urgent(engine, urgent)
        self.wait_claims(lab, [1, 50])
        pump(2.5)
        tr = lab.trace()
        urgent_on = tr.onset(URGENT, after=a_seg - 1.0)
        report("B7-urgent-before-claim", urgent_vs_segue=urgent_on - a_seg, claims=lab.claims)
        # The ordinary r0144 boundary: the 250 ms poll trigger at Next Start
        # plus the silence prime -- never the 1.65 s VT hold of the old plan.
        self.assertGreaterEqual(urgent_on - a_seg, 0.0)
        self.assertLess(urgent_on - a_seg, eng_module.POSITION_POLL_MS / 1000.0 + SILENCE_PRIME + 0.06)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "no VT speech over the urgent item")
        self.assertLess(tr.present(SPEECH, a_seg - 1.0, lab.now()), 0.011)

    def test_urgent_in_manual_hold_waits_and_follows_the_held_song(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.4, 10.0))
        engine._set_manual_mode(True)
        urgent = _urgent(lab, seconds=2.0)
        _insert_urgent(engine, urgent)
        pump(0.8)
        self.assertIsNone(lab.trace().onset(URGENT, after=a_seg), "Manual holds the urgent item too (r0137)")
        release_at = lab.now()
        engine._set_manual_mode(False)
        self.assertTrue(wait_until(lambda: 50 in lab.claims, 12.0))
        pump(2.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=release_at)
        urgent_on = tr.onset(URGENT, after=release_at)
        report("B7-urgent-manual", music_after_auto=music_on - release_at, urgent_after_auto=urgent_on - release_at,
               claims=lab.claims)
        self.assertLess(lab.claims.index(2), lab.claims.index(50), "claim order: the held song, then the urgent")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertIsNone(tr.onset(HEAD, after=release_at), "no VT speech")

    def test_urgent_near_music_admission_keeps_the_on_air_song_and_follows_it(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.deck_of(2) is not None
                                   and lab.deck_of(2).admission_state == "scheduled", 6.0))
        admission = lab.deck_of(2).scheduled_admission_ns / NS
        urgent = _urgent(lab, seconds=2.0)
        lab.lab.at(admission - 0.06, lambda: _insert_urgent(engine, urgent))
        self.assertTrue(wait_until(lambda: lab.now() > admission + 0.6, 10.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 0.5)
        report("B7-urgent-near-admission", music_vs_plan=music_on - (a_seg + 1.65))
        self.assertLess(abs(music_on - (a_seg + 1.65)), TOL, "a song on air (within 120 ms) is never touched")
        self.assertEqual(len(self.air_starts(lab, 2)), 1)

    def test_urgent_across_a_log_reload_is_delivered_once_without_vt_speech(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        urgent = _urgent(lab, seconds=2.0)
        engine._forced_next_items.append(urgent)          # pending forced urgent item
        engine._vtt_mark_urgent(urgent)
        engine._vtt_queue_changed("urgent_insert")
        replacement = lab.item(9, [(5.0, THIRD)], name="b7-urgent-reload")
        engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                            setattr(engine, "_queue_cursor", 0))
        command_at = lab.now()
        engine._reload_and_restart_current_log()
        self.assertTrue(wait_until(lambda: 50 in lab.claims, 6.0))
        pump(1.5)
        tr = lab.trace()
        report("B7-urgent-reload", claims=lab.claims)
        self.assertEqual(lab.claims.count(50), 1, "delivered once")
        self.assertLess(self.speech_end(tr, SPEECH, command_at) - command_at, FADE_LIMIT)
        self.assertIsNone(tr.onset(HEAD, after=command_at + 0.1), "no VT speech over the urgent item")
        self.assertIsNotNone(tr.onset(URGENT, after=command_at))


# ---------------------------------------------------------------------------
# Section 6: asynchronous lookup lifecycle (vt_lookup.py + the engine side)
# ---------------------------------------------------------------------------
import ast  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

from library.services import vt_lookup  # noqa: E402
from library.tests import test_vt_b6_retirement_state_lookup as _b6  # noqa: E402
from library.tests._vt_integration_lab import IntegrationLab  # noqa: E402


class LookupServiceLifecycleTests(SimpleTestCase):
    def test_worker_starts_on_demand_stops_cleanly_and_releases_its_connection(self):
        exits = []
        threads = set()

        def resolve(key, payload):
            threads.add(threading.current_thread().name)
            return payload
        service = vt_lookup.VtLookupService(resolve, on_thread_exit=lambda: exits.append(
            threading.current_thread().name))
        self.assertFalse(service.snapshot()["alive"], "nothing runs before the first request")
        self.assertTrue(service.request(("vt", 1), "a"))
        self.assertTrue(wait_until(lambda: service.get(("vt", 1)) is not None, 2.0))
        self.assertTrue(service.snapshot()["alive"])
        started = time.monotonic()
        self.assertTrue(service.stop(1.0))
        report("B7-lookup-lifecycle", stop_seconds=time.monotonic() - started, exits=len(exits),
               worker_threads=",".join(sorted(threads)))
        self.assertEqual(exits, ["vt-lookup"], "the worker's connection cleanup ran once, on the worker")
        self.assertEqual(threads, {"vt-lookup"}, "resolution never ran on the caller's thread")
        self.assertFalse(service.request(("vt", 2), "b"), "a stopped service refuses (never waits)")

    def test_shutdown_with_work_outstanding_is_bounded_and_serves_nothing_more(self):
        gate = threading.Event()
        served, notified, exits = [], [], []

        def resolve(key, payload):
            served.append(key)
            gate.wait(5.0)
            return payload
        service = vt_lookup.VtLookupService(resolve, notify=notified.append,
                                            on_thread_exit=lambda: exits.append(1))
        for n in range(6):
            service.request(("vt", n), n)
        self.assertTrue(wait_until(lambda: served, 2.0))
        started = time.monotonic()
        stopped = service.stop(0.2)
        elapsed = time.monotonic() - started
        gate.set()
        self.assertTrue(wait_until(lambda: exits, 3.0), "the worker exits after its current lookup")
        report("B7-lookup-shutdown", stop_returned_after=elapsed, stopped_in_time=stopped,
               served=len(served), queued_dropped=6 - len(served))
        self.assertLess(elapsed, 0.35, "shutdown never waits on a blocked database")
        self.assertFalse(stopped)
        self.assertEqual(len(served), 1, "queued work is abandoned, never served after shutdown")
        self.assertLessEqual(len(notified), 1)

    def test_a_result_for_a_superseded_request_is_rejected(self):
        clock = {"t": 100.0}
        service = vt_lookup.VtLookupService(lambda key, payload: payload, inline=True, clock=lambda: clock["t"])
        service.request(("vt", 1), "new", fresh_after=100.0)
        newest = service.get(("vt", 1))
        # An answer whose request was issued EARLIER finishes later (a slow
        # worker before a restart): it never replaces the newer answer.
        service._serve(("vt", 1), "old", 99.0)
        self.assertEqual(service.get(("vt", 1)), newest)
        self.assertEqual(service.snapshot()["stale_dropped"], 1)


class EngineLookupQualificationTests(_b6._ThreadedBase, SimpleTestCase):
    def build_threaded(self, *, wrap=None, **kwargs):
        lab, out, inc, deck, zero = self.build(poll=False, **kwargs)
        self.threaded(lab, wrap=wrap)
        lab.poll()
        return lab, out, inc, deck, zero

    def test_planning_answers_expire_and_are_re_requested(self):
        lab = IntegrationLab(self)
        engine = lab.engine
        engine._vtt_lookup_inline = True
        item = lab.item(2, [(2.0, MUSIC)], name="b7-expiry")
        lab.attach(item, "intro", fake_voicetrack(lab.vt("b7-expiry-vt", 1.0), 1.0))
        self.assertIsNotNone(engine._vtt_cached_voicetrack(item, "intro"))
        service = engine._vtt_lookups()
        key = engine._vtt_vt_key(item.track, "intro")
        fresh = service.get(key)
        before = service.snapshot()["requests"]
        service._cache[key] = vt_lookup.LookupResult(
            key, True, fresh.value, "", time.monotonic() - vt_transition.LOOKUP_PLAN_MAX_AGE_S - 1.0,
            time.monotonic() - 31.0, 0)
        self.assertIsNone(engine._vtt_cached_voicetrack(item, "intro"), "an expired answer never plans")
        self.assertEqual(service.snapshot()["requests"], before + 1, "an expired answer is re-requested")
        self.assertIsNotNone(engine._vtt_cached_voicetrack(item, "intro"), "the refreshed answer plans")
        service.inline = False
        service._cache[key] = vt_lookup.LookupResult(
            key, True, fresh.value, "", time.monotonic() - vt_transition.LOOKUP_PLAN_MAX_AGE_S - 1.0,
            time.monotonic() - 31.0, 0)
        service._inflight[key] = time.monotonic()          # the refresh is in flight
        self.assertIsNone(engine._vtt_cached_voicetrack(item, "intro"), "an expired answer never plans")

    def test_configuration_change_is_used_after_the_refresh_interval(self):
        lab = IntegrationLab(self)
        engine = lab.engine
        engine._vtt_lookup_inline = True
        first = engine._vtt_config()
        lab.config.program_duck_db = -12.0
        self.assertEqual(engine._vtt_config(), first, "within CONFIG_REFRESH_S the cached value stands")
        service = engine._vtt_lookups()
        cached = service.get(("config",))
        service._cache[("config",)] = vt_lookup.LookupResult(
            ("config",), True, cached.value, "", time.monotonic() - vt_transition.CONFIG_REFRESH_S - 0.5,
            time.monotonic(), 0)
        engine._vtt_config()                       # the refresh is requested (answered inline)
        updated = engine._vtt_config()
        report("B7-config-refresh", before=first[0], after=updated[0])
        self.assertEqual(first[0], -6.0)
        self.assertEqual(updated[0], -12.0)

    def test_recording_replaced_after_the_claim_never_airs_the_old_take(self):
        state = {"claimed": False}
        new_path = {}

        def wrap(resolve):
            def resolver(key, payload):
                if key[0] == "vt" and state["claimed"] and key[1] == "intro":
                    return vt_transition.Recording(attached=True, playable=True, duration=1.0,
                                                   source="resolved"), new_path["p"], 0.0
                return resolve(key, payload)
            return resolver
        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, intro_vt=1.2)
        new_path["p"] = str(lab.vt("b7-replacement-take", 1.0, speech=FOREIGN))
        engine = lab.engine
        original = engine._claim_playback_occurrence

        def claim(item):
            if item.id == 2:
                state["claimed"] = True
            return original(item)
        engine._claim_playback_occurrence = claim
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.6, 10.0))
        tr = lab.trace()
        record = _any_record(lab)
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        report("B7-recording-replaced", intro_reason=record.intro_reason, music_vs_segue=music_on - a_seg)
        self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0), "the planned (old) take never airs")
        self.assertEqual(record.intro_reason, vt_transition.RECORDING_CHANGED)
        self.assertLess(abs(music_on - a_seg), TOL)

    def test_media_invalid_after_the_answer_suppresses_only_the_vt(self):
        lab, out, inc, deck, zero = self.build_threaded(intro_vt=1.2)
        engine = lab.engine
        original = engine._vt2_prepare_fire
        removed = []

        def prepare(owner, path, *args, **kwargs):
            if owner.direction == vt_transition.INTRO and not removed:
                Path(path).unlink()                 # deleted after the lookup answered
                removed.append(path)
            return original(owner, path, *args, **kwargs)
        engine._vt2_prepare_fire = prepare
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.6, 10.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        record = _any_record(lab)
        report("B7-media-invalid", intro_reason=record.intro_reason, music_vs_segue=music_on - a_seg)
        self.assertTrue(removed)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0))
        self.assertLess(abs(music_on - a_seg), TOL, "the music plan never depends on the speech")
        self.assertGreater(tr.level(MUSIC, a_seg + 0.2, a_seg + 0.9), 0.37, "no speechless duck")

    def test_owner_retired_before_the_answer_arrives_prepares_nothing(self):
        state = {"claimed": False}

        def wrap(resolve):
            def resolver(key, payload):
                if key[0] == "vt" and state["claimed"]:
                    time.sleep(0.6)
                return resolve(key, payload)
            return resolver
        lab, out, inc, deck, zero = self.build_threaded(wrap=wrap, in_cue=0.25, in_intro=1.75, intro_vt=3.0)
        engine = lab.engine
        engine.log_items.append(lab.item(3, [(5.0, THIRD)], name="b7-owner-retired"))
        original = engine._claim_playback_occurrence

        def claim(item):
            if item.id == 2:
                state["claimed"] = True
                GLib.timeout_add(150, lambda: (engine._eject_deck(lab.deck_of(2).slot)
                                               if lab.deck_of(2) is not None else None, False)[1])
            return original(item)
        engine._claim_playback_occurrence = claim
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.5, 10.0))
        tr = lab.trace()
        report("B7-owner-retired", claims=lab.claims, third=tr.onset(THIRD, after=a_seg - 1.0))
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "a late answer for a retired owner airs nothing")
        self.assertEqual(self.air_starts(lab, 2) if hasattr(self, "air_starts") else [], [])
        self.assertIn(3, lab.claims, "the Eject replacement plays")

    def test_engine_shutdown_with_outstanding_lookups_is_bounded(self):
        gate = threading.Event()

        def wrap(resolve):
            def resolver(key, payload):
                gate.wait(5.0)
                return resolve(key, payload)
            return resolver
        lab = IntegrationLab(self)
        engine = lab.engine
        service = self.threaded(lab, wrap=wrap)
        service.request(("vt", "intro", 1), None)
        service.request(("config",))
        started = time.monotonic()
        engine._vtt_shutdown()
        elapsed = time.monotonic() - started
        gate.set()
        report("B7-engine-shutdown", seconds=elapsed)
        self.assertLess(elapsed, 0.8, "engine stop never waits on the lookup worker")
        self.assertFalse(service.request(("vt", "intro", 2), None), "no new work after shutdown")
        self.assertIsNone(engine.__dict__.get("_vtt"))

    def test_an_unavailable_worker_never_prevents_ordinary_playout(self):
        lab, out, inc, deck, zero = self.build_threaded(intro_vt=1.2)
        engine = lab.engine
        service = engine._vtt_lookups()
        service.unavailable = True                 # restarts exhausted
        a_seg = zero + 8.0
        self.assertTrue(wait_until(lambda: 2 in lab.claims, 12.0))
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.2, 6.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        report("B7-worker-unavailable", music_vs_segue=music_on - a_seg)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0))
        self.assertIsNotNone(music_on)
        self.assertLess(music_on - a_seg, eng_module.POSITION_POLL_MS / 1000.0 + SILENCE_PRIME + 0.25 + 0.06,
                        "the ordinary r0144 segue (poll trigger + prime + Cue In)")


class GlibCallbackAuditTests(SimpleTestCase):
    """No VoiceTrack / VoiceTrackConfig database access and no VoiceTrack
    media resolution outside the lookup worker's resolver (F13)."""

    WORKER_SIDE = {"_vtt_lookup_voicetrack", "_vtt_config_read", "_vtt_resolve_lookup", "_vt_resolve_audio",
                   # B2 infrastructure kept for its own tests; no engine caller (asserted below).
                   "_vt2_prepare_fire_for_voicetrack"}

    def test_voicetrack_reads_happen_only_on_the_lookup_worker(self):
        source = Path(eng_module.__file__).read_text()
        tree = ast.parse(source)
        offenders, callers = [], {}
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            for fn in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(fn):
                    text = None
                    if isinstance(node, ast.Attribute):
                        if isinstance(node.value, ast.Name) and node.value.id in ("VoiceTrack", "VoiceTrackConfig") \
                                and node.attr in ("objects", "load"):
                            text = f"{node.value.id}.{node.attr}"
                        elif node.attr in self.WORKER_SIDE:
                            callers.setdefault(node.attr, set()).add(fn.name)
                            continue
                    if text and fn.name not in self.WORKER_SIDE:
                        offenders.append((fn.name, text))
        report("B7-glib-audit", offenders=len(offenders),
               callers=";".join(f"{k}<-{','.join(sorted(v))}" for k, v in sorted(callers.items())))
        self.assertEqual(offenders, [])
        self.assertEqual(callers.get("_vtt_lookup_voicetrack"), {"_vtt_resolve_lookup"})
        self.assertEqual(callers.get("_vtt_config_read"), {"_vtt_resolve_lookup"})
        self.assertEqual(callers.get("_vt_resolve_audio"), {"_vtt_lookup_voicetrack",
                                                           "_vt2_prepare_fire_for_voicetrack"})
        self.assertNotIn("_vt2_prepare_fire_for_voicetrack", callers, "no engine caller")
        self.assertEqual(callers.get("_vtt_resolve_lookup"), {"_vtt_lookups"})


# ---------------------------------------------------------------------------
# Section 9: engine_state.json "vt_transition" compatibility
# ---------------------------------------------------------------------------
R0144_STATE_KEYS = {
    "date", "deck_recovery", "decks", "engine_session_id", "fx_fires", "hour", "log_id", "manual_from_mic",
    "manual_mode", "mic_configured", "mic_live", "mic_ok", "mic_recovery", "output_recovery",
    "ptt_auto_manual_enabled", "queue", "queue_cursor", "queue_resume_cursor", "queue_resume_next_log_item_id",
    "remote_dj_admin_result", "remote_dj_configured", "remote_dj_connected", "remote_dj_connection",
    "remote_dj_live", "remote_dj_owner", "remote_dj_quality", "remote_dj_reconnecting", "remote_dj_slot",
    "resume", "runtime_commit", "schedule_authority", "start_next", "state_revision", "timestamp",
    "total_items", "transport",
}


class StateCompatibilityTests(_B5Base, SimpleTestCase):
    def assert_safe(self, lab, state):
        text = json.dumps(state, sort_keys=True)
        self.assertNotIn(str(lab.root), text, "no media paths")
        for needle in ("/srv", "/opt", "/home", "/tmp", ".wav", ".flac", "password", "secret", "SECRET"):
            self.assertNotIn(needle, text)
        return text

    def test_terminal_outcomes_never_remain_active_and_worker_health_is_populated(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.0, 10.0))
        pump(0.5)
        state = lab.engine._vtt_state()
        self.assert_safe(lab, state)
        report("B7-state-terminal", active=state.get("active"), phase=state.get("phase"),
               recent=len(state.get("recent") or []), lookups=bool((state.get("workers") or {}).get("lookups")))
        self.assertEqual(state["version"], 1)
        self.assertFalse(state["active"], "a finished transition is never published as active")
        self.assertIsNone(state["current"])
        self.assertEqual(state["recent"][0]["finish_reason"], "complete")
        self.assertEqual(state["recent"][0]["intro"], "completed")
        self.assertEqual(state["phase"], "idle")
        self.assertFalse(state["transport_busy"])
        lookups = state["workers"]["lookups"]
        self.assertIsNotNone(lookups, "the f2f7f33 health correction: populated, never null")
        for key in ("alive", "unavailable", "queue_depth", "inflight", "restarts"):
            self.assertIn(key, lookups)
        self.assertTrue(lookups["alive"])
        self.assertFalse(lookups["unavailable"])

    def test_planned_and_actual_are_distinguishable_and_a_pending_fallback_is_published(self):
        lab, out, inc, deck, zero = _OutroBase.scenario(self, out_len=10.4, next_start=8.5, outro_at=8.0,
                                                        vt_len=2.6, poll=False)
        _stall_first_readiness(lab.engine)
        seen = {}

        def capture():
            seen["state"] = lab.engine._vtt_state()
        _after_fallback(lab, capture)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: "state" in seen, 14.0))
        state = seen["state"]
        self.assert_safe(lab, state)
        current = state["current"]
        held = state["held_occurrence"]
        report("B7-state-fallback", stage=current["stage"], held=held)
        self.assertEqual(current["stage"], "fallback_pending")
        self.assertEqual(held["log_item_id"], 2)
        self.assertTrue(held["automatic_start_pending"])
        self.assertTrue(held["reason"].startswith("fallback:"))
        outro = current["outro"]
        self.assertIsNotNone(outro["planned_start_at"])
        self.assertIsNotNone(outro["started_at"], "the outro really aired: actual start published")
        self.assertIsNone(current["intro"].get("started_at"), "no intro speech: no actual start")

    def test_a_finished_transition_never_publishes_a_side_as_planned(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.2)
        engine = lab.engine
        self.assertTrue(wait_until(lambda: lab.record() is not None and lab.record().plan is not None
                                   and lab.record().plan.intro_start is not None, 10.0))
        self.assertFalse(lab.record().committed, "planned, not yet committed")
        engine._vtt_abort("b7_state_probe")
        state = engine._vtt_state()
        recent = state["recent"][0]
        report("B7-state-not-attempted", intro=recent["intro"], finish=recent["finish_reason"])
        self.assertEqual(recent["intro"], "not_attempted")
        self.assertNotIn("planned", [r["intro"] for r in state["recent"]] + [r["outro"] for r in state["recent"]])

    def test_top_level_state_keys_are_a_superset_of_r0144(self):
        """The keys of the ``state = {...}`` literal json.dump'ed by
        _write_state: every r0144 key remains; the only new one is the
        additive ``vt_transition`` block (no old reader reads a removed key)."""
        tree = ast.parse(Path(eng_module.__file__).read_text())
        keys = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_write_state":
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Dict) \
                            and any(isinstance(t, ast.Name) and t.id == "state" for t in inner.targets):
                        keys = {k.value for k in inner.value.keys if isinstance(k, ast.Constant)}
        self.assertIsNotNone(keys)
        self.assertTrue(R0144_STATE_KEYS <= keys, sorted(R0144_STATE_KEYS - keys))
        self.assertEqual(keys - R0144_STATE_KEYS, {"vt_transition"}, "additive only")


# ---------------------------------------------------------------------------
# PostgreSQL: a failed scheduled admission keeps occurrence identity and
# records exactly one play, at the fallback's real first audio
# ---------------------------------------------------------------------------
from library.tests import test_vt_b4_intro_integration as _b4  # noqa: E402


class FallbackAccountingDbTests(_b4.IntegratedAccountingDbTests):
    # The inherited B4 test runs in its own module, not twice.
    test_swap_adopted_at_claim_and_played_at_only_at_the_admission = None

    def test_readiness_miss_falls_back_with_one_claim_and_one_play_event(self):
        from library.models import LogItem, PlayEvent, VoiceTrackConfig
        VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 100,
                                                                 "min_gap_ms": 200})
        lab = self.real_lab()
        engine = lab.engine
        out_track = self.track(lab, "b7db-out", [(9.0, OUT)], next_start_seconds=8.0)
        in_track = self.track(lab, "b7db-in", music(0.25, 4.25), cue_in_seconds=0.25, intro_until_seconds=4.25)
        self.voicetrack(lab, in_track, "b7db-in-vt", 1.0, SPEECH)
        out_item, in_item = self.item(out_track), self.item(in_track)
        engine.log_items = [out_item, in_item]
        engine._queue_cursor = 0
        _stall_first_readiness(engine)
        failed = _after_fallback(lab, lambda: None)
        lab.play()
        engine._start_next_track(slot="A")
        zero = engine._vtt_media_zero_ns(engine.decks["A"]) / NS
        a_seg = zero + 8.0
        wall0, run0 = time.time(), lab.now()
        self.assertTrue(wait_until(lambda: LogItem.objects.get(pk=in_item.pk).playback_claimed_at is not None, 10.0))
        claimed_at = LogItem.objects.get(pk=in_item.pk).playback_claimed_at
        self.assertTrue(wait_until(lambda: PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).exists(), 6.0))
        pump(2.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        row = LogItem.objects.get(pk=in_item.pk)
        events = PlayEvent.objects.filter(log_item_id_snapshot=in_item.id)
        expected_wall = wall0 + (music_on - 0.25 - run0)       # media 0 = Cue In - 0.25 s
        report("B7-db-fallback", failed_before_segue=a_seg - failed[0], music_vs_segue=music_on - a_seg,
               played_at_vs_media0=row.played_at.timestamp() - expected_wall, play_events=events.count(),
               claim_unchanged=row.playback_claimed_at == claimed_at)
        self.assertTrue(failed, "the readiness miss happened")
        self.assertEqual(events.count(), 1, "exactly one play")
        self.assertEqual(row.playback_claimed_at, claimed_at, "claim kept (idempotent re-claim)")
        # r0144 ordinary-start accounting (inherited, unchanged): the wall
        # clock when the FIRST REAL buffer is observed in the deck's streaming
        # thread -- about one silence prime ahead of its render. The fallback
        # must record exactly that, once, after the failure.
        failed_wall = wall0 + (failed[0] - run0)
        self.assertGreater(row.played_at.timestamp(), failed_wall, "never at the failed admission")
        self.assertLessEqual(abs(row.played_at.timestamp() - (expected_wall - eng_module.SILENCE_PRIME_SECONDS)),
                             0.05, "the fallback's own first real buffer (r0144 ordinary evidence)")
        self.assertEqual(events.get().started_at, row.played_at)
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 1.0), "no intro speech without viability")
