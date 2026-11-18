"""Roadmap 2.25 B4 -- independent incoming intro VT playback, integrated.

The real engine transition path (production 250 ms ``_poll_position`` tick,
``_start_next_track`` -> ``_next_queue_item`` -> ``_create_deck`` -> claim ->
``_vtt_admission_for_claim``, B2 scheduled decks and fires, the duck curve),
measured in rendered program PCM (sample-accurate onsets/ends of
tone-identified signals). The last class runs the claim, Swap adoption and
playback accounting against PostgreSQL.

Signals: 400 Hz outgoing music, 600 Hz incoming music (Cue In .. Intro
Until), 1400 Hz incoming vocal (from Intro Until), 2200 Hz VT head marker,
1000 Hz intro speech, 2600 Hz VT tail marker, 900 Hz outro speech, 1700 Hz a
different (foreign) occurrence's intro speech, 300 Hz urgent item, 1100 Hz
request, 800 Hz dedication.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

import library.services.engine as eng_module
from library.services import vt_transition
from library.services.vt_audio import FireState
from library.services.vt_transition import ItemRole, Reason, StartKind
from library.tests._vt_b0_lab import GLib, Gst
from library.tests._vt_integration_lab import (
    CONFIGS, DEDICATION, FOREIGN, HEAD, MUSIC, NS, OUT, OUTRO_SPEECH, REQUEST, SPEECH, TAIL, TOL, URGENT,
    VOCAL, IntegrationLab, fake_voicetrack, pump, report, unplayable_voicetrack, vt_segments, wait_until,
)

M = vt_transition.VOCAL_SAFETY_MARGIN_S
RAMP = 0.100

# Time-compressed KOGR 2026-11-16 shape: no outgoing Outro Starts, the
# incident's exact incoming Cue In / Intro Until and intro VT length.
INC_CUE, INC_INTRO, INC_VT = 0.124717, 15.008, 6.456229


def music(cue, intro_until, *, after=1.0, freq=MUSIC):
    return ([(cue, 0)] if cue > 0 else []) + [(intro_until - cue, freq), (after, VOCAL)]


class _IntroBase:
    def build(self, *, rate=44100, fmt="wav", out_len=9.0, out_next=8.0, out_outro=None, outro_vt=None,
              in_cue=0.25, in_intro=4.25, in_after=1.0, intro_vt=1.2, in_id=2, extra=(), gap_ms=200,
              poll=True, out_segments=None, intro_speech=SPEECH):
        lab = IntegrationLab(self, rate=rate, ramp_ms=int(RAMP * 1000), gap_ms=gap_ms)
        out = lab.item(1, out_segments or [(out_len, OUT)], next_start=out_next, outro_starts=out_outro,
                       name=f"out-{out_len}-{rate}-{out_segments is not None}")
        inc = lab.item(in_id, music(in_cue, in_intro, after=in_after) if in_intro else
                       ([(in_cue, 0)] if in_cue else []) + [(5.0, MUSIC)],
                       cue_in=in_cue, intro_until=in_intro or 0.0, fmt=fmt,
                       name=f"in-{in_cue}-{in_intro}-{in_after}-{fmt}-{rate}")
        lab.queue(out, inc, *extra)
        if outro_vt:
            path = lab.vt(f"outro-{outro_vt}-{fmt}-{rate}", outro_vt, fmt=fmt, speech=OUTRO_SPEECH)
            lab.attach(out, "outro", fake_voicetrack(path, outro_vt))
        if intro_vt:
            path = lab.vt(f"intro-{intro_vt}-{fmt}-{rate}-{intro_speech}", intro_vt, fmt=fmt, speech=intro_speech)
            lab.attach(inc, "intro", fake_voicetrack(path, intro_vt))
        if poll:
            lab.poll()
        lab.play()
        deck = lab.start("A")
        return lab, out, inc, deck, lab.media_zero(deck)

    def wait_claims(self, lab, claims, timeout=18.0):
        self.assertTrue(wait_until(lambda: lab.claims[:len(claims)] == claims, timeout), f"claims {lab.claims}")

    def measure(self, lab, after):
        tr = lab.trace()
        head = tr.onset(HEAD, after=after)
        tail_on = tr.onset(TAIL, after=head + 0.2) if head is not None else None
        tail = tr.end(TAIL, after=tail_on + 0.05) if tail_on is not None else None
        return tr, head, tail, tr.onset(MUSIC, after=after), tr.onset(VOCAL, after=after)

    def intro_fire(self, lab):
        record = lab.last_record()
        return None if record is None else record.intro_fire


class IncidentIntegrationTests(_IntroBase, SimpleTestCase):
    """F1 (the KOGR incident) as a permanent end-to-end regression: an
    outgoing WITHOUT Outro Starts; the incoming's valid intro VT airs at the
    segue (A_seg) with its music (U = 0), exactly, the whole recording."""

    def _run(self, rate, fmt, *, out_outro=None):
        lab, out, inc, deck, zero = self.build(rate=rate, fmt=fmt, in_cue=INC_CUE, in_intro=INC_INTRO,
                                               in_after=0.2, intro_vt=INC_VT, out_outro=out_outro)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + INC_VT + 0.4, 20.0))
        tr, head, tail, music_on, _vocal = self.measure(lab, a_seg - 1.0)
        fire = self.intro_fire(lab)
        plan = lab.last_record().plan
        report("B4-incident", rate=rate, fmt=fmt, outgoing_outro=out_outro, vt_onset_error=head - a_seg,
               music_onset_error=music_on - a_seg, vt_end_error=tail - (a_seg + INC_VT),
               vt_rendered=tail - head, underlap=plan.underlap, clearance=plan.clearance,
               speechless_duck=tr.level(OUT, zero + 1.0, a_seg - RAMP - 0.05))
        self.assertEqual(fire.state, FireState.ENDED)
        self.assertEqual((fire.owner.log_item_id, fire.owner.track_id), (2, inc.track.id))
        self.assertLess(abs(head - a_seg), TOL, "intro VT at the segue (F1 fixed)")
        self.assertLess(abs(music_on - a_seg), TOL, "music with the speech (D1, U = 0)")
        self.assertLess(abs(tail - (a_seg + INC_VT)), TOL, "whole recording (F14)")
        self.assertEqual(plan.underlap, 0.0)
        self.assertAlmostEqual(plan.clearance, 8.427054, places=6)
        self.assertGreater(tr.level(OUT, zero + 1.0, a_seg - RAMP - 0.05), 0.37, "no speechless duck (F5)")
        self.assertEqual(lab.claims, [1, 2])
        self.assertIn("committed", lab.event_names())

    def test_incident_44100_wav(self):
        self._run(*CONFIGS[0])

    def test_incident_44100_flac(self):
        self._run(*CONFIGS[1])

    def test_incident_48000_wav(self):
        self._run(*CONFIGS[2])

    def test_incident_48000_flac(self):
        self._run(*CONFIGS[3])

    def test_control_an_outgoing_outro_marker_changes_nothing_on_the_intro_side(self):
        self._run(44100, "wav", out_outro=6.0)


class IntroTimingTests(_IntroBase, SimpleTestCase):
    def test_short_outgoing_item_still_airs_the_next_intro_vt_exactly(self):
        # F8: a 3 s ID (no Outro Starts, under the 5 s poll gate).
        lab, out, inc, deck, zero = self.build(out_len=3.0, out_next=None, intro_vt=1.0)
        a_seg = zero + 3.0
        self.wait_claims(lab, [1, 2], 8.0)
        pump(2.0)
        tr, head, tail, music_on, _v = self.measure(lab, a_seg - 1.0)
        report("B4-short-outgoing", vt_onset_error=head - a_seg, music_onset_error=music_on - a_seg)
        self.assertLess(abs(head - a_seg), TOL)
        self.assertLess(abs(music_on - a_seg), TOL)

    def test_long_intro_vt_underlap_keeps_the_vocal_clear_by_exactly_the_margin(self):
        # D2: V 3.0, Cue In 0.25, Intro Until 1.75 -> W 1.5, U = 3.0 - 1.5 + 0.15 = 1.65.
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, in_after=2.0, intro_vt=3.0)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 3.6, 20.0))
        tr, head, tail, music_on, vocal_on = self.measure(lab, a_seg - 1.0)
        u = 3.0 - 1.5 + M
        report("B4-underlap", vt_onset_error=head - a_seg, underlap_measured=music_on - head,
               underlap_planned=u, clearance=vocal_on - tail, speech_end_error=tail - (a_seg + 3.0))
        self.assertLess(abs(head - a_seg), TOL, "speech first, at the segue")
        self.assertLess(abs((music_on - head) - u), TOL, "music delayed by U")
        self.assertLess(abs((vocal_on - tail) - M), TOL, "speech ends exactly M before the vocal")
        self.assertLess(tr.present(MUSIC, a_seg, music_on - 0.02), 0.011, "no music before A_I")

    def test_missing_intro_until_plays_the_whole_vt_then_the_margin_then_the_song(self):
        # D10: no usable window -> U = V + M.
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=None, intro_vt=1.2)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 2.2, 20.0))
        tr, head, tail, music_on, _v = self.measure(lab, a_seg - 1.0)
        report("B4-no-window", vt_onset_error=head - a_seg, music_after_speech_end=music_on - tail)
        self.assertLess(abs(head - a_seg), TOL)
        self.assertLess(abs((music_on - tail) - M), TOL)

    def test_combined_outro_then_intro_with_the_gap_and_no_overlap(self):
        # Outro 7.0..8.6 holds the segue (8.0); intro at 8.6 + gap 0.2; music with it (U = 0).
        lab, out, inc, deck, zero = self.build(out_outro=7.0, outro_vt=1.6, in_intro=4.25, intro_vt=1.0)
        o_s, o_end = zero + 7.0, zero + 8.6
        s_v = o_end + 0.2
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > s_v + 1.5, 20.0))
        tr = lab.trace()
        outro_head = tr.onset(HEAD, after=o_s - 0.5)
        outro_tail = tr.end(TAIL, after=o_end - 0.1)
        intro_head = tr.onset(HEAD, after=o_end + 0.05)
        intro_tail = tr.end(TAIL, after=tr.onset(TAIL, after=intro_head + 0.2) + 0.05)
        music_on = tr.onset(MUSIC, after=o_s)
        report("B4-combined", outro_onset_error=outro_head - o_s, separation=intro_head - outro_tail,
               intro_onset_error=intro_head - s_v, music_onset_error=music_on - s_v,
               intro_end_error=intro_tail - (s_v + 1.0))
        self.assertLess(abs(outro_head - o_s), TOL)
        self.assertLess(abs(outro_tail - o_end), TOL)
        self.assertLess(abs((intro_head - outro_tail) - 0.2), TOL, "min_gap_ms between the two voices")
        self.assertLess(abs(music_on - s_v), TOL)
        self.assertLess(tr.present(OUTRO_SPEECH, intro_head, intro_head + 0.8), 0.011, "no overlapping voices")
        self.assertEqual(lab.claims, [1, 2])

    def test_combined_without_hold_keeps_the_segue_for_the_intro(self):
        # Outro 5.0..6.6 ends before the segue: the intro VT is at A_seg itself.
        lab, out, inc, deck, zero = self.build(out_outro=5.0, outro_vt=1.6, intro_vt=1.0)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 1.5, 20.0))
        tr = lab.trace()
        intro_head = tr.onset(HEAD, after=zero + 6.8)
        report("B4-combined-no-hold", intro_onset_error=intro_head - a_seg,
               ducked_between=tr.level(OUT, zero + 6.6 + RAMP + 0.05, a_seg - RAMP - 0.05))
        self.assertLess(abs(intro_head - a_seg), TOL)
        self.assertGreater(tr.level(OUT, zero + 6.6 + RAMP + 0.05, a_seg - RAMP - 0.05), 0.37,
                           "duck released between the two voices (intervals 1.4 s apart)")


class IntroFailureTests(_IntroBase, SimpleTestCase):
    def _corrupt(self, lab, inc, length):
        path = lab.root / f"corrupt-intro-{length}.flac"
        path.write_bytes(b"\x00\xffnot audio\x13\x37" * 4096)
        lab.attach(inc, "intro", fake_voicetrack(path, length))

    def test_corrupt_intro_vt_with_underlap_readmits_the_held_music_at_once(self):
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=None, poll=False)
        self._corrupt(lab, inc, 3.0)
        lab.poll()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        pump(2.5)
        tr = lab.trace()
        record = lab.last_record()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        failed_at = next(t for t, name, _d in record.events if name == "intro_failed")
        report("B4-corrupt-intro-readmit", music_vs_failure=music_on - failed_at,
               original_music_at=a_seg + 1.65 - a_seg, music_vs_segue=music_on - a_seg)
        self.assertIn("intro_failed", lab.event_names(record))
        self.assertIn("readmit", lab.event_names(record))
        self.assertLess(music_on - failed_at, 0.5, "no dead air for U (held music re-admitted)")
        self.assertLess(music_on, a_seg + 1.65 - 0.5)
        self.assertLess(tr.present(SPEECH, a_seg - 1.0, lab.now()), 0.011)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)
        starts = [a for a in lab.air_starts if a["log_item_id"] == 2]
        self.assertEqual(len(starts), 1, "one air start (the regenerated generation)")
        self.assertEqual(lab.claims.count(2), 2, "same occurrence re-opened (idempotent claim in PostgreSQL)")

    def test_corrupt_intro_vt_without_underlap_leaves_the_music_exact(self):
        lab, out, inc, deck, zero = self.build(intro_vt=None, poll=False)
        self._corrupt(lab, inc, 1.0)
        lab.poll()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        pump(2.0)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        self.assertLess(abs(music_on - (a_seg + 0.0)), TOL)
        self.assertEqual(self.intro_fire(lab).state, FireState.FAILED)
        self.assertGreater(tr.level(MUSIC, a_seg + 0.1, a_seg + 0.9), 0.37,
                           "a failed VT never ducks the music (its planned duck is released)")
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)

    def test_failed_incoming_preparation_falls_back_to_an_ordinary_start_of_the_same_occurrence(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.0, poll=False)
        engine = lab.engine
        real_ready = engine._vt2_on_scheduled_deck_ready
        dropped = []

        def stall_first(slot, generation):
            if not dropped:
                dropped.append((slot, generation))     # this decode never reports readiness
                return False
            return real_ready(slot, generation)
        engine._vt2_on_scheduled_deck_ready = stall_first
        lab.poll()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        pump(2.5)
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.5)
        record = lab.last_record()
        report("B4-deck-fallback", music_vs_segue=music_on - a_seg,
               outgoing_continuity=tr.present(OUT, zero + 7.0, zero + 8.9) / 1.9)
        self.assertIn("fallback_ordinary", lab.event_names(record))
        self.assertLess(tr.present(SPEECH, a_seg - 1.0, lab.now()), 0.011, "no intro speech without viability")
        self.assertEqual(lab.claims, [1, 2, 2], "same occurrence (claim idempotent); no second queue item")
        self.assertIsNone(lab.deck_of(2).scheduled_admission_ns, "ordinary primed fallback")
        self.assertGreater(tr.present(OUT, zero + 7.0, zero + 8.9), 1.85, "outgoing continuous (F15)")
        self.assertLess(abs(music_on - a_seg), 0.05, "the ordinary fallback's music lands at the plan")

    def test_failed_held_deck_reopening_keeps_the_original_admission_and_never_advances(self):
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=None, poll=False)
        self._corrupt(lab, inc, 3.0)
        engine = lab.engine
        original = engine._vtt_incoming_ready

        def remove_file_after_open(record, deck):
            # The held generation has its file open and decoded; the file then
            # disappears, so a fresh re-open cannot succeed.
            if Path(deck.log_item.track.filepath).exists():
                os.unlink(deck.log_item.track.filepath)
            return original(record, deck)
        engine._vtt_incoming_ready = remove_file_after_open
        lab.poll()
        a_seg = zero + 8.0
        a_i = a_seg + 3.0 - 1.5 + M
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() > a_i + 1.0, 20.0))
        tr = lab.trace()
        music_on = tr.onset(MUSIC, after=a_seg - 1.0)
        record = lab.last_record()
        readmits = [d for _t, name, d in record.events if name == "readmit"]
        report("B4-reopen-failed", music_vs_original_admission=music_on - a_i,
               readmit_outcomes=",".join(d["outcome"] for d in readmits))
        self.assertEqual([d["outcome"] for d in readmits], ["failed"], "one bounded attempt, no loop")
        self.assertLess(abs(music_on - a_i), TOL, "held generation kept: music at its original admission")
        self.assertEqual(lab.claims, [1, 2], "no queue advance, no replacement")
        self.assertTrue(record.done)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)


class AuthoritativeOccurrenceTests(_IntroBase, SimpleTestCase):
    """F2: the intro VT is the CLAIMED occurrence's, or none."""

    def test_swap_adopted_at_claim_airs_the_adopted_tracks_vt_not_the_predicted_one(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.0, poll=False)
        adopted = lab.item(2, music(0.25, 4.25), track_id=7007, name="adopted", intro_until=4.25, cue_in=0.25)
        foreign = lab.vt("adopted-vt", 1.0, speech=FOREIGN)
        lab.attach(adopted, "intro", fake_voicetrack(foreign, 1.0))
        engine = lab.engine
        claim = engine._claim_playback_occurrence

        def adopt_at_claim(item):
            if item.id == 2 and item.track.id != 7007:
                item.track = adopted.track      # a Swap committed after the queue copy was read
            return claim(item)
        engine._claim_playback_occurrence = adopt_at_claim
        lab.poll()
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        pump(2.0)
        tr = lab.trace()
        report("B4-swap", foreign_onset_error=tr.onset(FOREIGN, after=a_seg - 1) - (a_seg + 0.15),
               predicted_vt_seconds=tr.present(SPEECH, a_seg - 1, lab.now()))
        self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0), "never the predicted (lookahead) VT")
        self.assertGreater(tr.present(FOREIGN, a_seg, a_seg + 1.0), 0.6, "the adopted track's own VT")
        self.assertEqual(self.intro_fire(lab).owner.track_id, 7007)

    def test_request_and_dedication_replacements_never_inherit_the_scheduled_songs_vt(self):
        for label in ("request", "dedication"):
            with self.subTest(label):
                lab, out, inc, deck, zero = self.build(intro_vt=1.0, poll=False)
                engine = lab.engine
                if label == "request":
                    replacement = lab.item(2, [(5.0, REQUEST)], track_id=3003, name="request")
                    swap = patch.object(eng_module, "maybe_schedule_song_request", new=lambda _item: replacement)
                    swap.start()
                    self.addCleanup(swap.stop)
                    freq = REQUEST
                else:
                    replacement = lab.item(60, [(3.0, DEDICATION)], track_id=4004, name="dedication")
                    replacement.category_id = 99
                    replacement.category = SimpleNamespace(code="Dedications")
                    engine._maybe_insert_dedication_intro = lambda _item: replacement
                    engine._restore_followup_for_intro = lambda _item: True
                    freq = DEDICATION
                lab.poll()
                a_seg = zero + 8.0
                self.assertTrue(wait_until(lambda: len(lab.claims) >= 2, 14.0))
                pump(1.5)
                tr = lab.trace()
                on = tr.onset(freq, after=a_seg - 1.0)
                report(f"B4-{label}", onset_vs_segue=on - a_seg,
                       scheduled_vt_seconds=tr.present(SPEECH, a_seg - 1, lab.now()))
                self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0), "no song-2 VT (R3/R3b)")
                self.assertLess(abs(on - a_seg), TOL, "the committed transition still lands exactly")
                lab.lab.stop()
                lab.engine.running = False

    def test_urgent_inserted_before_commit_gets_no_vt_and_no_hold(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.0)
        urgent = lab.item(50, [(4.0, URGENT)], name="urgent")
        urgent.category_id = 77
        urgent.category = SimpleNamespace(code="WxAlert")
        self.assertTrue(wait_until(lambda: lab.now() >= zero + 5.0, 10.0))
        engine = lab.engine
        engine.log_items.insert(engine._queue_cursor, urgent)
        engine._vtt_mark_urgent(urgent)
        engine._vtt_queue_changed("urgent_insert")
        self.wait_claims(lab, [1, 50])
        pump(1.5)
        tr = lab.trace()
        alert_on = tr.onset(URGENT, after=zero + 7.0)
        report("B4-urgent-before-commit", alert_vs_segue=alert_on - (zero + 8.0))
        self.assertLess(tr.present(SPEECH, zero, lab.now()), 0.011, "no VT over the alert (R2 inverted)")
        self.assertLess(alert_on - (zero + 8.0), 1.2)

    def test_urgent_inserted_after_commit_follows_the_claimed_song_and_its_vt(self):
        lab, out, inc, deck, zero = self.build(intro_vt=1.0)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        urgent = lab.item(50, [(4.0, URGENT)], name="urgent-late")
        urgent.category_id = 77
        urgent.category = SimpleNamespace(code="WxAlert")
        engine = lab.engine
        engine.log_items.insert(engine._queue_cursor, urgent)
        engine._vtt_mark_urgent(urgent)
        engine._vtt_queue_changed("urgent_insert")
        pump(2.5)
        tr = lab.trace()
        self.assertLess(abs(tr.onset(HEAD, after=a_seg - 1) - a_seg), TOL, "the claimed song's own VT")
        self.assertEqual(lab.claims, [1, 2], "the urgent item airs after the claimed song (r0137)")
        self.assertEqual(engine._next_queue_item_id(), 50)

    def test_next_hour_adoption_airs_the_adopted_items_intro_vt(self):
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000))
        out = lab.item(1, [(9.0, OUT)], next_start=8.0, name="hour-out")
        nxt = lab.item(70, music(0.25, 4.25), cue_in=0.25, intro_until=4.25, name="hour-next")
        path = lab.vt("hour-intro", 1.0)
        lab.attach(nxt, "intro", fake_voicetrack(path, 1.0))
        lab.queue(out)
        engine = lab.engine
        engine._peek_next_hour = lambda: (SimpleNamespace(id=999), [nxt])

        def roll_over():
            if engine.log_items and engine.log_items[-1] is nxt:
                return False
            engine.log_items = [nxt]
            engine._queue_cursor = 0
            return True
        engine._roll_over_to_next_hour = roll_over
        lab.poll()
        lab.play()
        deck = lab.start("A")
        zero = lab.media_zero(deck)
        self.wait_claims(lab, [1, 70])
        pump(2.0)
        tr = lab.trace()
        head = tr.onset(HEAD, after=zero + 7.0)
        report("B4-next-hour", vt_onset_error=head - (zero + 8.0))
        self.assertLess(abs(head - (zero + 8.0)), TOL)
        self.assertEqual(self.intro_fire(lab).owner.log_item_id, 70)

    def test_two_occurrences_of_the_same_track_each_air_their_intro_vt(self):
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000))
        a = lab.item(1, [(6.0, OUT)], next_start=5.5, name="same-out")
        b = lab.item(2, music(0.0, 6.5, after=0.5), track_id=4343, intro_until=6.5, next_start=6.0, name="same-in")
        c = lab.item(3, music(0.0, 6.5, after=0.5), track_id=4343, intro_until=6.5, next_start=6.0, name="same-in")
        path = lab.vt("same-intro", 0.8)
        lab.attach(b, "intro", fake_voicetrack(path, 0.8))
        lab.queue(a, b, c)
        lab.poll()
        lab.play()
        lab.start("A")
        self.wait_claims(lab, [1, 2, 3], 22.0)
        pump(1.5)
        owners = [r.intro_fire.owner.log_item_id for r in lab.history() if r.intro_fire is not None]
        self.assertEqual(owners, [2, 3], "owned by occurrence, not Track id (F12)")
        self.assertTrue(all(r.intro_fire.state == FireState.ENDED for r in lab.history() if r.intro_fire))

    def test_operator_and_recovery_starts_never_get_an_intro_vt(self):
        # D5 / D13: Start Next, a manual start, an Eject replacement, the
        # Manual-to-Auto handoff and interrupted-occurrence recovery.
        for kind in (StartKind.START_NEXT, StartKind.MANUAL, StartKind.EJECT_REPLACEMENT,
                     StartKind.MANUAL_TO_AUTO_HANDOFF, None):
            with self.subTest(kind=kind):
                lab = IntegrationLab(self)
                inc = lab.item(2, music(0.25, 4.25), cue_in=0.25, intro_until=4.25, name=f"op-{kind}")
                lab.attach(inc, "intro", fake_voicetrack(lab.vt("op-intro", 1.0), 1.0))
                lab.queue(inc)
                lab.play()
                with lab.engine._vtt_starting(kind):
                    lab.engine._start_next_track(slot="A")
                pump(0.3)
                self.assertIsNone(lab.deck_of(2).scheduled_admission_ns, kind)
                self.assertIsNone(lab.last_record(), kind)
                lab.lab.stop()
                lab.engine.running = False
        lab = IntegrationLab(self)
        inc = lab.item(2, music(0.25, 4.25), cue_in=0.25, intro_until=4.25, name="resume-intro")
        lab.attach(inc, "intro", fake_voicetrack(lab.vt("resume-vt", 1.0), 1.0))
        inc.playback_claimed_at = timezone.now()           # interrupted occurrence: already claimed
        lab.queue(inc)
        lab.play()
        with lab.engine._vtt_starting(StartKind.AUTOMATIC):
            lab.engine._start_next_track(slot="A")
        pump(0.3)
        self.assertIsNone(lab.deck_of(2).scheduled_admission_ns, "already claimed: no intro VT (D13)")


