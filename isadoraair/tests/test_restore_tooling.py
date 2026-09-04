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
import hashlib
import importlib.util
import os
import io
import json
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESTORE_DIR = REPO_ROOT / "deploy" / "restore"
STAGE_SCRIPTS = sorted(RESTORE_DIR.glob("*.sh"))


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered in sys.modules BEFORE exec -- dataclasses (used by
    # offline_snap_install.py) resolves its owning module via
    # sys.modules[cls.__module__] while the class body is still
    # executing, which requires the entry to already exist.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


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


class RestoreDbNameExportFunctionalTests(SimpleTestCase):
    """Runtime Foundation E7C (2026-08-29) restore-safety regression:
    real acceptance testing found that 30-postgresql.sh pg_restores into
    an isolated $RESTORE_DB_NAME under --staging-root, but
    $RESTORE_TARGET_ROOT/.env is a byte-faithful copy of the real
    station's .env -- so any later manage.py invocation would silently
    default to the real production database name (python-decouple's
    config() checks the OS environment before .env) unless an operator
    manually exported DB_NAME. The fix: restore_parse_common_args
    exports DB_NAME=$RESTORE_DB_NAME itself. Verified here via real
    subprocess execution -- including confirming it is genuinely
    EXPORTED (visible to a child process), not just a local shell
    variable that happens to share the name."""

    def _resolve(self, *args: str) -> subprocess.CompletedProcess:
        script = (
            'set -euo pipefail; '
            f'source "{RESTORE_DIR / "lib.sh"}"; '
            f'restore_parse_common_args {" ".join(args)} >/dev/null 2>&1; '
            # A child process only sees DB_NAME if it was actually
            # exported -- a plain (unexported) shell variable of the
            # same name would not appear here.
            'bash -c \'echo "CHILD_DB_NAME=$DB_NAME"\''
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)

    def test_staging_root_exports_the_isolated_db_name_to_child_processes(self):
        result = self._resolve("--staging-root /tmp/whatever-e7c-lib-test --apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHILD_DB_NAME=isadoraair_restore_test", result.stdout)

    def test_production_mode_exports_the_real_db_name(self):
        result = self._resolve("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHILD_DB_NAME=isadoraair", result.stdout)

    def test_explicit_db_name_override_is_exported_verbatim(self):
        result = self._resolve("--staging-root /tmp/whatever-e7c-lib-test --db-name custom_test_db --apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CHILD_DB_NAME=custom_test_db", result.stdout)

    def test_decouple_actually_prefers_an_exported_db_name_over_env_file(self):
        """Not just that DB_NAME is exported -- that the actual mechanism
        the restored .env is read through (python-decouple) honors it.
        A regression here would silently defeat the whole fix even if
        the export itself still looked correct."""
        tmpdir = Path(
            subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
        )
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        (tmpdir / ".env").write_text("DB_NAME=isadoraair\n", encoding="utf-8")
        python = Path(__file__).resolve().parent.parent.parent / "venv" / "bin" / "python"
        if not python.exists():
            self.skipTest("no venv symlink present in this worktree")
        probe = (
            "from decouple import Config, RepositoryEnv\n"
            "print(Config(RepositoryEnv('.env'))('DB_NAME'))\n"
        )
        result = subprocess.run(
            [str(python), "-c", probe],
            cwd=tmpdir,
            capture_output=True,
            text=True,
            env={**os.environ, "DB_NAME": "isadoraair_restore_test"},
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "isadoraair_restore_test")


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
        self.assertIn("do_or_plan_redacted", text)
        self.assertNotIn("CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASSWORD}'", text)
        # The password variable is used (exported to PGPASSWORD / fed to
        # createuser's non-echoing password prompt), but never passed to a
        # normal logging function.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "log_info" in line or "log_apply" in line or "log_warn" in line:
                self.assertNotIn("$DB_PASSWORD", line, f"line {lineno} logs DB_PASSWORD: {line!r}")

    def test_application_stage_never_echoes_env_file_contents(self):
        text = (RESTORE_DIR / "20-application.sh").read_text(encoding="utf-8")
        self.assertIn("value NOT logged", text)


class Stage30SecretLoggingFunctionalTests(SimpleTestCase):
    """Exercise Stage 30's apply path with a synthetic archive and fake
    PostgreSQL commands. The sentinel password is deliberately shell-hostile:
    it proves the real value reaches createuser over stdin while never reaching
    command logging, stdout/stderr, or the evidence transcript."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-stage30-secret-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.password = "E8-stage30-secret-'-$-20260904"
        self.db_user = "isadoraair_e8_test"

        self.staging_root = self.tmpdir / "staging"
        self.target_root = self.staging_root / "opt" / "isadoraair"
        self.target_root.mkdir(parents=True)
        self.env_file = self.target_root / ".env"
        self.env_file.write_text(
            f"DB_USER={self.db_user}\n"
            f"DB_PASSWORD={self.password}\n"
            "DB_HOST=localhost\n"
            "DB_PORT=5432\n",
            encoding="utf-8",
        )

        archive_root = self.tmpdir / "archive-root"
        archive_root.mkdir()
        (archive_root / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 64)
        self.archive = self.tmpdir / "backup.tar.gz"
        with tarfile.open(self.archive, "w:gz") as tf:
            tf.add(archive_root, arcname=".")

        self.fakebin = self.tmpdir / "fakebin"
        self.fakebin.mkdir()
        self.role_password_file = self.tmpdir / "role-password-received"
        self.createuser_args_file = self.tmpdir / "createuser-args"
        self.database_file = self.tmpdir / "database-created"
        self.restore_file = self.tmpdir / "pg-restore-completed"

        self._write_executable(
            "sudo",
            """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-u" ]; then
  shift 2
fi
exec "$@"
""",
        )
        self._write_executable(
            "createuser",
            """#!/usr/bin/env bash
set -euo pipefail
IFS= read -r first_password
IFS= read -r second_password
[ "$first_password" = "$second_password" ]
printf '%s' "$first_password" > "$FAKE_ROLE_PASSWORD_FILE"
printf '%s\n' "$*" > "$FAKE_CREATEUSER_ARGS_FILE"
""",
        )
        self._write_executable(
            "psql",
            """#!/usr/bin/env bash
set -euo pipefail
args="$*"
if [[ "$args" == *"FROM pg_roles"* ]]; then
  if [ -f "$FAKE_ROLE_PASSWORD_FILE" ]; then printf '1\n'; fi
elif [[ "$args" == *"FROM pg_database"* ]]; then
  if [ -f "$FAKE_DATABASE_FILE" ]; then printf '1\n'; fi
elif [[ "$args" == *"CREATE DATABASE"* ]]; then
  : > "$FAKE_DATABASE_FILE"
elif [[ "$args" == *"SELECT count(*) FROM django_migrations"* ]]; then
  printf '42\n'
elif [[ "$args" == *"table_name = 'django_migrations'"* ]]; then
  printf '1\n'
elif [[ "$args" == *"information_schema.tables"* ]]; then
  if [ -f "$FAKE_RESTORE_FILE" ]; then printf '7\n'; else printf '0\n'; fi
fi
""",
        )
        self._write_executable(
            "pg_restore",
            """#!/usr/bin/env bash
set -euo pipefail
: > "$FAKE_RESTORE_FILE"
""",
        )

        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "FAKE_ROLE_PASSWORD_FILE": str(self.role_password_file),
            "FAKE_CREATEUSER_ARGS_FILE": str(self.createuser_args_file),
            "FAKE_DATABASE_FILE": str(self.database_file),
            "FAKE_RESTORE_FILE": str(self.restore_file),
        }
        self.result = subprocess.run(
            [
                str(RESTORE_DIR / "30-postgresql.sh"),
                "--archive", str(self.archive),
                "--staging-root", str(self.staging_root),
                "--apply",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.combined_output = self.result.stdout + self.result.stderr
        self.evidence_file = self.tmpdir / "e8-stage30.evidence.log"
        self.evidence_file.write_text(self.combined_output, encoding="utf-8")

    def _write_executable(self, name: str, content: str):
        path = self.fakebin / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def test_db_password_is_absent_from_stage_stdout_and_stderr(self):
        self.assertNotIn(self.password, self.result.stdout)
        self.assertNotIn(self.password, self.result.stderr)

    def test_db_password_is_absent_from_generated_evidence_log(self):
        self.assertNotIn(self.password, self.evidence_file.read_text(encoding="utf-8"))

    def test_postgresql_role_database_and_dump_restore_still_succeed(self):
        self.assertEqual(self.result.returncode, 0, self.combined_output)
        self.assertEqual(self.role_password_file.read_text(encoding="utf-8"), self.password)
        self.assertEqual(
            self.createuser_args_file.read_text(encoding="utf-8").strip(),
            f"--pwprompt --no-password {self.db_user}",
        )
        self.assertTrue(self.database_file.is_file())
        self.assertTrue(self.restore_file.is_file())
        self.assertIn("30-postgresql: PASS", self.combined_output)

    def test_redacted_apply_log_remains_operator_useful(self):
        self.assertIn(
            f"[APPLY] create PostgreSQL login role '{self.db_user}' with createuser --pwprompt",
            self.combined_output,
        )
        self.assertIn(f"password from {self.env_file}: <redacted>", self.combined_output)


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


class RestoreLocateRecoveryPayloadFunctionalTests(SimpleTestCase):
    """Runtime Foundation E7B -- real subprocess execution of lib.sh's
    restore_locate_recovery_payload against small synthetic backup-v3-
    style archives, mirroring InspectBackupFunctionalTests' own
    established pattern (never a real production backup)."""

    def setUp(self):
        self.tmpdir = Path(
            subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _build_archive(self, workdir: Path, out_path: Path):
        payload = workdir / "runtime-recovery"
        if payload.is_dir():
            payload.chmod(0o755)
            for path in payload.rglob("*"):
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file() and (path.stat().st_mode & 0o777) not in (0o644, 0o755):
                    path.chmod(0o644)
        with tarfile.open(out_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")

    def _write_v3_metadata(self, workdir: Path, *, required=("native_fdkaac",)):
        (workdir / "runtime-recovery-archive.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backup_script_version": "3.0.0",
                    "archive_format_version": "3.0.0",
                    "recovery_class": "self_contained_v3",
                    "payload_included": True,
                    "payload_id": "p1",
                    "payload_schema_version": 1,
                    "product_contract_sha256": "0" * 64,
                    "included_components": ["native_fdkaac"],
                    "required_components": list(required),
                    "policy_satisfied": True,
                    "piper_freshness": "not_checked",
                },
                sort_keys=True,
            )
        )

    def _locate(self, archive_path: Path, dest: Path):
        script = (
            'set -euo pipefail; '
            f'source "{RESTORE_DIR / "lib.sh"}"; '
            f'RESTORE_ARCHIVE="{archive_path}"; '
            f'restore_locate_recovery_payload "{dest}"; '
            'echo "FOUND=$RESTORE_RECOVERY_PAYLOAD_FOUND"'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)

    def test_archive_with_runtime_recovery_extracts_it_as_the_payload_root(self):
        workdir = self.tmpdir / "work"
        (workdir / "runtime-recovery" / "native" / "fdkaac").mkdir(parents=True)
        (workdir / "runtime-recovery" / "runtime-recovery.json").write_text('{"payload_id": "p1"}')
        (workdir / "runtime-recovery" / "native" / "fdkaac" / "source.tar.gz").write_bytes(b"fake")
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        self._write_v3_metadata(workdir)
        archive_path = self.tmpdir / "backup.tar.gz"
        self._build_archive(workdir, archive_path)

        dest = self.tmpdir / "extracted-payload"
        result = self._locate(archive_path, dest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FOUND=1", result.stdout)
        self.assertTrue((dest / "runtime-recovery.json").is_file())
        self.assertTrue((dest / "native" / "fdkaac" / "source.tar.gz").is_file())
        # Landed directly at dest -- no leftover "runtime-recovery/" prefix.
        self.assertFalse((dest / "runtime-recovery").exists())

    def test_archive_extraction_preserves_only_the_trusted_file_modes(self):
        workdir = self.tmpdir / "mode-work"
        payload = workdir / "runtime-recovery"
        payload.mkdir(parents=True)
        regular = payload / "runtime-recovery.json"
        regular.write_text('{"payload_id": "p1"}')
        regular.chmod(0o644)
        executable = payload / "protected-updater" / "updaterd.py"
        executable.parent.mkdir()
        executable.write_text("#!/usr/bin/env python3\n")
        executable.chmod(0o755)
        self._write_v3_metadata(workdir)
        archive = self.tmpdir / "trusted-modes.tar.gz"
        self._build_archive(workdir, archive)

        destination = self.tmpdir / "trusted-modes-output"
        result = self._locate(archive, destination)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(regular.stat().st_mode & 0o777, 0o644)
        self.assertEqual((destination / "runtime-recovery.json").stat().st_mode & 0o777, 0o644)
        self.assertEqual((destination / "protected-updater" / "updaterd.py").stat().st_mode & 0o777, 0o755)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o755)

    def test_archive_extraction_rejects_writable_and_unrecognized_file_modes(self):
        metadata_root = self.tmpdir / "mode-meta"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        for mode in (0o444, 0o600, 0o664, 0o700, 0o777, 0o4755):
            with self.subTest(mode=f"{mode:04o}"):
                archive = self.tmpdir / f"untrusted-mode-{mode:04o}.tar.gz"
                with tarfile.open(archive, "w:gz") as tf:
                    tf.add(
                        metadata_root / "runtime-recovery-archive.json",
                        arcname="runtime-recovery-archive.json",
                    )
                    root = tarfile.TarInfo("runtime-recovery/")
                    root.type = tarfile.DIRTYPE
                    root.mode = 0o755
                    tf.addfile(root)
                    member = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
                    member.mode = mode
                    member.size = 2
                    tf.addfile(member, io.BytesIO(b"{}"))
                destination = self.tmpdir / f"untrusted-mode-output-{mode:04o}"
                result = self._locate(archive, destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"untrusted mode {mode:04o}", result.stderr)
                self.assertFalse(destination.exists())

    def test_archive_extraction_rejects_executable_mode_outside_protected_updater(self):
        metadata_root = self.tmpdir / "executable-meta"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        archive = self.tmpdir / "unexpected-executable.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            root = tarfile.TarInfo("runtime-recovery/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            member = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
            member.mode = 0o755
            member.size = 2
            tf.addfile(member, io.BytesIO(b"{}"))

        destination = self.tmpdir / "unexpected-executable-output"
        result = self._locate(archive, destination)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untrusted mode 0755, expected 0644", result.stderr)
        self.assertFalse(destination.exists())

    def test_archive_extraction_rejects_untrusted_directory_mode(self):
        metadata_root = self.tmpdir / "directory-mode-meta"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        archive = self.tmpdir / "untrusted-directory-mode.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            root = tarfile.TarInfo("runtime-recovery/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o775
            tf.addfile(root)

        destination = self.tmpdir / "untrusted-directory-output"
        result = self._locate(archive, destination)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("untrusted mode 0775, expected 0755", result.stderr)
        self.assertFalse(destination.exists())

    def test_archive_ownership_metadata_is_not_applied(self):
        metadata_root = self.tmpdir / "owner-meta"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        archive = self.tmpdir / "untrusted-owner.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            root = tarfile.TarInfo("runtime-recovery/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            root.uid = 12345
            root.gid = 23456
            tf.addfile(root)
            member = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
            member.mode = 0o644
            member.uid = 12345
            member.gid = 23456
            member.size = 2
            tf.addfile(member, io.BytesIO(b"{}"))

        destination = self.tmpdir / "owner-output"
        result = self._locate(archive, destination)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(destination.stat().st_uid, os.geteuid())
        self.assertEqual(destination.stat().st_gid, os.getegid())
        self.assertEqual((destination / "runtime-recovery.json").stat().st_uid, os.geteuid())
        self.assertEqual((destination / "runtime-recovery.json").stat().st_gid, os.getegid())

    def test_archive_without_runtime_recovery_reports_not_found_not_error(self):
        """A v2.x-style archive (or a v3 archive backed up with no
        current payload configured) -- this must be a clean, non-fatal
        FOUND=0, never a script error."""
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 20)
        archive_path = self.tmpdir / "backup.tar.gz"
        self._build_archive(workdir, archive_path)

        dest = self.tmpdir / "extracted-payload"
        result = self._locate(archive_path, dest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("FOUND=0", result.stdout)

    def test_no_archive_given_is_a_clean_failure(self):
        script = (
            'set -euo pipefail; '
            f'source "{RESTORE_DIR / "lib.sh"}"; '
            'RESTORE_ARCHIVE=""; '
            f'restore_locate_recovery_payload "{self.tmpdir / "dest"}"'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)

    def test_nonempty_destination_is_refused_rather_than_merged_into(self):
        workdir = self.tmpdir / "work"
        (workdir / "runtime-recovery").mkdir(parents=True)
        (workdir / "runtime-recovery" / "runtime-recovery.json").write_text("{}")
        self._write_v3_metadata(workdir)
        archive_path = self.tmpdir / "backup.tar.gz"
        self._build_archive(workdir, archive_path)

        dest = self.tmpdir / "already-has-stuff"
        dest.mkdir()
        (dest / "pre-existing-file").write_text("do not touch")
        result = self._locate(archive_path, dest)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((dest / "pre-existing-file").is_file())
        self.assertFalse((dest / "runtime-recovery.json").exists())

    def test_symlink_member_is_rejected_before_extraction(self):
        archive = self.tmpdir / "symlink.tar.gz"
        metadata_root = self.tmpdir / "meta"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            root = tarfile.TarInfo("runtime-recovery/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            link = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        result = self._locate(archive, self.tmpdir / "dest-symlink")
        self.assertNotEqual(result.returncode, 0)

    def test_special_file_member_is_rejected_before_extraction(self):
        archive = self.tmpdir / "special-file.tar.gz"
        metadata_root = self.tmpdir / "meta-special-file"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            root = tarfile.TarInfo("runtime-recovery/")
            root.type = tarfile.DIRTYPE
            root.mode = 0o755
            tf.addfile(root)
            fifo = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
            fifo.type = tarfile.FIFOTYPE
            fifo.mode = 0o644
            tf.addfile(fifo)
        destination = self.tmpdir / "dest-special-file"
        result = self._locate(archive, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a directory or regular file", result.stderr)
        self.assertFalse(destination.exists())

    def test_traversal_member_is_rejected_before_extraction(self):
        archive = self.tmpdir / "traversal.tar.gz"
        metadata_root = self.tmpdir / "meta-traversal"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            member = tarfile.TarInfo("runtime-recovery/../../escape")
            member.size = 4
            tf.addfile(member, io.BytesIO(b"evil"))
        result = self._locate(archive, self.tmpdir / "dest-traversal")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmpdir / "escape").exists())

    def test_duplicate_member_is_rejected_before_extraction(self):
        archive = self.tmpdir / "duplicate.tar.gz"
        metadata_root = self.tmpdir / "meta-duplicate"
        metadata_root.mkdir()
        self._write_v3_metadata(metadata_root)
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(metadata_root / "runtime-recovery-archive.json", arcname="runtime-recovery-archive.json")
            for content in (b"one", b"two"):
                member = tarfile.TarInfo("runtime-recovery/runtime-recovery.json")
                member.size = len(content)
                tf.addfile(member, io.BytesIO(content))
        result = self._locate(archive, self.tmpdir / "dest-duplicate")
        self.assertNotEqual(result.returncode, 0)

    def test_final_receipt_requires_every_policy_component(self):
        workdir = self.tmpdir / "receipt-work"
        (workdir / "runtime-recovery").mkdir(parents=True)
        (workdir / "runtime-recovery" / "runtime-recovery.json").write_text("{}")
        self._write_v3_metadata(workdir)
        archive = self.tmpdir / "receipt.tar.gz"
        self._build_archive(workdir, archive)
        helper = RESTORE_DIR / "runtime_recovery_archive.py"
        receipt = self.tmpdir / "receipt.json"
        before = subprocess.run(
            [str(helper), "accept", "--archive", str(archive), "--receipt", str(receipt)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(before.returncode, 0)
        recorded = subprocess.run(
            [str(helper), "record", "--archive", str(archive), "--receipt", str(receipt),
             "--component", "native_fdkaac"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        accepted = subprocess.run(
            [str(helper), "accept", "--archive", str(archive), "--receipt", str(receipt)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)


class ProtectedUpdaterReceiptSemanticsTests(SimpleTestCase):
    """r0030: the receipt/accept contract (runtime_recovery_archive.py)
    needed zero code changes to support protected_updater -- it was
    already fully generic across all four COMPONENTS. These tests prove
    that generic contract actually behaves correctly for
    protected_updater specifically, including alongside another
    component, matching the design requirement that one successful
    component can never mask another missing one."""

    def setUp(self):
        self.tmpdir = Path(
            subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.helper = RESTORE_DIR / "runtime_recovery_archive.py"

    def _write_schema_two_metadata(self, workdir: Path, *, required):
        (workdir / "runtime-recovery-archive.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backup_script_version": "3.0.0",
                    "archive_format_version": "3.0.0",
                    "recovery_class": "self_contained_v3",
                    "payload_included": True,
                    "payload_id": "p-phase-d",
                    "payload_schema_version": 2,
                    "product_contract_sha256": "0" * 64,
                    "included_components": ["native_fdkaac", "protected_updater"],
                    "required_components": list(required),
                    "policy_satisfied": True,
                    "piper_freshness": "not_checked",
                },
                sort_keys=True,
            )
        )

    def _build_archive(self, *, required) -> Path:
        workdir = self.tmpdir / f"work-{'-'.join(required)}"
        (workdir / "runtime-recovery").mkdir(parents=True)
        (workdir / "runtime-recovery" / "runtime-recovery.json").write_text("{}")
        self._write_schema_two_metadata(workdir, required=required)
        archive = self.tmpdir / f"{'-'.join(required)}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive

    def _run(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.helper), *args], capture_output=True, text=True, timeout=15,
        )

    def test_recording_one_required_component_does_not_mask_another_missing_one(self):
        """required_components = [native_fdkaac, protected_updater].
        Recording only native_fdkaac must still fail acceptance -- a
        successful component can never paper over a still-missing one."""
        archive = self._build_archive(required=["native_fdkaac", "protected_updater"])
        receipt = self.tmpdir / "receipt.json"

        recorded = self._run("record", "--archive", str(archive), "--receipt", str(receipt),
                              "--component", "native_fdkaac")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)

        still_incomplete = self._run("accept", "--archive", str(archive), "--receipt", str(receipt))
        self.assertNotEqual(still_incomplete.returncode, 0)
        self.assertIn("protected_updater", still_incomplete.stdout + still_incomplete.stderr)

        recorded_second = self._run("record", "--archive", str(archive), "--receipt", str(receipt),
                                     "--component", "protected_updater")
        self.assertEqual(recorded_second.returncode, 0, recorded_second.stdout + recorded_second.stderr)
        self.assertIn("native_fdkaac", json.loads(recorded_second.stdout)["recovered_components"],
                      "recording protected_updater must not lose the earlier native_fdkaac record")

        complete = self._run("accept", "--archive", str(archive), "--receipt", str(receipt))
        self.assertEqual(complete.returncode, 0, complete.stdout + complete.stderr)

    def test_protected_updater_required_and_never_recorded_fails_acceptance(self):
        """The exact original defect, reproduced directly against the
        receipt contract: a schema-2 archive requiring protected_updater
        where NO receipt file exists yet at all (no stage ever ran) must
        fail acceptance clearly -- exactly the state r0029 left every
        such archive in, since no stage called `record`."""
        archive = self._build_archive(required=["protected_updater"])
        receipt = self.tmpdir / "receipt.json"
        result = self._run("accept", "--archive", str(archive), "--receipt", str(receipt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or invalid", result.stdout + result.stderr)

    def test_protected_updater_required_and_recorded_for_a_different_component_only_fails(self):
        """The same defect, one step further: a receipt DOES exist (an
        earlier stage recorded something), but never protected_updater
        specifically -- acceptance must name it as the missing piece,
        never silently pass because the receipt file merely exists."""
        archive = self._build_archive(required=["native_fdkaac", "protected_updater"])
        receipt = self.tmpdir / "receipt.json"
        recorded = self._run("record", "--archive", str(archive), "--receipt", str(receipt),
                              "--component", "native_fdkaac")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        result = self._run("accept", "--archive", str(archive), "--receipt", str(receipt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected_updater", result.stdout + result.stderr)

    def test_protected_updater_not_required_accepts_without_ever_recording_it(self):
        """required_components = [native_fdkaac] only (protected_updater
        present in the archive but not policy-required) -- acceptance
        must succeed once native_fdkaac alone is recorded; stage 95 must
        never demand a component this station's policy didn't ask for."""
        archive = self._build_archive(required=["native_fdkaac"])
        receipt = self.tmpdir / "receipt.json"
        recorded = self._run("record", "--archive", str(archive), "--receipt", str(receipt),
                              "--component", "native_fdkaac")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        accepted = self._run("accept", "--archive", str(archive), "--receipt", str(receipt))
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_receipt_for_a_different_payload_id_is_rejected_not_silently_reused(self):
        """A receipt genuinely completed against one archive/payload
        must never be mistaken for evidence about a different one --
        e.g. a stale receipt left over from an earlier restore attempt
        against a different backup."""
        archive_one = self._build_archive(required=["protected_updater"])
        receipt = self.tmpdir / "receipt.json"
        recorded = self._run("record", "--archive", str(archive_one), "--receipt", str(receipt),
                              "--component", "protected_updater")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        accepted_original = self._run("accept", "--archive", str(archive_one), "--receipt", str(receipt))
        self.assertEqual(accepted_original.returncode, 0)

        # A hand-corrupted/foreign receipt claiming a payload_id this
        # archive never had -- no fake/manual receipt entry may satisfy
        # validation.
        tampered = json.loads(receipt.read_text())
        tampered["payload_id"] = "not-the-real-payload-id"
        receipt.write_text(json.dumps(tampered, sort_keys=True))
        result = self._run("accept", "--archive", str(archive_one), "--receipt", str(receipt))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stdout + result.stderr)

    def test_record_refuses_a_component_the_archive_never_declared_included(self):
        """A hand-typed --component that isn't in this archive's own
        included_components must be refused at record time -- never a
        route to a fabricated receipt entry for something the archive
        genuinely doesn't contain."""
        archive = self._build_archive(required=["protected_updater"])
        # piper is a real COMPONENTS name but this archive never declared it included.
        result = self._run(
            "record", "--archive", str(archive), "--receipt", str(self.tmpdir / "receipt.json"),
            "--component", "piper",
        )
        self.assertNotEqual(result.returncode, 0)


class RuntimeFoundationE7BStageModeSelectionTests(SimpleTestCase):
    """Real --plan executions of the rewritten 50/70/75 restore stages
    against a synthetic --archive, proving the archive-presence-driven
    mode selection actually runs (not just present in the source text)
    -- --plan is side-effect-free and needs no venv/DB, so this is cheap
    to exercise for real."""

    def setUp(self):
        self.tmpdir = Path(
            subprocess.run(["mktemp", "-d"], capture_output=True, text=True, check=True).stdout.strip()
        )
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text("IsadoraAir Git SHA: abc123\n")
        self.archive_path = self.tmpdir / "backup.tar.gz"
        with tarfile.open(self.archive_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")

    def _run(self, script_name, *extra_args):
        return subprocess.run(
            [str(RESTORE_DIR / script_name), "--plan", "--archive", str(self.archive_path), *extra_args],
            capture_output=True, text=True, timeout=15,
        )

    def test_all_runtime_recovery_stages_use_the_shared_safe_extractor(self):
        for script_name in ("50-native-deps.sh", "70-tts.sh", "75-protected-updater.sh"):
            with self.subTest(script=script_name):
                source = (RESTORE_DIR / script_name).read_text(encoding="utf-8")
                self.assertIn('restore_locate_recovery_payload "$PAYLOAD_DIR"', source)
                self.assertNotIn("runtime_recovery_archive.py", source)

    def test_native_deps_plan_with_archive_selects_the_recovery_payload_path(self):
        result = self._run("50-native-deps.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("provision_runtime_components --fdkaac --prepare-fdkaac --recovery-payload", result.stdout)
        self.assertNotIn("build_fdkaac.sh", result.stdout)

    def test_native_deps_plan_with_explicit_source_dir_keeps_the_legacy_path(self):
        source_dir = self.tmpdir / "native-src"
        source_dir.mkdir()
        result = self._run("50-native-deps.sh", "--source-dir", str(source_dir))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("build_fdkaac.sh", result.stdout)
        self.assertNotIn("--recovery-payload", result.stdout)

    def test_tts_plan_with_archive_selects_the_recovery_payload_path(self):
        result = self._run("70-tts.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("provision_runtime_components --recovery-payload", result.stdout)
        self.assertNotIn("pip install kokoro-onnx", result.stdout)

    def test_tts_plan_with_legacy_flag_keeps_the_old_path(self):
        result = self._run("70-tts.sh", "--legacy-connected-install")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pip install kokoro-onnx", result.stdout)
        self.assertNotIn("provision_runtime_components --recovery-payload", result.stdout)

    def test_tts_skip_flags_rejected_on_the_recovery_payload_path(self):
        result = self._run("70-tts.sh", "--skip-piper")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only apply to --legacy-connected-install", result.stdout + result.stderr)

    def test_protected_updater_plan_with_archive_targets_restore_phase_d_component(self):
        result = self._run("75-protected-updater.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_phase_d_component --recovery-payload", result.stdout)
        self.assertIn("--publish-root /", result.stdout)

    def test_protected_updater_plan_with_staging_root_publishes_under_it_not_real_root(self):
        result = subprocess.run(
            [str(RESTORE_DIR / "75-protected-updater.sh"), "--plan", "--archive", str(self.archive_path),
             "--staging-root", str(self.tmpdir / "staging")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"--publish-root {self.tmpdir / 'staging'}", result.stdout)
        # Never the real filesystem root under --staging-root.
        self.assertNotIn("--publish-root /\n", result.stdout)

    def test_protected_updater_plan_with_no_archive_is_a_clean_noop(self):
        result = subprocess.run(
            [str(RESTORE_DIR / "75-protected-updater.sh"), "--plan"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to restore", result.stdout)
        self.assertNotIn("restore_phase_d_component", result.stdout)

    def test_protected_updater_rejects_unrecognized_argument(self):
        result = self._run("75-protected-updater.sh", "--some-bogus-flag")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unrecognized argument", result.stdout + result.stderr)

    def test_protected_updater_never_references_github_or_network_fetch(self):
        """Offline-recovery requirement, proven directly against the
        stage script's own source: no git clone/fetch, no curl/wget, no
        pip/PyPI acquisition -- unlike 50/70's legacy connected-install
        branches, this component has no such branch to begin with."""
        source = (RESTORE_DIR / "75-protected-updater.sh").read_text()
        for forbidden in ("github.com", "git clone", "git fetch", "curl ", "wget ", "pip install"):
            self.assertNotIn(forbidden, source, f"stage 75 must never reference {forbidden!r}")


class RestoreManageSharedHelperTests(SimpleTestCase):
    """Runtime Foundation E7C (2026-09-04): lib.sh's restore_manage /
    restore_manage_command is the ONE shared mechanism stages 50/70/75/90
    use to run a manage.py command against a restored target. Real
    subprocess execution of lib.sh itself, against a fixture target whose
    own manage.py is a deliberately broken decoy that must never run --
    proving this checkout's own manage.py (never the target's) is what
    actually executes, using the restored target's venv Python."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-restore-manage-lib-test."))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.target = self.tmpdir / "target"
        venv_bin = self.target / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        # A wrapper (not a bare symlink) so argv[0] the interpreter itself
        # sees is the REAL venv's own python path -- CPython's venv/site-
        # packages detection keys off that path's own directory structure
        # (looking for a sibling pyvenv.cfg), which a symlink living
        # outside that real venv tree would not resolve correctly.
        (venv_bin / "python").write_text(
            f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
        )
        (venv_bin / "python").chmod(0o755)
        scratch = self.tmpdir / "scratch"
        for sub in ("library", "waveforms", "weather", "reports"):
            (scratch / sub).mkdir(parents=True)
        self.env_file = self.target / ".env"
        self.env_file.write_text(
            "DEBUG=True\n"
            "DB_USER=isadoraair\n"
            "DB_PASSWORD=unused-check-does-not-connect\n"
            f"LIBRARY_ROOT={scratch / 'library'}\n"
            f"WAVEFORMS_DIR={scratch / 'waveforms'}\n"
            f"WEATHER_DATA_DIR={scratch / 'weather'}\n"
            f"REPORTS_ROOT={scratch / 'reports'}\n",
            encoding="utf-8",
        )
        decoy = self.target / "manage.py"
        decoy.write_text(
            "import sys\nprint('DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN')\nsys.exit(97)\n",
            encoding="utf-8",
        )

    def _run(self, function_call: str):
        script = (
            'set -euo pipefail; '
            f'source "{RESTORE_DIR / "lib.sh"}"; '
            f'restore_parse_common_args --staging-root {self.tmpdir}/staging '
            f'--target-root {self.target} --db-name isadoraair_restore_test --apply >/dev/null 2>&1; '
            f'{function_call}'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    def test_restore_manage_runs_this_checkout_never_the_decoy(self):
        result = self._run("restore_manage check")
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 97)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_restore_manage_command_resolves_repo_root_to_this_checkout(self):
        """restore_manage_command only resolves the OUTER invocation (of
        restore_manage.py itself, which performs the compatibility probe
        and only THEN execs the real manage.py) -- the --repo-root it
        passes is what ultimately determines which manage.py runs, so
        that is what must point at this checkout, never the target."""
        result = self._run(
            'restore_manage_command check; printf "ARGV:%s\\n" "${RESTORE_MANAGE_CMD[@]}"'
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"ARGV:{RESTORE_DIR / 'restore_manage.py'}", result.stdout)
        self.assertIn("ARGV:--repo-root\nARGV:" + str(REPO_ROOT), result.stdout)
        self.assertIn("ARGV:--target-root\nARGV:" + str(self.target), result.stdout)
        self.assertNotIn(str(self.target / "manage.py"), result.stdout)

    def test_restore_manage_fails_closed_without_venv(self):
        shutil.rmtree(self.target / "venv")
        result = self._run("restore_manage check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("venv", result.stdout + result.stderr)
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout + result.stderr)

    def test_restore_manage_fails_closed_without_env(self):
        self.env_file.unlink()
        result = self._run("restore_manage check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env", result.stdout + result.stderr)
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout + result.stderr)

    def test_restore_manage_fails_closed_on_incompatible_venv(self):
        """A venv whose installed packages don't satisfy this checkout's
        own requirements.txt must never silently fall back to the
        restored target's own manage.py. Simulated cheaply: -I -S makes
        the same real interpreter run with no site-packages at all, so
        every pinned dependency (Django included) is "not installed" from
        its point of view -- without needing to build a second real venv."""
        stub = self.target / "venv" / "bin" / "python"
        stub.unlink()
        stub.write_text(f'#!/usr/bin/env bash\nexec {sys.executable} -I -S "$@"\n', encoding="utf-8")
        stub.chmod(0o755)
        result = self._run("restore_manage check")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("DECOY-MANAGE-RAN-THIS-MUST-NEVER-HAPPEN", result.stdout + result.stderr)


class RestoreManageStageWiringTests(SimpleTestCase):
    """Static source-identity checks: every stage that provisions/repairs
    runtime state via a manage.py command must go through the shared
    restore_manage/restore_manage_command helper -- never hand-roll
    "$RESTORE_TARGET_ROOT/venv/bin/python" "$RESTORE_TARGET_ROOT/manage.py"
    again, which is exactly the architectural defect this fix closes.
    A regression back to that literal pattern in any of these four
    stages must fail this test."""

    OLD_PATTERN_FRAGMENTS = (
        '"$VENV_PYTHON" "$RESTORE_TARGET_ROOT/manage.py"',
        '"$VENV_PYTHON" manage.py',
        '"$VENV_PY" manage.py',
    )

    def test_stages_50_70_75_90_use_the_shared_helper(self):
        for script_name in ("50-native-deps.sh", "70-tts.sh", "75-protected-updater.sh", "90-system-config.sh"):
            with self.subTest(script=script_name):
                text = (RESTORE_DIR / script_name).read_text(encoding="utf-8")
                self.assertTrue(
                    "restore_manage " in text or "restore_manage_command " in text or "restore_manage\n" in text,
                    f"{script_name} does not appear to call restore_manage/restore_manage_command",
                )
                for fragment in self.OLD_PATTERN_FRAGMENTS:
                    self.assertNotIn(
                        fragment, text,
                        f"{script_name} still contains the old direct-delegation pattern {fragment!r}",
                    )

    def test_stage_95_deliberately_still_binds_to_the_restored_target(self):
        """The one intentional exception: 95-validate.sh proves the
        RESTORED application (its own migrations/checks/runtime contract,
        exactly as installed) is internally self-consistent -- that is
        only meaningful using the restored target's own manage.py, never
        this checkout's. See that script's own header -- which now
        explains this in prose (mentioning restore_manage BY NAME to
        contrast with it), so the check below only needs to prove
        restore_manage is never actually CALLED from executable code,
        not that the string never appears anywhere in the file."""
        text = (RESTORE_DIR / "95-validate.sh").read_text(encoding="utf-8")
        self.assertIn('"$VENV_PY" manage.py check', text)
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn(
                "restore_manage", line,
                f"95-validate.sh:{lineno} actually invokes restore_manage: {line!r}",
            )

    def test_stage_80_never_actually_executes_manage_py(self):
        """80-companions.sh only mentions manage.py in a documentation
        comment (why weather-ingest must run after IsadoraAir) -- it must
        never itself invoke it."""
        text = (RESTORE_DIR / "80-companions.sh").read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            self.assertNotIn("manage.py", line)


class RuntimeRecoveryArchiveClassificationTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-archive-class-test-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.helper = RESTORE_DIR / "runtime_recovery_archive.py"

    def _write(self, status):
        output = self.tmpdir / f"metadata-{len(tuple(self.tmpdir.iterdir()))}.json"
        result = subprocess.run(
            [
                str(self.helper),
                "write-metadata",
                "--status-json",
                json.dumps(status),
                "--script-version",
                "3.0.0",
                "--output",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(output.read_text())

    def test_policy_satisfying_payload_is_unambiguously_self_contained_v3(self):
        metadata = self._write(
            {
                "schema_version": 1,
                "payload_id": "p1",
                "product_contract_sha256": "0" * 64,
                "components": {"native_fdkaac": {"state": "present"}},
                "tts_components": ["kokoro"],
                "piper_freshness": {"state": "not_checked"},
                "policy": {"required": ["kokoro"], "missing": [], "satisfied": True},
            }
        )
        self.assertEqual(metadata["archive_format_version"], "3.0.0")
        self.assertEqual(metadata["recovery_class"], "self_contained_v3")

    def test_no_payload_and_empty_policy_payload_remain_legacy_class(self):
        absent = self._write({"pointer_configured": False})
        self.assertEqual(absent["archive_format_version"], "2.1.0")
        self.assertEqual(absent["recovery_class"], "legacy_non_self_contained")

        optional = self._write(
            {
                "schema_version": 1,
                "payload_id": "p2",
                "product_contract_sha256": "0" * 64,
                "components": {"native_fdkaac": {"state": "present"}},
                "tts_components": [],
                "piper_freshness": {"state": "not_checked"},
                "policy": {"required": [], "missing": [], "satisfied": True},
            }
        )
        self.assertEqual(optional["archive_format_version"], "2.1.0")
        self.assertEqual(optional["recovery_class"], "legacy_non_self_contained")


class RuntimeFoundationE5TmpfilesMappingTests(SimpleTestCase):
    """Runtime Foundation E6 -- MUST CLOSE: 90-system-config.sh must
    install deploy/isadoraair-runtime-tmpfiles.conf at its own correct
    destination, distinct from the pre-existing isadoraair.conf, never
    at the generic loop's default systemd/system/<basename> path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.text = (RESTORE_DIR / "90-system-config.sh").read_text(encoding="utf-8")

    def test_runtime_tmpfiles_conf_excluded_from_the_generic_unit_loop(self):
        start = self.text.index('for f in "$REPO_ROOT"/deploy/*.service')
        end = self.text.index("\ndone", start)
        loop_body = self.text[start:end]
        self.assertIn("isadoraair-runtime-tmpfiles.conf", loop_body)
        # It must appear only inside the exclusion case, never as a
        # dest-mapping case that would route it through render()'s
        # @@ISA_USER@@-only substitution vocabulary.
        exclusion_line = next(
            line for line in loop_body.splitlines() if "continue ;;" in line and "isadoraair-runtime-tmpfiles.conf" in line
        )
        self.assertIn("isadoraair-runtime-tmpfiles.conf", exclusion_line)

    def test_isadoraair_conf_still_maps_to_its_own_pre_existing_destination(self):
        """Preserve the existing isadoraair-tmpfiles.conf -> isadoraair.conf
        mapping unchanged -- the two tmpfiles authorities must never merge."""
        self.assertIn('isadoraair-tmpfiles.conf) dest="$ETC_ROOT/tmpfiles.d/isadoraair.conf" ;;', self.text)

    def test_dedicated_e5_step_installs_at_the_correct_destination(self):
        self.assertIn('E5_TMPFILES_DEST="$ETC_ROOT/tmpfiles.d/isadoraair-runtime.conf"', self.text)

    def test_e5_step_prefers_the_management_command_and_falls_back_to_a_minimal_render(self):
        self.assertIn("provision_runtime_components --surfaces", self.text)
        self.assertIn("ISADORAAIR_SURFACE_UID", self.text)
        self.assertIn("ISADORAAIR_SURFACE_GID", self.text)

    def test_e5_target_root_mirrors_staging_vs_real_host_exactly_like_etc_root(self):
        start = self.text.index("if [ -n \"$RESTORE_STAGING_ROOT\" ]; then\n  E5_TARGET_ROOT=")
        self.assertNotEqual(start, -1)

    def test_stage_95_passes_staging_root_to_structural_baseline(self):
        text = (RESTORE_DIR / "95-validate.sh").read_text(encoding="utf-8")
        self.assertIn('--target-root "$RESTORE_STAGING_ROOT"', text)
        self.assertIn("--structural-only", text)

    def test_stage_95_gates_archive_restore_on_component_receipt(self):
        text = (RESTORE_DIR / "95-validate.sh").read_text(encoding="utf-8")
        self.assertIn("restore_accept_recovery_receipt", text)
        self.assertIn("--accept-legacy-runtime-recovery", text)
        self.assertIn("Required components were not positively reconstructed", text)


class RuntimeFoundationE5SystemConfigFunctionalTests(SimpleTestCase):
    """Real subprocess execution of 90-system-config.sh against a
    disposable --staging-root -- never a real /etc, /opt, /usr/local,
    or /var/lib path. Mirrors InspectBackupFunctionalTests' own
    real-execution-against-synthetic-fixtures approach."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e6-90-config-")
        self.addCleanup(shutil.rmtree, temporary.name, ignore_errors=True)
        self.staging = Path(temporary.name)

    def _run(self, *args, timeout=60, env=None):
        return subprocess.run(
            [str(RESTORE_DIR / "90-system-config.sh"), "--staging-root", str(self.staging), *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )

    def test_plan_mode_writes_nothing_and_previews_both_tmpfiles_destinations(self):
        result = self._run("--plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.staging.exists() and any(self.staging.rglob("*")))
        self.assertIn("isadoraair.conf", result.stdout)
        self.assertIn("isadoraair-runtime.conf", result.stdout)

    def test_apply_without_venv_falls_back_to_minimal_runtime_tmpfiles_install(self):
        app_root = self.staging / "opt" / "isadoraair"
        app_root.mkdir(parents=True)
        (app_root / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")

        result = self._run("--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        isadoraair_conf = self.staging / "etc" / "tmpfiles.d" / "isadoraair.conf"
        runtime_conf = self.staging / "etc" / "tmpfiles.d" / "isadoraair-runtime.conf"
        self.assertTrue(isadoraair_conf.is_file())
        self.assertTrue(runtime_conf.is_file())
        self.assertNotEqual(isadoraair_conf.read_text(), runtime_conf.read_text())

        runtime_text = runtime_conf.read_text(encoding="utf-8")
        self.assertNotIn("@@ISADORAAIR_SURFACE_UID@@", runtime_text)
        self.assertNotIn("@@ISADORAAIR_SURFACE_GID@@", runtime_text)
        self.assertIn("/opt/isadoraair-runtime", runtime_text)
        self.assertIn("/var/lib/isadoraair/tts", runtime_text)
        # Fallback path: file only -- no directories, no real tmpfiles
        # execution (that's E5's own authority, once the venv exists).
        self.assertFalse((self.staging / "opt" / "isadoraair-runtime").exists())

    def test_apply_with_real_venv_establishes_full_e5_contract_with_canonical_content(self):
        """The preferred path: a real venv + manage.py checkout is
        available at this stage, so 90-system-config.sh invokes
        Runtime Foundation E5's own RuntimeSystemSurfaceManager via
        provision_runtime_components --surfaces --apply. Proves BOTH
        the exact destination fix AND that the installed launcher
        embeds the canonical /opt/isadoraair -- never this staging
        root's own mount prefix (task section 17's regression).

        Runtime Foundation E7D: the preferred branch now also requires
        $ISA_ROOT/.env to exist (restore_manage needs it to relay
        config) -- a dedicated isolated app shell with its OWN .env
        inside the staging tree (never the real checkout's own root, and
        never a bare symlink to the whole project_root, which would put
        a live .env inside the actual repository)."""

        project_root = RESTORE_DIR.parent.parent
        venv_python = project_root / "venv" / "bin" / "python"
        if not venv_python.is_file():
            self.skipTest("no worktree-local venv symlink available for a real manage.py invocation")

        app_root = self.staging / "opt" / "isadoraair"
        app_root.mkdir(parents=True, exist_ok=True)
        (app_root / "manage.py").symlink_to(project_root / "manage.py")
        (app_root / "venv").symlink_to(project_root / "venv")
        scratch_weather = self.staging / "app-env-scratch" / "weather"
        scratch_weather.mkdir(parents=True, exist_ok=True)
        (app_root / ".env").write_text(
            "DEBUG=True\n"
            "DB_NAME=unused\nDB_USER=unused\nDB_PASSWORD=\nDB_HOST=127.0.0.1\nDB_PORT=65534\n"
            f"WEATHER_DATA_DIR={scratch_weather}\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("LIBRARY_ROOT", None)  # keep check_deploy_baseline-style canonical mapping intact
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        result = self._run("--apply", timeout=120, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("System-surface provisioning", result.stdout)
        self.assertIn("all surfaces healthy: True", result.stdout)

        launcher = self.staging / "usr" / "local" / "bin" / "isadoraair-tts"
        self.assertTrue(launcher.is_file())
        content = launcher.read_text(encoding="utf-8")
        self.assertIn('APPLICATION_ROOT_MARKER = "/opt/isadoraair"', content)
        self.assertNotIn(str(self.staging), content)

        runtime_conf = self.staging / "etc" / "tmpfiles.d" / "isadoraair-runtime.conf"
        self.assertTrue(runtime_conf.is_file())
        self.assertTrue((self.staging / "opt" / "isadoraair-runtime").is_dir())
        self.assertTrue((self.staging / "var" / "lib" / "isadoraair" / "tts").is_dir())


class RuntimeFoundationE6TargetValidationFunctionalTests(SimpleTestCase):
    """Exercise stages 90/95 only beneath a disposable target root.

    Runtime Foundation E7D (2026-09-04) regression: the previous version
    of this fixture pre-created BOTH the target's /etc/passwd AND
    /run/isadoraair/tts before ever running Stage 90 -- which completely
    masked the real E8 defect (Stage 90 never actually established that
    scratch surface itself, and an isolated staging target genuinely has
    no /etc/passwd of its own to resolve a named identity from). This
    fixture now creates NEITHER: it supplies a trusted --isa-uid/--isa-gid
    numeric pair instead (exactly what a real offline restore operator
    would use), and asserts Stage 90 itself establishes
    /run/isadoraair(/tts) with the correct numeric owner/group/modes,
    and that Stage 95 then resolves identity and PASSes without ever
    touching a staged passwd database."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e6-95-target-")
        self.addCleanup(temporary.cleanup)
        self.staging = Path(temporary.name)
        self.project_root = RESTORE_DIR.parent.parent
        if not (self.project_root / "venv" / "bin" / "python").is_file():
            self.skipTest("no worktree-local venv link available for restore functional proof")
        self.uid, self.gid = os.getuid(), os.getgid()
        self.env = os.environ.copy()
        self.env.update(
            {
                "DEBUG": "True",
                "SECRET_KEY": "e6-disposable-restore-only",
                "DB_NAME": "unused",
                "DB_USER": "unused",
                "DB_PASSWORD": "",
                "DB_HOST": "127.0.0.1",
                "DB_PORT": "65534",
                "PYTHONPATH": str(self.project_root),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        # Hermetic on purpose: this test's own outer runner may itself be
        # invoked with LIBRARY_ROOT (or similar) pointed at a scratch
        # directory for ITS OWN unrelated needs -- os.environ.copy() above
        # would otherwise leak that into these subprocess stage-script
        # calls, silently breaking check_deploy_baseline's canonical
        # /srv/isadoraair/music target-root mapping (which
        # _unrelated_structural_prerequisites below assumes). Popped, not
        # merely left unset, so an inherited override can never win.
        self.env.pop("LIBRARY_ROOT", None)

    def _application_shell(self, *, with_venv: bool) -> Path:
        app = self.staging / "opt" / "isadoraair"
        app.mkdir(parents=True, exist_ok=True)
        (app / "manage.py").symlink_to(self.project_root / "manage.py")
        if with_venv:
            (app / "venv").symlink_to(self.project_root / "venv")
            # restore_manage (lib.sh) -- the current-checkout recovery
            # authority Stage 90's preferred (E5) branch routes through --
            # needs a real .env to relay, and needs WEATHER_DATA_DIR
            # writable (weather/services.py mkdir's it at Django import
            # time); LIBRARY_ROOT is deliberately left at its own default
            # (/srv/isadoraair/music) so check_deploy_baseline's
            # structural mapping still matches what
            # _unrelated_structural_prerequisites below creates.
            scratch = self.staging / "app-env-scratch" / "weather"
            scratch.mkdir(parents=True, exist_ok=True)
            (app / ".env").write_text(
                f"DEBUG=True\nWEATHER_DATA_DIR={scratch}\n", encoding="utf-8"
            )
        return app

    def _unrelated_structural_prerequisites(self):
        """Only the music-library structural prerequisite
        check_deploy_baseline's own (identity-independent) checks need --
        never identity (/etc/passwd) and never the scratch surface this
        fix now proves Stage 90 establishes itself."""
        (self.staging / "srv" / "isadoraair" / "music").mkdir(parents=True)

    def _run_stage(self, name: str, *extra: str, with_identity: bool = True, timeout: int = 120):
        args = [str(RESTORE_DIR / name), "--staging-root", str(self.staging), "--apply"]
        if with_identity:
            args += ["--isa-user", "station", "--isa-uid", str(self.uid), "--isa-gid", str(self.gid)]
        args += list(extra)
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=self.env)

    def _assert_scratch_surface_established_by_stage_90(self):
        run_dir = self.staging / "run" / "isadoraair"
        tts_dir = run_dir / "tts"
        for directory, mode in ((run_dir, 0o755), (tts_dir, 0o700)):
            self.assertTrue(directory.is_dir(), f"{directory} was not created by Stage 90")
            self.assertFalse(directory.is_symlink())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)
            self.assertEqual(directory.stat().st_uid, self.uid)
            self.assertEqual(directory.stat().st_gid, self.gid)

    def test_preferred_stage_90_then_stage_95_targets_staging_and_detects_deleted_launcher(self):
        self._application_shell(with_venv=True)
        self._unrelated_structural_prerequisites()
        stage90 = self._run_stage("90-system-config.sh")
        self.assertEqual(stage90.returncode, 0, stage90.stdout + stage90.stderr)

        # Stage 90 itself established the legacy scratch surface -- not
        # pre-created by this fixture -- with the trusted numeric identity.
        self._assert_scratch_surface_established_by_stage_90()
        # And no staged passwd database was ever needed to do it.
        self.assertFalse((self.staging / "etc" / "passwd").exists())

        launcher = self.staging / "usr" / "local" / "bin" / "isadoraair-tts"
        self.assertTrue(launcher.is_file())
        self.assertIn(
            'APPLICATION_ROOT_MARKER = "/opt/isadoraair"',
            launcher.read_text(encoding="utf-8"),
        )
        self.assertTrue((self.staging / "etc/tmpfiles.d/isadoraair.conf").is_file())
        self.assertTrue((self.staging / "etc/tmpfiles.d/isadoraair-runtime.conf").is_file())

        healthy = self._run_stage("95-validate.sh")
        self.assertEqual(healthy.returncode, 0, healthy.stdout + healthy.stderr)
        self.assertIn("offline target check_deploy_baseline: PASS", healthy.stdout)
        self.assertFalse((self.staging / "etc" / "passwd").exists())

        launcher.unlink()
        missing = self._run_stage("95-validate.sh")
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)
        self.assertIn("offline target check_deploy_baseline: FAILED", missing.stderr)

    def test_stage_95_without_target_passwd_or_trusted_identity_fails_closed(self):
        """The exact real E8 failure this fix addresses, reproduced and
        pinned as a deliberate negative proof: with neither a target
        /etc/passwd entry NOR a trusted --isa-uid/--isa-gid pair, the TTS
        scratch surface identity is genuinely unresolvable -- Stage 95
        must fail closed, never guess."""
        self._application_shell(with_venv=True)
        self._unrelated_structural_prerequisites()
        stage90 = self._run_stage("90-system-config.sh")
        self.assertEqual(stage90.returncode, 0, stage90.stdout + stage90.stderr)

        unresolved = self._run_stage("95-validate.sh", with_identity=False)
        self.assertNotEqual(unresolved.returncode, 0, unresolved.stdout + unresolved.stderr)
        self.assertIn("offline target check_deploy_baseline: FAILED", unresolved.stderr)
        self.assertIn("unresolved_identity", unresolved.stdout)

    def test_stage_90_fallback_cannot_reach_final_success(self):
        app = self._application_shell(with_venv=False)
        self._unrelated_structural_prerequisites()
        fallback = self._run_stage("90-system-config.sh")
        self.assertEqual(fallback.returncode, 0, fallback.stdout + fallback.stderr)
        self.assertIn("falling back", fallback.stderr)
        self.assertFalse((self.staging / "usr/local/bin/isadoraair-tts").exists())
        # The fallback branch is E5-specific -- the legacy scratch surface
        # (section 4b) is a separate, unconditional step and is still
        # established regardless of which E5 branch ran.
        self._assert_scratch_surface_established_by_stage_90()

        (app / "venv").symlink_to(self.project_root / "venv")
        final = self._run_stage("95-validate.sh")
        self.assertNotEqual(final.returncode, 0, final.stdout + final.stderr)
        self.assertIn("offline target check_deploy_baseline: FAILED", final.stderr)


class RestoreStage90IdentityPairValidationTests(SimpleTestCase):
    """--isa-uid/--isa-gid must be all-or-nothing, for both stages that
    accept them -- cheap --plan-mode proof, no venv/DB required."""

    def setUp(self):
        super().setUp()
        self.staging = Path(tempfile.mkdtemp(prefix="isadoraair-e7d-pair-test-"))
        self.addCleanup(shutil.rmtree, self.staging, ignore_errors=True)

    def _run(self, script: str, *extra: str):
        return subprocess.run(
            [str(RESTORE_DIR / script), "--staging-root", str(self.staging), "--plan", *extra],
            capture_output=True, text=True, timeout=15,
        )

    def test_stage_90_uid_without_gid_fails(self):
        result = self._run("90-system-config.sh", "--isa-uid", "1000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be supplied together", result.stdout + result.stderr)

    def test_stage_90_gid_without_uid_fails(self):
        result = self._run("90-system-config.sh", "--isa-gid", "1000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be supplied together", result.stdout + result.stderr)

    def test_stage_95_uid_without_gid_fails(self):
        result = self._run("95-validate.sh", "--isa-uid", "1000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be supplied together", result.stdout + result.stderr)

    def test_stage_95_gid_without_uid_fails(self):
        result = self._run("95-validate.sh", "--isa-gid", "1000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("must be supplied together", result.stdout + result.stderr)

    def test_stage_90_non_numeric_uid_fails(self):
        result = self._run("90-system-config.sh", "--isa-uid", "notanumber", "--isa-gid", "1000")
        self.assertEqual(result.returncode, 2)
        self.assertIn("non-negative integer", result.stdout + result.stderr)


class RestoreStage90ScratchSurfaceSafetyTests(SimpleTestCase):
    """Fail-closed proof for the confined directory-establishment helper
    ensure_confined_directory (lib.sh), exercised through the real Stage
    90 --apply path -- no host /run is ever touched or at risk, since
    every target is confined beneath an isolated tmpdir staging root."""

    def setUp(self):
        super().setUp()
        self.staging = Path(tempfile.mkdtemp(prefix="isadoraair-e7d-scratch-safety-"))
        self.addCleanup(shutil.rmtree, self.staging, ignore_errors=True)
        self.uid, self.gid = os.getuid(), os.getgid()

    def _run_apply(self, *extra: str):
        return subprocess.run(
            [
                str(RESTORE_DIR / "90-system-config.sh"),
                "--staging-root", str(self.staging), "--apply",
                "--isa-uid", str(self.uid), "--isa-gid", str(self.gid),
                *extra,
            ],
            capture_output=True, text=True, timeout=60,
        )

    def test_symlinked_run_ancestor_fails_closed(self):
        outside = self.staging.parent / f"{self.staging.name}-outside-run"
        outside.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.staging / "run").symlink_to(outside, target_is_directory=True)
        result = self._run_apply()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("symlink", result.stdout + result.stderr)
        # Confined -- the real escape target was never touched.
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlinked_run_isadoraair_leaf_fails_closed(self):
        outside = self.staging.parent / f"{self.staging.name}-outside-tts"
        outside.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        (self.staging / "run").mkdir()
        (self.staging / "run" / "isadoraair").symlink_to(outside, target_is_directory=True)
        result = self._run_apply()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("symlink", result.stdout + result.stderr)

    def test_non_directory_collision_fails_closed(self):
        (self.staging / "run").mkdir()
        (self.staging / "run" / "isadoraair").write_text("not a directory", encoding="utf-8")
        result = self._run_apply()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("not a directory", result.stdout + result.stderr)

    def test_ownership_this_caller_cannot_establish_fails_closed_not_substituted(self):
        """An unprivileged staging run cannot chown to an arbitrary UID
        it isn't -- this must fail closed, never silently fall back to
        the caller's own (installer-host) identity instead."""
        result = self._run_apply("--isa-uid", "65001", "--isa-gid", "65001")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cannot set ownership", result.stdout + result.stderr)
        # The failure is on the FIRST directory (run/isadoraair) -- the
        # second (tts) is never even attempted, and the first is never
        # left silently owned by the caller's own uid as a substitute for
        # the requested-but-unachievable 65001.
        tts_dir = self.staging / "run" / "isadoraair" / "tts"
        self.assertFalse(tts_dir.exists())


# =======================================================================
# r0038 (E8 offline restore hardening): offline_snap_install.py,
# build_offline_closure.py, and 10-packages.sh's local-snap mode.
#
# The real Chromium/snap transition sequence needs actual root, a real
# snapd, and real apt/snap state -- none of which belongs in a unit test.
# Following Stage30SecretLoggingFunctionalTests's own established
# pattern, the functional tests below run the REAL 10-packages.sh via a
# fake PATH (sudo/systemctl/ss/dpkg/snap/apt-get/chromium-browser/
# chromedriver stand-ins that record every invocation to a log file and
# track a tiny bit of fake system state), so the actual bash control
# flow -- ordering, fail-closed behavior, DEBIAN_FRONTEND propagation,
# never falling back to an online snap/apt install -- is exercised for
# real, never asserted against by reading the script's source text alone.
# =======================================================================

offline_snap_install = _load_module(RESTORE_DIR / "offline_snap_install.py", "offline_snap_install_under_test")
build_offline_closure = _load_module(RESTORE_DIR / "build_offline_closure.py", "build_offline_closure_under_test")
TEN_PACKAGES_R38 = RESTORE_DIR / "10-packages.sh"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_snap_fixture(root: Path, entries):
    """entries: iterable of (name, revision, snap_bytes, assert_bytes).
    Writes the .snap/.assert files and returns the manifest 'snaps' list
    (SHA256 computed from the actual bytes written -- never asserted
    independently, so a test that wants a mismatch must tamper with the
    file or the manifest AFTER calling this)."""
    snaps = []
    for name, revision, snap_bytes, assert_bytes in entries:
        snap_path = root / f"{name}_{revision}.snap"
        assert_path = root / f"{name}_{revision}.assert"
        snap_path.write_bytes(snap_bytes)
        assert_path.write_bytes(assert_bytes)
        snaps.append({
            "name": name, "revision": revision,
            "snap_file": snap_path.name, "snap_sha256": _sha256_bytes(snap_bytes),
            "assert_file": assert_path.name, "assert_sha256": _sha256_bytes(assert_bytes),
        })
    return snaps


def _write_fakebin(fakebin_dir: Path, scripts: dict):
    fakebin_dir.mkdir(parents=True, exist_ok=True)
    for name, content in scripts.items():
        path = fakebin_dir / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)


class OfflineSnapInstallHelperTests(SimpleTestCase):
    """Pure-Python unit tests of offline_snap_install.py's manifest
    verification/ordering -- the fail-closed logic Stage 10's local-snap
    mode depends on to never proceed on an incomplete, mismatched, or
    tampered closure. See that module's own docstring for the full
    contract exercised here."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-offline-snap-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _manifest(self, snaps, install_order):
        (self.tmpdir / "snap-manifest.json").write_text(
            json.dumps({"schema_version": 1, "snaps": snaps, "install_order": install_order}),
            encoding="utf-8",
        )

    def _complete_closure(self):
        snaps = _write_snap_fixture(self.tmpdir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
            ("core22", "2437", b"core22-bytes", b"core22-assert-bytes"),
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        self._manifest(snaps, ["snapd", "core22", "chromium"])
        return snaps

    def test_complete_closure_produces_ordered_plan(self):
        self._complete_closure()
        plan = offline_snap_install.build_plan(self.tmpdir)
        self.assertEqual([e.name for e in plan], ["snapd", "core22", "chromium"])
        self.assertEqual(plan[0].revision, "27710")

    def test_missing_manifest_fails_closed(self):
        with self.assertRaises(offline_snap_install.ManifestError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_missing_snapd_fails_closed_with_required_snap_error(self):
        snaps = _write_snap_fixture(self.tmpdir, [
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        self._manifest(snaps, ["chromium"])
        with self.assertRaises(offline_snap_install.RequiredSnapMissingError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_missing_chromium_fails_closed_with_required_snap_error(self):
        snaps = _write_snap_fixture(self.tmpdir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
        ])
        self._manifest(snaps, ["snapd"])
        with self.assertRaises(offline_snap_install.RequiredSnapMissingError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_sha256_mismatch_fails_closed(self):
        self._complete_closure()
        # Tamper with the chromium .snap file's contents AFTER the
        # manifest was written from the original bytes' real SHA256.
        (self.tmpdir / "chromium_3520.snap").write_bytes(b"tampered-bytes")
        with self.assertRaises(offline_snap_install.HashMismatchError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_missing_referenced_file_fails_closed(self):
        self._complete_closure()
        (self.tmpdir / "chromium_3520.snap").unlink()
        with self.assertRaises(offline_snap_install.MissingFileError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_chromium_before_snapd_in_install_order_fails_closed(self):
        snaps = _write_snap_fixture(self.tmpdir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        self._manifest(snaps, ["chromium", "snapd"])
        with self.assertRaises(offline_snap_install.InstallOrderError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_install_order_not_a_permutation_fails_closed(self):
        snaps = _write_snap_fixture(self.tmpdir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        self._manifest(snaps, ["snapd"])  # chromium missing from install_order
        with self.assertRaises(offline_snap_install.InstallOrderError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_path_traversal_in_manifest_filename_rejected(self):
        self._complete_closure()
        data = json.loads((self.tmpdir / "snap-manifest.json").read_text())
        data["snaps"][0]["snap_file"] = "../evil.snap"
        (self.tmpdir / "snap-manifest.json").write_text(json.dumps(data))
        with self.assertRaises(offline_snap_install.ManifestError):
            offline_snap_install.build_plan(self.tmpdir)

    def test_cli_plan_prints_tsv_and_exits_zero_on_success(self):
        self._complete_closure()
        result = subprocess.run(
            [sys.executable, str(RESTORE_DIR / "offline_snap_install.py"), "plan", "--snap-dir", str(self.tmpdir)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = [line for line in result.stdout.splitlines() if line]
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("SNAP\tsnapd\t"))
        self.assertTrue(lines[-1].startswith("SNAP\tchromium\t"))

    def test_cli_plan_fails_with_no_stdout_on_bad_closure(self):
        """Fail-closed means no partial/misleading stdout a careless
        caller might consume despite the nonzero exit."""
        result = subprocess.run(
            [sys.executable, str(RESTORE_DIR / "offline_snap_install.py"), "plan", "--snap-dir", str(self.tmpdir)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 2)  # manifest missing entirely
        self.assertEqual(result.stdout, "")
        self.assertIn("snap manifest not found", result.stderr)


class BuildOfflineClosureTests(SimpleTestCase):
    """Pure-Python unit tests of build_offline_closure.py against a fake
    subprocess runner -- never touches real apt/snap/the network. Directly
    regression-tests the two root causes the E8 acceptance run found: an
    already-installed DIRECT package (`age`) silently missing from the
    apt closure, and a TRANSITIVE dependency (`bubblewrap`) missing
    entirely."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-closure-builder-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def test_parse_packages_file_matches_the_real_manifest(self):
        groups = build_offline_closure.parse_packages_file(REPO_ROOT / "deploy" / "packages-ubuntu-26.04.txt")
        self.assertIn("age", groups["OPTIONAL_BACKUP_ENCRYPTION"])
        self.assertIn("chromium-browser", groups["OPTIONAL_SYNDICATED_SELENIUM"])
        self.assertIn("python3", groups["CORE"])

    def test_resolve_direct_packages_rejects_unknown_group(self):
        groups = {"CORE": ["python3"]}
        with self.assertRaises(build_offline_closure.ClosureBuildError):
            build_offline_closure.resolve_direct_packages(groups, ["NOT_A_GROUP"])

    def _fake_apt_runner(self, archives_dir: Path, direct_names, transitive_names):
        """A fake apt-get/dpkg-deb/dpkg-scanpackages runner covering
        exactly the age (already-installed DIRECT) and bubblewrap
        (TRANSITIVE, never named in packages-ubuntu-26.04.txt) scenarios
        from the real E8 acceptance run -- generalized to whatever
        direct/transitive names the test passes in."""
        all_names = list(direct_names) + list(transitive_names)
        calls = []

        def runner(argv, **kwargs):
            calls.append((list(argv), kwargs))
            if "dpkg-scanpackages" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if "update" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if "-s" in argv:
                inst_lines = "".join(f"Inst {name} (1.0-1 Ubuntu:26.04 [amd64])\n" for name in transitive_names)
                return subprocess.CompletedProcess(argv, 0, stdout=inst_lines, stderr="")
            if "dpkg-deb" in argv:
                target = Path(argv[argv.index("-f") + 1])
                name = target.name.split("_")[0]
                out = f"Package: {name}\nVersion: 1.0-1\nArchitecture: amd64\n"
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            if "install" in argv and "--download-only" in argv:
                # The real download step: --reinstall means this always
                # (re-)populates the cache, even for an ALREADY-INSTALLED
                # direct package like `age` -- this fake mirrors that by
                # always (re-)writing every closure member's .deb here,
                # regardless of any prior on-disk state.
                for name in all_names:
                    (archives_dir / f"{name}_1.0-1_amd64.deb").write_bytes(f"{name}-bytes".encode())
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        return runner, calls

    def test_apt_closure_includes_already_installed_direct_package(self):
        """Regression test for the `age` omission: a direct package that
        is already installed on the source host must still be captured,
        because the real fix is `--reinstall` on the real download step,
        which never special-cases 'already installed'."""
        archives_dir = self.tmpdir / "archives"
        archives_dir.mkdir()
        runner, _ = self._fake_apt_runner(archives_dir, direct_names=["age"], transitive_names=[])
        result = build_offline_closure.build_apt_closure(
            ["age"], self.tmpdir / "out", runner=runner, archives_dir=archives_dir, sudo=False,
        )
        names = {e["name"]: e for e in result.entries}
        self.assertIn("age", names)
        self.assertTrue(names["age"]["direct"])

    def test_apt_closure_captures_transitive_dependency(self):
        """Regression test for the bubblewrap omission: a package NEVER
        named in packages-ubuntu-26.04.txt, resolved only via apt's own
        dependency graph, must still end up in the closure and manifest,
        marked non-direct."""
        archives_dir = self.tmpdir / "archives"
        archives_dir.mkdir()
        runner, _ = self._fake_apt_runner(archives_dir, direct_names=["glycin-loaders"], transitive_names=["bubblewrap"])
        result = build_offline_closure.build_apt_closure(
            ["glycin-loaders"], self.tmpdir / "out", runner=runner, archives_dir=archives_dir, sudo=False,
        )
        names = {e["name"]: e for e in result.entries}
        self.assertIn("bubblewrap", names)
        self.assertFalse(names["bubblewrap"]["direct"])

    def test_apt_closure_writes_manifest_and_direct_list_and_local_repo_index(self):
        archives_dir = self.tmpdir / "archives"
        archives_dir.mkdir()
        runner, calls = self._fake_apt_runner(archives_dir, direct_names=["age"], transitive_names=["bubblewrap"])
        result = build_offline_closure.build_apt_closure(
            ["age"], self.tmpdir / "out", runner=runner, archives_dir=archives_dir, sudo=False,
        )
        self.assertTrue(result.manifest_path.is_file())
        manifest = json.loads(result.manifest_path.read_text())
        manifest_names = {p["name"] for p in manifest["packages"]}
        self.assertEqual(manifest_names, {"age", "bubblewrap"})
        for pkg in manifest["packages"]:
            self.assertIn("sha256", pkg)
            self.assertIn("filename", pkg)
        self.assertEqual(result.direct_list_path.read_text().strip(), "age")
        self.assertTrue(any("dpkg-scanpackages" in c[0] for c in calls))
        self.assertTrue((result.apt_repo_dir / "Packages").is_file())

    def test_apt_closure_real_download_uses_reinstall(self):
        """The specific flag that fixes the `age` omission -- assert it's
        actually present on the real (non-simulate) download command."""
        archives_dir = self.tmpdir / "archives"
        archives_dir.mkdir()
        runner, calls = self._fake_apt_runner(archives_dir, direct_names=["age"], transitive_names=[])
        build_offline_closure.build_apt_closure(
            ["age"], self.tmpdir / "out", runner=runner, archives_dir=archives_dir, sudo=False,
        )
        download_calls = [c[0] for c in calls if "install" in c[0] and "--download-only" in c[0] and "-s" not in c[0]]
        self.assertEqual(len(download_calls), 1)
        self.assertIn("--reinstall", download_calls[0])

    def _fake_snap_runner(self, installed_revisions: dict, types: dict):
        def runner(argv, **kwargs):
            if argv[:2] == ["snap", "list"]:
                name = argv[2]
                if name in installed_revisions:
                    out = f"Name  Version  Rev  Tracking  Publisher  Notes\n{name}  1.0  {installed_revisions[name]}  latest  x  -\n"
                    return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no matching snaps")
            if argv[:2] == ["snap", "download"]:
                name = argv[2]
                cwd = Path(kwargs["cwd"])
                rev = argv[argv.index("--revision") + 1] if "--revision" in argv else "999"
                (cwd / f"{name}_{rev}.snap").write_bytes(f"{name}-snap".encode())
                (cwd / f"{name}_{rev}.assert").write_bytes(f"{name}-assert".encode())
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[:2] == ["snap", "info"]:
                name = argv[2]
                return subprocess.CompletedProcess(argv, 0, stdout=f"name: {name}\ntype: {types.get(name, 'app')}\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        return runner

    def test_snap_closure_includes_snapd_and_chromium_even_if_not_requested(self):
        runner = self._fake_snap_runner(installed_revisions={}, types={"snapd": "snapd"})
        result = build_offline_closure.build_snap_closure(["gtk-common-themes"], self.tmpdir / "out", runner=runner)
        names = {e["name"] for e in result.manifest["snaps"]}
        self.assertIn("snapd", names)
        self.assertIn("chromium", names)

    def test_snap_closure_prefers_actual_installed_revision(self):
        runner = self._fake_snap_runner(installed_revisions={"core22": "2437"}, types={"snapd": "snapd", "core22": "base"})
        result = build_offline_closure.build_snap_closure(["core22"], self.tmpdir / "out", runner=runner)
        entry = next(e for e in result.manifest["snaps"] if e["name"] == "core22")
        self.assertEqual(entry["revision"], "2437")

    def test_snap_closure_install_order_snapd_first_chromium_last(self):
        runner = self._fake_snap_runner(installed_revisions={}, types={"snapd": "snapd", "core22": "base"})
        result = build_offline_closure.build_snap_closure(["core22"], self.tmpdir / "out", runner=runner)
        order = result.manifest["install_order"]
        self.assertEqual(order[0], "snapd")
        self.assertEqual(order[-1], "chromium")

    def test_snap_closure_output_is_directly_consumable_by_offline_snap_install(self):
        """End-to-end: the manifest/files build_offline_closure.py writes
        must be exactly what offline_snap_install.py's fail-closed
        verification accepts -- no format drift between the two tools."""
        runner = self._fake_snap_runner(installed_revisions={}, types={"snapd": "snapd", "core22": "base"})
        result = build_offline_closure.build_snap_closure(["core22"], self.tmpdir / "out", runner=runner)
        plan = offline_snap_install.build_plan(result.snap_dir)
        self.assertEqual([e.name for e in plan], result.manifest["install_order"])


class OfflineClosureToolingSyntaxTests(SimpleTestCase):
    def test_offline_snap_install_compiles(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(RESTORE_DIR / "offline_snap_install.py")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_offline_closure_compiles(self):
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(RESTORE_DIR / "build_offline_closure.py")],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_snap_install_is_executable(self):
        self.assertTrue((RESTORE_DIR / "offline_snap_install.py").stat().st_mode & 0o111)

    def test_build_offline_closure_is_executable(self):
        self.assertTrue((RESTORE_DIR / "build_offline_closure.py").stat().st_mode & 0o111)

    def test_10_packages_documents_the_new_flags(self):
        text = TEN_PACKAGES_R38.read_text(encoding="utf-8")
        self.assertIn("--snap-dir", text)
        self.assertIn("--apt-repo-dir", text)


class Stage10LocalSnapModeFunctionalTests(SimpleTestCase):
    """Real subprocess execution of 10-packages.sh --apply in local-snap
    mode, against a fake PATH -- never real sudo/systemctl/dpkg/snap/apt,
    never a real network or Snap Store. Exercises the actual bash control
    flow proven by hand during the E8 acceptance run: snap install order,
    the noninteractive Chromium transition-package sequence, and snapd
    being stopped/restarted around it."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-stage10-localsnap-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fakebin = self.tmpdir / "fakebin"
        self.state_dir = self.tmpdir / "state"
        self.state_dir.mkdir()
        self.calls_log = self.tmpdir / "calls.log"
        self.calls_log.write_text("", encoding="utf-8")
        (self.state_dir / "snapd-state").write_text("active", encoding="utf-8")
        self.fake_socket = self.tmpdir / "fake-snapd.socket"
        self.fake_socket.write_text("", encoding="utf-8")

        self.snap_dir = self.tmpdir / "snapdir"
        self.snap_dir.mkdir()
        snaps = _write_snap_fixture(self.snap_dir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
            ("core22", "2437", b"core22-bytes", b"core22-assert-bytes"),
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        (self.snap_dir / "snap-manifest.json").write_text(
            json.dumps({"schema_version": 1, "snaps": snaps, "install_order": ["snapd", "core22", "chromium"]}),
            encoding="utf-8",
        )

        _write_fakebin(self.fakebin, {
            "sudo": """#!/usr/bin/env bash
set -euo pipefail
while [[ "${1:-}" =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]]; do export "$1"; shift; done
exec "$@"
""",
            "dpkg": """#!/usr/bin/env bash
set -euo pipefail
echo "dpkg $* | DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-<unset>}" >> "$FAKE_CALLS"
if [ "$1" = "-s" ]; then exit "${FAKE_DPKG_S_EXIT:-0}"; fi
if [ "$1" = "-i" ]; then exit "${FAKE_DPKG_I_EXIT:-0}"; fi
exit 0
""",
            "snap": """#!/usr/bin/env bash
set -euo pipefail
echo "snap $*" >> "$FAKE_CALLS"
case "$1" in
  list)
    name="$2"
    if [ -f "$FAKE_STATE_DIR/snap-installed-$name" ]; then
      rev=$(cat "$FAKE_STATE_DIR/snap-installed-$name")
      echo "Name  Version  Rev  Tracking  Publisher  Notes"
      echo "$name  1.0  $rev  latest  x  -"
    else
      echo "error: no matching snaps installed" >&2
      exit 1
    fi
    ;;
  ack) : ;;
  install)
    base=$(basename "$2")
    name="${base%_*}"
    rev="${base##*_}"; rev="${rev%.snap}"
    echo "$rev" > "$FAKE_STATE_DIR/snap-installed-$name"
    ;;
esac
""",
            "systemctl": """#!/usr/bin/env bash
set -euo pipefail
echo "systemctl $*" >> "$FAKE_CALLS"
case "$1" in
  is-active)
    if [ "$(cat "$FAKE_STATE_DIR/snapd-state" 2>/dev/null || echo active)" = "active" ]; then exit 0; else exit 3; fi
    ;;
  stop) echo "inactive" > "$FAKE_STATE_DIR/snapd-state" ;;
  start) echo "active" > "$FAKE_STATE_DIR/snapd-state" ;;
