"""P1 1.11 -- MonitorManager's own wiring of the systemd watchdog
keepalive (monitoring/services/sd_notify.py) and the supervision
marker (monitoring/services/supervision.py). STATE_PATH/MARKER_PATH
are redirected to temp files for every test -- never touches the real
/run/isadoraair/*.json a live isadoraair-monitoring service may also
be writing to. No test depends on a wall-clock sleep; MonitorManager's
own POLL_SECONDS loop is never actually run -- only _run_cycle() and
start()'s own pre/post-loop bookkeeping are exercised directly."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import monitoring.services.monitor as monitor_module
from monitoring.models import MonitorCheck, SystemEvent
from monitoring.services import sd_notify, supervision


class WatchdogKeepaliveTests(TestCase):
    """_run_cycle()'s own placement of the WATCHDOG=1 send -- must
    follow a successful _write_state, must never follow a failed one,
    and must never even attempt delivery when this process is not
    under systemd's watchdog supervision at all."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "monitoring_state.json"
        patcher = patch.object(monitor_module, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        listener_patcher = patch.object(
            monitor_module, "LISTENER_STATE_PATH", Path(self.tmp_dir.name) / "listeners.json",
        )
        listener_patcher.start()
        self.addCleanup(listener_patcher.stop)
        # _run_cycle() calls close_old_connections() at its own start
        # (pre-existing behavior, correct for the REAL long-running
        # poller process this class calls it outside of) -- inside a
        # Django TestCase's own wrapped transaction, that call can close
        # the very connection this test's transaction lives on. No-op
        # it here rather than changing production code for a test-only
        # concern.
        close_conn_patcher = patch.object(monitor_module, "close_old_connections")
        close_conn_patcher.start()
        self.addCleanup(close_conn_patcher.stop)
        self.manager = monitor_module.MonitorManager()

    def test_no_keepalive_attempted_when_watchdog_not_enabled(self):
        """The overwhelming common case (dev/CI/no systemd watchdog
        configured) -- notify() must not even be called, matching
        monitor.py's own `if sd_notify.watchdog_enabled() and ...`
        short-circuit."""
        with (
            patch.object(sd_notify, "watchdog_enabled", return_value=False),
            patch.object(sd_notify, "notify") as mock_notify,
        ):
            self.manager._run_cycle()
        mock_notify.assert_not_called()

    def test_keepalive_sent_after_successful_state_write(self):
        with (
            patch.object(sd_notify, "watchdog_enabled", return_value=True),
            patch.object(sd_notify, "notify", return_value=True) as mock_notify,
        ):
            self.manager._run_cycle()
        mock_notify.assert_called_once_with(watchdog=True)
        # The state file really was written -- the keepalive is
        # downstream of a REAL successful promotion, not a fiction.
        self.assertTrue(self.state_path.is_file())

    def test_no_keepalive_when_write_state_raises(self):
        """The cycle must not complete far enough to send a keepalive
        if monitoring_state.json's own authoritative write never
        succeeded."""
        with (
            patch.object(sd_notify, "watchdog_enabled", return_value=True),
            patch.object(sd_notify, "notify", return_value=True) as mock_notify,
            patch.object(self.manager, "_write_state", side_effect=OSError("disk full")),
        ):
            with self.assertRaises(OSError):
                self.manager._run_cycle()
        mock_notify.assert_not_called()

    def test_failed_delivery_does_not_raise_or_corrupt_state(self):
        """NOTIFY_SOCKET existed (watchdog_enabled() True) but the
        datagram could not be delivered -- must be silently absorbed,
        never crash the cycle or leave monitoring_state.json
        corrupted/unwritten."""
        with (
            patch.object(sd_notify, "watchdog_enabled", return_value=True),
            patch.object(sd_notify, "notify", return_value=False),
        ):
            self.manager._run_cycle()  # must not raise
        data = json.loads(self.state_path.read_text())
        self.assertIn("timestamp", data)

    def test_listener_poll_failure_does_not_suppress_keepalive(self):
        """The listener-statistics side path is best-effort and must
        NOT become a prerequisite for the watchdog heartbeat -- the
        keepalive already happened before the listener poll even
        starts."""
        with (
            patch.object(sd_notify, "watchdog_enabled", return_value=True),
            patch.object(sd_notify, "notify", return_value=True) as mock_notify,
            patch.object(self.manager, "_poll_shoutcast_listeners", side_effect=RuntimeError("boom")),
        ):
            self.manager._run_cycle()  # must not raise -- listener failure is caught internally
        mock_notify.assert_called_once_with(watchdog=True)


class SupervisionMarkerIntegrationTests(TestCase):
    """MonitorManager.start()'s own pre-loop unclean-recovery check and
    post-loop clean-shutdown marker. `_run_cycle` is replaced with a
    trivial one-shot stub (see
    _make_manager_that_runs_one_trivial_cycle) rather than ever running
    the real infinite poll loop -- start() unconditionally sets
    self.running = True as its own first line, so pre-setting it False
    beforehand has no effect."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "monitoring_state.json"
        self.marker_path = Path(self.tmp_dir.name) / "monitoring_supervision.json"
        patcher = patch.object(monitor_module, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        marker_patcher = patch.object(supervision, "MARKER_PATH", self.marker_path)
        marker_patcher.start()
        self.addCleanup(marker_patcher.stop)
        # start() calls close_old_connections() as its own first real
        # action -- see WatchdogKeepaliveTests.setUp's identical
        # comment for why this must be a no-op inside a Django
        # TestCase's own wrapped transaction.
        close_conn_patcher = patch.object(monitor_module, "close_old_connections")
        close_conn_patcher.start()
        self.addCleanup(close_conn_patcher.stop)
        # Never actually sleep or loop -- see class docstring.
        self.sleep_patcher = patch.object(monitor_module.time, "sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)
        self.signal_patcher = patch.object(monitor_module.signal, "signal")
        self.signal_patcher.start()
        self.addCleanup(self.signal_patcher.stop)
        self.psutil_patcher = patch.object(monitor_module.psutil, "cpu_percent")
        self.psutil_patcher.start()
        self.addCleanup(self.psutil_patcher.stop)

    def _make_manager_that_runs_one_trivial_cycle(self):
        """start() unconditionally sets self.running = True as its own
        first line (so pre-setting it False before calling start() has
        no effect and would spin the `while self.running:` loop
        forever with time.sleep() mocked to a no-op) -- instead,
        _run_cycle itself is replaced with a stub whose ONLY job is to
        flip self.running back to False, so the loop body executes
        exactly once (a real, if trivial, "cycle") and then exits via
        the SAME graceful path a real SIGTERM would take."""
        manager = monitor_module.MonitorManager()

        def _one_shot_cycle():
            manager.running = False

        manager._run_cycle = _one_shot_cycle
        return manager

    def test_first_start_since_boot_emits_no_incident_event(self):
        manager = self._make_manager_that_runs_one_trivial_cycle()
        manager.start()
        self.assertFalse(
            SystemEvent.objects.filter(dedupe_key="monitor|unclean-restart-recovered").exists()
        )

    def test_clean_shutdown_marker_written_on_graceful_loop_exit(self):
        manager = self._make_manager_that_runs_one_trivial_cycle()
        manager.start()
        data = json.loads(self.marker_path.read_text())
        self.assertTrue(data["clean_shutdown"])
        self.assertEqual(data["pid"], os.getpid())

    def test_unclean_prior_invocation_emits_a_system_event(self):
        # Simulate a PRIOR process that started and never cleanly
        # stopped (watchdog SIGABRT / OOM-kill / crash).
        supervision.record_new_invocation(runtime_commit="dead" * 10)

        manager = self._make_manager_that_runs_one_trivial_cycle()
        manager.start()

        event = SystemEvent.objects.filter(dedupe_key="monitor|unclean-restart-recovered").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.level, "error")
        self.assertEqual(event.detail["prior_runtime_commit"], "dead" * 10)

    def test_clean_prior_invocation_emits_no_event_on_next_start(self):
        supervision.record_new_invocation(runtime_commit="clean" * 8)
        supervision.mark_clean_shutdown()

        manager = self._make_manager_that_runs_one_trivial_cycle()
        manager.start()

        self.assertFalse(
            SystemEvent.objects.filter(dedupe_key="monitor|unclean-restart-recovered").exists()
        )

    def test_a_normal_restart_cycle_end_to_end_is_never_flagged(self):
        """Full realistic sequence: instance A starts, runs, shuts down
        cleanly (Update Center / systemctl restart); instance B starts
        next -- must see zero incident events."""
        manager_a = self._make_manager_that_runs_one_trivial_cycle()
        manager_a.start()

        manager_b = self._make_manager_that_runs_one_trivial_cycle()
        manager_b.start()

        self.assertFalse(
            SystemEvent.objects.filter(dedupe_key="monitor|unclean-restart-recovered").exists()
        )

    def test_uncaught_exception_mid_cycle_never_reaches_clean_shutdown(self):
        """The complementary proof to the watchdog-SIGABRT case a real
        process can't easily simulate in-process: an uncaught exception
        escaping _run_cycle() must propagate out of start() WITHOUT
        ever writing clean_shutdown=True -- exactly like the real
        `while self.running: self._run_cycle()` has no try/except
        around the call."""
        manager = monitor_module.MonitorManager()

        def _boom():
            raise RuntimeError("simulated wedge/crash mid-cycle")

        manager._run_cycle = _boom
        with self.assertRaises(RuntimeError):
            manager.start()

        data = json.loads(self.marker_path.read_text())
        self.assertFalse(data["clean_shutdown"])
