"""Roadmap 2.25 B7 supplemental -- held-occurrence and urgent authority.

The C1 deferred fallback (a claimed occurrence held with a token-guarded
start) must not survive, and must dispose of its occurrence exactly once
across: engine shutdown (C8), hour adoption, a queue reload that replaces
the in-memory occurrences, a dedication intro with its follow-up song, and a
pending Weather/AMBER announcement through a reload. Real engine transition
path, rendered program PCM (see test_vt_b7_qualification for the lab).

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from library.tests import test_vt_b7_qualification as _b7
from library.tests._vt_integration_lab import (
    DEDICATION, HEAD, MUSIC, OUTRO_SPEECH, SPEECH, URGENT, pump, report, wait_until,
)
from library.tests.test_vt_b3_outro_integration import _OutroBase
from library.tests.test_vt_b5_operator_integration import THIRD, _B5Base


class _HeldBase(_B5Base):
    def outro_hold(self, **kwargs):
        lab, out, inc, deck, zero = _OutroBase.scenario(self, out_len=10.4, next_start=8.5, outro_at=8.0,
                                                        vt_len=2.6, poll=False, **kwargs)
        lab.engine.log_items.append(lab.item(3, [(5.0, THIRD)], name="b7s-third"))
        _b7._stall_first_readiness(lab.engine)
        return lab, zero


class HeldAuthorityTests(_HeldBase, SimpleTestCase):
    def test_engine_shutdown_cancels_a_pending_fallback_start(self):
        lab, zero = self.outro_hold()
        engine = lab.engine
        state = {}

        attempts = []
        real_attempt = engine._vtt_fallback_attempt

        def counted(*args, **kwargs):
            attempts.append(lab.now())
            return real_attempt(*args, **kwargs)
        engine._vtt_fallback_attempt = counted

        def shutdown():
            # engine.stop(): the pipeline goes to NULL, then _vtt_shutdown.
            state["token_before"] = engine._vtt_held_occurrence.get("fallback_token") is not None
            engine.running = False
            lab.lab.stop()
            engine._vtt_shutdown()
            state["token_after"] = engine._vtt_held_occurrence.get("fallback_token") is not None
            state["at"] = lab.now()
        _b7._after_fallback(lab, shutdown)
        lab.poll()
        self.assertTrue(wait_until(lambda: "token_after" in state, 14.0))
        pump(1.5)                                   # well past the deferred due time
        report("B7S-held-shutdown", token_before=state["token_before"], token_after=state["token_after"],
               attempts_after_shutdown=len(attempts), decks=[d.log_item.id for d in engine.decks.values() if d])
        self.assertLessEqual(len(attempts), 1, "at most the timer's own no-op wake-up")
        self.assertTrue(state["token_before"])
        self.assertFalse(state["token_after"], "C8: the deferred start dies with the engine")
        self.assertEqual(self.air_starts(lab, 2), [], "no deck opened on a stopped engine")
        self.assertNotIn(2, [d.log_item.id for d in engine.decks.values() if d is not None])

    def test_hour_adoption_during_a_pending_fallback_airs_the_claim_once_then_the_new_hour(self):
        lab, zero = self.outro_hold()
        engine = lab.engine
        new_first = lab.item(70, [(4.0, DEDICATION)], name="b7s-new-hour-first")
        new_log = SimpleNamespace(id=7070, date=engine.current_log.date if engine.current_log else None, hour=23,
                                  pk=7070)

        def adopt():
            # The real _advance_to_next_hour_log swap: the next hour's queue
            # replaces the active one; decks (and the held claim) are untouched.
            engine.current_log = new_log
            engine.log_items = [new_first]
            engine._queue_cursor = 0
        at = _b7._after_fallback(lab, adopt)
        lab.poll()
        self.assertTrue(wait_until(lambda: 70 in lab.claims, 30.0))
        pump(1.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7S-held-adoption", claims=lab.claims, music_vs_plan=music_on - (zero + 10.6))
        self.assertEqual(len(self.air_starts(lab, 2)), 1, "the claimed occurrence airs exactly once")
        self.assertLess(abs(music_on - (zero + 10.6)), 0.05)
        self.assertNotIn(3, lab.claims, "the replaced hour's next item never airs")
        self.assertLess(lab.claims.index(2), lab.claims.index(70), "then the adopted hour")
        self.assertIsNotNone(at)

    def test_queue_reload_replacing_occurrences_during_a_pending_fallback_never_duplicates(self):
        lab, zero = self.outro_hold()
        engine = lab.engine

        def replace_objects():
            # A 3-second queue reload re-reads the log: new in-memory objects
            # for the SAME occurrences (the held claim keeps its own object).
            fresh = []
            for item in engine.log_items:
                clone = lab.item(item.id, [(5.0, THIRD)], track_id=item.track.id + 50000,
                                 name=f"b7s-reloaded-{item.id}")
                clone.playback_claimed_at = item.playback_claimed_at
                fresh.append(clone)
            engine.log_items = fresh
        _b7._after_fallback(lab, replace_objects)
        lab.poll()
        self.wait_claims(lab, [1, 2], timeout=14.0)
        self.assertTrue(wait_until(lambda: lab.now() > zero + 12.0, 14.0))
        report("B7S-held-reload-objects", claims=lab.claims)
        self.assertEqual(len(self.air_starts(lab, 2)), 1)
        self.assertEqual(lab.claims.count(2), 2, "the claim + its idempotent fallback re-claim, nothing more")


class DedicationAndAlertTests(_HeldBase, SimpleTestCase):
    def test_dedication_intro_recovered_by_fallback_is_followed_by_its_song_once(self):
        lab, zero = self.outro_hold()
        engine = lab.engine
        intro = lab.item(60, [(3.0, DEDICATION)], track_id=4004, name="b7s-dedication-intro")
        intro.category_id = 99
        intro.category = SimpleNamespace(code="Dedications")
        song = engine.log_items[1]                    # the scheduled song (item 2)
        handed = {}

        def splice(item):
            if item.id == 2 and not handed:
                handed["done"] = True
                engine._forced_next_items.append(song)    # the follow-up waits behind its intro
                return intro
            return None
        engine._maybe_insert_dedication_intro = splice
        engine._restore_followup_for_intro = lambda _item: True
        lab.poll()
        self.assertTrue(wait_until(lambda: lab.claims.count(2) >= 1 and 60 in lab.claims, 16.0))
        self.assertTrue(wait_until(lambda: lab.now() > zero + 16.0, 12.0))
        tr = lab.trace()
        intro_on = tr.onset(DEDICATION, after=zero + 9.0)
        song_on = tr.onset(MUSIC, after=zero + 9.0)
        report("B7S-dedication-fallback", claims=lab.claims, intro_on=None if intro_on is None else intro_on - zero,
               song_on=None if song_on is None else song_on - zero)
        self.assertEqual(len(self.air_starts(lab, 60)), 1, "the intro airs once (fallback of its failed deck)")
        self.assertEqual(len(self.air_starts(lab, 2)), 1, "its follow-up song airs once, after it")
        self.assertLess(intro_on, song_on)
        self.assertNotIn(3, lab.claims[:lab.claims.index(2) + 1])

    def test_pending_alert_through_a_reload_during_a_vt_transition_airs_once_without_speech(self):
        lab, out, inc, deck, zero = self.underlap()
        engine = lab.engine
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + 0.3, 10.0))
        alert = lab.item(50, [(2.0, URGENT)], name="b7s-wx-pending")
        alert.category_id = 77
        alert.category = SimpleNamespace(code="WxAlert")
        engine._forced_next_items.append(alert)            # pending, unclaimed announcement
        engine._vtt_mark_urgent(alert)
        engine._vtt_queue_changed("urgent_insert")
        replacement = lab.item(9, [(5.0, THIRD)], name="b7s-wx-reload")
        engine._load_log_for = lambda *_a: (setattr(engine, "log_items", [replacement]),
                                            setattr(engine, "_queue_cursor", 0))
        command_at = lab.now()
        engine._reload_and_restart_current_log()
        self.assertTrue(wait_until(lambda: 50 in lab.claims, 6.0))
        pump(2.5)
        tr = lab.trace()
        report("B7S-wx-reload", claims=lab.claims)
        self.assertEqual(lab.claims.count(50), 1, "the pending announcement airs once")
        self.assertIsNone(tr.onset(HEAD, after=command_at + 0.1), "no VT speech over it")
        self.assertLess(tr.present(SPEECH, command_at + 0.1, lab.now()), 0.011)
        self.assertLess(tr.present(OUTRO_SPEECH, command_at + 0.1, lab.now()), 0.011)
        self.assertEqual(self.air_starts(lab, 2), [], "the replaced program's held song never airs")


class StartNextIrreversibleWindowTests(_HeldBase, SimpleTestCase):
    """C3 under C7: the window in which a waiting deck can no longer be
    recalled is now ~30 ms wide, so it is entered deterministically here (the
    deck mixer has pushed past the admission): with the outgoing gone, Start
    Next must keep that song's admission -- never fade it as an outgoing and
    start the following item."""

    def test_start_next_with_only_an_irreversible_waiting_song_keeps_it(self):
        from library.tests.test_vt_b3_outro_integration import _OutroBase as _O
        lab, out, inc, deck, zero = _O.scenario(self, out_len=9.0, next_start=8.5, outro_at=8.0)
        engine = lab.engine
        engine.log_items.append(lab.item(3, [(5.0, THIRD)], name="b7s-irreversible-third"))
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 9.2, 12.0))       # outgoing ended
        held = lab.deck_of(2)
        self.assertIsNotNone(held)
        self.assertEqual(held.admission_state, "scheduled")
        # The deck mixer has pushed output covering the admission (C7 evidence).
        engine._vt2_mixer_pushed_ns = held.scheduled_admission_ns
        self.assertTrue(engine._vtt_incoming_on_air(held))
        self.assertEqual([d.log_item.id for d in engine._live_decks()], [2])
        command = self.start_next(lab, fade_ms=400)
        outcome = self.settle_start_next(lab)
        pump(1.5)
        report("B7S-irreversible-start-next", mode=outcome["mode"], claims=lab.claims)
        self.assertEqual(outcome["outcome"], "completed")
        self.assertEqual(outcome["mode"], "vt_admit", "never treated as the outgoing")
        self.assertEqual(len(self.air_starts(lab, 2)), 1, "the claimed song airs exactly once")
        self.assertNotIn(3, lab.claims, "the claimed song is not skipped for the following item")
        self.assertIsNotNone(command)
