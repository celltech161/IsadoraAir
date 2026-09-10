"""r0060 Phase 6, sections D/F -- deploy/verify_backup_roundtrip.sh's
static contract, and the two new optional systemd units
(isadoraair-backup-verify.service/.timer)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEPLOY_DIR = REPO_ROOT / "deploy"
SCRIPT_PATH = DEPLOY_DIR / "verify_backup_roundtrip.sh"
SERVICE_PATH = DEPLOY_DIR / "isadoraair-backup-verify.service"
TIMER_PATH = DEPLOY_DIR / "isadoraair-backup-verify.timer"
NIGHTLY_TIMER_PATH = DEPLOY_DIR / "isadoraair-backup.timer"
NINETY_SYSTEM_CONFIG = DEPLOY_DIR / "restore" / "90-system-config.sh"


class VerifyScriptExistsAndParsesTests(SimpleTestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT_PATH.is_file())
        self.assertTrue(SCRIPT_PATH.stat().st_mode & 0o111, "not executable (chmod +x)")

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class VerifyScriptContentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = SCRIPT_PATH.read_text(encoding="utf-8")

    def test_strict_failure_handling_enabled(self):
        self.assertIn("set -euo pipefail", self.text)

    def test_uses_same_credential_file_and_sshpass_convention(self):
        self.assertIn('CONFIG_FILE="$HOME/.iasboxbu.cred"', self.text)
        self.assertIn("export SSHPASS=", self.text)
        self.assertIn("sshpass -e", self.text)

    def test_password_never_in_argv_or_hardcoded(self):
        # BAK_PASS legitimately appears exactly twice: the required-value
        # guard (": ${BAK_PASS:?...}") and the one line that hands it to
        # SSHPASS for sshpass -e's own environment-variable convention.
        # It must never additionally appear as a literal argv token on
        # any sftp/sshpass/ssh invocation line.
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("sftp ", "sshpass ")) or " sftp " in stripped or " sshpass " in stripped:
                self.assertNotIn("BAK_PASS", line, f"password token appears on a command line: {line!r}")
        self.assertNotIn("PGPASSWORD", self.text)

    def test_does_not_parse_a_directory_listing_for_the_newest_backup(self):
        """Critical design simplification -- must read last-success.json
        for the exact filename/hash, never `ls`/glob the remote."""
        self.assertNotIn('"ls -1', self.text)
        self.assertNotIn("ls -1 isadoraair-backup", self.text)

    def test_reads_receipt_via_backup_assurance_module(self):
        self.assertIn("backup_assurance.py", self.text)
        self.assertIn("read-last-success", self.text)

    def test_validates_remote_filename_naming_contract(self):
        self.assertIn("REMOTE_FILENAME_RE=", self.text)

    def test_downloads_into_private_mode_0700_temp_dir(self):
        self.assertIn("mktemp -d", self.text)
        self.assertIn('chmod 0700 "$TMPDIR"', self.text)

    def test_cleanup_runs_on_every_exit(self):
        self.assertIn("trap cleanup EXIT", self.text)
        self.assertIn('rm -rf "$TMPDIR"', self.text)

    def test_calls_the_authoritative_inspector_and_pg_restore_list(self):
        self.assertIn('"$INSPECT_SCRIPT"', self.text)
        self.assertIn("pg_restore --list", self.text)

    def test_checks_git_sha_ancestry_against_main(self):
        self.assertIn("merge-base --is-ancestor", self.text)
        self.assertIn("cat-file -e", self.text)

    def test_never_deletes_or_mutates_remote_archive(self):
        for forbidden in ('"rm ', "echo \"rm ", "sftp_run <<<'rm"):
            self.assertNotIn(forbidden, self.text)

    def test_never_runs_a_database_restore(self):
        self.assertNotIn("pg_restore -d", self.text)
        self.assertNotIn("createdb", self.text)
        self.assertNotIn("dropdb", self.text)

    def test_records_separate_roundtrip_receipts(self):
        self.assertIn("roundtrip-attempt-start", self.text)
        self.assertIn("roundtrip-attempt-finish", self.text)
        self.assertIn("roundtrip-record-success", self.text)


class BackupVerifyServiceUnitTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = SERVICE_PATH.read_text(encoding="utf-8")

    def test_service_file_exists(self):
        self.assertTrue(SERVICE_PATH.is_file())

    def test_type_oneshot(self):
        self.assertIn("Type=oneshot", self.text)

    def test_execstart_runs_the_repo_managed_script(self):
        self.assertIn("ExecStart=@@ISA_ROOT@@/deploy/verify_backup_roundtrip.sh", self.text)

    def test_no_secret_value_embedded(self):
        for forbidden in ("BAK_PASS=", "BAK_HOST=", "PGPASSWORD"):
            self.assertNotIn(forbidden, self.text)

    def test_not_coupled_to_the_nightly_backup_unit(self):
        """Must not order/require against isadoraair-backup.service or
        its timer via a real systemd directive -- this verifies the
        already-promoted last-success receipt, independent of when/
        whether a nightly run is active. A comment MENTIONING the
        nightly unit (to explain the deliberate lack of coupling) is
        fine; only an actual directive line is forbidden."""
        for line in self.text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(("After=", "Before=", "Requires=", "Wants=", "BindsTo=", "PartOf=")):
                self.assertNotIn("isadoraair-backup.service", stripped)
                self.assertNotIn("isadoraair-backup.timer", stripped)


class BackupVerifyTimerUnitTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = TIMER_PATH.read_text(encoding="utf-8")

    def test_timer_file_exists(self):
        self.assertTrue(TIMER_PATH.is_file())

    def test_schedule_sunday_0630(self):
        self.assertIn("OnCalendar=Sun *-*-* 06:30:00", self.text)

    def test_persistent_true(self):
        self.assertIn("Persistent=true", self.text)

    def test_randomized_delay_900(self):
        self.assertIn("RandomizedDelaySec=900", self.text)

    def test_wanted_by_timers_target(self):
        self.assertIn("WantedBy=timers.target", self.text)


class NightlyBackupTimerUntouchedTests(SimpleTestCase):
    """The nightly isadoraair-backup.timer's own schedule must remain
    byte-identical -- r0060 must not alter it."""

    def test_nightly_timer_schedule_unchanged(self):
        text = NIGHTLY_TIMER_PATH.read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* 03:30:00", text)
        self.assertIn("Persistent=true", text)
        self.assertIn("RandomizedDelaySec=300", text)

    def test_two_timers_have_different_schedules(self):
        nightly = NIGHTLY_TIMER_PATH.read_text(encoding="utf-8")
        weekly = TIMER_PATH.read_text(encoding="utf-8")
        self.assertNotEqual(
            [l for l in nightly.splitlines() if l.startswith("OnCalendar=")],
            [l for l in weekly.splitlines() if l.startswith("OnCalendar=")],
        )


class NinetySystemConfigRendersVerifyUnitsTests(SimpleTestCase):
    """The two new optional units flow through the SAME generic
    deploy/*.service,*.timer render+install loop every other unit
    (including the existing optional isadoraair-updater.service) already
    uses -- Stage 90 only ever PLACES the file; it never `systemctl
    enable`s/starts anything, here or for any other unit (confirmed: no
    `systemctl enable`/`daemon-reload` call anywhere in this stage
    script) -- so appearing in this generic pass does not amount to
    auto-enablement."""

    def setUp(self):
        self.staging_root = Path(tempfile.mkdtemp(prefix="isadoraair-backup-verify-unit-test-"))
        self.addCleanup(shutil.rmtree, self.staging_root, ignore_errors=True)

    def test_ninety_system_config_never_enables_units(self):
        text = NINETY_SYSTEM_CONFIG.read_text(encoding="utf-8")
        self.assertNotIn("systemctl enable", text)

    def test_rendered_units_land_on_disk_but_are_not_enabled(self):
        result = subprocess.run(
            [str(NINETY_SYSTEM_CONFIG), "--staging-root", str(self.staging_root), "--apply"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        service_dest = self.staging_root / "etc" / "systemd" / "system" / "isadoraair-backup-verify.service"
        timer_dest = self.staging_root / "etc" / "systemd" / "system" / "isadoraair-backup-verify.timer"
        self.assertTrue(service_dest.is_file())
        self.assertTrue(timer_dest.is_file())
        self.assertIn(
            f"ExecStart={self.staging_root}/opt/isadoraair/deploy/verify_backup_roundtrip.sh",
            service_dest.read_text(encoding="utf-8"),
        )
        # No "enabled" symlink under wants.d/timers.target.wants was
        # created -- placement only, matching every other unit here.
        wants_dir = self.staging_root / "etc" / "systemd" / "system" / "timers.target.wants"
        if wants_dir.is_dir():
            self.assertNotIn("isadoraair-backup-verify.timer", [p.name for p in wants_dir.iterdir()])
