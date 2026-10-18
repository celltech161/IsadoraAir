"""D3E-B: the managed worker's lifecycle under a REAL systemd manager.

Each test runs a transient unit of the invoking user's own systemd user
manager (the precedent: production/tests/validation_service_support) with the
lifecycle of deploy/isadoraair-active-message-worker.service -- Type=simple,
Restart=on-failure, SIGTERM, the start gate as ExecStartPost= with the
unit's $MAINPID -- and a stand-in worker process that runs the REAL loop
(worker.run_forever), the REAL heartbeat Recorder and the command's own
signal handling, with scripted passes instead of the database. Only the
restart delay and stop timeout are shortened, to keep the tests quick; the
reviewed values are asserted in test_managed_worker.UnitTemplateTests.
Nothing here needs privilege, installs a unit file or touches a system unit.

* the start job completes only through the gate, and only for the CURRENT
  process -- a valid heartbeat left by an earlier process never satisfies it;
* SIGTERM stops promptly and records a clean stop;
* a stop during a long render waits only for the stop timeout, then the next
  start sees an unclean end (the D3E-A recovery path re-renders);
* an abnormal exit is restarted automatically with a new identity, which
  Monitoring's observer tells apart from the old one;
* a worker that cannot write its heartbeat fails its start (and is retried).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from django.test import SimpleTestCase

from active_messages import heartbeat, supervision, worker
from monitoring.services.probes import _systemd_epoch

REPO = Path(__file__).resolve().parents[2]

STAND_IN = r'''
import os, signal, sys, time, uuid
repo, path, mode, delay = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
sys.path.insert(0, repo)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
stopping = []
for signum in (signal.SIGTERM, signal.SIGINT):            # exactly the management command's handlers
    signal.signal(signum, lambda *_: stopping.append(True))
import django
django.setup()
from active_messages import heartbeat, worker
version_file = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "version")
version = open(version_file).read().strip() if os.path.exists(version_file) else ""   # "the code", read at start
time.sleep(delay)                                          # a slow start-up before the first record
recorder = heartbeat.Recorder(path)


def run():
    if mode == "render":                                   # a long render: SIGTERM does not cut it short
        recorder.rendering(uuid.uuid4(), 3600)
        while True:
            time.sleep(3600)
    return worker.Outcome("idle", code=version)


worker.run_forever(poll_seconds=1, stop=lambda: bool(stopping), run=run, recorder=recorder,
                   refresh=lambda: None, close=lambda: None)
'''


def _user_manager_available() -> bool:
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    try:
        state = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True,
                               timeout=10).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return state in ("running", "degraded")


class TransientWorkerUnit:
    def __init__(self, testcase, state_path: Path, *, stop_timeout=30):
        self.testcase, self.path, self.stop_timeout = testcase, state_path, stop_timeout
        self.unit = f"isadoraair-active-message-worker-t{uuid.uuid4().hex[:12]}.service"
        scratch = Path(tempfile.mkdtemp(prefix="amw-unit-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
        testcase.addCleanup(shutil.rmtree, scratch, True)
        self.script = scratch / "stand_in.py"
        self.script.write_text(STAND_IN)
        testcase.addCleanup(self.stop)

    def start(self, *, mode="idle", delay=0.0, gate_timeout=30):
        gate = (f"{sys.executable} {REPO}/manage.py active_message_worker_ready --pid ${{MAINPID}} "
                f"--timeout {gate_timeout} --state-path {self.path}")
        properties = ("Type=simple", "Restart=on-failure", "RestartSec=1", "KillSignal=SIGTERM",
                      f"TimeoutStopSec={self.stop_timeout}", "TimeoutStartSec=60", f"ExecStartPost={gate}",
                      f"WorkingDirectory={REPO}")
        command = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={self.unit}"]
        command += [f"--property={item}" for item in properties]
        command += [sys.executable, str(self.script), str(REPO), str(self.path), mode, str(delay)]
        started = time.monotonic()
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        return result.returncode, time.monotonic() - started

    def show(self) -> dict:
        output = subprocess.run(["systemctl", "--user", "show", self.unit,
                                 "--property=ActiveState,SubState,MainPID,NRestarts,ExecMainStartTimestamp"],
                                capture_output=True, text=True, timeout=10, env={**os.environ, "TZ": "UTC"}).stdout
        return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)

    def install_code(self, version: str):
        (self.script.parent / "version").write_text(version)

    def restart(self) -> float:
        started = time.monotonic()
        subprocess.run(["systemctl", "--user", "restart", self.unit], check=True, capture_output=True, timeout=120)
        return time.monotonic() - started

    def stop(self) -> float:
        started = time.monotonic()
        subprocess.run(["systemctl", "--user", "stop", self.unit], capture_output=True, timeout=120)
        subprocess.run(["systemctl", "--user", "reset-failed", self.unit], capture_output=True, timeout=30)
        return time.monotonic() - started

    def wait_for(self, predicate, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            props = self.show()
            if predicate(props):
                return props
            time.sleep(0.1)
        raise AssertionError(f"{self.unit}: condition not reached; last {self.show()}")

    def observe(self, props=None, *, main_pid=None):
        props = props or self.show()
        return supervision.observe(main_pid=int(props["MainPID"]) if main_pid is None else main_pid,
                                   process_started_at=_systemd_epoch(props["ExecMainStartTimestamp"]),
                                   now=time.time(), path=self.path)


class ManagedWorkerUnitTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _user_manager_available():
            raise unittest.SkipTest("no systemd user manager")

    def setUp(self):
        directory = Path(tempfile.mkdtemp(prefix="amw-state-", dir=os.environ.get("XDG_RUNTIME_DIR") or None))
        self.addCleanup(shutil.rmtree, directory, True)
        self.dir = directory
        self.path = directory / "active_message_worker.json"

    def stored(self):
        document, reason = heartbeat.read_state(self.path)
        self.assertEqual(reason, "")
        return heartbeat.validate(document)

    def dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    def test_the_start_gate_waits_for_the_current_process_never_a_stale_file(self):
        stale_start = time.time() - 300
        old = heartbeat.Recorder(self.path, clock=lambda: stale_start, pid=self.dead_pid())
        old.cycle(worker.Outcome("idle"))
        heartbeat._current = None
        self.assertEqual(self.stored().phase, "polling")                   # a valid, healthy-looking leftover

        unit = TransientWorkerUnit(self, self.path)
        returncode, elapsed = unit.start(delay=4)
        self.assertEqual(returncode, 0)
        self.assertGreaterEqual(elapsed, 4)                                # the leftover never satisfied the gate
        props = unit.show()
        self.assertEqual((props["ActiveState"], props["SubState"]), ("active", "running"))
        beat = self.stored()
        self.assertEqual(beat.pid, int(props["MainPID"]))
        self.assertGreaterEqual(beat.started_at, _systemd_epoch(props["ExecMainStartTimestamp"]))
        self.assertGreaterEqual(beat.cycle, 1)
        found = unit.observe(props)
        self.assertEqual((found["status"], found["condition"]), ("ok", "idle"))

    def test_sigterm_stops_promptly_and_records_a_clean_stop(self):
        unit = TransientWorkerUnit(self, self.path)
        self.assertEqual(unit.start()[0], 0)
        pid = int(unit.show()["MainPID"])
        self.assertLess(unit.stop(), 10)                                   # well inside TimeoutStopSec=30
        beat = self.stored()
        self.assertEqual((beat.pid, beat.phase), (pid, "stopped"))
        self.assertEqual(unit.start()[0], 0)                               # a clean stop resets the sequence
        self.assertEqual((self.stored().previous, len(self.stored().recent_starts)), ("clean", 1))

    def test_a_stop_during_a_long_render_waits_only_for_the_stop_timeout(self):
        unit = TransientWorkerUnit(self, self.path, stop_timeout=3)
        self.assertEqual(unit.start(mode="render")[0], 0)                  # a render in progress is a start
        self.assertEqual(self.stored().phase, "rendering")
        elapsed = unit.stop()
        self.assertGreaterEqual(elapsed, 3)
        self.assertLess(elapsed, 15)                                       # bounded: killed after the timeout
        self.assertEqual(self.stored().phase, "rendering")                 # no clean-stop record
        self.assertEqual(unit.start()[0], 0)
        beat = self.stored()
        self.assertEqual((beat.previous, len(beat.recent_starts)), ("unclean", 2))

    def test_an_abnormal_exit_restarts_with_a_new_identity(self):
        unit = TransientWorkerUnit(self, self.path)
        self.assertEqual(unit.start()[0], 0)
        first = unit.show()
        old_pid = int(first["MainPID"])
        os.kill(old_pid, 9)
        props = unit.wait_for(lambda p: p.get("ActiveState") == "active" and p.get("MainPID") not in ("0", str(old_pid)))
        self.assertEqual(int(props["NRestarts"]), 1)
        props = unit.wait_for(lambda p: self.path.exists() and self.stored().pid == int(p["MainPID"])
                              and self.stored().cycle >= 1)
        beat = self.stored()
        self.assertEqual(beat.previous, "unclean")
        found = unit.observe(props)
        self.assertEqual((found["status"], found["condition"]), ("ok", "idle"))
        stale = unit.observe(props, main_pid=old_pid)                      # the old identity is not current
        self.assertNotEqual(stale["status"], "ok")

    def test_an_authorized_restart_runs_the_new_code(self):
        """What a release-declared restart (correction B2) does to the installed
        unit: the stop is clean, the start gate passes again for the NEW process,
        and the new process runs the code present at its start."""
        unit = TransientWorkerUnit(self, self.path)
        unit.install_code("v1")
        self.assertEqual(unit.start()[0], 0)
        old_pid = int(unit.show()["MainPID"])
        self.assertEqual((self.stored().pid, self.stored().code), (old_pid, "v1"))
        unit.install_code("v2")                                            # the release advanced the checkout
        self.assertLess(unit.restart(), 30)                                # restart job: through the start gate
        props = unit.show()
        self.assertEqual((props["ActiveState"], props["NRestarts"]), ("active", "0"))   # not a crash restart
        beat = self.stored()
        self.assertNotEqual(beat.pid, old_pid)
        self.assertEqual((beat.pid, beat.code, beat.previous), (int(props["MainPID"]), "v2", "clean"))
        found = unit.observe(props)
        self.assertEqual((found["status"], found["condition"]), ("ok", "idle"))

    def test_a_worker_that_cannot_write_its_heartbeat_fails_its_start(self):
        readonly = self.dir / "readonly"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)
        unit = TransientWorkerUnit(self, readonly / "active_message_worker.json")
        returncode, elapsed = unit.start(gate_timeout=3)
        self.assertNotEqual(returncode, 0)                                 # the start job failed
        self.assertLess(elapsed, 30)
        props = unit.wait_for(lambda p: p.get("NRestarts", "0") != "0" or p.get("SubState") == "auto-restart")
        self.assertNotEqual((props["ActiveState"], props["SubState"]), ("active", "running"))
        self.assertEqual(list(readonly.iterdir()), [])
