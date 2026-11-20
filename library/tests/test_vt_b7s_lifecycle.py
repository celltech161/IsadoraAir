"""Roadmap 2.25 B7 supplemental -- VT fire and duck-curve resource lifecycle.

The B7S memory workload found every retired ScheduledVtFire (and its whole
decoding branch) retained for the life of the engine: its pad probes and the
decoder's pad-added handler capture the fire, and the fire kept its bin --
a cycle through native references that Python's collector cannot see. The
engine-lifetime VtDuckCurve also logged every no-owner release forever.

Evidence here is object lifetime, not RSS: a weakref on each fire and a
GObject finalize notification on its bin and decodebin, on the real engine
pipeline with real decoding (rendered program PCM for head/tail).

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import gc
import os
import time
import weakref

from django.test import SimpleTestCase

from library.services import vt_audio
from library.services.vt_audio import FireState
from library.tests._vt_b0_lab import NS, Gst, pump, report, wait_until
from library.tests.test_vt_b2_infrastructure import HEAD, MARK, TAIL, TOL, _B2Base, owner, vt_segments


class _LifecycleBase(_B2Base):
    def tracked(self, lab, path, *, admission, duration, own=None):
        """Prepare a fire and register lifetime evidence for it; returns
        (fire, probe) where probe holds ONLY weak references."""
        probe = {"finalized": set(), "refs": []}

        def watch(fire, elements):
            for name, obj in (("bin", fire.bin), ("decodebin", elements["decodebin"])):
                probe["refs"].append(obj.weak_ref(lambda n=name: probe["finalized"].add(n)))
        fire = self.fire(lab, path, admission=admission, duration=duration, own=own, before_start=watch)
        probe["fire"] = weakref.ref(fire)
        probe["built"] = bool(probe["refs"])
        return fire, probe

    def flush(self, lab):
        """Let retirement finish: the engine registry forgets retired fires
        (20 ms ticks) and the bounded NULL worker completes; one no-op job
        makes the worker drop its reference to the previous job."""
        engine = lab.engine
        self.settle(lab, 0.3)
        coordinator = engine.__dict__.get("_vt2_teardown")
        if coordinator is not None:
            coordinator.submit(10 ** 9 + int(time.monotonic() * 1000) % 10 ** 6, lambda: None)
            self.settle(lab, 0.2)
        for _ in range(3):
            gc.collect()
            pump(0.05)

    def assert_released(self, probe, label):
        fire = probe["fire"]()
        alive = fire is not None
        detail = {"fire_alive": alive, "finalized": sorted(probe["finalized"]), "built": probe["built"]}
        if alive:
            detail["referrers"] = [type(r).__name__ for r in gc.get_referrers(fire)][:8]
        report(f"LIFECYCLE {label}", **detail)
        del fire
        self.assertFalse(alive, f"{label}: the retired VtFire was not collected")
        if probe["built"]:
            self.assertEqual(probe["finalized"], {"bin", "decodebin"},
                             f"{label}: the retired branch was not finalized")


class FireCollectionTests(_LifecycleBase, SimpleTestCase):
    def test_completed_fires_are_collected_with_their_branches_and_tails_intact(self):
        lab = self.lab()
        engine = lab.engine
        path = lab.media("vt-life", vt_segments(0.6), fmt="flac")
        unstopped = len(vt_audio.UNSTOPPED_BINS)
        probes, timings = [], []
        for _n in range(6):
            admission = lab.now() + 0.4
            fire, probe = self.tracked(lab, path, admission=admission, duration=0.6)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            self.assertEqual(fire.teardown_outcome, "queued")
            self.assertIsNone(fire.bin, "the worker owns the accepted bin")
            self.assertEqual((fire._probe_handles, fire._signal_handles, fire._timer_ids), ([], [], []))
            pump(0.1)
            t = lab.trace()
            on = t.onset(HEAD, after=admission - 0.2, before=admission + 0.3)
            tail_on = t.onset(TAIL, after=admission + 0.2, before=admission + 0.8)
            tail_end = t.end(TAIL, after=tail_on + 0.05)
            timings.append((on - admission, tail_end - tail_on, tail_end - (admission + 0.6)))
            probes.append(probe)
            del fire
        self.flush(lab)
        report("LIFECYCLE completed", timings=[tuple(round(v, 4) for v in row) for row in timings],
               registry=len(engine.__dict__.get("_vt2_fires", {})),
               fire_bins=[c.get_name() for c in engine.main_pipeline.children if c.get_name().startswith("vt2_fire")])
        for i, probe in enumerate(probes):
            self.assert_released(probe, f"completed[{i}]")
        for onset_err, tail_len, end_err in timings:          # complete head and tail (F14) unchanged
            self.assertLessEqual(abs(onset_err), TOL)
            self.assertLessEqual(abs(tail_len - MARK), TOL)
            self.assertLessEqual(abs(end_err), TOL)
        self.assertEqual(engine.__dict__.get("_vt2_fires", {}), {})
        self.assertFalse([c for c in engine.main_pipeline.children if c.get_name().startswith("vt2_fire")])
        self.assertEqual(len(vt_audio.UNSTOPPED_BINS), unstopped)

    def test_cancellation_failure_and_shutdown_paths_release_their_resources(self):
        lab = self.lab()
        good = lab.media("vt-life-cancel", vt_segments(2.0))
        corrupt = lab.root / "life-corrupt.wav"
        corrupt.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk" + os.urandom(4096))
        cases = {}

        fire, cases["cancel_before_admission"] = self.tracked(lab, good, admission=lab.now() + 1.0, duration=2.0)
        pump(0.2)
        self.assertTrue(fire.cancel("test", fade_ms=50))
        self.assertEqual(fire.state, FireState.CANCELLED)

        admission = lab.now() + 0.4
        fire, cases["cancel_during_speech_fade"] = self.tracked(lab, good, admission=admission, duration=2.0)
        self.assertTrue(wait_until(lambda: lab.now() > admission + 0.5, 3.0))
        self.assertTrue(fire.cancel("test", fade_ms=60))
        pump(0.3)
        self.assertTrue(fire.retired)

        fire, cases["preparation_refused"] = self.tracked(lab, good, admission=lab.now() + 0.01, duration=2.0)
        self.assertEqual((fire.state, fire.reason), (FireState.FAILED, "admission_not_in_future"))

        fire, cases["corrupt_media"] = self.tracked(lab, corrupt, admission=lab.now() + 0.5, duration=1.0)
        self.assertTrue(wait_until(lambda: fire.state == FireState.FAILED, 3.0))

        fire, cases["missing_media"] = self.tracked(lab, lab.root / "life-missing.wav",
                                                    admission=lab.now() + 0.5, duration=1.0)
        self.assertTrue(wait_until(lambda: fire.state == FireState.FAILED, 3.0))

        admission = lab.now() + 0.4
        fire, cases["engine_shutdown"] = self.tracked(lab, good, admission=admission, duration=2.0)
        self.assertTrue(wait_until(lambda: lab.now() > admission + 0.3, 3.0))
        fire.cancel("engine_stopped", fade_ms=0)              # exactly what _vtt_shutdown does
        self.assertTrue(fire.retired)
        del fire
        self.flush(lab)
        for label, probe in cases.items():
            self.assert_released(probe, label)

    def test_repeated_cleanup_is_harmless_and_stale_callbacks_never_touch_a_new_fire(self):
        lab = self.lab()
        path = lab.media("vt-life-stale", vt_segments(0.8))
        first = self.fire(lab, path, admission=lab.now() + 0.4, duration=0.8)
        self.assertTrue(wait_until(lambda: first.state == FireState.ENDED, 3.0))
        outcome, errors = first.teardown_outcome, list(first.teardown_errors)
        for _ in range(3):                                    # repeated cleanup: harmless no-ops
            first._detach()
            first._release_registrations()
            first._cancel_timers()
            self.assertFalse(first.cancel("again"))
        self.assertEqual((first.teardown_outcome, first.teardown_errors), (outcome, errors))
        admission = lab.now() + 0.4
        second = self.fire(lab, path, admission=admission, duration=0.8)
        self.assertTrue(wait_until(lambda: second.state == FireState.ADMITTED, 3.0))
        history = list(second.history)
        old = (first.fire_id, 0)
        for callback in (first._on_ready, first._admission, first._on_eos, first._drained, first._ready_deadline,
                         first._first_audio_deadline, first._completion_deadline):
            callback(old)
        first._error(old, "late error")
        self.assertEqual(second.history, history, "a stale callback of a retired fire changed nothing")
        self.assertTrue(wait_until(lambda: second.state == FireState.ENDED, 3.0))
        pump(0.1)
        t = lab.trace()
        tail_on = t.onset(TAIL, after=admission + 0.4, before=admission + 1.0)
        tail_end = t.end(TAIL, after=tail_on + 0.05)
        report("LIFECYCLE stale", first_ignored=len(first.ignored), second_state=second.state,
               second_tail=tail_end - tail_on, second_end_err=tail_end - (admission + 0.8))
        self.assertEqual(first.state, FireState.ENDED)
        self.assertLessEqual(abs((tail_end - tail_on) - MARK), TOL)
        self.assertLessEqual(abs(tail_end - (admission + 0.8)), TOL)
        self.settle(lab)

    def test_a_teardown_the_worker_did_not_accept_keeps_its_bin_out_of_finalization(self):
        lab = self.lab()
        engine = lab.engine
        path = lab.media("vt-life-unaccepted", vt_segments(0.6))

        def refused(_fire, _bin):
            return "rejected_poisoned"
        held = len(vt_audio.UNSTOPPED_BINS)
        fire = vt_audio.ScheduledVtFire(
            owner=owner(), path=path, gain_db=0.0, admission_ns=lab.now_ns() + int(0.4 * NS),
            validated_duration=0.6, pipeline=engine.main_pipeline, mixer=engine.fx_submix,
            rate=lab.rate, now_ns=engine._vt2_running_ns, timer_at=engine._vt2_timer_at, null_submit=refused)
        self.assertTrue(fire.prepare())
        self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
        report("LIFECYCLE unaccepted", outcome=fire.teardown_outcome, held=len(vt_audio.UNSTOPPED_BINS) - held)
        self.assertEqual(fire.teardown_outcome, "rejected_poisoned")
        self.assertIsNotNone(fire.bin, "a bin nobody will stop is kept (never finalized while PLAYING)")
        self.assertEqual(vt_audio.UNSTOPPED_BINS[-1], (fire.fire_id, fire.bin))
        self.assertEqual((fire._probe_handles, fire._signal_handles), ([], []))
        self.assertIsNone(fire.mixer_pad)
        fire.bin.set_state(Gst.State.NULL)                    # test cleanup only
        vt_audio.UNSTOPPED_BINS.pop()


class DuckHistoryTests(_LifecycleBase, SimpleTestCase):
    def test_duck_diagnostics_stay_bounded_and_ownership_is_unchanged(self):
        lab = self.lab()
        curve = lab.engine._vt2_duck_curve()
        ramp = int(0.05 * NS)
        for _ in range(5000):                       # the engine's speech-free refresh (owner ("vtt",))
            self.assertFalse(curve.release(("vtt",), ramp))
        a, b = owner(seq=1), owner(seq=2)
        now = lab.now_ns()
        curve.apply(a, [(now + int(0.2 * NS), 0.5), (now + int(0.4 * NS), 0.5)])
        self.assertFalse(curve.release(b, ramp), "a stale owner's release is still refused")
        self.assertEqual(curve.owner, a)
        self.assertTrue(curve.release(a, ramp))
        self.assertIsNone(curve.owner)
        report("LIFECYCLE duck", ignored=len(curve.ignored), ignored_total=curve.ignored_total)
        self.assertEqual(len(curve.ignored), vt_audio.VtDuckCurve.IGNORED_HISTORY)
        self.assertEqual(curve.ignored_total, 5001)
        self.assertEqual(curve.ignored[-1], ("release", b))
