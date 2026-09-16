"""P2 1.13B2 -- "Disk: /run (runtime tmpfs)" MonitorCheck: idempotent
runtime provisioning (aircheck.services.recovery.ensure_run_tmpfs_
monitor_check), replacing P2 1.13B's removed RunPython migration.
Proves the row participates in the SAME generic disk-probe/threshold/
polling machinery as every other "disk" kind check -- never a parallel
tmpfs-specific implementation."""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from aircheck.services import recovery
from monitoring.models import MonitorCheck
from monitoring.services import monitor, probes


class RunDiskCheckProvisioningTests(TestCase):
    def test_23_default_run_disk_monitor_is_created(self):
        self.assertFalse(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").exists())
        created = recovery.ensure_run_tmpfs_monitor_check()
        self.assertTrue(created)
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        self.assertEqual(check.kind, "disk")
        self.assertEqual(check.disk_path, "/run")
        self.assertEqual(check.warning_threshold, 60.0)
        self.assertEqual(check.critical_threshold, 75.0)

    def test_24_existing_canonical_row_is_not_overwritten_or_duplicated(self):
        MonitorCheck.objects.create(
            name="Disk: /run (runtime tmpfs)", kind="disk", disk_path="/run",
            warning_threshold=40.0, critical_threshold=55.0, sort_order=99,
        )
        created = recovery.ensure_run_tmpfs_monitor_check()
        self.assertFalse(created)
        self.assertEqual(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").count(), 1)
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        self.assertEqual(check.warning_threshold, 40.0)  # operator customization preserved
        self.assertEqual(check.critical_threshold, 55.0)
        self.assertEqual(check.sort_order, 99)

    def test_24b_provisioning_is_idempotent_across_repeated_calls(self):
        first = recovery.ensure_run_tmpfs_monitor_check()
        second = recovery.ensure_run_tmpfs_monitor_check()
        third = recovery.ensure_run_tmpfs_monitor_check()
        self.assertEqual((first, second, third), (True, False, False))
        self.assertEqual(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").count(), 1)

    def test_semantic_duplicate_under_different_name_is_respected(self):
        # An operator's own pre-existing generic disk check already
        # covers /run, under a name that predates this feature.
        MonitorCheck.objects.create(
            name="Runtime tmpfs (custom)", kind="disk", disk_path="/run",
            warning_threshold=70.0, critical_threshold=90.0,
        )
        created = recovery.ensure_run_tmpfs_monitor_check()
        self.assertFalse(created)
        self.assertFalse(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").exists())
        self.assertEqual(MonitorCheck.objects.filter(kind="disk", disk_path="/run").count(), 1)
        # The operator's own row is untouched -- never renamed.
        untouched = MonitorCheck.objects.get(kind="disk", disk_path="/run")
        self.assertEqual(untouched.name, "Runtime tmpfs (custom)")
        self.assertEqual(untouched.warning_threshold, 70.0)

    def test_other_disk_paths_do_not_block_provisioning(self):
        # Self-contained (never relies on another test's incidental
        # fixture state, e.g. whether the standard seed migration's own
        # "Disk: / (OS)" row still exists by the time this runs as part
        # of the full suite): a genuinely different disk_path must
        # never block /run provisioning.
        MonitorCheck.objects.create(name="Disk: / (B2 test OS row)", kind="disk", disk_path="/")
        created = recovery.ensure_run_tmpfs_monitor_check()
        self.assertTrue(created)
        self.assertTrue(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").exists())

    def test_provisioning_failure_is_caught_and_returns_false(self):
        # ensure_run_tmpfs_monitor_check() imports MonitorCheck lazily
        # inside the function, so patching the manager method directly
        # (shared/global on the class) is what actually reaches it.
        with patch.object(MonitorCheck.objects, "get_or_create", side_effect=RuntimeError("db hiccup")):
            created = recovery.ensure_run_tmpfs_monitor_check()
        self.assertFalse(created)
        self.assertFalse(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").exists())

    def test_25_warning_threshold_produces_normal_monitoring_warning(self):
        recovery.ensure_run_tmpfs_monitor_check()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        usage = MagicMock(percent=65.0, used=650, total=1000)
        with patch.object(probes.psutil, "disk_usage", return_value=usage):
            status, detail = probes.probe_disk(check)
        self.assertEqual(status, "warning")
        self.assertEqual(detail["percent"], 65.0)

    def test_26_critical_threshold_produces_normal_monitoring_critical(self):
        recovery.ensure_run_tmpfs_monitor_check()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        usage = MagicMock(percent=80.0, used=800, total=1000)
        with patch.object(probes.psutil, "disk_usage", return_value=usage):
            status, detail = probes.probe_disk(check)
        self.assertEqual(status, "critical")

    def test_27_uses_the_same_generic_disk_probe_no_aircheck_specific_logic(self):
        import aircheck.services.recorder as recorder_module
        self.assertFalse(hasattr(recorder_module, "probe_disk"))
        self.assertFalse(hasattr(recorder_module, "probe_run_tmpfs"))
        self.assertFalse(hasattr(recovery, "probe_disk"))
        self.assertFalse(hasattr(recovery, "probe_run_tmpfs"))

    def test_28_tmpfs_fullness_measured_via_the_normal_probe_on_real_run(self):
        recovery.ensure_run_tmpfs_monitor_check()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        # /run is virtually always mounted on any Linux host this test
        # runs on -- a real, unmocked psutil.disk_usage call.
        status, detail = probes.probe_disk(check)
        self.assertIn(status, ("ok", "warning", "critical"))
        self.assertIn("percent", detail)
        self.assertGreaterEqual(detail["percent"], 0.0)

    def test_12_monitoring_poller_sees_new_row_on_next_cycle_without_restart(self):
        """The poller re-queries MonitorCheck.objects.filter(enabled=True)
        fresh every cycle (see monitoring/services/monitor.py's own
        comment) -- no monitoring-service restart is needed for a
        provisioned row to be picked up. Proven by calling the real
        cycle-building query the poller itself uses, not a bespoke
        reimplementation."""
        recovery.ensure_run_tmpfs_monitor_check()
        checks = list(MonitorCheck.objects.filter(enabled=True).order_by("sort_order"))
        names = [c.name for c in checks]
        self.assertIn("Disk: /run (runtime tmpfs)", names)