esac
""",
            "ss": """#!/usr/bin/env bash
echo "ss $*" >> "$FAKE_CALLS"
if [ -f "$FAKE_STATE_DIR/socket-listener" ]; then
  echo "u_str LISTEN 0 4096 ${*: -1} 1 * 0"
fi
exit 0
""",
            "apt-get": """#!/usr/bin/env bash
set -euo pipefail
echo "apt-get $*" >> "$FAKE_CALLS"
if [[ " $* " == *" download "* ]]; then
  : > chromium-browser_1snap1-0ubuntu4_amd64.deb
  : > chromium-chromedriver_1snap1-0ubuntu4_amd64.deb
fi
exit 0
""",
            "chromium-browser": """#!/usr/bin/env bash
echo "chromium-browser $*" >> "$FAKE_CALLS"
if [ "$1" = "--version" ]; then echo "Chromium 152.0.7977.64 snap"; exit 0; fi
if [ "$1" = "--headless" ]; then echo "<html><head></head><body>${FAKE_SMOKE_MARKER:-E8-CHROMIUM-OFFLINE-PASS}</body></html>"; exit 0; fi
exit 0
""",
            "chromedriver": """#!/usr/bin/env bash
echo "chromedriver $*" >> "$FAKE_CALLS"
echo "ChromeDriver 152.0.7977.64 (abcdef-refs/branch-heads/7977@{#1891})"
""",
        })

    def _env(self, **extra):
        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "FAKE_CALLS": str(self.calls_log),
            "FAKE_STATE_DIR": str(self.state_dir),
            "RESTORE_SNAPD_SOCKET_PATH": str(self.fake_socket),
        }
        env.update(extra)
        return env

    def _run(self, *extra_args, env_extra=None):
        return subprocess.run(
            [
                str(TEN_PACKAGES_R38), "--staging-root", str(self.tmpdir / "staging"), "--apply",
                "--with-syndicated-selenium", "--snap-dir", str(self.snap_dir),
                *extra_args,
            ],
            capture_output=True, text=True, timeout=60,
            env=self._env(**(env_extra or {})),
        )

    def _calls(self):
        return self.calls_log.read_text(encoding="utf-8").splitlines()

    def test_succeeds_and_reports_pass(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("10-packages: PASS", result.stdout)

    def test_snaps_ack_and_install_in_manifest_order(self):
        self._run()
        calls = self._calls()
        snap_calls = [c for c in calls if c.startswith("snap ")]
        expected_order = []
        for name in ("snapd", "core22", "chromium"):
            expected_order.append(f"snap list {name}")
            expected_order.append(f"snap ack {self.snap_dir}/{name}_")
            expected_order.append(f"snap install {self.snap_dir}/{name}_")
        # Every ack/install call references the exact local file path --
        # never a bare snap name (which would mean a Snap Store install).
        for name in ("snapd", "core22", "chromium"):
            ack = next(c for c in snap_calls if c.startswith(f"snap ack {self.snap_dir}/{name}_"))
            install = next(c for c in snap_calls if c.startswith(f"snap install {self.snap_dir}/{name}_"))
            self.assertIn(ack, snap_calls)
            self.assertIn(install, snap_calls)
        # snapd installs before chromium.
        self.assertLess(
            snap_calls.index(next(c for c in snap_calls if c.startswith(f"snap install {self.snap_dir}/snapd_"))),
            snap_calls.index(next(c for c in snap_calls if c.startswith(f"snap install {self.snap_dir}/chromium_"))),
        )

    def test_never_installs_chromium_via_bare_snap_store_or_apt_name(self):
        """The single most important local-snap-mode invariant: no
        fallback to the network under any circumstance."""
        self._run()
        calls = self._calls()
        self.assertNotIn("snap install chromium", calls)
        for c in calls:
            if c.startswith("apt-get") and "install" in c:
                self.assertNotIn("chromium-browser", c)
                self.assertNotIn("chromium-chromedriver", c)

    def test_chromium_snap_installed_before_wrapper_debs(self):
        self._run()
        calls = self._calls()
        chromium_install_idx = calls.index(next(c for c in calls if c.startswith(f"snap install {self.snap_dir}/chromium_")))
        dpkg_i_idx = calls.index(next(c for c in calls if c.startswith("dpkg -i")))
        self.assertLess(chromium_install_idx, dpkg_i_idx)

    def test_dpkg_transition_install_is_noninteractive(self):
        self._run()
        dpkg_i_line = next(c for c in self._calls() if c.startswith("dpkg -i"))
        self.assertIn("DEBIAN_FRONTEND=noninteractive", dpkg_i_line)

    def test_snapd_stopped_before_dpkg_and_restarted_after(self):
        self._run()
        calls = self._calls()
        stop_idx = calls.index("systemctl stop snapd.socket snapd.service")
        dpkg_i_idx = calls.index(next(c for c in calls if c.startswith("dpkg -i")))
        start_idx = calls.index("systemctl start snapd.socket snapd.service")
        self.assertLess(stop_idx, dpkg_i_idx)
        self.assertLess(dpkg_i_idx, start_idx)
        self.assertEqual((self.state_dir / "snapd-state").read_text(encoding="utf-8").strip(), "active")

    def test_stale_socket_removed_only_after_listener_check(self):
        self._run()
        calls = self._calls()
        ss_idx = next(i for i, c in enumerate(calls) if c.startswith("ss "))
        stop_idx = calls.index("systemctl stop snapd.socket snapd.service")
        self.assertLess(stop_idx, ss_idx)
        self.assertFalse(self.fake_socket.exists())

    def test_listener_present_aborts_without_removing_socket_but_restores_snapd(self):
        (self.state_dir / "socket-listener").write_text("", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to remove it", result.stdout + result.stderr)
        self.assertTrue(self.fake_socket.exists(), "a live listener's socket must never be removed")
        self.assertIn("systemctl start snapd.socket snapd.service", self._calls())
        self.assertEqual((self.state_dir / "snapd-state").read_text(encoding="utf-8").strip(), "active")

    def test_dpkg_failure_still_restores_snapd(self):
        result = self._run(env_extra={"FAKE_DPKG_I_EXIT": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("systemctl start snapd.socket snapd.service", self._calls())
        self.assertEqual((self.state_dir / "snapd-state").read_text(encoding="utf-8").strip(), "active")

    def test_version_mismatch_fails(self):
        chromedriver = self.fakebin / "chromedriver"
        chromedriver.write_text(
            "#!/usr/bin/env bash\necho 'ChromeDriver 99.0.1.2 (abc)'\n", encoding="utf-8",
        )
        chromedriver.chmod(0o755)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("do not match", result.stdout + result.stderr)

    def test_headless_smoke_test_marker_is_checked(self):
        result = self._run()
        self.assertIn("Headless Chromium smoke test: PASS", result.stdout)

    def test_already_installed_at_matching_revision_skips_reinstall(self):
        (self.state_dir / "snap-installed-snapd").write_text("27710", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self._calls()
        self.assertNotIn(f"snap install {self.snap_dir}/snapd_27710.snap", calls)
        self.assertIn("already installed at revision 27710 -- skipping", result.stdout)

    def test_installed_at_different_revision_fails_closed_not_silently_reinstalled(self):
        (self.state_dir / "snap-installed-snapd").write_text("99999", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatched revision", result.stdout + result.stderr)
        self.assertNotIn(f"snap install {self.snap_dir}/snapd_27710.snap", self._calls())


class Stage10IncompleteClosureFailsClosedFunctionalTests(SimpleTestCase):
    """An incomplete/tampered closure must be rejected BEFORE any
    privileged action -- not partway through, and never by falling back
    to an online install."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-stage10-incomplete-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fakebin = self.tmpdir / "fakebin"
        self.calls_log = self.tmpdir / "poison-calls.log"
        self.calls_log.write_text("", encoding="utf-8")
        poison = """#!/usr/bin/env bash
echo "POISON:{name} $*" >> "{calls}"
exit 99
"""
        _write_fakebin(self.fakebin, {
            name: poison.format(name=name, calls=self.calls_log)
            for name in ("sudo", "systemctl", "ss", "apt-get")
        })
        _write_fakebin(self.fakebin, {
            "dpkg": f"""#!/usr/bin/env bash
if [ "$1" = "-s" ]; then exit 1; fi
echo "POISON:dpkg $*" >> "{self.calls_log}"
exit 99
""",
            "snap": f"""#!/usr/bin/env bash
if [ "$1" = "list" ]; then echo "error: none" >&2; exit 1; fi
echo "POISON:snap $*" >> "{self.calls_log}"
exit 99
""",
        })
        self.snap_dir = self.tmpdir / "snapdir"
        self.snap_dir.mkdir()
        # Incomplete closure: snapd is missing entirely.
        snaps = _write_snap_fixture(self.snap_dir, [
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        (self.snap_dir / "snap-manifest.json").write_text(
            json.dumps({"schema_version": 1, "snaps": snaps, "install_order": ["chromium"]}),
            encoding="utf-8",
        )

    def test_incomplete_closure_fails_closed_before_any_privileged_action(self):
        result = subprocess.run(
            [
                str(TEN_PACKAGES_R38), "--staging-root", str(self.tmpdir / "staging"), "--apply",
                "--with-syndicated-selenium", "--snap-dir", str(self.snap_dir),
            ],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": f"{self.fakebin}:{os.environ['PATH']}"},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("offline snap closure verification FAILED", result.stdout + result.stderr)
        self.assertIn("No fallback to the Snap Store", result.stdout + result.stderr)
        self.assertEqual(self.calls_log.read_text(encoding="utf-8"), "", "no privileged/mutating command may run before closure verification")


class Stage10PlanModeSafetyFunctionalTests(SimpleTestCase):
    """--plan must stay non-destructive and non-root even with
    --with-syndicated-selenium --snap-dir given -- the header comment's
    own documented contract ('never touches apt or needs root')."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-stage10-plan-safety-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fakebin = self.tmpdir / "fakebin"
        self.calls_log = self.tmpdir / "poison-calls.log"
        self.calls_log.write_text("", encoding="utf-8")
        poison = """#!/usr/bin/env bash
echo "POISON:{name} $*" >> "{calls}"
exit 99
"""
        _write_fakebin(self.fakebin, {
            name: poison.format(name=name, calls=self.calls_log)
            for name in ("sudo", "systemctl", "ss", "apt-get")
        })
        _write_fakebin(self.fakebin, {
            "dpkg": f"""#!/usr/bin/env bash
if [ "$1" = "-s" ]; then exit 1; fi
echo "POISON:dpkg $*" >> "{self.calls_log}"
exit 99
""",
            "snap": f"""#!/usr/bin/env bash
if [ "$1" = "list" ]; then echo "error: none" >&2; exit 1; fi
echo "POISON:snap $*" >> "{self.calls_log}"
exit 99
""",
        })
        self.snap_dir = self.tmpdir / "snapdir"
        self.snap_dir.mkdir()
        snaps = _write_snap_fixture(self.snap_dir, [
            ("snapd", "27710", b"snapd-bytes", b"snapd-assert-bytes"),
            ("chromium", "3520", b"chromium-bytes", b"chromium-assert-bytes"),
        ])
        (self.snap_dir / "snap-manifest.json").write_text(
            json.dumps({"schema_version": 1, "snaps": snaps, "install_order": ["snapd", "chromium"]}),
            encoding="utf-8",
        )

    def _env(self):
        return {**os.environ, "PATH": f"{self.fakebin}:{os.environ['PATH']}"}

    def test_plan_mode_with_local_snap_is_non_destructive_and_non_root(self):
        result = subprocess.run(
            [str(TEN_PACKAGES_R38), "--plan", "--with-syndicated-selenium", "--snap-dir", str(self.snap_dir)],
            capture_output=True, text=True, timeout=30, env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_log.read_text(encoding="utf-8"), "")
        self.assertIn("[PLAN]", result.stdout)

    def test_default_plan_mode_also_never_touches_apt_or_root(self):
        result = subprocess.run(
            [str(TEN_PACKAGES_R38), "--plan"],
            capture_output=True, text=True, timeout=30, env=self._env(),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.calls_log.read_text(encoding="utf-8"), "")


class Stage10DefaultBehaviorUnchangedFunctionalTests(SimpleTestCase):
    """Selenium selected WITHOUT --snap-dir must behave exactly as
    before r0038 -- chromium-browser/chromium-chromedriver go through
    the ordinary apt install list, and none of the new snap/systemctl/ss
    machinery is touched at all."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-stage10-default-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fakebin = self.tmpdir / "fakebin"
        self.calls_log = self.tmpdir / "calls.log"
        self.calls_log.write_text("", encoding="utf-8")
        _write_fakebin(self.fakebin, {
            "sudo": """#!/usr/bin/env bash
set -euo pipefail
exec "$@"
""",
            "dpkg": f"""#!/usr/bin/env bash
echo "dpkg $*" >> "{self.calls_log}"
if [ "$1" = "-s" ]; then exit 1; fi
exit 0
""",
            "apt-get": f"""#!/usr/bin/env bash
echo "apt-get $*" >> "{self.calls_log}"
exit 0
""",
        })
        for poisoned in ("systemctl", "ss", "snap"):
            path = self.fakebin / poisoned
            path.write_text(f"""#!/usr/bin/env bash
echo "POISON:{poisoned} $*" >> "{self.calls_log}"
exit 99
""", encoding="utf-8")
            path.chmod(0o755)

    def test_selenium_without_snap_dir_uses_ordinary_apt_install(self):
        result = subprocess.run(
            [str(TEN_PACKAGES_R38), "--staging-root", str(self.tmpdir / "staging"), "--apply", "--with-syndicated-selenium"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "PATH": f"{self.fakebin}:{os.environ['PATH']}"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.calls_log.read_text(encoding="utf-8").splitlines()
        install_call = next(c for c in calls if c.startswith("apt-get") and "install" in c)
        self.assertIn("chromium-browser", install_call)
        self.assertIn("chromium-chromedriver", install_call)
        self.assertFalse(any(c.startswith("POISON:") for c in calls), "local-snap-only machinery must not run")
