"""Roadmap 2.25 C10 -- durable ownership of VT branch teardown.

A detached VT bin must have an owner until its NULL transition is confirmed,
or it must be quarantined for the life of the process: a GStreamer element
finalized while PLAYING is a lifetime violation ("Trying to dispose element
..., but it is in PLAYING instead of the NULL state"). The bounded VT
teardown coordinator owns every accepted bin (``retain=``) until its
operation completes, and moves every bin whose teardown did not complete --
refused, failed, timed out, abandoned without starting -- to
``audio_recovery.TEARDOWN_QUARANTINE``. Any such outcome suspends further VT
preparation (ordinary playout continues) and the in-flight cap bounds how
many bins can ever be retained.

Evidence is real: engine pipeline, real decoding branches, GObject finalize
notifications, the bins' own states, and file descriptor 2 captured for
GStreamer critical warnings.

Run only through production/tests/run_with_validation_service.sh.
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services import audio_recovery
from library.services.vt_audio import FireState
from library.tests._vt_b0_lab import Gst, pump, report, wait_until
from library.tests.test_vt_b2_infrastructure import HEAD, MARK, MUSIC, TAIL, TOL, _B2Base, vt_segments

BLOCKER = 900_000_001
DISPOSE_MARKERS = ("Trying to dispose", "GStreamer-CRITICAL")


class _Fd2:
    """Capture file descriptor 2 (where GStreamer prints criticals); the
    captured text is re-emitted on exit so nothing is hidden."""

    def __enter__(self):
        sys.stderr.flush()
        self._tmp = tempfile.TemporaryFile()
        self._saved = os.dup(2)
        os.dup2(self._tmp.fileno(), 2)
        self.text = ""
        return self

    def read(self):
        sys.stderr.flush()
        self._tmp.seek(0)
        self.text = self._tmp.read().decode(errors="replace")
        return self.text

    def __exit__(self, *_exc):
        self.read()
        os.dup2(self._saved, 2)
        os.close(self._saved)
        self._tmp.close()
        if self.text:
            sys.stderr.write(self.text)
            sys.stderr.flush()
        return False

    def criticals(self):
        return [line for line in self.read().splitlines() if any(m in line for m in DISPOSE_MARKERS)]


def playing_bin(name):
    """A real, self-contained bin in PLAYING (unsafe to finalize until NULL)."""
    b = Gst.Bin.new(name)
    src = Gst.ElementFactory.make("audiotestsrc", None)
    sink = Gst.ElementFactory.make("fakesink", None)
    src.set_property("is-live", True)
    sink.set_property("sync", False)
    for el in (src, sink):
        b.add(el)
    src.link(sink)
    b.set_state(Gst.State.PLAYING)
    b.get_state(Gst.CLOCK_TIME_NONE)
    return b


def state_of(element):
    return element.get_state(0)[1]


class _C10Base(_B2Base):
    def setUp(self):
        super().setUp()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        self.addCleanup(self._release_quarantine, start)

    def _release_quarantine(self, start):
        """Test cleanup only: what the process would hold until exit."""
        entries = audio_recovery.TEARDOWN_QUARANTINE[start:]
        del audio_recovery.TEARDOWN_QUARANTINE[start:]
        for entry in entries:
            obj = entry["obj"]
            if isinstance(obj, Gst.Element):
                obj.__dict__.pop("set_state", None)
                obj.set_state(Gst.State.NULL)

    def quarantined_since(self, start):
        return audio_recovery.TEARDOWN_QUARANTINE[start:]

    def watched_fire(self, lab, path, *, admission, duration, extra=None):
        """Prepare a real fire; only WEAK evidence is kept (GObject finalize
        notification on bin and decodebin), so nothing here owns the bin."""
        finalized = set()
        refs = self.__dict__.setdefault("_weak_handles", [])     # kept by the test, not the fire

        def watch(fire, elements):
            for name, obj in (("bin", fire.bin), ("decodebin", elements["decodebin"])):
                refs.append(obj.weak_ref(lambda n=name: finalized.add(n)))
            if extra is not None:
                extra(fire, elements)
        fire = self.fire(lab, path, admission=admission, duration=duration, before_start=watch)
        return fire, finalized

    def block_worker(self, coordinator):
        gate = threading.Event()
        self.addCleanup(gate.set)
        self.assertEqual(coordinator.submit(BLOCKER, lambda: gate.wait(30), metadata={"blocker": True}), "queued")
        self.assertTrue(wait_until(lambda: coordinator.snapshot()["active_generation"] == BLOCKER, 2.0))
        return gate

    def settle_gc(self, seconds=0.3):
        pump(seconds)
        for _ in range(3):
            gc.collect()
            pump(0.05)

    def capture_events(self):
        events = []
        patcher = patch.object(eng_module, "emit_event", new=lambda **kw: events.append(kw))
        patcher.start()
        self.addCleanup(patcher.stop)
        return events

    @staticmethod
    def keys(events):
        return [e.get("dedupe_key") for e in events]


class AbnormalTeardownTests(_C10Base, SimpleTestCase):
    def test_healthy_queued_teardown_releases_promptly_without_quarantine(self):
        lab = self.lab()
        engine = lab.engine
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        path = lab.media("c10-healthy", vt_segments(0.6), fmt="flac")
        with _Fd2() as fd2:
            admission = lab.now() + 0.4
            fire, finalized = self.watched_fire(lab, path, admission=admission, duration=0.6)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            self.assertEqual(fire.teardown_outcome, "queued")
            self.assertIsNone(fire.bin)
            pump(0.1)
            t = lab.trace()
            tail_on = t.onset(TAIL, after=admission + 0.2, before=admission + 0.8)
            tail_end = t.end(TAIL, after=tail_on + 0.05)
            onset = t.onset(HEAD, after=admission - 0.2, before=admission + 0.3)
            coordinator = engine._vt2_teardown_coordinator()
            self.assertTrue(wait_until(lambda: coordinator.snapshot()["owned"] == 0, 2.0))
            coordinator.submit(10 ** 9 + 1, lambda: None)          # the worker drops its previous job
            del fire
            self.settle_gc()
            criticals = fd2.criticals()
        snap = coordinator.snapshot()
        report("C10 healthy", finalized=sorted(finalized), owned=snap["owned"], quarantined=snap["quarantined"],
               onset_err=onset - admission, tail=tail_end - tail_on, criticals=len(criticals))
        self.assertEqual(finalized, {"bin", "decodebin"})
        self.assertEqual((snap["owned"], snap["failed"], snap["quarantined"]), (0, 0, 0))
        self.assertEqual(self.quarantined_since(start), [])
        self.assertEqual(criticals, [])
        self.assertLessEqual(abs(onset - admission), TOL)
        self.assertLessEqual(abs((tail_end - tail_on) - MARK), TOL)
        self.assertEqual(engine._vt2_teardown_admission(), (True, ""))

    def test_accepted_teardown_abandoned_after_an_earlier_timeout_stays_owned_and_suspends_vt(self):
        """Codex C9 reproduction: an earlier NULL hangs, a completed VT's
        teardown is queued behind it, the hang times out and the queue is
        abandoned. The queued bin must stay owned (quarantined), never be
        finalized while PLAYING; VT suspends; ordinary playout continues."""
        lab = self.lab()
        engine = lab.engine
        events = self.capture_events()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        music = lab.media("c10-music", [(14.0, MUSIC)])
        engine._create_deck("A", lab.item(1, music, duration=14.0))
        pump(0.6)
        path = lab.media("c10-abandon", vt_segments(0.6), fmt="flac")
        coordinator = engine._vt2_teardown_coordinator()
        with _Fd2() as fd2:
            gate = self.block_worker(coordinator)
            fire, finalized = self.watched_fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            self.assertEqual(fire.teardown_outcome, "queued")
            self.assertIsNone(fire.bin, "the coordinator, not the fire, owns the queued bin")
            self.assertEqual(coordinator.snapshot()["owned"], 1)
            coordinator.tick(now=time.monotonic() + 60.0)            # the earlier NULL outlived its bound
            snap = coordinator.snapshot()
            self.assertTrue(snap["poisoned"])
            self.assertEqual(snap["owned"], 0)
            self.settle_gc(0.5)                                      # engine tick reports; gc runs
            held = self.quarantined_since(start)
            self.assertEqual([(e["generation"], e["kind"]) for e in held],
                             [(fire.fire_id, "abandoned_without_start")])
            vt_bin = held[0]["obj"]
            self.assertEqual(state_of(vt_bin), Gst.State.PLAYING, "unsafe to finalize: still PLAYING")
            self.assertEqual(finalized, set(), "a quarantined PLAYING bin is never finalized")

            # Repeated cleanup is harmless: no second quarantine entry, no error.
            for _ in range(3):
                fire._detach()
                fire._release_registrations()
                fire._cancel_timers()
            self.assertEqual(len(self.quarantined_since(start)), 1)

            # VT suspended: no new branch is built; ordinary program audio continues.
            refused, _ = self.watched_fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
            self.assertEqual((refused.state, refused.reason), (FireState.FAILED, "vt_teardown_unavailable:poisoned"))
            self.assertIsNone(refused.bin)
            self.assertFalse(engine._vtt_fires_available())
            t0 = lab.now()
            pump(1.0)
            aired = lab.trace().present(MUSIC, t0, t0 + 0.9)

            # A late result from the hung operation never releases quarantine.
            gate.set()
            self.assertTrue(wait_until(lambda: any(
                e.get("detail", {}).get("teardown_event") == "late_result_discarded" for e in events), 3.0))
            self.settle_gc(0.3)
            criticals = fd2.criticals()
        keys = self.keys(events)
        report("C10 abandoned", quarantine=[e["kind"] for e in self.quarantined_since(start)],
               bin_state=state_of(vt_bin).value_nick, finalized=sorted(finalized), music_aired=aired,
               events=sorted(set(k for k in keys if k)), criticals=len(criticals))
        self.assertEqual(len(self.quarantined_since(start)), 1)
        self.assertEqual(finalized, set())
        self.assertEqual(criticals, [])
        self.assertGreaterEqual(aired, 0.85, "ordinary playout continues")
        for kind in ("timed_out", "abandoned_without_start", "late_result_discarded"):
            self.assertIn(f"engine|vt2-teardown|{kind}", keys)
        self.assertEqual(keys.count("engine|vt-teardown-unavailable"), 1)
        detail = next(e["detail"] for e in events if e.get("dedupe_key") == "engine|vt2-teardown|abandoned_without_start")
        self.assertEqual((detail["voice_tracks_suspended"], detail["program_playout_interrupted"],
                          detail["generation"]), (True, False, fire.fire_id))

    def test_an_asynchronous_null_failure_quarantines_the_bin_and_suspends_vt(self):
        lab = self.lab()
        engine = lab.engine
        events = self.capture_events()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        path = lab.media("c10-failed", vt_segments(0.6))

        def refuse_null(fire, _elements):
            b = fire.bin

            def set_state(state):
                if state == Gst.State.NULL:
                    return Gst.StateChangeReturn.FAILURE
                return Gst.Element.set_state(b, state)
            b.set_state = set_state
        with _Fd2() as fd2:
            fire, finalized = self.watched_fire(lab, path, admission=lab.now() + 0.4, duration=0.6, extra=refuse_null)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            coordinator = engine._vt2_teardown_coordinator()
            self.assertTrue(wait_until(lambda: coordinator.snapshot()["failed"] == 1, 2.0))
            self.settle_gc(0.4)
            held = self.quarantined_since(start)
            criticals = fd2.criticals()
        snap = coordinator.snapshot()
        report("C10 failed", outcome=fire.teardown_outcome, quarantine=[e["kind"] for e in held],
               state=state_of(held[0]["obj"]).value_nick, poisoned=snap["poisoned"], owned=snap["owned"],
               admission=engine._vt2_teardown_admission(), criticals=len(criticals))
        self.assertEqual(fire.teardown_outcome, "queued")
        self.assertEqual([(e["generation"], e["kind"]) for e in held], [(fire.fire_id, "failed")])
        self.assertEqual(state_of(held[0]["obj"]), Gst.State.PLAYING)
        self.assertEqual(finalized, set())
        self.assertEqual(snap["owned"], 0)
        self.assertFalse(snap["poisoned"], "a failure is not a hang: the worker stays usable for decks")
        self.assertEqual(engine._vt2_teardown_admission(), (False, "quarantined"))
        self.assertIn("engine|vt2-teardown|failed", self.keys(events))
        self.assertEqual(self.keys(events).count("engine|vt-teardown-unavailable"), 1)
        refused = self.fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
        self.assertEqual(refused.reason, "vt_teardown_unavailable:quarantined")
        self.assertEqual(criticals, [])

    def test_queue_full_rejections_quarantine_and_further_vt_admission_is_refused(self):
        lab = self.lab()
        engine = lab.engine
        events = self.capture_events()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        path = lab.media("c10-full", vt_segments(0.6))
        coordinator = engine._vt2_teardown_coordinator()
        with _Fd2() as fd2:
            gate = self.block_worker(coordinator)
            for n in range(coordinator.queue_capacity):
                self.assertEqual(coordinator.submit(BLOCKER + 1 + n, lambda: None), "queued")
            fire, finalized = self.watched_fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            self.assertEqual(fire.teardown_outcome, "rejected_full")
            built = []
            for _ in range(20):                                   # repeated attempts build nothing
                again = self.fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
                built.append(again.bin is not None or again.state != FireState.FAILED)
            # Repeated queue-full rejections at the coordinator: every refused bin is kept.
            extra = [playing_bin(f"c10-full-{n}") for n in range(5)]
            refs, gone = [], set()
            for n, b in enumerate(extra):
                refs.append(b.weak_ref(lambda n=n: gone.add(n)))
                self.assertEqual(coordinator.submit(5_000 + n, lambda: None, retain=b), "rejected_full")
            del extra, b
            self.settle_gc(0.4)
            held = self.quarantined_since(start)
            criticals_before_drain = fd2.criticals()
            gate.set()
            self.assertTrue(wait_until(lambda: coordinator.snapshot()["queue_depth"] == 0, 3.0))
            self.settle_gc(0.2)
            criticals = fd2.criticals()
        report("C10 queue_full", quarantine=[e["kind"] for e in held], built_after=sum(built),
               finalized=sorted(finalized), extra_finalized=sorted(gone), criticals=len(criticals))
        self.assertEqual([e["kind"] for e in held], ["rejected_full"] * 6)
        self.assertTrue(all(state_of(e["obj"]) == Gst.State.PLAYING for e in held))
        self.assertEqual((finalized, gone), (set(), set()))
        self.assertEqual(sum(built), 0, "no VT branch is built while the teardown system is unhealthy")
        self.assertIn("engine|vt2-teardown|rejected_full", self.keys(events))
        self.assertEqual(self.keys(events).count("engine|vt-teardown-unavailable"), 1)
        self.assertEqual((criticals_before_drain, criticals), ([], []))

    def test_coordinator_shutdown_keeps_accepted_work_owned_until_its_null_completes(self):
        lab = self.lab()
        engine = lab.engine
        events = self.capture_events()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        path = lab.media("c10-stop", vt_segments(0.6))
        coordinator = engine._vt2_teardown_coordinator()
        with _Fd2() as fd2:
            gate = self.block_worker(coordinator)
            fire, finalized = self.watched_fire(lab, path, admission=lab.now() + 0.4, duration=0.6)
            self.assertTrue(wait_until(lambda: fire.state == FireState.ENDED, 3.0))
            self.assertEqual(fire.teardown_outcome, "queued")
            coordinator.stop()
            late = playing_bin("c10-after-stop")
            self.assertEqual(coordinator.submit(6_000, lambda: None, retain=late), "rejected_stopping")
            del late, fire
            self.settle_gc(0.3)
            owned_while_blocked = coordinator.snapshot()["owned"]
            finalized_while_blocked = set(finalized)
            gate.set()                                            # the stopping worker drains its queue
            self.assertTrue(wait_until(lambda: coordinator.snapshot()["owned"] == 0, 3.0))
            self.assertTrue(wait_until(lambda: not coordinator.snapshot()["worker_alive"], 3.0))
            self.settle_gc(0.3)
            criticals = fd2.criticals()
        held = self.quarantined_since(start)
        report("C10 shutdown", owned_while_blocked=owned_while_blocked,
               finalized_while_blocked=sorted(finalized_while_blocked), finalized_after=sorted(finalized),
               quarantine=[e["kind"] for e in held], criticals=len(criticals))
        self.assertEqual(owned_while_blocked, 1)
        self.assertEqual(finalized_while_blocked, set())
        self.assertEqual(finalized, {"bin", "decodebin"}, "confirmed NULL, then released")
        self.assertEqual([e["kind"] for e in held], ["rejected_stopping"])
        self.assertEqual(engine._vt2_teardown_admission(), (False, "stopping"))
        self.assertIn("engine|vt2-teardown|rejected_stopping", self.keys(events))
        self.assertEqual(criticals, [])

    def test_in_flight_cap_refuses_new_vt_without_quarantine_and_recovers(self):
        lab = self.lab()
        engine = lab.engine
        events = self.capture_events()
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        path = lab.media("c10-cap", vt_segments(2.0))
        cap = eng_module.VT_MAX_IN_FLIGHT_BINS
        live = [self.fire(lab, path, admission=lab.now() + 0.6, duration=2.0) for _ in range(cap)]
        self.assertTrue(all(f.state not in FireState.TERMINAL for f in live))
        over = self.fire(lab, path, admission=lab.now() + 0.6, duration=2.0)
        reason, refused_bin = over.reason, over.bin
        for f in live:
            f.cancel("test", fade_ms=0)
        self.settle(lab, 0.4)
        after = self.fire(lab, path, admission=lab.now() + 0.4, duration=2.0)
        after_state = after.state
        after.cancel("test", fade_ms=0)
        self.settle(lab, 0.3)
        report("C10 in_flight_cap", cap=cap, refused=reason, recovered=after_state,
               quarantine=len(self.quarantined_since(start)))
        self.assertEqual(reason, "vt_teardown_unavailable:in_flight_limit")
        self.assertIsNone(refused_bin)
        self.assertNotEqual(after_state, FireState.FAILED, "transient: admission resumes after retirement")
        self.assertEqual(self.quarantined_since(start), [])
        self.assertNotIn("engine|vt-teardown-unavailable", self.keys(events))


class CoordinatorOwnershipTests(_C10Base, SimpleTestCase):
    """The generic coordinator with real PLAYING bins (no engine)."""

    def coordinator(self):
        c = audio_recovery.BoundedTeardownCoordinator("c10-unit", timeout_s=5.0, queue_capacity=2)
        self.addCleanup(c.stop)
        return c

    def test_an_earlier_or_duplicate_completion_never_releases_another_generations_object(self):
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        c = self.coordinator()
        with _Fd2() as fd2:
            gate = self.block_worker(c)
            first, dup = playing_bin("c10-gen7"), playing_bin("c10-gen7-dup")
            done, refs = set(), [first.weak_ref(lambda: done.add("first")), dup.weak_ref(lambda: done.add("dup"))]

            def null(b):
                return lambda: b.set_state(Gst.State.NULL)
            self.assertEqual(c.submit(7, null(first), retain=first), "queued")
            self.assertEqual(c.submit(7, lambda: None, retain=dup), "duplicate")
            del first, dup
            gc.collect()
            mid = (set(done), c.snapshot()["owned"])
            gate.set()
            self.assertTrue(wait_until(lambda: c.snapshot()["owned"] == 0, 3.0))
            c.submit(8, lambda: None)                          # the worker drops its previous job
            self.assertTrue(wait_until(lambda: c.snapshot()["completed"] >= 3, 3.0))
            self.settle_gc(0.1)
            held = self.quarantined_since(start)
            criticals = fd2.criticals()
        report("C10 generations", mid=mid, after=sorted(done), quarantine=[(e["generation"], e["kind"]) for e in held])
        self.assertEqual(mid, (set(), 1))
        self.assertEqual(done, {"first"}, "gen 7 released only after its own NULL; the duplicate stays held")
        self.assertEqual([(e["generation"], e["kind"]) for e in held], [(7, "duplicate_ignored")])
        self.assertEqual(state_of(held[0]["obj"]), Gst.State.PLAYING)
        self.assertEqual(criticals, [])

    def test_poisoned_drain_and_repeated_quarantine_are_idempotent_and_bounded(self):
        start = len(audio_recovery.TEARDOWN_QUARANTINE)
        c = self.coordinator()
        with _Fd2() as fd2:
            gate = self.block_worker(c)
            pending = [playing_bin(f"c10-p{n}") for n in range(2)]
            for n, b in enumerate(pending):
                self.assertEqual(c.submit(10 + n, lambda: None, retain=b), "queued")
            c.tick(now=time.monotonic() + 60.0)
            c.tick(now=time.monotonic() + 120.0)                  # repeated: no second abandonment
            for n in range(3):
                self.assertEqual(c.submit(20 + n, lambda: None, retain=playing_bin(f"c10-r{n}")),
                                 "rejected_poisoned")
            del pending, b
            self.settle_gc(0.1)
            gate.set()
            self.settle_gc(0.3)
            held = self.quarantined_since(start)
            criticals = fd2.criticals()
        snap = c.snapshot()
        report("C10 poisoned_drain", quarantine=[(e["generation"], e["kind"]) for e in held], snap_q=snap["quarantined"])
        self.assertEqual([(e["generation"], e["kind"]) for e in held],
                         [(10, "abandoned_without_start"), (11, "abandoned_without_start"),
                          (20, "rejected_poisoned"), (21, "rejected_poisoned"), (22, "rejected_poisoned")])
        self.assertEqual((snap["owned"], snap["quarantined"]), (0, 5))
        self.assertTrue(all(state_of(e["obj"]) == Gst.State.PLAYING for e in held))
        self.assertEqual(criticals, [])
