"""D3E-A corrective pass: the worker heartbeat contract (active_messages.heartbeat).

* the strict decoder: every document that breaks the contract is "malformed"
  (critical) -- never an exception, never "ok", never partially interpreted;
* file safety: the reader never follows a symlink or reads a non-regular or
  oversized file; the writer creates an unpredictable temporary exclusively
  (a pre-planted file or symlink is never opened), 0600, and replaces
  atomically;
* crash-loop accounting: a clean stop resets the sequence; unclean starts are
  counted in a true sliding window measured from the observation time;
* the readiness conditions and their read-only reader;
* the management command's --idle-seconds range.
"""
import json
import math
import os
import stat
import tempfile
import threading
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from active_messages import heartbeat, services, worker

from .support import Domain

T0 = 1_760_000_000.0
EXECUTION = "0f8fad5b-d9cb-469f-a165-70867728950e"


def document(**changes):
    """A valid polling document (as the worker writes one), with ``changes``."""
    base = {"schema": heartbeat.SCHEMA, "pid": 4242, "started_at": T0, "checked_at": T0 + 10,
            "recent_starts": [T0], "previous": "none", "phase": "polling", "cycle": 2, "result": "idle",
            "code": "", "execution": "", "repeats": 2, "consecutive_errors": 0, "last_progress_at": None,
            "render_deadline": None}
    base.update(changes)
    return base


def assess(state, now=T0 + 10, gate_open=False, **kwargs):
    return heartbeat.assess(state, now=now, gate_open=gate_open, **kwargs)


