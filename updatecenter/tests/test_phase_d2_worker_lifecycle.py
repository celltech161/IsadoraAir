"""D2 corrective review, Correction 4: worker process lifecycle
ownership -- normal exit, crash, supervisor restart, systemd stop/
restart, orphan prevention, process-group termination, bounded restart
attempts, no duplicate simultaneous active workers. Not yet wired to a
real event loop (D2-S's own scope) -- these tests exercise the pure
policy tracker (worker_lifecycle.py) and the already-existing process-
group termination primitive (process.py's TrackedChild) directly."""
from pathlib import Path
import tempfile
import time

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.launch import launch_worker
from isadoraair_updater_bootstrap.worker_lifecycle import (
    WorkerLifecycle, WorkerLifecycleError, WorkerLifecycleState,
)

FIXTURE_WORKER_QUICK_EXIT = "import sys; sys.exit(0)\n"
FIXTURE_WORKER_SLEEPS = "import time; time.sleep(5)\n"
FIXTURE_WORKER_SPAWNS_GRANDCHILD_THAT_SLEEPS = (
    "import subprocess, time\n"
    "subprocess.Popen(['/usr/bin/python3', '-c', 'import time; time.sleep(30)'])\n"
    "time.sleep(5)\n"
)


class WorkerLifecycleStateMachineTests(SimpleTestCase):
    def test_fresh_lifecycle_allows_launch(self):
        lifecycle = WorkerLifecycle()
        self.assertTrue(lifecycle.can_launch())
        lifecycle.record_launch(pid=1234)
        self.assertEqual(lifecycle.state, WorkerLifecycleState.RUNNING)
        self.assertEqual(lifecycle.pid, 1234)

    def test_cannot_launch_while_running_no_duplicate_simultaneous_workers(self):
        lifecycle = WorkerLifecycle()
        lifecycle.record_launch(pid=1234)
        self.assertFalse(lifecycle.can_launch())
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_launch(pid=5678)
        # The original tracked pid is untouched by the refused attempt.
        self.assertEqual(lifecycle.pid, 1234)

    def test_normal_exit_then_acknowledge_then_relaunch_legal(self):
        lifecycle = WorkerLifecycle()
        lifecycle.record_launch(pid=1234)
        lifecycle.record_exit()
        self.assertFalse(lifecycle.can_launch())  # not yet acknowledged
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_launch(pid=5678)
        lifecycle.acknowledge_exit()
        self.assertTrue(lifecycle.can_launch())
        lifecycle.record_launch(pid=5678)
        self.assertEqual(lifecycle.pid, 5678)

    def test_crash_is_indistinguishable_from_normal_exit_at_this_layer(self):
        # record_exit() takes no exit-code argument -- both a clean
        # exit and a crash reach this same state; the caller (future
        # D3 wiring) is responsible for deciding what a crash MEANS,
        # this module only tracks "is a new launch legal."
        lifecycle = WorkerLifecycle()
        lifecycle.record_launch(pid=1234)
        lifecycle.record_exit()
        self.assertEqual(lifecycle.state, WorkerLifecycleState.EXITED_UNACKNOWLEDGED)

    def test_record_exit_without_a_tracked_launch_refused(self):
        lifecycle = WorkerLifecycle()
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_exit()

    def test_acknowledge_without_a_recorded_exit_refused(self):
        lifecycle = WorkerLifecycle()
        lifecycle.record_launch(pid=1234)
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.acknowledge_exit()  # still RUNNING, not yet exited

    def test_double_acknowledge_refused(self):
        lifecycle = WorkerLifecycle()
        lifecycle.record_launch(pid=1234)
        lifecycle.record_exit()
        lifecycle.acknowledge_exit()
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.acknowledge_exit()

    def test_supervisor_restart_is_a_fresh_lifecycle_never_assumes_old_worker_still_alive(self):
        # A restarted supervisor process constructs a brand-new
        # WorkerLifecycle() -- it has no memory of any PID a prior
        # process instance may have launched. This is intentional: see
        # supervisor.py's own recovery_action_for(), which never trusts
        # an in-memory candidate-process handle across a restart
        # either. A fresh instance always starts able to launch.
        lifecycle = WorkerLifecycle()
        self.assertTrue(lifecycle.can_launch())
        self.assertIsNone(lifecycle.pid)


