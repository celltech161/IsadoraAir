"""D3E-B: the managed worker unit -- start gate, start-up scenarios, the unit
template, and the native-airing boundary.

* the start gate (active_messages.supervision.startup_failure and
  ``manage.py active_message_worker_ready``): only THIS process's valid
  heartbeat after its first pass (or a render in progress) counts -- never a
  file left by an earlier process, never a completed render or an active
  message;
* start-up through the REAL loop (worker.run_forever + run_once + Recorder):
  nothing active, a blocked composition, READY behind the closed gate, the
  database unavailable then recovering, the renderer unavailable, a missing
  or unwritable heartbeat directory, and termination during a render with
  recovery of the same execution;
* deploy/isadoraair-active-message-worker.service, statically and through
  ``systemd-analyze verify``;
* the native-airing gate stays closed and nothing here reads or writes the
  database.

The unit's lifecycle under a real systemd manager is
test_managed_worker_unit; Monitoring's view is
monitoring/tests/test_active_message_worker_check.
"""
import ast
import configparser
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import psutil
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import SimpleTestCase, TestCase

from active_messages import heartbeat, services, supervision, worker
from active_messages.models import ActiveBroadcastExecution
from production.services.render.engine import RenderError

from .support import Domain
from .test_worker_lifecycle import fails, renders_ready

REPO = Path(__file__).resolve().parents[2]
UNIT_PATH = REPO / "deploy" / "isadoraair-active-message-worker.service"
TMPFILES = REPO / "deploy" / "isadoraair-tmpfiles.conf"
EXECUTION = "0f8fad5b-d9cb-469f-a165-70867728950e"


def own_start():
    return psutil.Process(os.getpid()).create_time()


def document(**changes):
    """A valid document from THIS test process, after its first pass."""
    now = time.time()
    base = {"schema": heartbeat.SCHEMA, "pid": os.getpid(), "started_at": now - 5, "checked_at": now,
            "recent_starts": [now - 5], "previous": "none", "phase": "polling", "cycle": 1, "result": "idle",
            "code": "", "execution": "", "repeats": 1, "consecutive_errors": 0, "last_progress_at": None,
            "render_deadline": None}
    base.update(changes)
    return base


class Scratch:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / "active_message_worker.json"
        self.addCleanup(setattr, heartbeat, "_current", None)

    def write(self, doc):
        self.path.write_text(json.dumps(doc))

    def gate(self, *, pid=None, started=None, now=None):
        doc, unreadable = heartbeat.read_state(self.path)
        return supervision.startup_failure(doc, unreadable, pid=os.getpid() if pid is None else pid,
                                           process_started_at=own_start() if started is None else started,
                                           now=time.time() if now is None else now)

    def observe(self, *, pid=None, started=None, now=None):
        return supervision.observe(main_pid=os.getpid() if pid is None else pid,
                                   process_started_at=own_start() if started is None else started,
                                   now=time.time() if now is None else now, path=self.path)