class IntroOperatorTests(_IntroBase, SimpleTestCase):
    def _eject_incoming(self, offset):
        lab, out, inc, deck, zero = self.build(
            intro_vt=1.0, extra=())
        third = lab.item(3, [(5.0, URGENT)], name="replacement")
        lab.attach(third, "intro", fake_voicetrack(lab.vt("third-vt", 1.0, speech=FOREIGN), 1.0))
        lab.engine.log_items.append(third)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        self.assertTrue(wait_until(lambda: lab.now() >= a_seg + offset, 10.0))
        command = lab.now()
        slot = lab.deck_of(2).slot
        lab.engine._eject_deck(slot)
        self.wait_claims(lab, [1, 2, 3], 4.0)
        pump(1.5)
        return lab, lab.trace(), command, a_seg

    def test_eject_of_the_incoming_just_before_its_admission(self):
        lab, tr, command, a_seg = self._eject_incoming(-0.15)
        report("B4-eject-before", speech=tr.present(SPEECH, a_seg - 0.5, lab.now()),
               music=tr.present(MUSIC, a_seg - 0.5, lab.now()))
        self.assertIsNone(tr.onset(HEAD, after=a_seg - 0.5), "ejected VT never audible (F10)")
        self.assertIsNone(tr.onset(MUSIC, after=a_seg - 0.5), "held music never airs")
        self.assertLess(tr.present(FOREIGN, command, lab.now()), 0.011, "the Eject replacement has no intro VT (D5)")
        self.assertGreater(tr.present(URGENT, command, lab.now()), 0.8)

    def test_eject_of_the_incoming_just_after_its_admission(self):
        lab, tr, command, a_seg = self._eject_incoming(0.35)
        speech_end = tr.end(SPEECH, after=a_seg + 0.2)
        report("B4-eject-after", speech_end_after_command=speech_end - command)
        self.assertLess(speech_end - command, 0.07, "the ejected occurrence's VT never continues (F10)")
        self.assertLess(tr.present(FOREIGN, command, lab.now()), 0.011)

    def test_mic_before_and_during_intro_speech(self):
        for offset in (-0.2, 0.3):
            with self.subTest(offset=offset):
                lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=3.0)
                a_seg = zero + 8.0
                self.wait_claims(lab, [1, 2])
                self.assertTrue(wait_until(lambda: lab.now() >= a_seg + offset, 10.0))
                command = lab.now()
                lab.mic(True)
                pump(2.0)
                tr = lab.trace()
                speech = tr.present(SPEECH, command + 0.07, lab.now())
                music_on = tr.onset(MUSIC, after=a_seg - 0.5)
                report("B4-mic", offset=offset, speech_after_command=speech, music_vs_command=music_on - command)
                self.assertIsNone(tr.onset(SPEECH, after=command + 0.07),
                                  "no recorded speech while a mic is live (D6)")
                self.assertLess(music_on - command, 0.45, "held music re-admitted at once")
                self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)
                lab.lab.stop()
                lab.engine.running = False

    def test_pause_and_seek_of_a_scheduled_incoming_are_refused_and_start_next_is_busy(self):
        lab, out, inc, deck, zero = self.build(in_cue=0.25, in_intro=1.75, intro_vt=3.0)
        a_seg = zero + 8.0
        self.wait_claims(lab, [1, 2])
        held = lab.deck_of(2)
        self.assertIn(held.admission_state, ("preparing", "scheduled"))
        lab.engine._pause_deck(held.slot)
        lab.engine._seek_deck(held.slot, 1.0)
        self.assertIs(lab.engine.decks[held.slot], held)
        self.assertFalse(held.paused)
        self.assertIs(lab.engine._leading_deck(), deck, "a held deck never becomes the leading deck")
        self.assertEqual(lab.engine._vt["phase"], "vt_transition")
        self.assertTrue(lab.engine._vtt_owns_next_start())
        self.assertTrue(wait_until(lambda: lab.now() > a_seg + 3.5, 12.0))
        self.assertEqual(lab.deck_of(2).admission_state, "audible")


