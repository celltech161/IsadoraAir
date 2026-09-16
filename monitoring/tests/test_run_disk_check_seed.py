"""P2 1.13B -- "Disk: /run (runtime tmpfs)" MonitorCheck: seed-migration
idempotency/non-overwrite behavior, and proof it participates in the
SAME generic disk-probe/threshold machinery as every other "disk" kind
check (never a parallel tmpfs-specific implementation)."""
import importlib

from django.test import TestCase

from monitoring.models import MonitorCheck
from monitoring.services import probes

_seed_module = importlib.import_module(
    "monitoring.migrations.0014_seed_run_tmpfs_disk_check"
)


def _run_seed():
    """Calls the migration's own RunPython function directly against
    the real ORM model -- a plain get_or_create, so this exercises the
    exact same code path a real migration run executes without needing
    the full migration-graph apparatus for what is, in substance, one
    idempotent write."""
    _seed_module.seed_run_disk_check(apps=_RealAppsShim(), schema_editor=None)


class _RealAppsShim:
    """seed_run_disk_check(apps, schema_editor) calls apps.get_model(...)
    -- the real MonitorCheck model (not a frozen historical one) is
    exactly right here since this migration makes no field/shape
    change, only a data write."""

    def get_model(self, app_label, model_name):
        assert (app_label, model_name) == ("monitoring", "MonitorCheck")
        return MonitorCheck


class RunDiskCheckSeedTests(TestCase):
    def test_23_default_run_disk_monitor_is_created(self):
        self.assertFalse(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").exists())
        _run_seed()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        self.assertEqual(check.kind, "disk")
        self.assertEqual(check.disk_path, "/run")
        self.assertEqual(check.warning_threshold, 60.0)
        self.assertEqual(check.critical_threshold, 75.0)

    def test_24_existing_customized_equivalent_is_not_overwritten_or_duplicated(self):
        # An operator (or an earlier partial run of this same migration)
        # already created a row with this exact name, customized.
        MonitorCheck.objects.create(
            name="Disk: /run (runtime tmpfs)", kind="disk", disk_path="/run",
            warning_threshold=40.0, critical_threshold=55.0, sort_order=99,
        )
        _run_seed()
        self.assertEqual(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").count(), 1)
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        self.assertEqual(check.warning_threshold, 40.0)  # operator customization preserved
        self.assertEqual(check.critical_threshold, 55.0)
        self.assertEqual(check.sort_order, 99)

    def test_24b_seed_is_idempotent_across_repeated_runs(self):
        _run_seed()
        _run_seed()
        _run_seed()
        self.assertEqual(MonitorCheck.objects.filter(name="Disk: /run (runtime tmpfs)").count(), 1)

    def test_25_warning_threshold_produces_normal_monitoring_warning(self):
        _run_seed()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        from unittest.mock import patch, MagicMock
        usage = MagicMock(percent=65.0, used=650, total=1000)
        with patch.object(probes.psutil, "disk_usage", return_value=usage):
            status, detail = probes.probe_disk(check)
        self.assertEqual(status, "warning")
        self.assertEqual(detail["percent"], 65.0)

    def test_26_critical_threshold_produces_normal_monitoring_critical(self):
        _run_seed()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        from unittest.mock import patch, MagicMock
        usage = MagicMock(percent=80.0, used=800, total=1000)
        with patch.object(probes.psutil, "disk_usage", return_value=usage):
            status, detail = probes.probe_disk(check)
        self.assertEqual(status, "critical")

    def test_27_uses_the_same_generic_disk_probe_no_aircheck_specific_logic(self):
        # There is exactly one code path that evaluates a "disk" kind
        # check -- probes.probe_disk -- and this migration's row is
        # indistinguishable from any other disk check to it. Proven by
        # reusing it directly above (25/26) rather than any bespoke
        # tmpfs-only evaluator; this test additionally confirms no
        # Aircheck-specific disk-probe function exists to duplicate it.
        import aircheck.services.recorder as recorder_module
        self.assertFalse(hasattr(recorder_module, "probe_disk"))
        self.assertFalse(hasattr(recorder_module, "probe_run_tmpfs"))

    def test_28_tmpfs_fullness_measured_via_the_normal_probe_on_real_run(self):
        _run_seed()
        check = MonitorCheck.objects.get(name="Disk: /run (runtime tmpfs)")
        # /run is virtually always mounted on any Linux host this test
        # runs on -- a real, unmocked psutil.disk_usage call.
        status, detail = probes.probe_disk(check)
        self.assertIn(status, ("ok", "warning", "critical"))
        self.assertIn("percent", detail)
        self.assertGreaterEqual(detail["percent"], 0.0)
