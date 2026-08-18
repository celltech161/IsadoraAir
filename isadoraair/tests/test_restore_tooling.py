"""1.2 disaster-recovery Phase 4 -- deploy/restore/.

Mirrors test_deploy_backup_script.py's own approach: real stage scripts
need production secrets, a real backup archive, and (for several stages)
real system state to actually run meaningfully, so most of what's tested
here is static/structural -- syntax, safety-guard presence, and the
kind of "never touches the music library / never hardcodes a secret"
properties that matter most for disaster-recovery correctness.

inspect_backup.sh is the one script fully exercised for real here --
against small, synthetic, temp-directory-only archives (never a real
production backup), which is enough to prove its actual pass/fail logic
works without needing production data in the test suite."""
import shutil
import subprocess
import tarfile
from pathlib import Path

from django.test import SimpleTestCase

RESTORE_DIR = Path(__file__).resolve().parent.parent.parent / "deploy" / "restore"
STAGE_SCRIPTS = sorted(RESTORE_DIR.glob("*.sh"))


class RestoreScriptsExistAndParseTests(SimpleTestCase):
    def test_at_least_the_expected_stage_scripts_exist(self):
        names = {p.name for p in STAGE_SCRIPTS}
        for expected in (
            "lib.sh", "inspect_backup.sh", "restore.sh",
            "00-preflight.sh", "10-packages.sh", "20-application.sh",
            "30-postgresql.sh", "40-station-content.sh", "50-native-deps.sh",
            "60-python.sh", "70-tts.sh", "80-companions.sh",
            "90-system-config.sh", "95-validate.sh",
        ):
            self.assertIn(expected, names, f"{expected} missing from {RESTORE_DIR}")

    def test_every_script_is_executable(self):
        for p in STAGE_SCRIPTS:
            if p.name == "lib.sh":
                continue  # sourced, not executed directly -- see its own header comment
            self.assertTrue(p.stat().st_mode & 0o111, f"{p} is not executable (chmod +x)")

    def test_every_script_has_valid_bash_syntax(self):
        for p in STAGE_SCRIPTS:
            result = subprocess.run(
                ["bash", "-n", str(p)], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, f"bash -n failed for {p}:\n{result.stderr}")

    def test_every_stage_script_sources_lib_sh(self):
        """Stage scripts (everything except lib.sh itself) must go
        through the shared safety-mode machinery, not reimplement their
        own ad hoc plan/apply handling."""
        for p in STAGE_SCRIPTS:
            if p.name == "lib.sh":
                continue
            text = p.read_text(encoding="utf-8")
            if p.name == "restore.sh":
                continue  # orchestrator only -- delegates to the stage scripts
            self.assertIn('source "$SCRIPT_DIR/lib.sh"', text, f"{p} does not source lib.sh")


class LibShSafetyGuardTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = (RESTORE_DIR / "lib.sh").read_text(encoding="utf-8")

    def test_default_mode_is_plan_not_apply(self):
        self.assertIn('RESTORE_MODE="plan"', self.text)

    def test_music_library_guard_has_no_force_override(self):
        """The one guard with NO --force-* escape hatch, on purpose --
        see deploy/restore/README.md. Verified structurally: the
        guard_never_touch_music_library function body must not
        reference any RESTORE_FORCE_* variable."""
        start = self.text.index("guard_never_touch_music_library()")
        end = self.text.index("\n}", start)
        body = self.text[start:end]
        self.assertNotIn("RESTORE_FORCE", body)
        self.assertIn("/srv/isadoraair/music", body)

    def test_production_target_guard_requires_explicit_force_flag(self):
        self.assertIn("RESTORE_FORCE_PRODUCTION_TARGET", self.text)
        self.assertIn("--force-production-target", self.text)

    def test_db_and_env_guards_exist(self):
        self.assertIn("guard_db_overwrite()", self.text)
        self.assertIn("guard_env_overwrite()", self.text)

    def test_do_or_plan_never_executes_under_plan_mode(self):
        start = self.text.index("do_or_plan()")
        end = self.text.index("\n}", start)
        body = self.text[start:end]
        self.assertIn('if [ "$RESTORE_MODE" = "apply" ]', body)


