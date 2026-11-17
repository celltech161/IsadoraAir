"""Roadmap 2.25 B2 -- scheduled VT audio infrastructure (dormant).

Exercises the REAL engine building blocks (``_vt2_*``, ``_create_deck(...,
scheduled_admission_ns=...)``, ``vt_audio.ScheduledVtFire`` /
``VtDuckCurve``) in the r0144 program topology of the 2.23 transport
harness, measuring rendered program PCM with the B0 lab (sample-accurate
onset/end, tone-identified signals). Nothing here connects the
infrastructure to live automatic transitions.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from library.services import vt_audio, vt_transition
from library.services.vt_audio import FireState, VtOwner
from library.tests._vt_b0_lab import NS, P, GLib, Gst, Lab, pump, report, stats, wait_until

OUT, MUSIC, VOCAL, SPEECH, HEAD, TAIL, MIC = 400, 600, 1400, 1000, 2200, 2600, 3000
MARK = 0.150
TOL = 0.015
CONFIGS = ((44100, "wav"), (44100, "flac"), (48000, "wav"), (48000, "flac"))


def vt_segments(total):
    """150 ms head marker, speech, 150 ms tail marker: both ends measurable."""
    return [(MARK, HEAD), (total - 2 * MARK, SPEECH), (MARK, TAIL)]


def music_segments(cue, intro_until, after=0.5):
    segs = [(cue, 0)] if cue > 0 else []
    return segs + [(intro_until - cue, MUSIC), (after, VOCAL)]


_fire_ids = iter(range(1, 10_000))


def owner(direction="intro", *, item=2, track=5002, slot="B", generation=1, seq=1):
    return VtOwner(direction, item, track, slot, generation, seq, next(_fire_ids))


class _B2Base:
    def lab(self, rate=44100, mic=False):
        previous = getattr(self, "_previous_lab", None)
        if previous is not None:
            previous.stop()
        lab = Lab(self, rate=rate, mic_freq=MIC if mic else None)
        self._previous_lab = lab
        engine = lab.engine
        # Accounting seams recorded (the PostgreSQL class below runs them for real).
        self.air_starts = []
        self.now_playing = []

        def record_air_start(deck, air_started_at=None):
            self.air_starts.append({"slot": deck.slot, "generation": deck.generation,
                                    "log_item_id": deck.log_item.id, "air_started_at": air_started_at,
                                    "running": lab.now()})
        engine._schedule_occurrence_air_start_from_probe = record_air_start
        engine._write_now_playing = lambda track: self.now_playing.append((track.id, lab.now()))
        lab.play()
        return lab

    def fire(self, lab, path, *, admission, duration, own=None, before_start=None, events=None):
        own = own or owner()

        def on_event(fire, state):
            if events is not None:
                events.append((state, fire.reason, lab.now()))
        return lab.engine._vt2_prepare_fire(own, str(path), 0.0, int(admission * NS), duration,
                                             on_event=on_event, before_start=before_start)

    def scheduled_deck(self, lab, slot, item, admission, *, events=None, before_sync=None):
        def on_event(deck, kind, reason):
            if events is not None:
                events.append((kind, reason, lab.now(), deck.generation))
        return lab.engine._create_deck(slot, item, scheduled_admission_ns=int(admission * NS),
                                       on_scheduled_event=on_event, before_sync=before_sync)

    def settle(self, lab, seconds=0.5):
        pump(seconds)
        coordinator = lab.engine.__dict__.get("_vt2_teardown")
        deadline = time.monotonic() + 3.0
        while coordinator is not None and time.monotonic() < deadline:
            snap = coordinator.snapshot()
            if snap.get("active_generation") is None and not snap.get("queue_depth"):
                break
            pump(0.05)


# ---------------------------------------------------------------------------
# Scheduled VT fire: onset, head and tail (F14), across rates and formats.
# ---------------------------------------------------------------------------
class ScheduledFireTimingTests(_B2Base, SimpleTestCase):
    def test_onset_head_and_tail_preserved_44_48_wav_flac(self):
        for rate, fmt in CONFIGS:
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                path = lab.media(f"vt-{fmt}", vt_segments(1.0), fmt=fmt)
                errs = []
                for rep in range(2):
                    admission = lab.now() + 0.4
                    events = []
                    fire = self.fire(lab, path, admission=admission, duration=1.0, events=events)
                    pump(1.0 + 0.8)
                    t = lab.trace()
                    on = t.onset(HEAD, after=admission - 0.2, before=admission + 0.3)
                    head_end = t.end(HEAD, after=on + 0.05)
                    tail_on = t.onset(TAIL, after=admission + 0.5, before=admission + 1.2)
                    tail_end = t.end(TAIL, after=tail_on + 0.05)
                    states = [s for s, _r, _t in events]
                    report(f"FIRE rate={rate} fmt={fmt} rep={rep}", onset_err=on - admission,
                           head=head_end - on, tail=tail_end - tail_on,
                           end_err=tail_end - (admission + 1.0), rendered=tail_end - on,
                           state=fire.state, detach_ms=fire.detach_ms,
                           ready_before_admission=admission - fire.ready_ns / NS)
                    self.assertEqual(fire.state, FireState.ENDED)
                    self.assertEqual(states[:2], [FireState.PREPARING, FireState.READY])
                    self.assertIn(FireState.ADMITTED, states)
                    self.assertIn(FireState.DRAINING, states)
                    self.assertLessEqual(abs(on - admission), TOL)
                    self.assertLessEqual(abs((head_end - on) - MARK), TOL)
                    self.assertLessEqual(abs((tail_end - tail_on) - MARK), TOL)      # F14: whole tail
                    self.assertLessEqual(abs(tail_end - (admission + 1.0)), TOL)
                    errs.append(on - admission)
                self.settle(lab)

    def test_r0144_vt_fire_loses_the_tail_marker_negative_control(self):
        """The same tail measurement on the unchanged r0144 _vt_fire_file
        path: proves the test detects premature EOS retirement."""
        lab = self.lab()
        path = lab.media("vt-r0144", vt_segments(1.0))
        cut = []
        for _rep in range(2):
            fired = lab.now()
            lab.engine._vt_fire_file(str(path), 0.0, "intro")
            pump(1.6)
            t = lab.trace()
            tail_on = t.onset(TAIL, after=fired + 0.5, before=fired + 1.3)
            tail_end = t.end(TAIL, after=tail_on + 0.02) if tail_on is not None else None
            heard = 0.0 if tail_on is None else tail_end - tail_on
            cut.append(MARK - heard)
            report("FIRE r0144_tail_marker", heard=heard, cut=MARK - heard)
        self.assertGreater(min(cut), 0.03)

    def test_voicetrack_resolution_production_media_and_legacy(self):
        lab = self.lab()
        media = lab.media("vt-pm", vt_segments(0.6), fmt="flac")
        legacy = lab.media("vt-legacy", vt_segments(0.6))
        production = SimpleNamespace(playable_audio=lambda: SimpleNamespace(path=str(media), duration_seconds=0.6),
                                     filepath="/nonexistent/legacy.wav", duration_seconds=9.0, gain_db=0.0)
        legacy_only = SimpleNamespace(playable_audio=lambda: None, filepath=str(legacy), duration_seconds=0.6,
                                      gain_db=0.0)
        unplayable = SimpleNamespace(playable_audio=lambda: None, filepath="", duration_seconds=0.0, gain_db=0.0)
        fires = {}
        for name, record in (("production_media", production), ("legacy", legacy_only)):
            admission = lab.now() + 0.4
            fire = lab.engine._vt2_prepare_fire_for_voicetrack(owner(), record, int(admission * NS))
            pump(1.4)
            t = lab.trace()
            on = t.onset(HEAD, after=admission - 0.2, before=admission + 0.3)
            fires[name] = (fire, on - admission)
        self.assertIsNone(lab.engine._vt2_prepare_fire_for_voicetrack(owner(), unplayable, int((lab.now() + 1) * NS)))
        report("RESOLVE", production_media_path=Path(fires["production_media"][0].path).name,
               production_media_err=fires["production_media"][1], legacy_err=fires["legacy"][1])
        self.assertEqual(fires["production_media"][0].path, str(media))
        self.assertEqual(fires["production_media"][0].duration, 0.6)
        self.assertEqual(fires["legacy"][0].path, str(legacy))
        for fire, err in fires.values():
            self.assertEqual(fire.state, FireState.ENDED)
            self.assertLessEqual(abs(err), TOL)

    def test_waiting_for_a_distant_admission_is_not_a_stall(self):
        lab = self.lab()
        path = lab.media("vt-wait", vt_segments(0.6))
        admission = lab.now() + 3.0
        fire = self.fire(lab, path, admission=admission, duration=0.6)
        pump(1.5)
        self.assertEqual(fire.state, FireState.READY)          # ready long before; nothing failed
        pump(3.0)
        t = lab.trace()
        on = t.onset(HEAD, after=admission - 0.2)
        report("FIRE distant_admission", onset_err=on - admission, state=fire.state)
        self.assertEqual(fire.state, FireState.ENDED)
        self.assertLessEqual(abs(on - admission), TOL)


# ---------------------------------------------------------------------------
# Failure: routed errors and bounded deadlines.
# ---------------------------------------------------------------------------
class ScheduledFireFailureTests(_B2Base, SimpleTestCase):
    def test_corrupt_missing_stalled_and_overrun(self):
        lab = self.lab()
        corrupt = lab.root / "corrupt.wav"
        corrupt.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk" + os.urandom(4096))
        good = lab.media("vt-fail", vt_segments(3.0))

        def stall(_fire, elements):
            pad = elements["caps"].get_static_pad("src")
            pid = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM | Gst.PadProbeType.BUFFER,
                                lambda *_a: Gst.PadProbeReturn.OK)
            GLib.timeout_add(1500, lambda: (pad.remove_probe(pid), False)[1])

        cases = {
            "corrupt_file": dict(path=corrupt, duration=1.0),
            "missing_file": dict(path=lab.root / "missing.wav", duration=1.0),
            "stalled_preparation": dict(path=good, duration=3.0, before_start=stall),
            "overrun_validated_duration": dict(path=good, duration=0.5),
        }
        for name, spec in cases.items():
            with self.subTest(case=name):
                admission = lab.now() + 0.5
                fire = self.fire(lab, spec["path"], admission=admission, duration=spec["duration"],
                                 before_start=spec.get("before_start"))
                pump(spec["duration"] + 3.2 if name == "overrun_validated_duration" else 1.6)
                t = lab.trace()
                aired = t.present(HEAD, admission - 0.2, admission + 1.0)
                ended = (fire.ended_ns or 0) / NS
                report(f"FAIL {name}", state=fire.state, reason=fire.reason,
                       failed_vs_admission=ended - admission, head_aired=aired,
                       errors=len(fire.teardown_errors), retired=fire.retired)
                self.assertEqual(fire.state, FireState.FAILED)
                self.assertTrue(fire.retired)
                if name == "overrun_validated_duration":
                    self.assertEqual(fire.reason, "no_completion")
                    self.assertLessEqual(abs(ended - (admission + 0.5 + vt_transition.COMPLETION_GRACE_S)), 0.05)
                else:
                    self.assertEqual(aired, 0.0)                    # never a clipped head
                    self.assertLess(ended, admission)               # failed before it could be heard
                if name == "corrupt_file":
                    self.assertTrue(fire.reason.startswith("bus_error:"), fire.reason)
                if name == "stalled_preparation":
                    self.assertEqual(fire.reason, "not_ready_before_admission")
        self.settle(lab, 1.0)


# ---------------------------------------------------------------------------
# Cancellation, idempotency, late callbacks, teardown order and errors.
# ---------------------------------------------------------------------------
class ScheduledFireCancellationTests(_B2Base, SimpleTestCase):
    def test_cancel_before_admission_never_blocks_for_the_hold(self):
        lab = self.lab()
        path = lab.media("vt-cancel", vt_segments(1.0))
        before = lab.resources()
        admission = lab.now() + 1.5
        fire = self.fire(lab, path, admission=admission, duration=1.0)
        pump(0.3)
        began = time.monotonic()
        self.assertTrue(fire.cancel("test", fade_ms=50))
        cancel_ms = (time.monotonic() - began) * 1000
        pump(1.8)
        self.settle(lab)
        t = lab.trace()
        after = lab.resources()
        report("CANCEL before_admission", main_loop_ms=cancel_ms, detach_ms=fire.detach_ms,
               hold_remaining_ms=(admission - (lab.now() - 1.8)) * 1000,
               aired=t.present(HEAD, admission - 0.2, admission + 1.2) + t.present(SPEECH, admission, admission + 1.2),
               fx_pads_before=before["fx_pads"], fx_pads_after=after["fx_pads"],
               children_before=before["children"], children_after=after["children"])
        self.assertEqual(fire.state, FireState.CANCELLED)
        self.assertLess(cancel_ms, 20.0)                    # B0 E09c NULL-first blocked 1237 ms
        self.assertEqual(t.present(HEAD, admission - 0.2, admission + 1.2), 0.0)
        self.assertEqual(after["fx_pads"], before["fx_pads"])
        self.assertEqual(after["children"], before["children"])

    def test_cancel_during_speech_near_eos_and_repeated(self):
        lab = self.lab()
        path = lab.media("vt-cancel2", vt_segments(1.2))
        # During audible speech.
        admission = lab.now() + 0.4
        fire = self.fire(lab, path, admission=admission, duration=1.2)
        pump(0.4 + 0.5)
        at = lab.now()
        self.assertTrue(fire.cancel("mic_live", fade_ms=50))
        self.assertFalse(fire.cancel("again", fade_ms=50))   # repeated: ignored
        pump(1.0)
        t = lab.trace()
        speech_end = t.end(SPEECH, after=admission + 0.3)
        report("CANCEL during_speech", command_to_silence=speech_end - at, ignored=len(fire.ignored))
        self.assertLessEqual(speech_end - at, 0.050 + 0.020)
        self.assertEqual(fire.state, FireState.CANCELLED)
        # Near EOS: cancel while draining (EOS queued, tail still rendering).
        admission = lab.now() + 0.4
        fire2 = self.fire(lab, path, admission=admission, duration=1.2)
        self.assertTrue(wait_until(lambda: fire2.state == FireState.DRAINING, 3.0))
        self.assertTrue(fire2.cancel("near_eos", fade_ms=20))
        pump(0.6)
        report("CANCEL near_eos", state=fire2.state, retired=fire2.retired, errors=len(fire2.teardown_errors))
        self.assertEqual(fire2.state, FireState.CANCELLED)
        self.assertTrue(fire2.retired)
        self.settle(lab)

    def test_late_callbacks_from_a_retired_fire_are_ignored(self):
        lab = self.lab()
        path = lab.media("vt-late", vt_segments(0.6))
        fire = self.fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
        self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
        old = (fire.fire_id, 0)
        history = list(fire.history)
        for callback in (fire._on_ready, fire._admission, fire._on_eos, fire._drained, fire._ready_deadline,
                         fire._first_audio_deadline, fire._completion_deadline):
            callback(old)
        fire._error(old, "late error")
        report("LATE callbacks", ignored=len(fire.ignored), state=fire.state)
        self.assertEqual(fire.state, FireState.ENDED)
        self.assertEqual(fire.history, history)
        self.assertGreaterEqual(len(fire.ignored), 8)

    def test_shutdown_errors_are_contained(self):
        lab = self.lab()
        path = lab.media("vt-shutdown", vt_segments(0.6))
        engine = lab.engine

        def broken_submit(_fire, _bin):
            raise RuntimeError("worker unavailable")
        fire = vt_audio.ScheduledVtFire(
            owner=owner(), path=path, gain_db=0.0, admission_ns=lab.now_ns() + int(0.4 * NS),
            validated_duration=0.6, pipeline=engine.main_pipeline, mixer=engine.fx_submix,
            rate=lab.rate, now_ns=engine._vt2_running_ns, timer_at=engine._vt2_timer_at,
            null_submit=broken_submit)
        self.assertTrue(fire.prepare())
        self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
        pump(0.2)
        report("SHUTDOWN error", outcome=fire.teardown_outcome, errors=";".join(fire.teardown_errors))
        self.assertTrue(fire.retired)
        self.assertEqual(fire.teardown_outcome, "error")
        self.assertIsNone(fire.mixer_pad)        # detached even though the worker failed
        fire.bin.set_state(Gst.State.NULL)


# ---------------------------------------------------------------------------
# Speech-owned duck.
# ---------------------------------------------------------------------------
class DuckCurveTests(_B2Base, SimpleTestCase):
    def test_attack_release_stale_owner_overlap_failure_and_mic_independence(self):
        lab = self.lab()
        engine = lab.engine
        out = lab.media("duck-out", [(9.0, OUT)])
        t0 = lab.now() + 0.2
        engine._create_deck("A", lab.item(1, out, duration=9.0))
        curve = engine._vt2_duck_curve()
        ramp = 0.1
        gain = vt_transition.duck_gain_for(-6.0)
        a = owner(seq=1)
        b = owner(seq=2)
        s1 = lab.now() + 0.8
        curve.apply(a, [(int(t * NS), g) for t, g in vt_transition.duck_points([(s1, s1 + 1.0)], ramp, gain)])
        pump(0.8 + 1.0 + ramp + 0.3)
        # Stale owner b tries to release while nobody owns -> ignored.
        self.assertFalse(curve.release(b, int(ramp * NS)))
        # Overlap: a plans, b takes over; a's late release is ignored.
        s2 = lab.now() + 0.4
        curve.apply(a, [(int(t * NS), g) for t, g in vt_transition.duck_points([(s2, s2 + 2.0)], ramp, gain)])
        pump(0.6)
        curve.apply(b, [(int(t * NS), g) for t, g in vt_transition.duck_points([(s2, s2 + 2.0)], ramp, gain)])
        self.assertFalse(curve.release(a, int(ramp * NS)))
        pump(0.4)
        # Mic duck independent: mic ducks while the VT duck is held, then the VT duck releases.
        engine.duck_gain.set_property("volume", 0.3)
        mic_at = lab.now()
        pump(0.3)
        released_at = lab.now()
        self.assertTrue(curve.release(b, int(ramp * NS)))       # failed/cancelled VT: back to unity
        pump(0.6)
        tr = lab.trace()
        before = tr.level(OUT, s1 - ramp - 0.4, s1 - ramp - 0.05)
        during = tr.level(OUT, s1 + 0.05, s1 + 0.95)
        after1 = tr.level(OUT, s1 + 1.0 + ramp + 0.05, s1 + 1.0 + ramp + 0.25)
        attack_start = None
        for run, amp in tr.coarse(OUT):
            if run >= s1 - ramp - 0.2 and amp < before * 0.97:
                attack_start = run
                break
        held = tr.level(OUT, mic_at - 0.25, mic_at - 0.05)
        both = tr.level(OUT, mic_at + 0.05, released_at - 0.02)
        mic_only = tr.level(OUT, released_at + ramp + 0.1, released_at + ramp + 0.4)
        report("DUCK", unducked=before, ducked=during, released=after1,
               attack_start_vs_speech=None if attack_start is None else attack_start - s1,
               held_through_stale_release=held, vt_plus_mic=both, mic_only_after_vt_release=mic_only,
               ignored=len(curve.ignored))
        self.assertAlmostEqual(during / before, gain, delta=0.03)
        self.assertAlmostEqual(after1 / before, 1.0, delta=0.03)
        self.assertLessEqual(abs(attack_start - (s1 - ramp)), 0.03)
        self.assertAlmostEqual(held / before, gain, delta=0.03)              # stale release changed nothing
        self.assertAlmostEqual(both / before, gain * 0.3, delta=0.03)
        self.assertAlmostEqual(mic_only / before, 0.3, delta=0.03)           # VT released, mic duck stays
        engine.duck_gain.set_property("volume", 1.0)


# ---------------------------------------------------------------------------
# Scheduled deck admission, accounting seams, F15, re-admission.
# ---------------------------------------------------------------------------
class ScheduledDeckTests(_B2Base, SimpleTestCase):
    def test_onset_readiness_and_premature_accounting(self):
        for rate, fmt in CONFIGS:
            with self.subTest(rate=rate, fmt=fmt):
                lab = self.lab(rate)
                cue = 0.25
                path = lab.media(f"sched-{fmt}", music_segments(cue, 1.2), fmt=fmt)
                events = []
                admission = lab.now() + 1.2          # media 0 renders here; music at + cue
                wall0, run0 = time.time(), lab.now()
                deck = self.scheduled_deck(lab, "B", lab.item(20, path, duration=1.7, cue_in=cue),
                                           admission, events=events)
                self.assertIsNotNone(deck)
                self.assertEqual(deck.admission_state, "preparing")
                self.assertIsNone(deck.mixer_pad)              # not linked until viable
                pump(0.6)
                pre = {"state": deck.admission_state, "air_starts": len(self.air_starts),
                       "now_playing": len(self.now_playing), "position": lab.engine._get_deck_position(deck),
                       "pending": deck.scheduled_pending_seconds, "segment": deck.duration_segment_active}
                self.assertEqual(pre["air_starts"], 0)          # no premature PlayEvent/played_at path
                self.assertEqual(pre["now_playing"], 0)         # no premature presentation
                pump(admission - lab.now() + 2.0)
                t = lab.trace()
                on = t.onset(MUSIC, after=admission - 0.3, before=admission + cue + 0.6)
                planned = admission + cue
                expected_wall = wall0 + (admission - run0)
                air = self.air_starts[0] if self.air_starts else {}
                report(f"DECK rate={rate} fmt={fmt}", onset_err=on - planned,
                       ready_after_open=deck.scheduled_ready_ns / NS - run0,
                       pre_state=pre["state"], pre_air_starts=pre["air_starts"],
                       pre_now_playing=pre["now_playing"], pre_position=pre["position"],
                       pre_pending=pre["pending"],
                       air_start_wall_err=air["air_started_at"].timestamp() - expected_wall,
                       air_starts=len(self.air_starts), confirmed=deck.duration_confirmed_seconds,
                       events=",".join(k for k, *_ in events))
                self.assertLessEqual(abs(on - planned), TOL)
                self.assertEqual(pre["state"], "scheduled")
                self.assertEqual(pre["air_starts"], 0)          # no premature PlayEvent/played_at path
                self.assertEqual(pre["now_playing"], 0)         # no premature presentation
                self.assertEqual(pre["position"], 0.0)
                self.assertFalse(pre["segment"])                # no premature duration evidence
                self.assertGreater(pre["pending"], 0.0)         # queued, not yet credited
                self.assertEqual(len(self.air_starts), 1)
                self.assertLessEqual(abs(air["air_started_at"].timestamp() - expected_wall), 0.020)
                self.assertEqual([k for k, *_ in events], ["ready", "audible"])
                self.assertAlmostEqual(deck.duration_confirmed_seconds, 1.7, delta=0.05)

    def test_f15_stalled_preparation_never_starves_the_outgoing(self):
        lab = self.lab()
        out = lab.media("f15-out", [(3.0, OUT)])
        path = lab.media("f15-in", music_segments(0.2, 1.0))
        t0 = lab.now()
        lab.engine._create_deck("A", lab.item(1, out, duration=3.0))
        pump(0.6)
        events = []

        def stall(deck):
            pad = deck.seek_gate_pad
            pid = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM | Gst.PadProbeType.BUFFER,
                                lambda *_a: Gst.PadProbeReturn.OK)
            GLib.timeout_add(1000, lambda: (pad.remove_probe(pid), False)[1])
        claims_before = list(lab.h.claims)
        import traceback
        exhausted_from = []
        original_exhausted = lab.engine._on_log_exhausted

        def exhausted(slot):
            exhausted_from.append((slot, "<".join(f.name for f in traceback.extract_stack()[-6:-1])))
            return original_exhausted(slot)
        lab.engine._on_log_exhausted = exhausted
        opened = lab.now()
        deck = self.scheduled_deck(lab, "B", lab.item(2, path, duration=1.5, cue_in=0.2), opened + 0.6,
                                   events=events, before_sync=stall)
        pump(2.8)
        t = lab.trace()
        out_on = t.onset(OUT, after=t0 - 0.1)
        span = (out_on + 0.05, out_on + 2.75)
        continuity = t.present(OUT, *span) / (span[1] - span[0])
        failed_at = next((at for kind, _r, at, _g in events if kind == "failed"), None)
        report("F15 stalled_scheduled_deck", outgoing_continuity=continuity,
               outgoing_missing_seconds=(1 - continuity) * (span[1] - span[0]),
               failed_after_open=None if failed_at is None else failed_at - opened,
               reason=deck.eos_milestones.get("VT2_SCHEDULED_FAILED", {}).get("state"),
               music_aired=t.present(MUSIC, opened, opened + 2.5),
               incoming_air_starts=len([a for a in self.air_starts if a["log_item_id"] == 2]),
               slot_b=lab.engine.decks["B"] is None, exhausted=len(exhausted_from),
               exhausted_from=" | ".join(f"{slot}:{where}" for slot, where in exhausted_from))
        self.assertGreaterEqual(continuity, 0.995)          # B0 E12c (linked at open): 0.976
        self.assertIsNotNone(failed_at)
        self.assertLess(failed_at - opened, 0.2)
        self.assertEqual(t.present(MUSIC, opened, opened + 2.5), 0.0)
        self.assertEqual([a for a in self.air_starts if a["log_item_id"] == 2], [])
        self.assertIsNone(lab.engine.decks["B"])
        # Every queue-exhausted attempt belongs to the outgoing's own (r0144)
        # end-of-deck path on slot A; the failed scheduled deck never advances.
        self.assertTrue(exhausted_from)
        self.assertEqual({slot for slot, _where in exhausted_from}, {"A"})
        self.assertEqual(lab.h.claims[:len(claims_before)], claims_before)
        self.assertEqual(lab.h.claims[len(claims_before):], [2])   # only the one claim, no advance

    def test_reopen_same_occurrence_bounded_and_stale_admission_ignored(self):
        lab = self.lab()
        out = lab.media("reopen-out", [(5.0, OUT)])
        path = lab.media("reopen-in", music_segments(0.2, 1.2))
        lab.engine._create_deck("A", lab.item(1, out, duration=5.0))
        events = []
        item = lab.item(2, path, duration=1.7, cue_in=0.2)
        held_admission = lab.now() + 3.5
        held = self.scheduled_deck(lab, "B", item, held_admission, events=events)
        pump(0.8)
        old_generation = held.generation
        wall0, run0 = time.time(), lab.now()
        new_admission = lab.now() + vt_transition.REOPEN_LEAD_S
        outcome, deck = lab.engine._vt2_reopen_held_occurrence("B", int(new_admission * NS))
        self.assertEqual(outcome, "reopened")
        self.assertIs(deck.log_item, item)
        self.assertGreater(deck.generation, old_generation)
        # Stale admission for the retired generation: ignored.
        lab.engine._vt2_admit_scheduled_deck("B", old_generation)
        pump(new_admission - lab.now() + 0.5)
        # Audible occurrences are never reopened.
        self.assertEqual(deck.admission_state, "audible")
        self.assertEqual(lab.engine._vt2_reopen_held_occurrence("B", int((lab.now() + 1) * NS))[0], "refused")
        pump(1.9)
        t = lab.trace()
        on = t.onset(MUSIC, after=new_admission - 0.3, before=held_admission)
        aired_at_old_plan = t.present(MUSIC, held_admission + 0.2 - 0.05, held_admission + 0.2 + 0.05)
        incoming = [a for a in self.air_starts if a["log_item_id"] == 2]
        report("REOPEN", new_target_err=on - (new_admission + 0.2), generations=f"{old_generation}->{deck.generation}",
               claims=",".join(map(str, lab.h.claims)), incoming_air_starts=len(incoming),
               air_generation=incoming[0]["generation"] if incoming else None,
               old_plan_aired=aired_at_old_plan)
        self.assertLessEqual(abs(on - (new_admission + 0.2)), TOL)
        self.assertEqual(lab.h.claims, [1, 2, 2])         # same occurrence re-claimed (idempotent), no other item
        self.assertEqual(len(incoming), 1)                 # one air start, for the replacement only
        self.assertEqual(incoming[0]["generation"], deck.generation)
        self.assertLessEqual(abs(incoming[0]["air_started_at"].timestamp() - (wall0 + new_admission - run0)), 0.020)
        self.assertEqual(aired_at_old_plan, 0.0)
        self.assertEqual(lab.h.on_log_exhausted_calls, [])
        # Bounded attempts per occurrence.
        self.assertTrue(wait_until(lambda: lab.engine.decks["B"] is None, 3.0))
        item3 = lab.item(3, path, duration=1.7, cue_in=0.2)
        self.scheduled_deck(lab, "B", item3, lab.now() + 3.0)
        pump(0.3)
        outcomes = [lab.engine._vt2_reopen_held_occurrence("B", int((lab.now() + 2.0) * NS))[0]
                    for _ in range(3)]
        report("REOPEN bound", outcomes=",".join(outcomes))
        self.assertEqual(outcomes, ["reopened", "reopened", "exhausted"])

    def test_failed_reopen_does_not_advance(self):
        lab = self.lab()
        src = lab.media("reopen-fail", music_segments(0.2, 1.0))
        path = lab.root / "reopen-fail-copy.wav"
        path.write_bytes(Path(src).read_bytes())
        item = lab.item(2, path, duration=1.5, cue_in=0.2)
        self.scheduled_deck(lab, "B", item, lab.now() + 3.0)
        pump(0.4)
        path.unlink()
        outcome, deck = lab.engine._vt2_reopen_held_occurrence("B", int((lab.now() + 0.5) * NS))
        pump(1.0)
        held = lab.engine.decks["B"]
        report("REOPEN failed", outcome=outcome, held_kept=held is not None,
               claims=",".join(map(str, lab.h.claims)), exhausted=len(lab.h.on_log_exhausted_calls))
        self.assertEqual(outcome, "failed")
        self.assertIsNone(deck)
        self.assertIsNotNone(held)                         # the held generation is kept
        self.assertEqual(held.admission_state, "scheduled")
        self.assertEqual(lab.h.claims, [2])                # no second claim, no other item
        self.assertEqual(lab.h.on_log_exhausted_calls, [])
        self.assertEqual(self.air_starts, [])

    def test_ordinary_deck_path_is_unchanged(self):
        lab = self.lab()
        path = lab.media("ordinary", music_segments(0.0, 1.0))
        deck = lab.engine._create_deck("A", lab.item(1, path, duration=1.5))
        self.assertTrue(deck.silence_primed)
        self.assertIsNone(deck.admission_state)
        self.assertIsNone(deck.scheduled_admission_ns)
        self.assertIsNotNone(deck.mixer_pad)
        pump(0.1)
        self.assertEqual(len(self.now_playing), 1)          # presentation at open, as r0144

    def test_resume_or_continuation_is_never_scheduled(self):
        lab = self.lab()
        path = lab.media("resume", music_segments(0.0, 1.0))
        item = lab.item(9, path, duration=1.5)
        lab.engine._resume_hint = {"track_id": item.track.id, "log_item_id": 9, "position": 0.5}
        self.assertIsNone(self.scheduled_deck(lab, "A", item, lab.now() + 1.0))
        self.assertIsNotNone(lab.engine._resume_hint)       # left for the ordinary resume path
        lab.engine._resume_hint = None
        self.assertIsNone(lab.engine._create_deck("A", item, resume_position_ns=1,
                                                  scheduled_admission_ns=int((lab.now() + 1) * NS)))


# ---------------------------------------------------------------------------
# Repeated cycles: no drift, no retained resources.
# ---------------------------------------------------------------------------
class RepeatedCycleTests(_B2Base, SimpleTestCase):
    CYCLES = 12

    def test_cycles(self):
        lab = self.lab()
        vt = {f: lab.media(f"cyc-vt-{f}", vt_segments(0.5), fmt=f) for f in ("wav", "flac")}
        music = {f: lab.media(f"cyc-in-{f}", music_segments(0.1, 0.7, after=0.3), fmt=f) for f in ("wav", "flac")}
        # Warm-up cycle: lazily-created bounded workers (per-slot deck
        # teardown, the VT teardown worker) and GStreamer's task-pool threads
        # exist after it; the baseline is taken after them, so any growth
        # across the 12 measured cycles is retention.
        warm = lab.now() + 0.9
        self.scheduled_deck(lab, "A", lab.item(99, music["wav"], duration=1.1, cue_in=0.1), warm - 0.1)
        self.fire(lab, vt["wav"], admission=warm, duration=0.5)
        lab.at(warm + 0.5, lambda: self.scheduled_deck(lab, "B", lab.item(98, music["flac"], duration=1.1,
                                                                       cue_in=0.1), warm + 1.0))
        pump(3.0)
        self.settle(lab, 0.5)
        before = lab.resources()
        plans = []
        base = lab.now() + 0.9
        for n in range(self.CYCLES):
            fmt = "wav" if n % 2 == 0 else "flac"
            s_v = base + n * 1.3
            plans.append(s_v)
            item = lab.item(100 + n, music[fmt], duration=1.1, cue_in=0.1)
            lab.at(s_v - 0.8, lambda slot, it, adm: self.scheduled_deck(lab, slot, it, adm), "AB"[n % 2], item,
                   s_v - 0.1)
            lab.at(s_v - 0.35, lambda path, s: self.fire(lab, path, admission=s, duration=0.5), vt[fmt], s_v)
        pump(base + self.CYCLES * 1.3 - lab.now() + 1.0)
        self.settle(lab, 1.0)
        after = lab.resources()
        t = lab.trace()
        vt_errs = [t.onset(HEAD, after=s - 0.3, before=s + 0.3) - s for s in plans]
        music_errs = [t.onset(MUSIC, after=s - 0.3, before=s + 0.3) - s for s in plans]
        tails = [t.end(TAIL, after=s + 0.36) - (s + 0.5) for s in plans]
        coordinator = lab.engine._vt2_teardown_coordinator().snapshot()
        sv, sm, st = stats(vt_errs), stats(music_errs), stats(tails)
        report("CYCLES", n=self.CYCLES, vt_max_abs=sv["max_abs"], music_max_abs=sm["max_abs"],
               tail_end_max_abs=st["max_abs"], drift_vt=vt_errs[-1] - vt_errs[0],
               registry=len(lab.engine.__dict__.get("_vt2_fires", {})),
               teardown_completed=coordinator.get("completed"),
               **{f"{k}_before": v for k, v in before.items()}, **{f"{k}_after": v for k, v in after.items()})
        self.assertLessEqual(sv["max_abs"], TOL)
        self.assertLessEqual(sm["max_abs"], TOL)
        self.assertLessEqual(st["max_abs"], TOL)
        for key in ("deck_pads", "fx_pads", "children"):
            self.assertEqual(after[key], before[key], key)
        self.assertLessEqual(after["threads"] - before["threads"], 1)
        self.assertLessEqual(after["fds"] - before["fds"], 4)
        self.assertEqual(len(lab.engine.__dict__.get("_vt2_fires", {})), 0)


# ---------------------------------------------------------------------------
# PostgreSQL: the real claim and air-start transaction for scheduled decks.
# ---------------------------------------------------------------------------
class ScheduledDeckAccountingDbTests(TransactionTestCase):
    def setUp(self):
        from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
        self.models = SimpleNamespace(LogItem=LogItem, Track=Track)
        kind = CategoryKind.objects.create(code="b2music", name="B2 Music")
        self.category = Category.objects.create(code="B2MUSIC", name="B2 Music", kind=kind)
        self.artist = Artist.objects.create(name="B2 Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 10, 1), hour=9, status="approved")
        self.serial = 0

    def make(self, lab, name):
        self.serial += 1
        path = lab.media(name, music_segments(0.2, 1.2))
        track = self.models.Track.objects.create(
            filepath=str(path), filename=Path(path).name, title=f"B2 {self.serial}", artist=self.artist,
            category=self.category, ready2air=True, duration_seconds=1.9, next_start_seconds=1.8)
        item = self.models.LogItem.objects.create(
            playlist_log=self.log, position=self.serial, scheduled_time=timezone.now(), track=track,
            track_title=track.title, track_artist=self.artist.name, category=self.category)
        return self.models.LogItem.objects.select_related("track", "category").get(pk=item.pk)

    def real_lab(self):
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        lab.play()
        return lab

    def test_played_only_at_admission_once_and_reopen_keeps_the_claim(self):
        from library.models import PlayEvent
        lab = self.real_lab()
        engine = lab.engine
        item = self.make(lab, "db-sched")
        admission = lab.now() + 1.5
        wall0, run0 = time.time(), lab.now()
        engine._create_deck("B", item, scheduled_admission_ns=int(admission * NS))
        pump(0.5)
        row = self.models.LogItem.objects.get(pk=item.pk)
        claimed_at = row.playback_claimed_at
        self.assertIsNotNone(claimed_at)
        self.assertIsNone(row.played_at)
        self.assertFalse(PlayEvent.objects.exists())
        # Re-admission by regeneration: same row, claim timestamp untouched.
        new_admission = lab.now() + vt_transition.REOPEN_LEAD_S
        outcome, deck = engine._vt2_reopen_held_occurrence("B", int(new_admission * NS))
        self.assertEqual(outcome, "reopened")
        pump(0.05)
        row = self.models.LogItem.objects.get(pk=item.pk)
        self.assertEqual(row.playback_claimed_at, claimed_at)
        self.assertIsNone(row.played_at)
        self.assertTrue(wait_until(lambda: PlayEvent.objects.filter(log_item_id_snapshot=item.id).exists(), 3.0))
        row = self.models.LogItem.objects.get(pk=item.pk)
        expected_wall = wall0 + (new_admission - run0)
        event = PlayEvent.objects.get(log_item_id_snapshot=item.id)
        pump(2.5)
        event.refresh_from_db()
        report("DB scheduled_accounting", played_at_err=row.played_at.timestamp() - expected_wall,
               play_events=PlayEvent.objects.count(), duration=event.duration_played_seconds,
               claim_unchanged=row.playback_claimed_at == claimed_at)
        self.assertLessEqual(abs(row.played_at.timestamp() - expected_wall), 0.020)
        self.assertEqual(PlayEvent.objects.count(), 1)
        self.assertEqual(event.started_at, row.played_at)
        self.assertEqual(row.playback_claimed_at, claimed_at)
        self.assertIsNotNone(event.duration_played_seconds)
        self.assertAlmostEqual(event.duration_played_seconds, 1.7, delta=0.1)
        lab.stop()

    def test_failed_scheduled_deck_is_claimed_but_never_played(self):
        from library.models import PlayEvent
        lab = self.real_lab()
        item = self.make(lab, "db-fail")

        def stall(deck):
            pad = deck.seek_gate_pad
            pid = pad.add_probe(Gst.PadProbeType.BLOCK_DOWNSTREAM | Gst.PadProbeType.BUFFER,
                                lambda *_a: Gst.PadProbeReturn.OK)
            GLib.timeout_add(800, lambda: (pad.remove_probe(pid), False)[1])
        lab.engine._create_deck("B", item, scheduled_admission_ns=int((lab.now() + 0.6) * NS), before_sync=stall)
        pump(1.5)
        row = self.models.LogItem.objects.get(pk=item.pk)
        report("DB scheduled_failed", claimed=row.playback_claimed_at is not None, played=row.played_at is not None,
               play_events=PlayEvent.objects.count())
        self.assertIsNotNone(row.playback_claimed_at)
        self.assertIsNone(row.played_at)
        self.assertFalse(PlayEvent.objects.exists())
        lab.stop()
