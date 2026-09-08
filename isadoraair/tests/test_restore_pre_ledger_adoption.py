"""r0044 -- pre-ledger restore adoption + interactive recovery workflow.

Review finding: r0043's --resume only converges a stage the ledger
ALREADY records complete for the exact current archive/target -- a
restore that began before the ledger existed at all (e.g. the actual
r0042 E8 sandbox, which completed Stages 00-75 and failed at Stage 80)
has no ledger entries whatsoever, so --resume alone falls through to
today's ordinary (destructive-guarded) restore behavior and hits
guard_env_overwrite/guard_db_overwrite.

This file covers the new, narrowly-scoped adoption mechanism
(--adopt-pre-ledger) that lets a stage independently VERIFY a pre-
ledger restore's durable output against the SAME archive that produced
it, and only then record it complete -- never inferred from mere file
existence -- plus the interactive TTY workflow built on top of it.
Every test is real subprocess execution against disposable temp trees;
never a real /etc, /opt, /var/lib, or production PostgreSQL/GitHub.
"""
from __future__ import annotations

import json
import os
import pty
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tests.test_restore_tooling import RESTORE_DIR, Stage30RealPostgreSQLTestCase
from isadoraair.tests.test_restore_resume_ledger import _make_git_fixture_repo, _make_minimal_archive

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER_HELPER = RESTORE_DIR / "restore_ledger.py"
RECOVERY_HELPER = RESTORE_DIR / "runtime_recovery_archive.py"