class StartGateTests(Scratch, SimpleTestCase):
    """What the start gate accepts and refuses (pure; this process plays the worker)."""

    def test_a_valid_heartbeat_from_this_process_after_its_first_pass(self):
        for changes in ({}, {"result": "blocked", "code": "render_failed"},
                        {"result": "idle", "code": "gated", "execution": EXECUTION},
                        {"result": "error", "code": "OperationalError", "consecutive_errors": 1},
                        {"result": "retry", "code": "unavailable", "execution": EXECUTION}):
            with self.subTest(changes=changes):
                self.write(document(**changes))
                self.assertEqual(self.gate(), "")

    def test_a_render_during_the_first_pass_counts(self):
        now = time.time()
        self.write(document(phase="rendering", cycle=0, result="", repeats=0, execution=EXECUTION,
                            render_deadline=now + 600))
        self.assertEqual(self.gate(), "")

    def test_before_the_first_pass_it_waits(self):
        now = time.time()
        self.write(document(phase="starting", cycle=0, result="", repeats=0, started_at=now - 5,
                            checked_at=now - 5, recent_starts=[now - 5]))
        self.assertEqual(self.gate(), "first_pass_pending")

    def test_a_file_left_by_an_earlier_process_never_counts(self):
        self.write(document(pid=os.getpid() + 1))                                 # another pid
        self.assertEqual(self.gate(), "previous_process")
        started = own_start()
        earlier = started - supervision.START_SLACK_SECONDS - 1                  # same pid, earlier process
        self.write(document(started_at=earlier, recent_starts=[earlier]))
        self.assertEqual(self.gate(), "previous_process")
        self.write(document(phase="stopped"))                                     # this pid, but stopped
        self.assertEqual(self.gate(), "stopped")

    def test_missing_unreadable_malformed_and_future(self):
        self.assertEqual(self.gate(), "missing")
        self.path.write_text("{not json")
        self.assertEqual(self.gate(), "invalid_json")
        self.write(document(schema=1))
        self.assertEqual(self.gate(), "malformed")
        self.write(document(checked_at=time.time() + 600))
        self.assertEqual(self.gate(), "future_timestamp")
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(self.gate(), "not_a_regular_file")

    def test_identity_needs_a_real_pid(self):
        beat = heartbeat.validate(document())
        for pid in (None, 0, -1, "1", True):
            self.assertFalse(supervision.belongs(beat, pid=pid, process_started_at=None))
        self.assertTrue(supervision.belongs(beat, pid=os.getpid(), process_started_at=None))
        self.assertFalse(supervision.belongs(beat, pid=os.getpid(), process_started_at=float("nan")))


