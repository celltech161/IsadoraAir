"""Roadmap 2.27 Phase D: isolated timing harness (development host only).

A real GLib main loop on its own thread plays the engine thread. A 250 ms
GLib timer calls the UNCHANGED PlaybackEngine._reload_queue_if_changed on a
stand-in engine (real 3 s throttle, random starting phase, real database
reads on that thread's own connection). The runner runs on its real thread
with its real wake, and its snapshot captures are real GLib.idle_add
callbacks. No audio device, deck pipeline or transmitter is involved; one
scenario adds an audiotestsrc ! audiomixer ! fakesink pipeline whose bus is
watched on the same loop.

Every figure is a monotonic measurement of THIS development host. None is a
KOGR or WRJE pipeline measurement. Assertions are structural only: each
install is adopted by the reload before its earliest trigger, a held lock
longer than 250 ms abstains with nothing written. Set
HOUR_REFILL_PHASE_D_TIMING=<dir> to write the distributions."""
import json
import os
import random
import statistics
import threading
import time
from pathlib import Path

from django.db import connection, connections, transaction
from django.test import TransactionTestCase, override_settings

from library.models import HourRefillCause, HourRefillInstall, HourRefillPolicy, LogItem, PlaylistLog
from library.services import hour_refill_runner as runner_mod
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_hour_refill_install_2_27 import InstallFixtures
from library.tests.test_hour_refill_proposal_2_27 import at

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst
except (ImportError, ValueError):  # pragma: no cover -- environment without PyGObject
    GLib = Gst = None


def _stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {"n": len(values), "min": round(values[0], 4), "median": round(statistics.median(values), 4),
            "p90": round(values[min(len(values) - 1, int(len(values) * 0.9))], 4), "max": round(values[-1], 4)}


class GLibHarness:
    """The engine thread stand-in: one GLib loop, one 250 ms poll."""

    def __init__(self, *, busy_ms=0, pipeline=False):
        self.busy_ms, self.pipeline_wanted = busy_ms, pipeline
        self.engine, self.watch, self.errors = None, None, []
        self.loop, self.thread, self.pipeline = GLib.MainLoop(), None, None
        self.poll_id, self.bus, self.stopped = None, None, False

    def start(self):
        ready = threading.Event()

        def run():
            self.poll_id = GLib.timeout_add(250, self._poll)
            GLib.idle_add(lambda: (ready.set(), False)[1])
            if self.pipeline_wanted:
                Gst.init(None)
                self.pipeline = Gst.parse_launch(
                    "audiotestsrc is-live=true wave=silence ! audiomixer ! fakesink sync=true")
                self.bus = bus = self.pipeline.get_bus()
                bus.add_signal_watch()
                bus.connect("message", lambda *a: None)
                self.pipeline.set_state(Gst.State.PLAYING)
            self.loop.run()

        self.thread = threading.Thread(target=run, name="glib-harness", daemon=True)
        self.thread.start()
        assert ready.wait(10)

    def _poll(self):
        if self.stopped:
            return False
        try:
            if self.busy_ms:
                end = time.monotonic() + self.busy_ms / 1000.0
                while time.monotonic() < end:  # simulated heavy GLib work, not a sleep
                    pass
            engine, watch = self.engine, self.watch
            if engine is not None:
                engine._reload_queue_if_changed()
                if watch is not None and watch["ids"] <= {item.id for item in engine.log_items}:
                    watch["seen"] = time.monotonic()
                    watch["event"].set()
                    self.watch = None
        except Exception as exc:  # noqa: BLE001
            self.errors.append(repr(exc))
        return True

    def stop(self):
        done = threading.Event()

        def finish():
            # Leave nothing attached to the shared default main context: later
            # tests (real GStreamer fades) run their own loops on it.
            self.stopped = True
            if self.poll_id is not None:
                GLib.source_remove(self.poll_id)
                self.poll_id = None
            if self.bus is not None:
                self.bus.remove_signal_watch()
            if self.pipeline is not None:
                self.pipeline.set_state(Gst.State.NULL)
            connection.close()
            self.loop.quit()
            done.set()
            return False

        GLib.idle_add(finish)
        done.wait(10)
        self.thread.join(10)


