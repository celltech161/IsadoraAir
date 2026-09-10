"""r0060 Phase 6 -- the "backup"/"Backup Recovery Assurance" MonitorCheck
kind: model validation, the ABSENCE of any automatic seeded row
(migration 0013 is additive-only -- the default row is created
explicitly during station Phase 6 activation, never by a migration,
post_migrate signal, AppConfig startup hook, or hidden get-or-create),
and probe_backup's delegation to isadoraair.backup_assurance."""
import os
import tempfile
from datetime import datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.db import migrations as django_migrations
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

from isadoraair import backup_assurance as ba
from monitoring.models import MonitorCheck
from monitoring.services.probes import PROBE_DISPATCH, probe_backup


class BackupKindCleanTests(TestCase):
    def test_no_unrelated_fields_required(self):
        """The "backup" kind needs none of the systemd/disk/transmitter/
        encoder_group/audio_silence fields every other kind's clean()
        can require."""
        check = MonitorCheck(name="x-backup-clean", kind="backup")
        check.clean()  # must not raise

    def test_other_kinds_unaffected(self):
        check = MonitorCheck(name="x-systemd", kind="systemd", systemd_unit="isadoraair-engine.service")
        check.clean()  # must not raise


class NoAutomaticBackupCheckRowTests(TestCase):
    """r0060 correction: migration 0013 is additive-ONLY (see that
    file's own header) -- it must never create a "Backup Recovery
    Assurance" row, or any "backup"-kind row at all, by itself. The
    migration ran when this test database was built (same as every
    other migration), so an absent row here is the real, functional
    proof this feature has no automatic seeding mechanism -- not just a
    reading of the migration file's text."""

    def test_no_backup_recovery_assurance_row_created_by_migration(self):
        self.assertFalse(
            MonitorCheck.objects.filter(name="Backup Recovery Assurance").exists()
        )

    def test_no_backup_kind_row_of_any_name_created_by_migration(self):
        self.assertFalse(MonitorCheck.objects.filter(kind="backup").exists())

    def test_kind_choice_is_registered(self):
        self.assertIn("backup", dict(MonitorCheck.KIND_CHOICES))
        self.assertEqual(dict(MonitorCheck.KIND_CHOICES)["backup"], "Backup Recovery Assurance")


class ExplicitStationActivationRowTests(TestCase):
    """Proves the model fully supports creating the recommended
    "Backup Recovery Assurance" row BY HAND (standing in for explicit
    Phase 6 station activation, after real backup/round-trip receipts
    already exist) -- the exact settings docs/DISASTER_RECOVERY_STATUS.md
    recommends, with no migration/signal/AppConfig involvement."""

    def test_recommended_row_saves_and_validates_cleanly(self):
        check = MonitorCheck(
            name="Backup Recovery Assurance",
            kind="backup",
            enabled=True,
            show_as_card=True,
            notify_on_warning=True,
            notify_on_critical=True,
            consecutive_failures_required=1,
        )
        check.full_clean()  # must not raise
        check.save()
        self.assertEqual(MonitorCheck.objects.filter(kind="backup").count(), 1)


class Migration0013ContentTests(TestCase):
    """r0060 correction: the most literal, direct proof possible that
    monitoring.0013_backup_recovery_assurance_check carries no
    RunPython (or any other data-mutating) operation -- reads the
    actual on-disk migration's `operations` list directly, independent
    of Update Center's own classifier (that's covered separately in
    updatecenter.tests.test_updatecenter_probe.
    ActualMonitoring0013ClassificationTests)."""

    def test_no_runpython_operation(self):
        loader = MigrationLoader(None)
        migration = loader.disk_migrations[("monitoring", "0013_backup_recovery_assurance_check")]
        self.assertFalse(
            any(isinstance(op, django_migrations.RunPython) for op in migration.operations),
            "monitoring.0013 must not contain a RunPython operation",
        )

    def test_exactly_one_alterfield_operation(self):
        loader = MigrationLoader(None)
        migration = loader.disk_migrations[("monitoring", "0013_backup_recovery_assurance_check")]
        self.assertEqual(len(migration.operations), 1)
        self.assertIsInstance(migration.operations[0], django_migrations.AlterField)


class ProbeDispatchWiringTests(TestCase):
    def test_backup_kind_dispatches_to_probe_backup(self):
        self.assertIs(PROBE_DISPATCH["backup"], probe_backup)


class ProbeBackupDelegationTests(TestCase):
    """probe_backup must be a thin, read-only delegation to
    isadoraair.backup_assurance.evaluate_backup_health() -- covered here
    at the MonitorCheck-probe boundary; evaluate_backup_health()'s own
    threshold logic is covered exhaustively in
    isadoraair/tests/test_backup_assurance.py."""

    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="isadoraair-probe-backup-")
        self._prior = os.environ.get(ba.STATE_DIR_ENV)
        os.environ[ba.STATE_DIR_ENV] = self.state_dir
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._prior is None:
            os.environ.pop(ba.STATE_DIR_ENV, None)
        else:
            os.environ[ba.STATE_DIR_ENV] = self._prior

    def test_uninitialized_reports_unknown(self):
        check = MonitorCheck(name="x", kind="backup")
        status, detail = probe_backup(check)
        self.assertEqual(status, "unknown")

    def test_fresh_success_reports_ok(self):
        ba.record_success(
            remote_filename="isadoraair-backup-20260910-033000.tar.gz",
            archive_bytes=100, archive_sha256="a" * 64,
            backup_script_version="3.2.0", archive_format_version="3.0.0",
            recovery_class="self_contained_v3", retention_days=30,
            validations={"database_catalog": True, "archive_integrity": True,
                         "runtime_extractable": True, "recovery_policy_satisfied": True,
                         "remote_promotion": True},
            git_state={"sha": "a" * 40, "branch": "main", "detached": False, "dirty": False},
        )
        ba.record_roundtrip_success(
            remote_filename="isadoraair-backup-20260910-033000.tar.gz",
            expected_sha256="b" * 64, observed_sha256="b" * 64,
            backup_git_sha="a" * 40, inspector_result="pass",
            pg_restore_catalog_result="pass", main_ancestry_result="pass",
        )
        check = MonitorCheck(name="x", kind="backup")
        status, detail = probe_backup(check)
        self.assertEqual(status, "ok")
        self.assertIn("message", detail)

    def test_probe_never_raises_on_corrupt_state(self):
        os.makedirs(self.state_dir, exist_ok=True)
        with open(os.path.join(self.state_dir, ba.SUCCESS_FILE), "w") as fh:
            fh.write("{not json")
        check = MonitorCheck(name="x", kind="backup")
        status, detail = probe_backup(check)
        self.assertEqual(status, "critical")