class Workspace(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "worker.json"
        self.clock = [T0]
        for name in ("_current",):
            self.addCleanup(setattr, heartbeat, name, None)

    def recorder(self, pid=4242):
        return heartbeat.Recorder(self.path, clock=lambda: self.clock[0], pid=pid)

    def stored(self):
        return json.loads(self.path.read_text())


class DecoderTests(Workspace):
    def test_documents_the_worker_writes_validate_in_every_phase(self):
        recorder = self.recorder()
        self.assertEqual(heartbeat.validate(self.stored()).phase, "starting")
        self.clock[0] += 1
        recorder.rendering(EXECUTION, 600)
        self.assertEqual(heartbeat.validate(self.stored()).phase, "rendering")
        for outcome in (worker.Outcome("ready", EXECUTION), worker.Outcome("idle", EXECUTION, "gated"),
                        worker.Outcome("error", code="OperationalError"), worker.Outcome("playout", code="in_flight")):
            self.clock[0] += 5
            recorder.cycle(outcome)
            self.assertEqual(heartbeat.validate(self.stored()).result, outcome.result)
        recorder.stopped()
        self.assertEqual(heartbeat.validate(self.stored()).phase, "stopped")
        self.assertEqual(assess(document())["condition"], "idle")

    def test_the_reviewer_reproductions_are_malformed_not_exceptions_or_ok(self):
        cases = {
            "recent_starts [1]": document(recent_starts=[1]),
            "consecutive_errors {}": document(consecutive_errors={}),
            "consecutive_errors {a: 1}": document(consecutive_errors={"a": 1}, result="busy"),
            "repeats 'many'": document(repeats="many", result="busy"),
            "only schema and checked_at": {"schema": heartbeat.SCHEMA, "checked_at": T0},
        }
        for label, state in cases.items():
            with self.subTest(label):
                verdict = assess(state)
                self.assertEqual((verdict["status"], verdict["condition"]), ("critical", "malformed"))

    def test_every_contract_violation_is_malformed(self):
        cases = {
            "not an object (list)": [document()], "not an object (string)": "ok", "not an object (number)": 1,
            "unexpected key": document(extra=1),
            "schema 1": document(schema=1), "schema 3": document(schema=3), "schema '2'": document(schema="2"),
            "schema 2.0": document(schema=2.0), "schema true": document(schema=True),
            "pid 0": document(pid=0), "pid bool": document(pid=True), "pid float": document(pid=1.5),
            "checked_at bool": document(checked_at=True), "checked_at string": document(checked_at="now"),
            "checked_at nan": document(checked_at=math.nan), "checked_at inf": document(checked_at=math.inf),
            "checked_at 1970": document(checked_at=1000.0, started_at=1000.0, recent_starts=[1000.0]),
            "checked_at 2200": document(checked_at=7.3e9),
            "checked before start": document(checked_at=T0 - 1),
            "recent_starts dict": document(recent_starts={"a": T0}),
            "recent_starts string": document(recent_starts="T0"),
            "recent_starts empty": document(recent_starts=[]),
            "recent_starts too many": document(recent_starts=[T0] * (heartbeat.MAX_STARTS + 1)),
            "recent_starts unsorted": document(recent_starts=[T0, T0 - 5, T0]),
            "recent_starts not ending at start": document(recent_starts=[T0 - 5]),
            "recent_starts beyond the window": document(recent_starts=[T0 - heartbeat.CRASH_WINDOW_SECONDS - 1, T0]),
            "recent_starts with a bool": document(recent_starts=[True, T0]),
            "recent_starts nested": document(recent_starts=[[T0]]),
            "previous unknown": document(previous="maybe"), "previous null": document(previous=None),
            "phase unknown": document(phase="sleeping"), "phase null": document(phase=None),
            "result unknown": document(result="dancing"), "result null": document(result=None),
            "cycle negative": document(cycle=-1), "cycle huge": document(cycle=10 ** 13),
            "cycle null": document(cycle=None),
            "repeats zero after a cycle": document(repeats=0), "repeats beyond cycle": document(repeats=3),
            "errors without an error result": document(consecutive_errors=1),
            "errors disagree with repeats": document(result="error", consecutive_errors=1),
            "result before the first cycle": document(cycle=0, repeats=0),
            "starting after a cycle": document(phase="starting"),
            "code with a slash": document(code="a/b"), "code too long": document(code="x" * 65),
            "code null": document(code=None),
            "execution not a uuid": document(execution="nope"),
            "execution not canonical": document(execution=EXECUTION.upper()),
            "last progress before start": document(last_progress_at=T0 - 1),
            "last progress after check": document(last_progress_at=T0 + 11),
            "deadline outside rendering": document(render_deadline=T0 + 100),
            "rendering without a deadline": document(phase="rendering", execution=EXECUTION),
            "rendering deadline in the past": document(phase="rendering", execution=EXECUTION,
                                                       render_deadline=T0 + 10),
            "rendering deadline too far": document(phase="rendering", execution=EXECUTION,
                                                   render_deadline=T0 + 11 + heartbeat.RENDER_DEADLINE_MAX_SECONDS),
            "rendering without an execution": document(phase="rendering", render_deadline=T0 + 100),
        }
        for key in heartbeat.KEYS:
            missing = document()
            del missing[key]
            cases[f"missing {key}"] = missing
        for label, state in cases.items():
            with self.subTest(label):
                verdict = assess(state)
                self.assertEqual((verdict["status"], verdict["condition"]), ("critical", "malformed"), verdict)
                self.assertIn("reason", verdict["detail"])

    def test_a_future_timestamp_is_not_healthy(self):
        state = document(checked_at=T0 + 10)
        self.assertEqual(assess(state, now=T0 + 10 - heartbeat.MAX_FUTURE_SKEW_SECONDS)["status"], "ok")
        verdict = assess(state, now=T0 + 10 - heartbeat.MAX_FUTURE_SKEW_SECONDS - 1)
        self.assertEqual((verdict["condition"], verdict["detail"]["reason"]), ("malformed", "future_timestamp"))
        self.assertEqual(assess(state, now=math.nan)["status"], "unknown")

    def test_malformed_files_are_reported_never_raised(self):
        cases = {b"{not json": "invalid_json", b"": "invalid_json", b"\xff\xfe": "invalid_json",
                 b'{"schema": NaN}': "invalid_json", b'{"schema": Infinity}': "invalid_json",
                 b"[" * 3000: "invalid_json", b" " * (heartbeat.MAX_DOCUMENT_BYTES + 1): "oversized",
                 json.dumps(document(recent_starts=[T0] * 2000)).encode(): "oversized",
                 b"[1, 2, 3]": "not_an_object", b'"ok"': "not_an_object"}
        for raw, reason in cases.items():
            with self.subTest(raw[:20]):
                self.path.write_bytes(raw)
                verdict = heartbeat.readiness(path=self.path, now=T0)
                self.assertEqual((verdict["status"], verdict["condition"], verdict["detail"]["reason"]),
                                 ("critical", "malformed", reason))


class FileSafetyTests(Workspace):
    def readiness(self, path=None):
        return heartbeat.readiness(path=path or self.path, now=T0 + 10)

    def test_the_reader_never_follows_a_symlink(self):
        target = self.dir / "elsewhere.json"
        target.write_text(json.dumps(document()))
        self.assertEqual(self.readiness(target)["condition"], "idle")              # the same file, read directly
        self.path.symlink_to(target)
        verdict = self.readiness()
        self.assertEqual((verdict["condition"], verdict["detail"]["reason"]), ("malformed", "not_a_regular_file"))

    def test_non_regular_files_are_refused_without_blocking(self):
        self.path.mkdir()
        self.assertEqual(self.readiness()["detail"]["reason"], "not_a_regular_file")
        self.path.rmdir()
        os.mkfifo(self.path)                              # a FIFO with no writer: O_NONBLOCK, never hangs
        self.assertEqual(self.readiness()["detail"]["reason"], "not_a_regular_file")

    def test_missing_or_unreadable_is_unknown_never_ok(self):
        verdict = self.readiness()
        self.assertEqual((verdict["status"], verdict["condition"], verdict["detail"]["reason"]),
                         ("unknown", "no_heartbeat", "missing"))
        if os.geteuid() != 0:
            self.path.write_text(json.dumps(document()))
            self.path.chmod(0)
            verdict = self.readiness()
            self.assertEqual((verdict["status"], verdict["detail"]["reason"]), ("unknown", "unreadable"))

    def test_the_writer_creates_a_private_regular_file_whatever_the_umask(self):
        previous = os.umask(0)
        try:
            self.recorder()
        finally:
            os.umask(previous)
        info = os.lstat(self.path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["worker.json"])   # no temporary left

    def test_a_pre_planted_temporary_file_is_never_followed_or_overwritten(self):
        victim = self.dir / "victim.txt"
        victim.write_text("precious")
        planted = self.dir / ".worker.json.planted.tmp"
        planted.symlink_to(victim)
        with mock.patch("tempfile._get_candidate_names", return_value=iter(["planted", "fresh"])):
            self.recorder()
        self.assertEqual(victim.read_text(), "precious")                          # never written through
        self.assertTrue(planted.is_symlink())                                     # never opened or replaced
        self.assertTrue(stat.S_ISREG(os.lstat(self.path).st_mode))
        self.assertEqual(heartbeat.validate(self.stored()).phase, "starting")

    def test_a_symlink_at_the_heartbeat_path_is_replaced_never_written_through(self):
        victim = self.dir / "victim.txt"
        victim.write_text("precious")
        self.path.symlink_to(victim)
        self.recorder()
        self.assertEqual(victim.read_text(), "precious")
        self.assertTrue(stat.S_ISREG(os.lstat(self.path).st_mode))

    def test_write_failures_never_raise_and_leave_nothing_behind(self):
        missing = heartbeat.Recorder(self.dir / "no" / "such" / "dir.json", clock=lambda: T0)
        missing.cycle(worker.Outcome("idle"))
        missing.stopped()
        self.path.mkdir()                                                         # os.replace onto a directory fails
        self.assertFalse(heartbeat._write(self.path, document()))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["worker.json"])
        self.assertFalse(heartbeat._write(self.dir / "x.json", {"value": math.nan}))   # never a non-JSON document

    def test_a_reader_racing_atomic_replacement_sees_only_complete_documents(self):
        recorder = self.recorder()
        stop, problems = threading.Event(), []

        def write():
            while not stop.is_set():
                self.clock[0] += 0.01
                recorder.cycle(worker.Outcome("idle"))
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            for _ in range(400):
                document_, reason = heartbeat.read_state(self.path)
                if reason:
                    problems.append(reason)
                    continue
                try:
                    heartbeat.validate(document_)
                except heartbeat.Malformed as exc:
                    problems.append(str(exc))
        finally:
            stop.set()
            writer.join(10)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["worker.json"])