@override_settings(HOUR_REFILL_INSTALL_ARMED=True)
class TimingHarnessTests(InstallFixtures, TransactionTestCase):
    def setUp(self):
        if GLib is None:
            self.skipTest("PyGObject/GLib not available")
        ensure_schedule_profile_state()
        self.build()
        HourRefillPolicy(enabled=True, dry_run_only=False, capture_request_substitution=True,
                         capture_operator_eject=True).save()
        self.rng = random.Random(2027)
        self.samples = {}

    def tearDown(self):
        connections.close_all()

    def _reset(self):
        LogItem.objects.all().delete()
        HourRefillCause.objects.all().delete()
        HourRefillInstall.objects.all().delete()
        PlaylistLog.objects.exclude(pk=self.log.pk).delete()

    def _one(self, harness, scenario, *, hold_lock_ms=0):
        """One cause -> wake -> runner -> installer -> COMMIT -> reload."""
        self._reset()
        self.eject_hour()
        HourRefillCause.objects.all().delete()  # the cause is re-created after the runner starts
        self.track(200.0), self.track(180.0)
        engine = self.engine(cursor=3, deck_index=2, deck_position=10.0)
        engine._last_queue_reload = time.time() - self.rng.uniform(0.0, 3.0)  # random throttle phase
        harness.engine = engine
        marks, captures = {}, []

        def idle_add(fn, *args):
            requested = time.monotonic()

            def callback(*cb_args):
                started = time.monotonic()
                try:
                    return fn(*cb_args)
                finally:
                    captures.append((requested, started, time.monotonic()))

            return GLib.idle_add(callback, *args)

        def hold():
            held = threading.Event()

            def holder():
                try:
                    with transaction.atomic():
                        PlaylistLog.objects.select_for_update().get(pk=self.log.pk)
                        held.set()
                        end = time.monotonic() + hold_lock_ms / 1000.0
                        while time.monotonic() < end:
                            time.sleep(0.005)
                finally:
                    connections.close_all()

            threading.Thread(target=holder, daemon=True).start()
            held.wait(5)

        hooks = {
            "before_second_snapshot": lambda: marks.setdefault("proposal_done", time.monotonic()),
            "before_transaction": lambda s2: (marks.setdefault("cas1_done", time.monotonic()),
                                              hold() if hold_lock_ms else None),
            "locked": lambda: marks.setdefault("locked", time.monotonic()),
            "before_commit": lambda: marks.setdefault("writes_done", time.monotonic()),
        }
        runner = runner_mod.HourRefillRunner(engine, idle_add=idle_add, status_path=None,
                                             wall_clock=lambda: at(14, 40), check_interval=60.0,
                                             installer_hooks=hooks)
        first, checked = threading.Event(), threading.Event()
        original_once, original_record = runner.run_once, runner._record_install

        def run_once(generation=None):
            marks.setdefault("check_start", time.monotonic())
            try:
                return original_once(generation)
            finally:
                (checked if "woken" in marks else first).set()

        def record(result, projection):
            marks["committed"] = time.monotonic()
            harness.watch = {"ids": frozenset(result.installed_log_item_ids), "event": threading.Event(),
                             "seen": None}
            marks["watch"] = harness.watch
            return original_record(result, projection)

        runner.run_once, runner._record_install = run_once, record
        runner.start()
        self.assertTrue(first.wait(30))  # the first (empty) check ends before the cause exists
        marks.clear()
        self.eject_cause(self.ejected)  # committed by "another thread": the recorder's insert
        marks["woken"] = time.monotonic()
        runner.wake()
        self.assertTrue(checked.wait(30), scenario)
        runner.stop(join_seconds=2)
        outcome = runner.last_outcome or {}
        sample = {"status": outcome.get("status"), "reasons": outcome.get("reasons")}
        if outcome.get("status") == "installed":
            watch = marks["watch"]
            self.assertTrue(watch["event"].wait(15), "reload never adopted the committed rows")
            s2_done = captures[2][2]
            sample.update(
                wake_to_check=marks["check_start"] - marks["woken"],
                capture_queue=[c[1] - c[0] for c in captures], capture_run=[c[2] - c[1] for c in captures],
                s1_to_proposal=marks["proposal_done"] - captures[1][2],
                s2_to_lock=marks["locked"] - s2_done, lock_to_commit=marks["committed"] - marks["locked"],
                s2_to_commit=marks["committed"] - s2_done, commit_to_adoption=watch["seen"] - marks["committed"],
                s2_to_adoption=watch["seen"] - s2_done, earliest_trigger_lead=outcome["insertion_lead_seconds"],
                wake_to_adoption=watch["seen"] - marks["woken"],
            )
            # The whole point of the 20 s lead: the reload adopts before the trigger.
            self.assertLess(sample["s2_to_adoption"], sample["earliest_trigger_lead"], sample)
            self.assertLess(sample["s2_to_adoption"], 20.0, sample)
        self.samples.setdefault(scenario, []).append(sample)
        return sample

    def _run_scenario(self, scenario, n, *, busy_ms=0, pipeline=False, cpu_threads=0, hold_lock_ms=0):
        harness = GLibHarness(busy_ms=busy_ms, pipeline=pipeline)
        harness.start()
        stop_cpu = threading.Event()

        def burn():
            while not stop_cpu.is_set():
                sum(range(2000))  # pure-Python work that contends for the GIL

        burners = [threading.Thread(target=burn, daemon=True) for _ in range(cpu_threads)]
        for t in burners:
            t.start()
        try:
            return [self._one(harness, scenario, hold_lock_ms=hold_lock_ms) for _ in range(n)]
        finally:
            stop_cpu.set()
            for t in burners:
                t.join(5)
            harness.stop()
            self.assertEqual(harness.errors, [])

    def _summarise(self):
        summary = {}
        for scenario, samples in self.samples.items():
            installed = [s for s in samples if s["status"] == "installed"]
            metrics = {}
            for key in ("wake_to_check", "s1_to_proposal", "s2_to_lock", "lock_to_commit", "s2_to_commit",
                        "commit_to_adoption", "s2_to_adoption", "wake_to_adoption", "earliest_trigger_lead"):
                metrics[key] = _stats([s.get(key) for s in installed])
            metrics["capture_queue"] = _stats([v for s in installed for v in s.get("capture_queue", [])])
            metrics["capture_run"] = _stats([v for s in installed for v in s.get("capture_run", [])])
            summary[scenario] = {"runs": len(samples), "statuses": sorted({s["status"] for s in samples}),
                                 "installed": len(installed), "seconds": metrics}
        target = os.environ.get("HOUR_REFILL_PHASE_D_TIMING")
        if target:
            Path(target).mkdir(parents=True, exist_ok=True)
            (Path(target) / "timing-summary.json").write_text(json.dumps(
                {"host_note": "development host only; not a KOGR/WRJE measurement", "scenarios": summary},
                indent=2, sort_keys=True))
            (Path(target) / "timing-samples.json").write_text(json.dumps(self.samples, indent=1, default=str))
        return summary

    def test_timing_distributions_and_structural_limits(self):
        self._run_scenario("idle", 12)
        self._run_scenario("cpu_contention_3_gil_threads", 8, cpu_threads=3)
        self._run_scenario("glib_busy_150ms_per_tick_with_gst_pipeline", 8, busy_ms=150, pipeline=True)
        self._run_scenario("db_lock_held_150ms", 5, hold_lock_ms=150)
        held = self._run_scenario("db_lock_held_400ms", 2, hold_lock_ms=400)
        summary = self._summarise()
        for scenario in ("idle", "cpu_contention_3_gil_threads", "glib_busy_150ms_per_tick_with_gst_pipeline",
                         "db_lock_held_150ms"):
            self.assertEqual(summary[scenario]["statuses"], ["installed"], (scenario, self.samples[scenario]))
        self.assertEqual([(s["status"], s["reasons"]) for s in held],
                         [("lock_contended", ["lock_timeout_or_deadlock"])] * 2)