class StageScriptsNeverHardcodeSecretsTests(SimpleTestCase):
    """Same property test_deploy_backup_script.py already enforces on
    the backup script itself, applied to every restore stage script --
    a restore tool handling .env/DB credentials is exactly the kind of
    file where a hardcoded secret would be catastrophic if ever
    committed by accident."""

    def test_no_hardcoded_password_assignments(self):
        for p in STAGE_SCRIPTS:
            text = p.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                for var in ("DB_PASSWORD", "PGPASSWORD", "SECRET_KEY"):
                    if f'{var}="' in line and "$" not in line.split(f'{var}="', 1)[1].split('"')[0]:
                        # A literal, non-empty, non-variable value assigned directly.
                        value = line.split(f'{var}="', 1)[1].split('"')[0]
                        if value:
                            self.fail(f"{p.name}:{lineno} appears to hardcode {var}: {line!r}")


class StageScriptsNeverLogSecretValuesTests(SimpleTestCase):
    def test_postgresql_stage_never_echoes_password_value(self):
        text = (RESTORE_DIR / "30-postgresql.sh").read_text(encoding="utf-8")
        self.assertIn("not logged", text.lower())
        # The password variable is used (exported to PGPASSWORD / used in
        # a CREATE USER statement), but never passed to log_info/log_apply.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "log_info" in line or "log_apply" in line or "log_warn" in line:
                self.assertNotIn("$DB_PASSWORD", line, f"line {lineno} logs DB_PASSWORD: {line!r}")

    def test_application_stage_never_echoes_env_file_contents(self):
        text = (RESTORE_DIR / "20-application.sh").read_text(encoding="utf-8")
        self.assertIn("value NOT logged", text)