class CrashLoopTests(Workspace):
    def start(self, at, *, clean_stop_before=False):
        self.clock[0] = at
        return self.recorder(pid=int(at) % 100000 + 1)

    def condition(self, at):
        return assess(self.stored(), now=at)["condition"]

    def test_clean_stop_start_cycles_never_crash_loop(self):
        for index in range(10):
            recorder = self.start(T0 + 10 * index)
            recorder.cycle(worker.Outcome("idle"))
            recorder.stopped()
        self.start(T0 + 100)
        self.assertEqual((self.stored()["recent_starts"], self.stored()["previous"]), ([T0 + 100], "clean"))
        self.assertEqual(self.condition(T0 + 101), "idle")

    def test_three_rapid_unclean_restarts_crash_loop(self):
        for offset in (0, 20, 40):
            self.start(T0 + offset)                                               # each dies without a stop
        self.assertEqual(self.stored()["previous"], "unclean")
        self.assertEqual(self.condition(T0 + 41), "crash_looping")

    def test_the_window_slides_with_the_observation_time(self):
        for offset in (0, 850, 1700):
            self.start(T0 + offset)
        self.assertEqual(self.stored()["recent_starts"], [T0 + 850, T0 + 1700])     # 0 left the window at 1700
        self.assertEqual(self.condition(T0 + 1700), "idle")
        # The same three starts read later: they leave the window as time passes.
        state = document(started_at=T0 + 900, checked_at=T0 + 900, recent_starts=[T0, T0 + 450, T0 + 900],
                         previous="unclean", phase="starting", cycle=0, result="", repeats=0)
        self.assertEqual(assess(state, now=T0 + 900)["condition"], "crash_looping")   # exactly at 900: inside
        self.assertEqual(assess(state, now=T0 + 900.001)["condition"], "idle")        # just past: outside
        self.assertEqual(assess(state, now=T0 + 1350)["condition"], "stale")         # two starts left; no record since

    def test_write_time_trimming_boundaries(self):
        self.start(T0)
        self.start(T0 + heartbeat.CRASH_WINDOW_SECONDS)                             # exactly 900 s later: kept
        self.assertEqual(self.stored()["recent_starts"], [T0, T0 + 900])
        self.start(T0 + 1800.001)                                                  # 900.001 after the second
        self.assertEqual(self.stored()["recent_starts"], [T0 + 1800.001])

    def test_a_clean_stop_after_crashes_resets_the_sequence(self):
        self.start(T0)
        self.start(T0 + 10)
        recorder = self.start(T0 + 20)
        self.assertEqual(self.condition(T0 + 21), "crash_looping")
        recorder.stopped()
        self.start(T0 + 30)                                                        # an operator restart
        self.assertEqual(self.stored()["recent_starts"], [T0 + 30])
        self.assertEqual(self.condition(T0 + 31), "idle")
        self.start(T0 + 40)
        self.assertEqual(self.condition(T0 + 41), "idle")                         # two unclean: not yet
        self.start(T0 + 50)
        self.assertEqual(self.condition(T0 + 51), "crash_looping")

    def test_corrupt_history_starts_afresh_and_says_so(self):
        for raw in (b"{garbage", json.dumps(document(recent_starts=[1])).encode()):
            with self.subTest(raw[:12]):
                self.path.write_bytes(raw)
                self.start(T0)
                stored = self.stored()
                self.assertEqual((stored["previous"], stored["recent_starts"]), ("malformed", [T0]))
                verdict = assess(stored, now=T0 + 1)
                self.assertEqual((verdict["status"], verdict["condition"]), ("warning", "history_unverified"))
                self.assertEqual(assess(stored, now=T0 + heartbeat.CRASH_WINDOW_SECONDS + 1)["condition"], "stale")
                self.path.unlink()

    def test_missing_history_is_not_a_crash(self):
        self.start(T0)
        self.assertEqual((self.stored()["previous"], self.stored()["recent_starts"]), ("none", [T0]))
        self.assertEqual(self.condition(T0 + 1), "idle")

    def test_restart_history_is_bounded(self):
        for index in range(50):
            self.start(T0 + index)
        self.assertEqual(len(self.stored()["recent_starts"]), heartbeat.MAX_STARTS)

    def test_a_dead_process_is_never_healthy_for_long(self):
        recorder = self.start(T0)
        self.assertEqual(self.condition(T0 + heartbeat.STALE_AFTER_SECONDS), "idle")       # starting
        self.assertEqual(self.condition(T0 + heartbeat.STALE_AFTER_SECONDS + 1), "stale")
        recorder.cycle(worker.Outcome("idle"))
        self.assertEqual(self.condition(T0 + heartbeat.STALE_AFTER_SECONDS + 1), "stale")
        recorder.rendering(EXECUTION, 600)
        deadline = self.stored()["render_deadline"]
        self.assertEqual(self.condition(deadline), "rendering")
        self.assertEqual(self.condition(deadline + 1), "render_overdue")


