"""r0060 Phase 6, section D -- deploy/verify_backup_roundtrip.sh.

Real end-to-end execution against a fully faked remote (a local
directory) and a real small local git repository -- never real
production SFTP, matching this project's established fakebin
convention (see isadoraair/tests/test_backup_shell_contract.py and
isadoraair/tests/test_restore_tooling.py)."""
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair import backup_assurance as ba

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "deploy" / "verify_backup_roundtrip.sh"

_FAKE_PG_RESTORE = """#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_PG_RESTORE_FAIL") == "1":
    print("simulated pg_restore catalog failure", file=sys.stderr)
    sys.exit(1)
print("; fake TOC")
sys.exit(0)
"""

_FAKE_SSHPASS = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args and args[0] == "-e":
    args = args[1:]
os.execvp(args[0], args)
"""

_FAKE_SFTP = """#!/usr/bin/env python3
import os, shutil, sys

remote_dir = os.environ["FAKE_REMOTE_DIR"]
fail_stage = os.environ.get("FAKE_SFTP_FAIL_STAGE", "")

for raw in sys.stdin:
    line = raw.strip()
    if not line or line == "bye":
        continue
    parts = line.split()
    cmd = parts[0]
    if cmd == "cd":
        continue
    if cmd == "get":
        if fail_stage == "get":
            print("simulated sftp get failure", file=sys.stderr)
            sys.exit(1)
        shutil.copyfile(os.path.join(remote_dir, parts[1]), parts[2])