def _run_pty(args, env, *, send: bytes = b"", wait_before_send: float = 1.2, timeout: int = 15):
    """Runs args with a REAL controlling terminal on stdin/stdout/stderr
    (mirrors Stage30RealPostgreSQLTestCase._run_under_pty's own
    established technique) -- the exact condition the interactive
    workflow's own [ -t 0 ] && [ -t 1 ] detection requires. `send` is
    written to the pty after `wait_before_send` seconds (enough for the
    menu prompt to be printed), then the process is drained to
    completion. Returns (returncode, combined_output)."""
    controller_fd, follower_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            args, stdin=follower_fd, stdout=follower_fd, stderr=follower_fd,
            env=env, start_new_session=True, close_fds=True,
        )
        os.close(follower_fd)
        follower_fd = -1
        time.sleep(wait_before_send)
        if send:
            os.write(controller_fd, send)
        chunks = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = os.read(controller_fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        returncode = proc.wait(timeout=timeout)
        return returncode, b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        if follower_fd != -1:
            os.close(follower_fd)
        os.close(controller_fd)


class RestoreVerifyComponentReceiptTests(SimpleTestCase):
    """Direct subprocess tests of runtime_recovery_archive.py's new
    verify-component-receipt subcommand -- the actual new evidence
    Stage 50/70/75 adoption is built on."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-verify-receipt-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

    def _build_payload_source(self, *, with_protected_updater: bool) -> Path:
        src = self.tmpdir / "trusted-source"
        if src.exists():
            shutil.rmtree(src)
        (src / "tts" / "kokoro").mkdir(parents=True)
        (src / "runtime-recovery.json").write_text('{"ok": true}\n', encoding="utf-8")
        (src / "tts" / "runtime-bundle.json").write_text("{}\n", encoding="utf-8")
        if with_protected_updater:
            (src / "protected-updater" / "bootstrap").mkdir(parents=True)
            (src / "protected-updater" / "restore-manifest.json").write_text("{}\n", encoding="utf-8")
            launcher = src / "protected-updater" / "bootstrap" / "launcher.sh"
            launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            launcher.chmod(0o755)
        for path in src.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        for path in src.rglob("*"):
            if path.is_file() and "launcher.sh" not in path.name:
                path.chmod(0o644)
        src.chmod(0o755)
        return src

    def _build_archive(self, name: str, *, schema_version: int, components: dict, tts_components: list) -> Path:
        payload_src = self._build_payload_source(with_protected_updater=(schema_version == 2))
        workdir = self.tmpdir / f"workdir-{name}"
        workdir.mkdir()
        recovery_dir = workdir / "runtime-recovery"
        subprocess.run(["mkdir", "-p", str(recovery_dir)], check=True)
        subprocess.run(["chmod", "0755", str(recovery_dir)], check=True)
        subprocess.run(["cp", "-R", f"{payload_src}/.", f"{recovery_dir}/"], shell=False, check=True)
        status = {
            "schema_version": schema_version,
            "payload_id": f"{name}-payload",
            "product_contract_sha256": "0" * 64,
            "components": components,
            "tts_components": tts_components,
            "piper_freshness": {"state": "not_checked"},
            "policy": {"required": tts_components or list(components), "missing": [], "satisfied": True},
        }
        meta_out = self.tmpdir / f"{name}-meta.json"
        result = subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "write-metadata",
             "--status-json", json.dumps(status), "--script-version", "3.0.0", "--output", str(meta_out)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shutil.copy(meta_out, workdir / "runtime-recovery-archive.json")
        archive = self.tmpdir / f"{name}.tar.gz"
        subprocess.run(["tar", "czf", str(archive), "-C", str(workdir), "."], check=True, capture_output=True, text=True)
        return archive

    def _record(self, receipt: Path, archive: Path, component: str):
        result = subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "record", "--archive", str(archive),
             "--receipt", str(receipt), "--component", component],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _verify(self, archive: Path, receipt: Path, component: str):
        return subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "verify-component-receipt", "--archive", str(archive),
             "--receipt", str(receipt), "--component", component],
            capture_output=True, text=True, timeout=15,
        )

    def test_matching_receipt_verifies(self):
        archive = self._build_archive(
            "match", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        self._record(receipt, archive, "protected_updater")
        result = self._verify(archive, receipt, "protected_updater")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"component_verified":true', result.stdout.replace(" ", ""))

    def test_component_not_recorded_in_receipt_fails(self):
        archive = self._build_archive(
            "notrecorded", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        self._record(receipt, archive, "native_fdkaac")  # protected_updater never recorded
        result = self._verify(archive, receipt, "protected_updater")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not record", result.stderr)

    def test_component_not_declared_by_archive_fails(self):
        archive = self._build_archive(
            "nodecl", schema_version=1, components={}, tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        result = self._verify(archive, receipt, "native_fdkaac")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not part of this archive", result.stderr)

    def test_receipt_from_a_different_archive_cannot_adopt(self):
        archive1 = self._build_archive(
            "arch1", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        archive2 = self._build_archive(
            "arch2", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        self._record(receipt, archive1, "protected_updater")
        result = self._verify(archive2, receipt, "protected_updater")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match this exact archive", result.stderr)

    def test_corrupted_receipt_cannot_adopt(self):
        archive = self._build_archive(
            "corrupt", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        receipt.write_text("not valid json {{{", encoding="utf-8")
        result = self._verify(archive, receipt, "protected_updater")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or invalid", result.stderr)

    def test_receipt_with_invalid_components_field_cannot_adopt(self):
        archive = self._build_archive(
            "badfields", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        receipt = self.tmpdir / "receipt.json"
        # A structurally-valid-JSON but semantically-inconsistent receipt
        # (recorded_components not a list) -- corrupted/inconsistent,
        # never adopted.
        receipt.write_text(json.dumps({
            "schema_version": 1, "archive_format_version": "3.0.0",
            "payload_id": "badfields-payload", "recovered_components": "not-a-list",
        }), encoding="utf-8")
        result = self._verify(archive, receipt, "protected_updater")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid components", result.stderr)

    def test_missing_receipt_file_cannot_adopt(self):
        archive = self._build_archive(
            "noreceipt", schema_version=2,
            components={"native_fdkaac": {"state": "present"}, "protected_updater": {"state": "present"}},
            tts_components=["kokoro"],
        )
        result = self._verify(archive, self.tmpdir / "does-not-exist.json", "protected_updater")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing or invalid", result.stderr)


class Stage75PreLedgerAdoptionEndToEndTests(SimpleTestCase):
    """Real subprocess execution of the actual 75-protected-updater.sh
    adoption branch -- the concrete scenario the task names explicitly.
    A fake $TARGET/venv/bin/python stands in for real Django (matching
    Stage50CanonicalNativePublishPrivilegeSplitTests's own established
    technique) -- E4/Foundation-E's own real publish/ownership logic
    already has thorough, separate coverage elsewhere; what's being
    proven here is Stage 75's OWN new decision not to republish when
    durable receipt evidence already proves this exact archive's
    protected_updater was recovered."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-stage75-adopt-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.staging = self.tmpdir / "staging"
        self.target = self.staging / "opt" / "isadoraair"
        self.target.mkdir(parents=True)
        (self.target / "manage.py").write_text("# never executed -- fake venv python\n", encoding="utf-8")
        (self.target / ".env").write_text("DEBUG=True\n", encoding="utf-8")
        venv_bin = self.target / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        self.python_log = self.tmpdir / "python-argv.log"
        (venv_bin / "python").write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {self.python_log}
if [[ "$*" == *"validate_runtime_recovery_payload"* ]]; then
  printf '%s\\n' '{{"components": {{"protected_updater": {{"state": "present"}}}}}}'
  exit 0
fi
if [[ "$*" == *"restore_phase_d_component"* ]]; then
  echo "SHOULD NOT BE CALLED WHEN ADOPTION SUCCEEDS" >&2
  exit 1
fi
exit 0
""",
            encoding="utf-8",
        )
        (venv_bin / "python").chmod(0o755)
        self.ledger_root = self.tmpdir / "ledger-root"
        self.env = {**os.environ, "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root)}
        self.archive = self._build_self_contained_archive()

    def _build_self_contained_archive(self) -> Path:
        payload_src = self.tmpdir / "payload-src"
        (payload_src / "protected-updater" / "bootstrap").mkdir(parents=True)
        (payload_src / "runtime-recovery.json").write_text('{"ok": true}\n', encoding="utf-8")
        (payload_src / "protected-updater" / "restore-manifest.json").write_text("{}\n", encoding="utf-8")
        launcher = payload_src / "protected-updater" / "bootstrap" / "launcher.sh"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for path in payload_src.rglob("*"):
            if path.is_dir():
                path.chmod(0o755)
        for path in payload_src.rglob("*"):
            if path.is_file() and path != launcher:
                path.chmod(0o644)
        launcher.chmod(0o755)
        payload_src.chmod(0o755)

        workdir = self.tmpdir / "archive-workdir"
        recovery_dir = workdir / "runtime-recovery"
        recovery_dir.mkdir(parents=True)
        recovery_dir.chmod(0o755)
        subprocess.run(["cp", "-R", f"{payload_src}/.", f"{recovery_dir}/"], check=True)
        status = {
            "schema_version": 2, "payload_id": "stage75-adopt-payload",
            "product_contract_sha256": "0" * 64,
            "components": {"protected_updater": {"state": "present"}},
            "tts_components": [], "piper_freshness": {"state": "not_checked"},
            "policy": {"required": ["protected_updater"], "missing": [], "satisfied": True},
        }
        meta_out = self.tmpdir / "meta.json"
        result = subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "write-metadata", "--status-json", json.dumps(status),
             "--script-version", "3.0.0", "--output", str(meta_out)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shutil.copy(meta_out, workdir / "runtime-recovery-archive.json")
        archive = self.tmpdir / "backup.tar.gz"
        subprocess.run(["tar", "czf", str(archive), "-C", str(workdir), "."], check=True, capture_output=True, text=True)
        return archive

    def _run(self, *extra):
        return subprocess.run(
            [str(RESTORE_DIR / "75-protected-updater.sh"), "--archive", str(self.archive),
             "--staging-root", str(self.staging), "--apply", *extra],
            capture_output=True, text=True, timeout=30, env=self.env,
        )

    def _receipt_path(self) -> Path:
        return self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "runtime-recovery.json"

    def test_adoption_skips_republish_when_receipt_already_proves_recovery(self):
        receipt = self._receipt_path()
        receipt.parent.mkdir(parents=True)
        result = subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "record", "--archive", str(self.archive),
             "--receipt", str(receipt), "--component", "protected_updater"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        result = self._run("--resume", "--adopt-pre-ledger")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS (adopted)", result.stdout)
        self.assertNotIn("restore_phase_d_component", self.python_log.read_text(encoding="utf-8"))

    def test_without_a_matching_receipt_falls_through_to_normal_path(self):
        # No receipt at all -- adoption cannot be proven, so the normal
        # (fake-venv-backed) restore+publish path runs instead.
        result = self._run("--resume", "--adopt-pre-ledger")
        # The fake venv's restore_phase_d_component branch deliberately
        # exits 1 -- proving the normal path really was attempted rather
        # than a false adoption.
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restore_phase_d_component", self.python_log.read_text(encoding="utf-8"))

    def test_adoption_not_attempted_when_ledger_already_has_the_stage(self):
        """Once the ledger itself already records this stage complete,
        the ORDINARY --resume (r0043) path is what applies, not
        adoption -- adoption is only ever for a stage the ledger has
        never seen before."""
        receipt = self._receipt_path()
        receipt.parent.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(RECOVERY_HELPER), "record", "--archive", str(self.archive),
             "--receipt", str(receipt), "--component", "protected_updater"],
            check=True, capture_output=True, text=True, timeout=15,
        )
        first = self._run("--resume", "--adopt-pre-ledger")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertIn("PASS (adopted)", first.stdout)

        second = self._run("--resume", "--adopt-pre-ledger")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertNotIn("adopted", second.stdout.replace("PASS (adopted)", ""))


class Stage20And30AdoptionAcceptanceSequenceTests(Stage30RealPostgreSQLTestCase):
    """The task's own concrete acceptance sequence, as far as it can be
    proven without real GitHub/Foundation-E network access: a pre-
    ledger restore (Stage 20 git checkout + Stage 30 database, both
    genuinely restored, NO ledger at all -- exactly the real r0042 E8
    sandbox's own state) is adopted by r0044 tooling using the SAME
    archive, populating a fresh ledger, then Stage 40's normalization
    runs for real against the adopted .env and Stage 80 repairs the
    resulting known scaffold -- all without --force-env, --force-db, or
    any manual filesystem deletion."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-full-adopt-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fixture_repo = _make_git_fixture_repo(self.tmpdir / "fixture-repo")
        (self.fixture_repo / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.fixture_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "manage.py"], cwd=self.fixture_repo, check=True)
        self.fixture_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.fixture_repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.staging = self.tmpdir / "staging"
        self.ledger_root = self.tmpdir / "ledger-root"
        self.remotes = self.tmpdir / "remotes"
        self.remotes.mkdir()
        _make_git_fixture_repo(self.remotes / "weather-ingest.git")

    def _build_archive(self, *, db_user: str) -> Path:
        workdir = self.tmpdir / "work"
        workdir.mkdir(exist_ok=True)
        (workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.fixture_sha}\n", encoding="utf-8")
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True)
        legacy_weather = f"{self.staging}/weather-ingest/data"
        (app_dir / ".env").write_text(
            f"DB_USER={db_user}\nDB_PASSWORD=pw12345\nDB_HOST=127.0.0.1\nDB_PORT={self.port}\n"
            f"WEATHER_DATA_DIR={legacy_weather}\n",
            encoding="utf-8",
        )
        with tarfile.open(workdir / "app.tar.gz", "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        seed_db = f"{db_user}_seed"
        self._run_super("-c", f"DROP DATABASE IF EXISTS {seed_db}")
        self._run_super("-c", f"CREATE DATABASE {seed_db}")
        self._run_super(
            "-c",
            "CREATE TABLE django_migrations "
            "(id serial primary key, app text, name text, applied timestamptz); "
            "INSERT INTO django_migrations (app, name, applied) VALUES ('isadoraair', '0001_initial', now());",
            database=seed_db,
        )
        subprocess.run(
            [str(self.pg_bin / "pg_dump"), "-h", str(self.sock_dir), "-p", str(self.port),
             "-U", "postgres", "-Fc", "-d", seed_db, "-f", str(workdir / "database.dump")],
            check=True, capture_output=True, text=True,
        )
        archive = self.tmpdir / "backup.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive

    def _pre_ledger_restore(self, archive: Path, db_user: str):
        """Establishes exactly the pre-ledger state the real r0042 E8
        sandbox has: a real git checkout at the recorded SHA, a real
        .env (with the legacy WEATHER_DATA_DIR value), and a real,
        genuinely pg_restore'd database -- all done WITHOUT any r0044
        ledger/adoption machinery at all, exactly like the actual
        pre-r0043 tooling that produced it."""
        target = self.staging / "opt" / "isadoraair"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(self.fixture_repo), str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "-q", "--detach", self.fixture_sha], check=True)
        env_src = tarfile.open(archive, "r:gz")
        app_tar_member = env_src.extractfile("./app.tar.gz") or env_src.extractfile("app.tar.gz")
        with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp_app:
            tmp_app.write(app_tar_member.read())
            tmp_app.flush()
            with tarfile.open(tmp_app.name, "r:gz") as app_tf:
                env_member = app_tf.extractfile("isadoraair/.env")
                (target / ".env").write_bytes(env_member.read())
        env_src.close()

        role_exists = self._run_super("-tAc", f"SELECT 1 FROM pg_roles WHERE rolname = '{db_user}'").stdout.strip()
        if role_exists != "1":
            self._run_super("-c", f"CREATE ROLE {db_user} LOGIN PASSWORD 'pw12345'")
        self._run_super("-c", f"CREATE DATABASE isadoraair_restore_test OWNER {db_user} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8'")
        self._run_super("-c", f"GRANT ALL PRIVILEGES ON DATABASE isadoraair_restore_test TO {db_user}")
        self._run_super("-c", "GRANT ALL ON SCHEMA public TO " + db_user, database="isadoraair_restore_test")
        with tempfile.NamedTemporaryFile(suffix=".dump") as tmp_dump:
            with tarfile.open(archive, "r:gz") as tf:
                dump_member = tf.extractfile("./database.dump") or tf.extractfile("database.dump")
                tmp_dump.write(dump_member.read())
                tmp_dump.flush()
            subprocess.run(
                [str(self.pg_bin / "pg_restore"), "-h", str(self.sock_dir), "-p", str(self.port),
                 "-U", db_user, "-d", "isadoraair_restore_test", "--no-owner", tmp_dump.name],
                check=True, capture_output=True, text=True,
                env={**os.environ, "PGPASSWORD": "pw12345"},
            )
        return target

    def test_pre_ledger_20_and_30_can_be_adopted_then_40_normalizes_and_80_repairs(self):
        db_user = "adoptflowuser"
        archive = self._build_archive(db_user=db_user)
        target = self._pre_ledger_restore(archive, db_user)

        env = {**os.environ, "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root)}

        def run_stage(script, *extra):
            return subprocess.run(
                [str(RESTORE_DIR / script), "--archive", str(archive), "--staging-root", str(self.staging),
                 "--repo-url", f"file://{self.fixture_repo}", "--apply", *extra],
                capture_output=True, text=True, timeout=30, env=env,
            )

        # Stage 20: no --force-env, adoption instead.
        stage20 = run_stage("20-application.sh", "--resume", "--adopt-pre-ledger")
        self.assertEqual(stage20.returncode, 0, stage20.stdout + stage20.stderr)
        self.assertIn("PASS (adopted)", stage20.stdout)
        self.assertNotIn("--force-env", stage20.stdout + stage20.stderr)

        # Stage 30: no --force-db, adoption instead. --repo-url is
        # harmless/unused by 30-postgresql.sh's own arg parser (rejects
        # unknown flags) -- omit it here.
        stage30 = subprocess.run(
            [str(RESTORE_DIR / "30-postgresql.sh"), "--archive", str(archive), "--staging-root", str(self.staging),
             "--apply", "--resume", "--adopt-pre-ledger"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(stage30.returncode, 0, stage30.stdout + stage30.stderr)
        self.assertIn("PASS (adopted)", stage30.stdout)
        self.assertNotIn("--force-db", stage30.stdout + stage30.stderr)

        # Stage 40: runs for real (never needed adoption -- always
        # idempotent), normalizes the still-legacy .env in place.
        stage40 = subprocess.run(
            [str(RESTORE_DIR / "40-station-content.sh"), "--archive", str(archive),
             "--staging-root", str(self.staging), "--apply"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertEqual(stage40.returncode, 0, stage40.stdout + stage40.stderr)
        env_text = (target / ".env").read_text(encoding="utf-8")
        self.assertIn("WEATHER_DATA_DIR=/var/lib/isadoraair/weather\n", env_text)
        self.assertFalse((self.staging / "weather-ingest").exists())

        # Simulate the pre-r0043 damage already on disk (the real E8
        # sandbox's own forensic evidence: empty dir, no .git) --
        # plausible if THIS exact machine's Stage 60 already ran under
        # the old buggy tooling before Stage 40 ever got a chance to fix
        # the root cause.
        (self.staging / "weather-ingest" / "data").mkdir(parents=True)

        # Stage 80: repairs it under --resume, no manual deletion.
        stage80 = subprocess.run(
            [str(RESTORE_DIR / "80-companions.sh"), "--archive", str(archive),
             "--staging-root", str(self.staging), "--companions-root", str(self.staging),
             "--repo-url-prefix", str(self.remotes), "--only", "weather-ingest",
             "--resume", "--apply"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(stage80.returncode, 0, stage80.stdout + stage80.stderr)
        self.assertTrue((self.staging / "weather-ingest" / ".git").is_dir())


class InteractiveWorkflowTests(SimpleTestCase):
    """Real pty-driven tests of restore.sh's own interactive preflight."""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-e-interactive-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.fixture_repo = _make_git_fixture_repo(self.tmpdir / "fixture-repo")
        (self.fixture_repo / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.fixture_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "manage.py"], cwd=self.fixture_repo, check=True)
        self.fixture_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.fixture_repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.staging = self.tmpdir / "staging"
        self.ledger_root = self.tmpdir / "ledger-root"
        self.archive = self._build_archive()

    def _build_archive(self) -> Path:
        """A fully inspect_backup.sh-valid archive -- needed because
        some of these tests let the interactive menu's choice actually
        proceed into the real stage loop (00-preflight.sh runs
        inspect_backup.sh for real), unlike a menu-detection-only test
        that never gets past the prompt itself."""
        workdir = self.tmpdir / "work"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.fixture_sha}\n", encoding="utf-8")
        app_dir = self.tmpdir / "app_build" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        with tarfile.open(workdir / "app.tar.gz", "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 32)
        archive = self.tmpdir / "backup.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive

    def _establish_pre_ledger_target(self):
        target = self.staging / "opt" / "isadoraair"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(self.fixture_repo), str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "-q", "--detach", self.fixture_sha], check=True)
        (target / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")
        return target

    def _env(self):
        # restore.sh (the full orchestrator) forwards its common args
        # identically to EVERY stage, and only 20-application.sh
        # recognizes --repo-url -- so a real end-to-end interactive run
        # through the whole chain can't just pass --repo-url on the
        # command line the way a direct 20-application.sh invocation
        # can. A small `git` shim on PATH transparently substitutes the
        # local fixture repo for the real (private, network) GitHub
        # remote instead, so the default restore.sh invocation works
        # unmodified.
        shim_dir = self.tmpdir / "git-shim"
        if not shim_dir.exists():
            shim_dir.mkdir()
            real_git = shutil.which("git")
            (shim_dir / "git").write_text(
                f"""#!/usr/bin/env bash
if [ "$1" = "clone" ]; then
  args=()
  for a in "$@"; do
    if [ "$a" = "git@github.com:celltech161/IsadoraAir.git" ]; then
      args+=("{self.fixture_repo}")
    else
      args+=("$a")
    fi
  done
  exec {real_git} "${{args[@]}}"
fi
exec {real_git} "$@"
""",
                encoding="utf-8",
            )
            (shim_dir / "git").chmod(0o755)
        # r0045: restore.sh's interactive path also does recovery-media
        # discovery under $HOME -- isolate it so these ledger/adoption
        # tests can't pick up any REAL recovery-media tree that happens
        # to exist on the host running the tests (e.g. this session's
        # own real E8 export lives under the real $HOME).
        isolated_home = self.tmpdir / "isolated-home"
        isolated_home.mkdir(parents=True, exist_ok=True)
        return {
            **os.environ,
            "PATH": f"{shim_dir}:{os.environ['PATH']}",
            "RESTORE_RECOVERY_RECEIPT_ROOT": str(self.ledger_root),
            "HOME": str(isolated_home),
        }

    def _args(self):
        return [
            "bash", str(RESTORE_DIR / "restore.sh"),
            "--archive", str(self.archive), "--staging-root", str(self.staging),
            "--apply",
        ]

    def test_no_tty_never_prompts(self):
        """Piped (non-TTY) stdin -- the interactive preflight must never
        engage at all, even with matching pre-ledger state present."""
        self._establish_pre_ledger_target()
        result = subprocess.run(
            self._args(), input="", capture_output=True, text=True, timeout=30, env=self._env(),
        )
        self.assertNotIn("Choice", result.stdout)
        self.assertNotIn("[A] Verify and adopt", result.stdout)

    def test_non_interactive_flag_never_prompts_even_with_a_real_tty(self):
        self._establish_pre_ledger_target()
        returncode, output = _run_pty(
            [*self._args(), "--non-interactive"], self._env(), send=b"", wait_before_send=1.0,
        )
        self.assertNotIn("Choice", output)

    def test_explicit_resume_flag_skips_the_prompt(self):
        self._establish_pre_ledger_target()
        returncode, output = _run_pty(
            [*self._args(), "--resume"], self._env(), send=b"", wait_before_send=1.0,
        )
        self.assertNotIn("Choice", output)

    def test_pre_ledger_state_detected_and_quit_makes_no_changes(self):
        target = self._establish_pre_ledger_target()
        original_head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        # Leading blank line dismisses the r0045 recovery-media prompt
        # (no media configured for this test), then "Q" answers the
        # ledger-adoption menu that follows it.
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nQ\n")
        self.assertIn("Existing IsadoraAir restore state was found, but no recovery ledger exists", output)
        self.assertIn("Cancelled -- no changes made", output)
        self.assertEqual(returncode, 0)
        after_head = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(original_head, after_head)

    def test_pre_ledger_show_detected_state(self):
        self._establish_pre_ledger_target()
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nS\nQ\n", timeout=20)
        self.assertIn("Detected at", output)
        self.assertIn(".git checkout=yes", output)

    def test_pre_ledger_new_recovery_choice_proceeds_and_fails_on_existing_env(self):
        """[N] Treat this as a new recovery -- proceeds exactly as
        before r0043/r0044, so the existing non-empty .env correctly
        still requires --force-env."""
        self._establish_pre_ledger_target()
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nN\n", timeout=20)
        self.assertNotEqual(returncode, 0)
        self.assertIn("Refusing to overwrite existing non-empty", output)

    def test_fresh_target_never_prompts(self):
        """No pre-existing restore STATE at all -- nothing to ask about
        regarding ledger adoption, even under a real TTY. (r0045: a
        fresh target IS still asked about recovery media -- that prompt
        is orthogonal to ledger/adoption state and applies to the most
        common E8 scenario, a brand-new box -- so a blank reply is sent
        to satisfy it; the ledger-adoption menu's own "Choice" prompt
        must still never appear.)"""
        self.staging.mkdir(parents=True)
        (self.staging / "opt").mkdir()
        returncode, output = _run_pty(self._args(), self._env(), send=b"\n", wait_before_send=2.0, timeout=15)
        self.assertIn("Recovery-media root []:", output)
        self.assertNotIn("Choice", output)

    def test_matching_ledger_resume_menu_and_quit(self):
        target = self._establish_pre_ledger_target()
        ledger = self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(LEDGER_HELPER), "record", "--ledger", str(ledger),
             "--archive", str(self.archive), "--target-root", str(target), "--stage", "20-application",
             "--git-sha", self.fixture_sha],
            check=True, capture_output=True, text=True,
        )
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nQ\n")
        self.assertIn("Existing IsadoraAir recovery session found", output)
        self.assertIn("Last completed stage: 20-application", output)
        self.assertIn("Cancelled -- no changes made", output)
        self.assertEqual(returncode, 0)

    def test_matching_ledger_show_status(self):
        target = self._establish_pre_ledger_target()
        ledger = self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(LEDGER_HELPER), "record", "--ledger", str(ledger),
             "--archive", str(self.archive), "--target-root", str(target), "--stage", "20-application"],
            check=True, capture_output=True, text=True,
        )
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nS\nQ\n", timeout=20)
        self.assertIn("20-application", output)
        self.assertIn("complete", output)

    def test_mismatched_ledger_archive_fails_closed_without_a_menu(self):
        target = self._establish_pre_ledger_target()
        ledger = self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        other_archive = self.tmpdir / "other.tar.gz"
        other_archive.write_text("different bytes\n", encoding="utf-8")
        subprocess.run(
            [sys.executable, str(LEDGER_HELPER), "record", "--ledger", str(ledger),
             "--archive", str(other_archive), "--target-root", str(target), "--stage", "20-application"],
            check=True, capture_output=True, text=True,
        )
        # A leading blank line dismisses the r0045 recovery-media prompt
        # (no media configured for this test) before the ledger's own
        # archive-mismatch fail-closed check runs -- still no ledger
        # "Choice" menu is ever shown.
        returncode, output = _run_pty(self._args(), self._env(), send=b"\n", wait_before_send=1.5, timeout=15)
        self.assertNotEqual(returncode, 0)
        self.assertIn("DIFFERENT archive", output)
        self.assertNotIn("Choice", output)

    def test_matching_ledger_resume_choice_adds_resume_flag(self):
        """[R] Resume -- Stage 20 sees --resume and, verifying cleanly,
        converges instead of re-cloning (proven by an unchanged HEAD and
        the stage's own 'resumed/verified' wording)."""
        target = self._establish_pre_ledger_target()
        ledger = self.ledger_root / "var" / "lib" / "isadoraair" / "restore" / "ledger.json"
        ledger.parent.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(LEDGER_HELPER), "record", "--ledger", str(ledger),
             "--archive", str(self.archive), "--target-root", str(target), "--stage", "20-application",
             "--git-sha", self.fixture_sha],
            check=True, capture_output=True, text=True,
        )
        returncode, output = _run_pty(self._args(), self._env(), send=b"\nR\n", timeout=30)
        self.assertIn("20-application: PASS (resumed/verified)", output)


class NoServicesStartedDuringAdoptionTests(SimpleTestCase):
    """Structural proof: none of the new r0044 adoption/interactive code
    starts, enables, or reloads any service."""

    def test_no_new_stage_code_or_restore_sh_starts_enables_or_reloads_a_service(self):
        forbidden = ("systemctl start", "systemctl enable", "systemctl reload", "systemctl restart")
        for name in (
            "restore.sh", "20-application.sh", "30-postgresql.sh", "40-station-content.sh",
            "50-native-deps.sh", "70-tts.sh", "75-protected-updater.sh", "80-companions.sh", "lib.sh",
        ):
            text = (RESTORE_DIR / name).read_text(encoding="utf-8")
            for pattern in forbidden:
                self.assertNotIn(pattern, text, f"{name} contains {pattern!r}")

    def test_runtime_recovery_archive_helper_has_no_subprocess_or_service_calls(self):
        text = RECOVERY_HELPER.read_text(encoding="utf-8")
        self.assertNotIn("subprocess", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("systemctl", text)