class IntroCycleTests(_IntroBase, SimpleTestCase):
    def test_repeated_intro_transitions_leave_no_residue(self):
        lab = IntegrationLab(self, ramp_ms=int(RAMP * 1000))
        items = [lab.item(1, [(5.0, OUT)], next_start=4.5, name="cycle-0")]
        path = lab.vt("cycle-vt", 0.6)
        for index in range(2, 10):
            item = lab.item(index, music(0.1, 2.5, after=2.0), cue_in=0.1, intro_until=2.5, next_start=4.0,
                            name="cycle-n")
            lab.attach(item, "intro", fake_voicetrack(path, 0.6))
            items.append(item)
        lab.queue(*items)
        lab.poll()
        lab.play()
        lab.start("A")
        self.assertTrue(wait_until(lambda: lab.claims[:3] == [1, 2, 3], 15.0))
        engine = lab.engine

        resources = lab.lab.resources
        lab.settle(0.5)
        self.assertTrue(wait_until(lambda: lab.claims[:4] == [1, 2, 3, 4], 8.0))
        # Baseline between transitions: one deck, no transition, no fire.
        self.assertTrue(wait_until(lambda: lab.record() is None and None in engine.decks.values()
                                   and not engine.__dict__.get("_vt2_fires"), 6.0))
        lab.settle(0.3)
        before = resources()
        self.assertTrue(wait_until(lambda: lab.claims[:9] == list(range(1, 10)), 40.0), lab.claims)
        self.assertTrue(wait_until(lambda: lab.record() is None and None in engine.decks.values()
                                   and not engine.__dict__.get("_vt2_fires"), 8.0))
        lab.settle(0.6)
        after = resources()
        fires = [r.intro_fire for r in lab.history() if r.intro_fire is not None]
        report("B4-cycles", transitions=len(fires), **{f"{k}_before": v for k, v in before.items()},
               **{f"{k}_after": v for k, v in after.items()},
               registry=len(engine.__dict__.get("_vt2_fires", {})))
        self.assertGreaterEqual(len(fires), 5)
        self.assertTrue(all(f.state == FireState.ENDED and f.retired for f in fires))
        self.assertEqual(before["fx_pads"], after["fx_pads"])
        self.assertEqual(before["deck_pads"], after["deck_pads"])
        self.assertEqual(before["children"], after["children"])
        self.assertLessEqual(after["threads"], before["threads"] + 1)
        self.assertLessEqual(after["fds"], before["fds"] + 2)
        self.assertAlmostEqual(lab.curve_value(), 1.0, places=3)