class ReadyCommandTests(Scratch, SimpleTestCase):
    """``manage.py active_message_worker_ready`` (the unit's ExecStartPost=)."""

    def ready(self, pid, timeout=0.5):
        return call_command("active_message_worker_ready", pid=pid, timeout=timeout, state_path=str(self.path),
                            stdout=io.StringIO())

    def child(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_accepts_the_current_process_at_once(self):
        self.write(document())
        started = time.monotonic()
        self.ready(os.getpid(), timeout=10)
        self.assertLess(time.monotonic() - started, 2)

    def test_a_real_child_process_and_a_stale_file_for_its_pid(self):
        child = self.child()
        created = psutil.Process(child.pid).create_time()
        self.write(document(pid=child.pid, started_at=created, recent_starts=[created]))
        self.ready(child.pid)
        stale = created - 60                          # the same pid number, recorded by an earlier process
        self.write(document(pid=child.pid, started_at=stale, recent_starts=[stale]))
        with self.assertRaisesRegex(CommandError, "not started after 0.5s: previous_process"):
            self.ready(child.pid)

    def test_waits_then_times_out_without_a_current_heartbeat(self):
        self.write(document(pid=os.getpid() + 1))
        started = time.monotonic()
        with self.assertRaisesRegex(CommandError, "previous_process"):
            self.ready(os.getpid(), timeout=0.6)
        self.assertGreaterEqual(time.monotonic() - started, 0.6)

    def test_a_worker_that_exits_fails_at_once(self):
        child = self.child()
        child.kill()
        child.wait()
        started = time.monotonic()
        with self.assertRaisesRegex(CommandError, "exited during start-up"):
            self.ready(child.pid, timeout=30)
        self.assertLess(time.monotonic() - started, 5)

    def test_bounded_arguments(self):
        for pid, timeout in ((0, 1), (os.getpid(), 0), (os.getpid(), 121), (os.getpid(), float("nan"))):
            with self.subTest(pid=pid, timeout=timeout), self.assertRaises(CommandError):
                self.ready(pid, timeout)

    def test_the_default_path_is_the_configured_heartbeat(self):
        self.write(document())
        with self.settings(ACTIVE_MESSAGE_WORKER_STATE_PATH=str(self.path)):
            call_command("active_message_worker_ready", pid=os.getpid(), timeout=1, stdout=io.StringIO())


class StartupScenarioTests(Scratch, Domain, TestCase):
    """The REAL loop (worker.run_forever + run_once + Recorder) from start-up;
    after every pass, what the start gate and Monitoring say about this
    (current) process."""

    def run_loop(self, run, *, passes=1, path=None):
        """[(outcome, gate, (status, condition))] for each pass, then a clean stop."""
        seen = []

        def report(outcome):
            found = self.observe()
            seen.append((outcome, self.gate(), (found["status"], found["condition"])))

        worker.run_forever(poll_seconds=0.01, stop=lambda: len(seen) >= passes, sleep=lambda _s: None, run=run,
                           recorder=heartbeat.Recorder(path or self.path), report=report,
                           refresh=lambda: None, close=lambda: self.closed.append(True))
        return seen

    def setUp(self):
        super().setUp()
        self.closed = []

    def test_nothing_active(self):
        [(outcome, gate, verdict)] = self.run_loop(lambda: worker.run_once(render_fn=renders_ready))
        self.assertEqual((outcome.result, gate, verdict), ("idle", "", ("ok", "idle")))
        self.assertFalse(ActiveBroadcastExecution.objects.exists())

    def test_a_blocked_composition(self):
        self.active(kind="urgent", seconds="10.000000")
        seen = self.run_loop(lambda: worker.run_once(render_fn=fails()), passes=2)
        self.assertEqual([(o.result, gate, verdict) for o, gate, verdict in seen],
                         [("failed", "", ("ok", "working")), ("blocked", "", ("warning", "blocked"))])

    def test_ready_waiting_behind_the_closed_gate(self):
        self.active(kind="urgent", seconds="10.000000")
        seen = self.run_loop(lambda: worker.run_once(render_fn=renders_ready), passes=2)
        self.assertEqual([(o.result, o.code, gate, verdict) for o, gate, verdict in seen],
                         [("ready", "", "", ("ok", "working")),
                          ("idle", "gated", "", ("ok", "ready_waiting_for_gate"))])
        self.assertEqual(self.observe()["detail"]["native_airing"], "closed")
        self.assertEqual(ActiveBroadcastExecution.objects.get().state, "ready")

    def test_the_database_unavailable_at_start_then_recovering(self):
        failures = [OperationalError("connection refused")]

        def run():
            if failures:
                raise failures.pop()
            return worker.run_once(render_fn=renders_ready)

        seen = self.run_loop(run, passes=2)
        self.assertEqual([(o.result, o.code, gate, verdict) for o, gate, verdict in seen],
                         [("error", "OperationalError", "", ("warning", "database_errors")),   # up; no database
                          ("idle", "", "", ("ok", "idle"))])                                    # PostgreSQL is back
        self.assertTrue(self.closed)                                       # the failed connection was closed

    def test_the_renderer_unavailable(self):
        self.active(kind="urgent", seconds="10.000000")
        unavailable = mock.Mock(side_effect=RenderError("unavailable", "no socket", retryable=True))
        [(outcome, gate, verdict)] = self.run_loop(lambda: worker.run_once(render_fn=unavailable))
        self.assertEqual((outcome.result, outcome.code, gate, verdict),
                         ("retry", "unavailable", "", ("ok", "retry_backoff")))
        self.assertEqual(ActiveBroadcastExecution.objects.get().state, "pending_render")

    def test_a_clean_stop_is_recorded(self):
        self.run_loop(lambda: worker.run_once(render_fn=renders_ready))
        self.assertEqual(heartbeat.validate(json.loads(self.path.read_text())).phase, "stopped")
        self.assertEqual(self.gate(), "stopped")
        self.assertEqual(self.observe()["condition"], "stopped")          # critical: the unit should be running

    def test_killed_during_a_render_then_the_same_execution_renders(self):
        self.active(kind="urgent", seconds="10.000000")

        class Killed(BaseException):
            pass

        def killed_mid_render(execution):
            beat = heartbeat.validate(json.loads(self.path.read_text()))
            self.assertEqual((beat.phase, beat.execution), ("rendering", str(execution.pk)))
            raise Killed()                                                  # SIGKILL: no further record

        heartbeat.Recorder(self.path, pid=os.getpid() + 1)                 # the process that gets killed
        with self.assertRaises(Killed):
            worker.run_once(render_fn=killed_mid_render)
        unfinished = ActiveBroadcastExecution.objects.get()
        self.assertEqual(unfinished.state, "pending_render")
        self.assertEqual(self.gate(), "previous_process")                   # its file never starts the next one

        seen = self.run_loop(lambda: worker.run_once(render_fn=renders_ready))     # systemd's restart
        [(outcome, gate, verdict)] = seen
        self.assertEqual((outcome.result, outcome.execution, gate, verdict),
                         ("ready", str(unfinished.pk), "", ("ok", "working")))
        self.assertEqual(ActiveBroadcastExecution.objects.count(), 1)
        self.assertEqual(json.loads(self.path.read_text())["previous"], "unclean")

    def test_a_missing_or_unwritable_heartbeat_directory(self):
        missing = self.dir / "absent" / "active_message_worker.json"
        readonly = self.dir / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)
        if os.access(readonly, os.W_OK):
            self.skipTest("running with privileges that ignore directory permissions")
        recent = time.time() - 5
        for path in (missing, readonly / "active_message_worker.json"):
            with self.subTest(path=path):
                self.path = path
                [(outcome, gate, _verdict)] = self.run_loop(lambda: worker.run_once(render_fn=renders_ready))
                self.assertEqual((outcome.result, gate), ("idle", "missing"))   # working, but the start gate fails
                self.assertFalse(path.exists())
                self.assertEqual(self.observe(started=recent)["condition"], "starting")      # unknown in the grace
                late = time.time() - supervision.STARTUP_GRACE_SECONDS - 1
                self.assertEqual((self.observe(started=late)["status"], self.observe(started=late)["condition"]),
                                 ("critical", "no_heartbeat"))
        self.assertEqual(list(readonly.iterdir()), [])                     # no stray temporary left behind