sys.exit(0)
"""


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git") and shutil.which("pg_restore"), "git/pg_restore not installed")
class VerifyBackupRoundtripTests(SimpleTestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-roundtrip-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.home = self.tmpdir / "home"
        self.project_dir = self.tmpdir / "project"
        self.remote_dir = self.tmpdir / "remote"
        self.fakebin = self.tmpdir / "fakebin"
        self.scratch_tmp = self.tmpdir / "scratch-tmp"
        for d in (self.home, self.project_dir, self.remote_dir, self.fakebin, self.scratch_tmp):
            d.mkdir(parents=True)

        _run_git(self.project_dir, "init", "-q", "-b", "main")
        _run_git(self.project_dir, "config", "user.email", "test@example.invalid")
        _run_git(self.project_dir, "config", "user.name", "Test")
        (self.project_dir / "manage.py").write_text("# initial\n", encoding="utf-8")
        _run_git(self.project_dir, "add", "manage.py")
        _run_git(self.project_dir, "commit", "-q", "-m", "initial")
        self.main_sha = _run_git(self.project_dir, "rev-parse", "HEAD").stdout.strip()

        (self.home / ".iasboxbu.cred").write_text(
            'BAK_HOST="localhost"\nBAK_USER="fake"\nBAK_PORT="22"\n'
            f'BAK_PATH="{self.remote_dir}"\nBAK_PASS="s3cr3t-fake-password"\n',
            encoding="utf-8",
        )

        _write_executable(self.fakebin / "pg_restore", _FAKE_PG_RESTORE)
        _write_executable(self.fakebin / "sshpass", _FAKE_SSHPASS)
        _write_executable(self.fakebin / "sftp", _FAKE_SFTP)

        self.state_dir = self.home / ".local" / "state" / "isadoraair" / "backup-assurance"
        self.remote_filename = "isadoraair-backup-20260910-033000.tar.gz"

    def _build_archive(self, git_sha):
        workdir = self.tmpdir / "archive_build"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(
            "IsadoraAir disaster-recovery backup manifest\n"
            "Backup script version: 3.2.0\n"
            "Created (UTC):          2026-09-10T03:30:00+00:00\n"
            f"IsadoraAir Git SHA:     {git_sha}\n"
            "IsadoraAir Git Branch:  main\n"
            "IsadoraAir Git Dirty:   false\n"
            "Database catalog check (pg_restore --list): ok\n",
            encoding="utf-8",
        )
        (workdir / "database.dump").write_bytes(b"PGDMP" + b"\x00" * 100)
        app_dir = self.tmpdir / "archive_app" / "isadoraair"
        app_dir.mkdir(parents=True)
        (app_dir / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
        (app_dir / ".env").write_text("SECRET_KEY=test\n", encoding="utf-8")
        with tarfile.open(workdir / "app.tar.gz", "w:gz") as tf:
            tf.add(app_dir, arcname="isadoraair")
        archive_path = self.remote_dir / self.remote_filename
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        return archive_path

    def _sha256(self, path):
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _write_success_receipt(self, git_sha, archive_sha256=None, remote_filename=None):
        # Hash/size always come from the REAL archive on disk (built
        # under self.remote_filename) -- remote_filename is only what
        # the receipt CLAIMS, letting a test deliberately point the
        # receipt at a name that doesn't match the naming contract.
        archive_path = self.remote_dir / self.remote_filename
        ba.record_success(
            remote_filename=remote_filename or self.remote_filename,
            archive_bytes=archive_path.stat().st_size,
            archive_sha256=archive_sha256 or self._sha256(archive_path),
            backup_script_version="3.2.0", archive_format_version="2.1.0",
            recovery_class="legacy_non_self_contained", retention_days=30,
            validations={"database_catalog": True, "archive_integrity": True,
                         "runtime_extractable": True, "recovery_policy_satisfied": True,
                         "remote_promotion": True},
            git_state={"sha": git_sha, "branch": "main", "detached": False, "dirty": False},
            state_dir=self.state_dir,
        )

    def _env(self, **overrides):
        env = {
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "PROJECT_DIR": str(self.project_dir),
            "FAKE_REMOTE_DIR": str(self.remote_dir),
            "TMPDIR": str(self.scratch_tmp),
        }
        env.update(overrides)
        return env

    def _run(self, **env_overrides):
        return subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            env=self._env(**env_overrides),
            capture_output=True, text=True, timeout=60,
        )

    def _read_receipt(self, name):
        path = self.state_dir / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _assert_scratch_tmp_empty(self):
        self.assertEqual(list(self.scratch_tmp.iterdir()), [], "temporary download/extraction directory was not cleaned up")

    # -- happy path -----------------------------------------------------

    def test_exact_receipt_object_valid_hash_passes(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt(self.main_sha)

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASSED", result.stdout)
        self._assert_scratch_tmp_empty()

        success = self._read_receipt("roundtrip-last-success.json")
        self.assertIsNotNone(success)
        self.assertEqual(success["backup_git_sha"], self.main_sha)
        self.assertEqual(success["inspector_result"], "pass")
        self.assertEqual(success["pg_restore_catalog_result"], "pass")
        self.assertEqual(success["main_ancestry_result"], "pass")

        attempt = self._read_receipt("roundtrip-last-attempt.json")
        self.assertEqual(attempt["outcome"], "success")

        # Password must never leak anywhere.
        blob = result.stdout + result.stderr + json.dumps(success) + json.dumps(attempt)
        self.assertNotIn("s3cr3t-fake-password", blob)

    # -- hash mismatch ----------------------------------------------------

    def test_hash_mismatch_fails_and_cleans_up(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt(self.main_sha, archive_sha256="f" * 64)

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
        self.assertIsNone(self._read_receipt("roundtrip-last-success.json"))
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "hash_verify")

    # -- download failure ---------------------------------------------

    def test_download_failure(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt(self.main_sha)

        result = self._run(FAKE_SFTP_FAIL_STAGE="get")
        self.assertNotEqual(result.returncode, 0)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "downloading")

    # -- inspector failure (corrupt/invalid archive) ---------------------

    def test_inspector_failure(self):
        # A structurally-broken archive (no database.dump) still needs a
        # matching hash in the receipt to reach the inspector stage.
        workdir = self.tmpdir / "broken"
        workdir.mkdir()
        (workdir / "MANIFEST.txt").write_text(f"IsadoraAir Git SHA:     {self.main_sha}\n", encoding="utf-8")
        archive_path = self.remote_dir / self.remote_filename
        with tarfile.open(archive_path, "w:gz") as tf:
            tf.add(workdir, arcname=".")
        self._write_success_receipt(self.main_sha)

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("structural FAIL", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "inspector")

    # -- pg_restore --list failure --------------------------------------

    def test_pg_restore_list_failure(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt(self.main_sha)

        result = self._run(FAKE_PG_RESTORE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "pg_restore_list")

    # -- receipt/archive git sha mismatch --------------------------------

    def test_receipt_and_archive_git_sha_mismatch(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt("b" * 40)  # receipt claims a different SHA than the archive manifest

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match last-success.json", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "git_sha_match")

    # -- missing git commit ------------------------------------------------

    def test_missing_git_commit(self):
        missing_sha = "d" * 40
        self._build_archive(missing_sha)
        self._write_success_receipt(missing_sha)

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist in the local", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "main_ancestry")

    # -- sha not ancestor of main ------------------------------------------

    def test_sha_not_ancestor_of_main(self):
        _run_git(self.project_dir, "checkout", "-q", "-b", "side-branch")
        (self.project_dir / "extra.txt").write_text("x", encoding="utf-8")
        _run_git(self.project_dir, "add", "extra.txt")
        _run_git(self.project_dir, "commit", "-q", "-m", "side commit")
        side_sha = _run_git(self.project_dir, "rev-parse", "HEAD").stdout.strip()
        _run_git(self.project_dir, "checkout", "-q", "main")

        self._build_archive(side_sha)
        self._write_success_receipt(side_sha)

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not an ancestor", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
        self.assertEqual(self._read_receipt("roundtrip-last-attempt.json")["stage"], "main_ancestry")

    # -- no receipt yet -----------------------------------------------------

    def test_no_receipt_yet_fails_clearly(self):
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no valid last-success.json", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()

    # -- naming contract ----------------------------------------------------

    def test_malformed_remote_filename_in_receipt_refused(self):
        self._build_archive(self.main_sha)
        self._write_success_receipt(self.main_sha, remote_filename="not-a-real-backup-name.tar.gz")
        # Move the archive to the (invalid) name the receipt claims so a
        # download WOULD otherwise be possible -- proving the refusal is
        # about the naming contract, not just a missing file.
        (self.remote_dir / self.remote_filename).rename(self.remote_dir / "not-a-real-backup-name.tar.gz")
        self.remote_filename = "not-a-real-backup-name.tar.gz"  # for any later helper calls in this test

        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("naming contract", result.stdout + result.stderr)
        self._assert_scratch_tmp_empty()