class ConditionTests(Workspace):
    def test_each_condition(self):
        recorder = self.recorder()
        cases = [
            (worker.Outcome("idle"), "ok", "idle"),
            (worker.Outcome("idle", EXECUTION, "gated"), "ok", "ready_waiting_for_gate"),
            (worker.Outcome("ready", EXECUTION), "ok", "working"),
            (worker.Outcome("retry", EXECUTION, "render_unavailable"), "ok", "retry_backoff"),
            (worker.Outcome("backoff", EXECUTION), "ok", "retry_backoff"),
            (worker.Outcome("playout", code="accepted"), "ok", "playout_in_flight"),
            (worker.Outcome("blocked", code="render_failed"), "warning", "blocked"),
            (worker.Outcome("busy"), "ok", "busy"),
        ]
        for outcome, status, condition in cases:
            with self.subTest(condition):
                self.clock[0] += 5
                recorder.cycle(outcome)
                verdict = assess(self.stored(), now=self.clock[0])
                self.assertEqual((verdict["status"], verdict["condition"]), (status, condition))
                self.assertEqual(verdict["detail"]["native_airing"], "closed")    # informational only
        for _ in range(heartbeat.BUSY_WARNING_REPEATS):
            recorder.cycle(worker.Outcome("busy"))
        self.assertEqual(assess(self.stored(), now=self.clock[0])["condition"], "lock_held_elsewhere")
        recorder.cycle(worker.Outcome("error", code="OperationalError"))
        self.assertEqual(assess(self.stored(), now=self.clock[0])["condition"], "database_errors")
        for _ in range(heartbeat.ERROR_CRITICAL_REPEATS):
            recorder.cycle(worker.Outcome("error", code="OperationalError"))
        self.assertEqual(assess(self.stored(), now=self.clock[0])["condition"], "malfunction")
        recorder.cycle(worker.Outcome("idle"))
        self.assertEqual(assess(self.stored(), now=self.clock[0])["condition"], "idle")     # errors reset
        self.assertEqual(assess(self.stored(), now=self.clock[0], gate_open=True)["detail"]["native_airing"], "open")
        recorder.stopped()
        self.assertEqual(assess(self.stored(), now=self.clock[0])["condition"], "stopped")
        self.assertIsNone(heartbeat.current())

    def test_unexpected_outcome_values_are_recorded_safely(self):
        recorder = self.recorder()
        recorder.cycle(worker.Outcome("weird", "not-a-uuid", "has spaces/and slashes"))
        stored = heartbeat.validate(self.stored())
        self.assertEqual((stored.result, stored.execution, stored.code), ("error", "", "unrecognized"))