class UnitTemplateTests(SimpleTestCase):
    """deploy/isadoraair-active-message-worker.service as reviewed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = UNIT_PATH.read_text()
        parser = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=",))
        parser.optionxform = str
        parser.read_string(cls.text)
        cls.unit, cls.service, cls.install = parser["Unit"], parser["Service"], parser["Install"]

    def test_identity_and_command(self):
        self.assertEqual(self.service["Type"], "simple")
        self.assertEqual((self.service["User"], self.service["Group"]), ("@@ISA_USER@@", "@@ISA_USER@@"))
        self.assertEqual(self.service["WorkingDirectory"], "@@ISA_ROOT@@")
        self.assertEqual(self.service["EnvironmentFile"], "@@ISA_ROOT@@/.env")
        self.assertEqual(self.service["ExecStart"],
                         "@@ISA_ROOT@@/venv/bin/python @@ISA_ROOT@@/manage.py active_message_worker")
        self.assertEqual(self.install["WantedBy"], "multi-user.target")
        self.assertNotIn("Environment", self.service)                      # nothing set beyond the station .env

    def test_start_gate_and_its_bounds(self):
        self.assertEqual(self.service["ExecStartPost"],
                         "@@ISA_ROOT@@/venv/bin/python @@ISA_ROOT@@/manage.py active_message_worker_ready "
                         "--pid ${MAINPID} --timeout 45")
        self.assertEqual(float(45), supervision.STARTUP_TIMEOUT_SECONDS)
        self.assertEqual(int(self.service["TimeoutStartSec"]), supervision.STARTUP_GRACE_SECONDS)
        self.assertLess(supervision.STARTUP_TIMEOUT_SECONDS, int(self.service["TimeoutStartSec"]))

    def test_restart_and_stop_are_bounded(self):
        self.assertEqual(self.service["Restart"], "on-failure")
        self.assertEqual(int(self.service["RestartSec"]), 10)
        self.assertLessEqual(int(self.service["RestartMaxDelaySec"]), 120)
        self.assertEqual(self.service["KillSignal"], "SIGTERM")
        self.assertEqual(int(self.service["TimeoutStopSec"]), 30)
        self.assertNotIn("KillMode", self.service)                         # control-group: nothing outlives a stop

    def test_ordering_without_requiring_a_healthy_renderer(self):
        after = self.unit["After"].split()
        for unit in ("postgresql.service", "isadoraair-render.service", "isadoraair-validation.service"):
            self.assertIn(unit, after)
        self.assertEqual(self.unit["Wants"].split(), ["isadoraair-render.service", "isadoraair-validation.service"])
        for strong in ("Requires", "BindsTo", "Requisite", "PartOf"):
            self.assertNotIn(strong, self.unit)

    def test_shared_runtime_directory_and_no_privilege(self):
        for forbidden in ("RuntimeDirectory", "RuntimeDirectoryMode", "StateDirectory", "UMask", "DynamicUser",
                          "CPUSchedulingPolicy", "CPUSchedulingPriority", "IOSchedulingClass", "Nice",
                          "LimitRTPRIO", "PermissionsStartOnly", "ExecStartPre"):
            self.assertNotIn(forbidden, self.service)
        self.assertEqual(self.service["CapabilityBoundingSet"], "")
        self.assertEqual(self.service["AmbientCapabilities"], "")
        for flag in ("NoNewPrivileges", "RestrictRealtime", "RestrictSUIDSGID", "PrivateTmp"):
            self.assertEqual(self.service[flag], "yes")
        self.assertNotRegex(self.text, r"(?m)^\s*ExecStart\w*=\s*[+!]")      # no privileged exec prefixes
        # the shared directory, as provisioned at boot for every station service
        self.assertIn("d /run/isadoraair 0755 @@ISA_USER@@ @@ISA_USER@@ -", TMPFILES.read_text().splitlines())
        self.assertEqual(Path(heartbeat.DEFAULT_STATE_PATH).parent, Path("/run/isadoraair"))

    def test_broadcast_units_are_never_named(self):
        directives = "\n".join(line for line in self.text.lower().splitlines() if not line.lstrip().startswith("#"))
        for name in ("ogremote", "isadoraair-engine", "liquidsoap", "weather", "amber", "encoders", "gunicorn"):
            self.assertNotIn(name, directives)

    def test_systemd_analyze_verify(self):
        analyze = shutil.which("systemd-analyze")
        if analyze is None:
            self.skipTest("systemd-analyze is not available")
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "root"
            (root / "venv" / "bin").mkdir(parents=True)
            (root / "venv" / "bin" / "python").symlink_to(sys.executable)
            (root / "manage.py").touch()
            (root / ".env").touch()
            units = Path(scratch) / "units"
            units.mkdir()
            rendered = self.text.replace("@@ISA_ROOT@@", str(root)).replace("@@ISA_USER@@",
                                                                            psutil.Process().username())
            (units / UNIT_PATH.name).write_text(rendered)
            result = subprocess.run([analyze, "verify", "--man=no", UNIT_PATH.name], cwd=units,
                                    capture_output=True, text=True, timeout=60)
        own = [line for line in result.stderr.splitlines() if UNIT_PATH.name in line]
        self.assertEqual((result.returncode, own), (0, []), result.stderr)


class NativeAiringBoundaryTests(Scratch, TestCase):
    """Starting, gating and observing the worker never opens native airing."""

    def test_the_gate_is_a_source_constant(self):
        tree = ast.parse((REPO / "active_messages" / "services.py").read_text())
        [value] = [node.value for node in tree.body if isinstance(node, ast.Assign)
                   and any(getattr(t, "id", None) == "NATIVE_AIRING_AVAILABLE" for t in node.targets)]
        self.assertIsInstance(value, ast.Constant)
        self.assertIs(value.value, False)
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        self.assertIs(heartbeat.gate_open_read_only(), False)

    def test_the_gate_and_the_observer_touch_no_database(self):
        self.write(document())
        with self.assertNumQueries(0):
            self.assertEqual(self.gate(), "")
            self.assertEqual(self.observe()["status"], "ok")
            call_command("active_message_worker_ready", pid=os.getpid(), timeout=1, state_path=str(self.path),
                         stdout=io.StringIO())