class BoundedRestartAttemptTests(SimpleTestCase):
    def test_bounded_after_max_consecutive_attempts(self):
        lifecycle = WorkerLifecycle(max_consecutive_restart_attempts=3)
        now = 1000.0
        for pid in (1, 2, 3):
            lifecycle.record_launch(pid=pid, now=now)
            lifecycle.record_exit()
            lifecycle.acknowledge_exit()
            now += 1.0
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_launch(pid=4, now=now)

    def test_attempts_outside_the_window_do_not_count(self):
        lifecycle = WorkerLifecycle(max_consecutive_restart_attempts=2, restart_attempt_window_seconds=60)
        lifecycle.record_launch(pid=1, now=0.0)
        lifecycle.record_exit()
        lifecycle.acknowledge_exit()
        lifecycle.record_launch(pid=2, now=10.0)
        lifecycle.record_exit()
        lifecycle.acknowledge_exit()
        # Both attempts within the window -- a third right now would
        # be refused...
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_launch(pid=3, now=15.0)
        # ...but far enough past the window, the earlier attempts have
        # aged out and a launch is legal again.
        lifecycle.record_launch(pid=3, now=1000.0)
        self.assertEqual(lifecycle.pid, 3)

    def test_reset_restart_attempt_history_is_explicit_only(self):
        lifecycle = WorkerLifecycle(max_consecutive_restart_attempts=1)
        lifecycle.record_launch(pid=1, now=0.0)
        lifecycle.record_exit()
        lifecycle.acknowledge_exit()
        with self.assertRaises(WorkerLifecycleError):
            lifecycle.record_launch(pid=2, now=1.0)
        lifecycle.reset_restart_attempt_history()
        lifecycle.record_launch(pid=2, now=1.0)
        self.assertEqual(lifecycle.pid, 2)


class ProcessGroupTerminationTests(SimpleTestCase):
    """Orphan prevention: process.py's TrackedChild already signals the
    whole PROCESS GROUP (os.killpg), not just the direct child, and
    already escalates SIGTERM -> grace -> SIGKILL -- this suite proves
    that primitive directly against a real subprocess tree, since a
    supervisor that only killed the direct child could leave a
    grandchild running as an orphan."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.slot_path = Path(self.temp.name)

    def _write_worker(self, content: str):
        (self.slot_path / "updaterd.py").write_text(content)
        (self.slot_path / "updaterd.py").chmod(0o755)

    def test_terminate_stops_a_sleeping_worker_promptly(self):
        self._write_worker(FIXTURE_WORKER_SLEEPS)
        child = launch_worker(self.slot_path, "updaterd.py", config_path=self.slot_path / "config.json")
        self.assertIsNone(child.poll())
        started = time.monotonic()
        child.terminate()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.5)  # well under the 5s sleep -- proves termination, not natural exit
        self.assertIsNotNone(child.poll())

    def test_terminate_is_idempotent_on_an_already_exited_child(self):
        self._write_worker(FIXTURE_WORKER_QUICK_EXIT)
        child = launch_worker(self.slot_path, "updaterd.py", config_path=self.slot_path / "config.json")
        deadline = time.monotonic() + 5
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIsNotNone(child.poll())
        child.terminate()  # must not raise for an already-dead child

    def test_terminate_kills_a_spawned_grandchild_too_no_orphan_left_behind(self):
        import subprocess
        self._write_worker(FIXTURE_WORKER_SPAWNS_GRANDCHILD_THAT_SLEEPS)
        child = launch_worker(self.slot_path, "updaterd.py", config_path=self.slot_path / "config.json")
        time.sleep(0.3)  # let the worker's own grandchild actually start
        child.terminate()
        # The worker's process GROUP (pgid == child.pid, start_new_session=True)
        # should have no surviving members -- check via pgrep on the pgid.
        time.sleep(0.3)
        result = subprocess.run(["/usr/bin/pgrep", "-g", str(child.pid)], stdout=subprocess.PIPE)
        self.assertEqual(result.stdout.strip(), b"", "a grandchild process survived termination (orphan)")