class ReadinessReadOnlyTests(TestCase):
    def test_readiness_only_reads(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from active_messages.models import ActiveBroadcastPolicy
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "worker.json"
        recorder = heartbeat.Recorder(path, clock=lambda: T0)
        self.addCleanup(recorder.stopped)
        recorder.cycle(worker.Outcome("idle"))
        with mock.patch.object(services, "NATIVE_AIRING_AVAILABLE", True), \
                CaptureQueriesContext(connection) as queries:
            verdict = heartbeat.readiness(path=path, now=T0)
        self.assertEqual(verdict["condition"], "idle")
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        self.assertFalse(ActiveBroadcastPolicy.objects.exists())                 # the singleton was not created


class HeartbeatRenderTests(Domain, TestCase):
    def test_a_real_render_records_its_deadline_first(self):
        from .test_worker_lifecycle import renders_ready
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "worker.json"
        self.active(kind="urgent", seconds="10.000000")
        recorder = heartbeat.Recorder(path, clock=lambda: T0)
        self.addCleanup(recorder.stopped)
        seen = []

        def render(execution):
            seen.append(json.loads(path.read_text()))
            return renders_ready(execution)
        self.assertIs(heartbeat.current(), recorder)
        self.assertEqual(worker.run_once(render_fn=render).result, "ready")
        self.assertEqual(heartbeat.validate(seen[0]).phase, "rendering")
        self.assertGreater(seen[0]["render_deadline"], T0 + heartbeat.RENDER_GRACE_SECONDS)


class IdleSecondsTests(SimpleTestCase):
    def run_command(self, *args):
        with mock.patch.object(worker, "run_forever") as loop, \
                mock.patch.object(heartbeat, "Recorder"), \
                mock.patch("signal.signal"):
            call_command("active_message_worker", *args, stdout=mock.Mock())
        return loop.call_args.kwargs["poll_seconds"]

    def test_the_accepted_range_and_default(self):
        self.assertEqual(self.run_command(), worker.POLL_SECONDS)
        for value in ("1", "5", "29.5", "30"):
            self.assertEqual(self.run_command("--idle-seconds", value), float(value))
        self.assertLess(30.0, heartbeat.STALE_AFTER_SECONDS)                      # a wait never looks stale

    def test_out_of_range_and_non_finite_values_are_refused(self):
        for value in ("0", "0.999", "-1", "30.001", "60", "1e9", "nan", "inf", "-inf", "abc"):
            with self.subTest(value), self.assertRaises(CommandError):
                self.run_command("--idle-seconds", value)

    def test_a_stop_during_the_longest_wait_is_prompt(self):
        stopped, slept = [], []

        def sleep(seconds):
            slept.append(seconds)
            stopped.append(True)
        worker.run_forever(poll_seconds=30.0, stop=lambda: bool(stopped), sleep=sleep,
                           run=lambda: worker.Outcome("idle"), refresh=mock.Mock(), close=mock.Mock())
        self.assertEqual(slept, [worker.PAUSE_SLICE_SECONDS])