class IntegratedAccountingDbTests(TransactionTestCase):
    """PostgreSQL: the integrated planned path claims the CLAIMED row (Swap
    adoption under the claim's row lock), resolves that track's VoiceTrack,
    and records played_at / PlayEvent only at the exact admission."""

    def setUp(self):
        from library.models import Artist, Category, CategoryKind, PlaylistLog
        kind = CategoryKind.objects.create(code="b4music", name="B4 Music")
        self.category = Category.objects.create(code="B4MUSIC", name="B4 Music", kind=kind)
        self.artist = Artist.objects.create(name="B4 Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 11, 1), hour=9, status="approved")
        self.serial = 0

    def track(self, lab, name, segments, **fields):
        from library.models import Track
        path = lab.media(name, segments)
        duration = round(sum(s for s, _f in segments), 6)
        return Track.objects.create(
            filepath=str(path), filename=Path(path).name, title=name, artist=self.artist,
            category=self.category, ready2air=True, duration_seconds=duration, **fields)

    def item(self, track):
        from library.models import LogItem
        self.serial += 1
        row = LogItem.objects.create(
            playlist_log=self.log, position=self.serial, scheduled_time=timezone.now(), track=track,
            track_title=track.title, track_artist=self.artist.name, category=self.category)
        return LogItem.objects.select_related("track", "category").get(pk=row.pk)

    def voicetrack(self, lab, track, name, seconds, speech):
        from library.models import VoiceTrack
        path = lab.media(name, vt_segments(seconds, speech))
        return VoiceTrack.objects.create(track=track, position="intro", filepath=str(path),
                                         duration_seconds=seconds, gain_db=0.0)

    def real_lab(self):
        from library.tests._vt_b0_lab import Lab
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        for name in ("_check_commands", "_check_stuck_decks", "_reload_queue_if_changed",
                     "_sweep_active_message_queue", "_warm_track_cache"):
            setattr(engine, name, lambda *_a, **_k: None)
        source = GLib.timeout_add(eng_module.POSITION_POLL_MS, engine._poll_position)
        self.addCleanup(lambda: GLib.MainContext.default().find_source_by_id(source) and GLib.source_remove(source))
        return lab

    def test_swap_adopted_at_claim_and_played_at_only_at_the_admission(self):
        from library.models import LogItem, PlayEvent, VoiceTrackConfig
        VoiceTrackConfig.objects.update_or_create(pk=1, defaults={"program_duck_db": -6.0, "duck_ramp_ms": 100,
                                                                 "min_gap_ms": 200})
        lab = self.real_lab()
        out_track = self.track(lab, "db-out", [(9.0, OUT)], next_start_seconds=8.0)
        predicted = self.track(lab, "db-predicted", music(0.25, 4.25), cue_in_seconds=0.25,
                               intro_until_seconds=4.25)
        adopted = self.track(lab, "db-adopted", music(0.25, 4.25), cue_in_seconds=0.25,
                             intro_until_seconds=4.25)
        self.voicetrack(lab, predicted, "db-predicted-vt", 1.0, SPEECH)
        self.voicetrack(lab, adopted, "db-adopted-vt", 1.0, FOREIGN)
        out_item, in_item = self.item(out_track), self.item(predicted)
        engine = lab.engine
        engine.log_items = [out_item, in_item]
        engine._queue_cursor = 0
        lab.play()
        engine._start_next_track(slot="A")
        zero = engine._vtt_media_zero_ns(engine.decks["A"]) / NS
        a_seg = zero + 8.0
        # An operator Swap commits AFTER the in-memory queue copy was read.
        self.assertTrue(wait_until(lambda: lab.now() > zero + 4.0, 8.0))
        LogItem.objects.filter(pk=in_item.pk).update(track=adopted, track_title=adopted.title)
        wall0, run0 = time.time(), lab.now()
        self.assertTrue(wait_until(lambda: LogItem.objects.get(pk=in_item.pk).playback_claimed_at is not None, 8.0))
        claimed = LogItem.objects.get(pk=in_item.pk)
        self.assertIsNone(claimed.played_at, "claimed at commit, not played")
        self.assertFalse(PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).exists())
        self.assertTrue(wait_until(lambda: PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).exists(), 6.0))
        pump(2.0)
        row = LogItem.objects.get(pk=in_item.pk)
        record = engine.__dict__.get("_vtt_history")[-1]
        deck_offset = record.plan.deck_offset
        expected_wall = wall0 + (deck_offset - run0)
        tr = lab.trace()
        report("B4-db-swap", played_at_error=row.played_at.timestamp() - expected_wall,
               play_events=PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).count(),
               adopted_vt_onset_error=tr.onset(HEAD, after=a_seg - 1) - a_seg,
               predicted_vt_seconds=tr.present(SPEECH, a_seg - 1, lab.now()))
        self.assertEqual(row.track_id, adopted.id)
        self.assertEqual(record.intro_fire.owner.track_id, adopted.id, "the claimed track's VoiceTrack")
        self.assertIsNone(tr.onset(SPEECH, after=a_seg - 1.0), "never the predicted track's VT")
        self.assertGreater(tr.present(FOREIGN, a_seg, a_seg + 1.0), 0.6)
        self.assertLessEqual(abs(row.played_at.timestamp() - expected_wall), 0.020,
                             "played_at is the exact admission (media 0), never the claim")
        self.assertEqual(row.playback_claimed_at, claimed.playback_claimed_at)
        self.assertEqual(PlayEvent.objects.filter(log_item_id_snapshot=in_item.id).count(), 1)
        lab.stop()
