"""1.7 release/version-skew visibility -- MonitorManager's own runtime-
commit capture (monitor.py __init__) and its inclusion in
monitoring_state.json (_write_state). STATE_PATH is redirected to a
temp file for every test -- never touches the real
/run/isadoraair/monitoring_state.json the live isadoraair-monitoring
service is also writing to."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

import monitoring.services.monitor as monitor_module
from monitoring.models import MonitorCheck


class RuntimeCommitStateTests(TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "monitoring_state.json"
        patcher = patch.object(monitor_module, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_runtime_commit_captured_once_and_written(self):
        with patch.object(monitor_module, "capture_runtime_commit", return_value="f" * 40):
            manager = monitor_module.MonitorManager()
        self.assertEqual(manager._runtime_commit, "f" * 40)
        manager._write_state([])
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["runtime_commit"], "f" * 40)

    def test_capture_not_repeated_on_every_write(self):
        with patch.object(monitor_module, "capture_runtime_commit", return_value="g" * 40) as mock_capture:
            manager = monitor_module.MonitorManager()
            manager._write_state([])
            manager._write_state([])
        mock_capture.assert_called_once()

    def test_git_unavailable_writes_none_not_a_crash(self):
        with patch.object(monitor_module, "capture_runtime_commit", return_value=None):
            manager = monitor_module.MonitorManager()
        manager._write_state([])
        state = json.loads(self.state_path.read_text())
        self.assertIsNone(state["runtime_commit"])


class BuildResultSystemdUnitTests(TestCase):
    """_build_result() (monitor.py) -- the systemd_unit field this
    round adds, so release_status.py's merge step can match a check to
    its runtime-commit lookup without depending on display names."""

    def setUp(self):
        self.manager = monitor_module.MonitorManager()

    def test_systemd_unit_present_for_systemd_check(self):
        check = MonitorCheck.objects.create(
            name="Test Systemd Unit Field Check", kind="systemd",
            systemd_unit="isadoraair-engine.service",
        )
        result = self.manager._build_result(check, "ok", {})
        self.assertEqual(result["systemd_unit"], "isadoraair-engine.service")

    def test_systemd_unit_none_for_blank_field(self):
        """A non-systemd check (or a systemd check with no unit
        configured, which clean() normally prevents but the field
        itself allows blank) must report None, never an empty string
        the merge step would need to special-case."""
        check = MonitorCheck.objects.create(name="Test Non-Systemd Field Check", kind="disk", disk_path="/")
        result = self.manager._build_result(check, "ok", {})
        self.assertIsNone(result["systemd_unit"])