class InspectBackupFunctionalTests(SimpleTestCase):
    """Real subprocess execution against small synthetic archives built
    entirely in a temp directory -- never a real production backup."""

    def setUp(self):
        self.tmpdir = Path(
            subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _build_archive(self, workdir: Path, out_path: Path):
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")

    def _run_inspect(self, archive_path):
        return subprocess.run(
            [str(RESTORE_DIR / "inspect_backup.sh"), str(archive_path)],
            capture_output=True, text=True, timeout=30,
        )

    def test_valid_minimal_archive_passes(self):
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(
            "IsadoraAir disaster-recovery backup manifest\n"
            "Backup script version: 2.0.0\n"
            "Created (UTC):          2026-01-01T00:00:00+00:00\n"
            "IsadoraAir Git SHA:     deadbeefcafebabe0000000000000000000000\n"
        )
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n")
        (app_dir / ".env").write_text("SECRET_KEY=test\n")
        app_tar = workdir / "app.tar.gz"
        with tarfile.open(app_tar, "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL: PASS", result.stdout)
        self.assertIn("deadbeefcafebabe", result.stdout)

    def test_missing_env_in_app_tar_fails(self):
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n")
        # deliberately no .env
        app_tar = workdir / "app.tar.gz"
        with tarfile.open(app_tar, "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("OVERALL: FAIL", result.stdout)
        self.assertIn(".env", result.stdout)

    def test_missing_database_dump_fails(self):
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        # no database.dump at all
        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Database dump", result.stdout)

    def test_dump_with_wrong_magic_bytes_fails(self):
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        (workdir / "database.dump").write_bytes(b"NOTAPGDUMP" + b"\x00" * 100)
        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("PGDMP", result.stdout)

    def test_nonexistent_archive_fails_clearly(self):
        result = self._run_inspect(self.tmpdir / "does-not-exist.tar.gz")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Archive exists", result.stdout)

    def test_corrupt_archive_fails_clearly(self):
        bad = self.tmpdir / "corrupt.tar.gz"
        bad.write_bytes(b"this is not a valid gzip tar file")
        result = self._run_inspect(bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Outer tar readable", result.stdout)

    def test_env_bak_present_warns_but_does_not_fail(self):
        """A stray .env.bak inside app.tar.gz should never be fatal on
        its own -- it's a warning (the backup script is supposed to
        exclude it; if it's ever present anyway, .env itself still being
        present is what actually matters for a restore)."""
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n")
        (app_dir / ".env").write_text("SECRET_KEY=test\n")
        (app_dir / ".env.bak").write_text("SECRET_KEY=stale\n")
        app_tar = workdir / "app.tar.gz"
        with tarfile.open(app_tar, "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARN", result.stdout)
        self.assertIn(".env.bak", result.stdout)

    def test_no_argument_prints_usage_and_exits_nonzero(self):
        result = subprocess.run(
            [str(RESTORE_DIR / "inspect_backup.sh")], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    # ---- 2026-08-18 Phase 4.5 final follow-up: encrypted recovery-
    # credential preservation checks. These build small synthetic archives
    # entirely under self.tmpdir -- never real credential content, never a
    # real age keypair (the archive inspector never needs one; it only
    # confirms ciphertext presence/non-emptiness, per its own documented
    # "no private key needed to inspect" contract).

    def _minimal_valid_workdir(self, workdir: Path):
        """Same minimally-valid shape as test_valid_minimal_archive_passes,
        factored out so the recovery-credential tests don't have to repeat
        it and can focus on just the new checks."""
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(
            "IsadoraAir disaster-recovery backup manifest\n"
            "Backup script version: 2.1.0\n"
            "Created (UTC):          2026-08-18T00:00:00+00:00\n"
            "IsadoraAir Git SHA:     deadbeefcafebabe0000000000000000000000\n"
        )
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n")
        (app_dir / ".env").write_text("SECRET_KEY=test\n")
        app_tar = workdir / "app.tar.gz"
        with tarfile.open(app_tar, "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")

    def _append_manifest(self, workdir: Path, extra_text: str):
        manifest = workdir / "MANIFEST.txt"
        manifest.write_text(manifest.read_text() + extra_text)

    def test_plaintext_credential_file_present_fails_regardless_of_manifest(self):
        """The plaintext-leak check is unconditional -- it must fire even
        on an archive whose manifest never mentions recovery-credential
        encryption at all (the worst case: something copied a real
        credential file in by accident, completely outside this
        feature)."""
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        (workdir / ".iasboxbu.cred").write_text("BAK_HOST=example.test\nBAK_PASS=oops\n")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("OVERALL: FAIL", result.stdout)
        self.assertIn("Plaintext credential check", result.stdout)
        self.assertIn(".iasboxbu.cred", result.stdout)
        # The fixture's fake secret value must never appear in the tool's
        # own output either.
        self.assertNotIn("oops", result.stdout)

    def test_plaintext_credential_file_at_nested_path_still_fails(self):
        """Matches at any path, not just top-level -- an accidental `cp`
        into the wrong place should still be caught."""
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        nested = workdir / "recovery-credentials"
        nested.mkdir()
        (nested / ".syndicated_ingest.cred").write_text("SMTP_PASSWORD=oops\n")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Plaintext credential check", result.stdout)
        self.assertIn(".syndicated_ingest.cred", result.stdout)

    def test_old_format_archive_with_no_recovery_credential_manifest_line_passes(self):
        """Backward compatibility: an archive produced before this
        feature existed (no "Recovery credential encryption:" line in
        MANIFEST.txt at all) must still pass overall -- treated as "not
        applicable", never a failure."""
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL: PASS", result.stdout)
        self.assertIn("Recovery credential encryption", result.stdout)
        self.assertIn("not applicable", result.stdout)

    def test_recovery_credential_encryption_disabled_manifest_line_passes(self):
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        self._append_manifest(
            workdir,
            "\nRecovery credential encryption: disabled (BACKUP_RECOVERY_AGE_RECIPIENT/_FILE not configured)\n",
        )
        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL: PASS", result.stdout)
        self.assertIn("disabled for this backup", result.stdout)

    def test_recovery_credential_encryption_enabled_with_valid_ciphertext_passes(self):
        """A well-formed new-format archive: manifest claims one included
        credential, and a real (synthetic, non-empty) ciphertext-shaped
        file exists at the expected path -- inspection must confirm
        presence/size only, never attempt to decrypt it."""
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        self._append_manifest(
            workdir,
            "\nRecovery credential encryption: enabled\n"
            "Recovery credential cipher: age\n"
            "Recovery credential iasboxbu: included\n"
            "Recovery credential syndicated_ingest: absent\n"
            "Recovery credential ogremote_ingest: absent\n",
        )
        recovery_dir = workdir / "recovery-credentials"
        recovery_dir.mkdir()
        # Not real age ciphertext -- inspection never parses/decrypts the
        # bytes, only confirms the file exists and is non-empty, so a
        # synthetic placeholder is sufficient and appropriate here.
        (recovery_dir / "iasboxbu.cred.age").write_bytes(b"age-encryption.org/v1\n" + b"\x00" * 64)

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL: PASS", result.stdout)
        self.assertIn("Recovery credential: iasboxbu", result.stdout)
        self.assertIn("bytes ciphertext", result.stdout)
        # syndicated_ingest/ogremote_ingest are "absent" in the manifest,
        # not "included" -- must not be checked for a matching file at all.
        self.assertNotIn("Recovery credential: syndicated_ingest", result.stdout)
        self.assertNotIn("Recovery credential: ogremote_ingest", result.stdout)

    def test_recovery_credential_enabled_but_file_missing_fails(self):
        """The exact "looks protected but isn't" failure mode this
        feature exists to prevent: manifest claims inclusion, but the
        archive genuinely has no matching .age file."""
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        self._append_manifest(
            workdir,
            "\nRecovery credential encryption: enabled\n"
            "Recovery credential cipher: age\n"
            "Recovery credential iasboxbu: included\n",
        )
        # Deliberately no recovery-credentials/ directory at all.
        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("OVERALL: FAIL", result.stdout)
        self.assertIn("Recovery credential: iasboxbu", result.stdout)
        self.assertIn("missing from the archive", result.stdout)

    def test_recovery_credential_enabled_but_ciphertext_empty_fails(self):
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        self._append_manifest(
            workdir,
            "\nRecovery credential encryption: enabled\n"
            "Recovery credential cipher: age\n"
            "Recovery credential ogremote_ingest: included\n",
        )
        recovery_dir = workdir / "recovery-credentials"
        recovery_dir.mkdir()
        (recovery_dir / "ogremote_ingest.cred.age").write_bytes(b"")

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("OVERALL: FAIL", result.stdout)
        self.assertIn("Recovery credential: ogremote_ingest", result.stdout)
        self.assertIn("EMPTY", result.stdout)

    def test_recovery_credential_enabled_all_three_included_and_valid_passes(self):
        workdir = self.tmpdir / "work"
        self._minimal_valid_workdir(workdir)
        self._append_manifest(
            workdir,
            "\nRecovery credential encryption: enabled\n"
            "Recovery credential cipher: age\n"
            "Recovery credential iasboxbu: included\n"
            "Recovery credential syndicated_ingest: included\n"
            "Recovery credential ogremote_ingest: included\n",
        )
        recovery_dir = workdir / "recovery-credentials"
        recovery_dir.mkdir()
        for name in ("iasboxbu", "syndicated_ingest", "ogremote_ingest"):
            (recovery_dir / f"{name}.cred.age").write_bytes(b"age-encryption.org/v1\n" + name.encode() + b"\x00" * 32)

        archive_path = self.tmpdir / "test-backup.tar.gz"
        self._build_archive(workdir, archive_path)

        result = self._run_inspect(archive_path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OVERALL: PASS", result.stdout)
        for name in ("iasboxbu", "syndicated_ingest", "ogremote_ingest"):
            self.assertIn(f"Recovery credential: {name}", result.stdout)

    def test_inspection_never_invokes_age_or_requires_a_private_key(self):
        """The archive inspector must not shell out to `age` at all --
        confirming ciphertext presence/size is enough; decrypting is
        explicitly out of scope (no private key exists to inspect with,
        by design)."""
        text = (RESTORE_DIR / "inspect_backup.sh").read_text(encoding="utf-8")
        self.assertNotIn("age --decrypt", text)
        self.assertNotIn("age -d", text)
        self.assertNotRegex(text, r"\bage\s+-i\b")
