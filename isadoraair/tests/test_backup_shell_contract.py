"""r0060 Phase 6 -- real, end-to-end execution of
deploy/backup_isadoraair.sh, exercising the NEW pg_restore catalog
check, git-cleanliness capture, and backup-assurance receipts.

Unlike the rest of this file's siblings (test_deploy_backup_script.py's
own docstring explains why that file sticks to static assertions --
real production secrets and network access), the pieces this test
covers are all internal/local: pg_dump, pg_restore, sshpass, and sftp
are faked (fakebin/ prepended to PATH, matching the established
convention in isadoraair/tests/test_restore_tooling.py), the "remote"
SFTP target is a plain local directory, and PROJECT_DIR/HOME both point
at a disposable temp tree with a REAL small git repo (so git-state
detection is exercised against real git, not a mock). Nothing here
ever touches production secrets, a real network, or a real PostgreSQL
server -- see fakebin/pg_dump and fakebin/pg_restore below."""
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "deploy" / "backup_isadoraair.sh"

_FAKE_PG_DUMP = """#!/usr/bin/env python3
import sys
args = sys.argv[1:]
out = args[args.index("-f") + 1]
with open(out, "wb") as fh:
    fh.write(b"PGDMP" + b"\\x00" * 32)
sys.exit(0)
"""

_FAKE_PG_RESTORE = """#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_PG_RESTORE_FAIL") == "1":
    print("simulated pg_restore catalog failure", file=sys.stderr)
    sys.exit(1)
print("; fake pg_restore -Fc archive TOC")
sys.exit(0)
"""

_FAKE_SSHPASS = """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
# Real invocation is: sshpass -e sftp -P PORT user@host -- drop the
# leading "-e" and exec the rest, same argv this test's fake sftp needs.
if args and args[0] == "-e":
    args = args[1:]
os.execvp(args[0], args)
"""

_FAKE_SFTP = """#!/usr/bin/env python3
import fnmatch
import os
import shutil
import sys

remote_dir = os.environ["FAKE_REMOTE_DIR"]
fail_stage = os.environ.get("FAKE_SFTP_FAIL_STAGE", "")
os.makedirs(remote_dir, exist_ok=True)

for raw in sys.stdin:
    line = raw.strip()
    if not line or line == "bye":
        continue
    parts = line.split()
    cmd = parts[0]
    if cmd == "cd":
        continue
    if cmd == "put":
        if fail_stage == "put":
            print("simulated sftp put failure", file=sys.stderr)
            sys.exit(1)
        shutil.copyfile(parts[1], os.path.join(remote_dir, parts[2]))
    elif cmd == "rename":
        if fail_stage == "rename":
            print("simulated sftp rename failure", file=sys.stderr)
            sys.exit(1)
        os.rename(os.path.join(remote_dir, parts[1]), os.path.join(remote_dir, parts[2]))
    elif cmd == "rm":
        target = os.path.join(remote_dir, parts[1])
        if os.path.exists(target):
            os.remove(target)
    elif cmd == "ls":
        pattern = parts[-1]
        for name in sorted(os.listdir(remote_dir)):
            if fnmatch.fnmatch(name, pattern):
                print(name)
sys.exit(0)
"""

_FAKE_VENV_PYTHON = """#!/usr/bin/env python3
# Stands in for $PROJECT_DIR/venv/bin/python. Only implements the one
# manage.py subcommand backup_isadoraair.sh calls for a station with no
# Runtime Foundation E7B recovery payload adopted -- the normal,
# unconfigured-by-default case this test's PROJECT_DIR fixture
# represents (no /var/lib/isadoraair/runtime-recovery on this box
# either way).
import sys
if len(sys.argv) > 2 and sys.argv[2] == "validate_runtime_recovery_payload":
    print('{"policy": {"required": null, "satisfied": null, "source": null, "reasons": {}}, "resolved_path": null}')
    sys.exit(2)
print(f"unsupported fake manage.py invocation: {sys.argv[1:]}", file=sys.stderr)
sys.exit(90)
"""


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class BackupShellContractTests(SimpleTestCase):
    """Real execution of deploy/backup_isadoraair.sh against a fully
    faked project/remote -- see this module's own docstring."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="isadoraair-backup-contract-"))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.home = self.tmpdir / "home"
        self.project_dir = self.tmpdir / "project"
        self.remote_dir = self.tmpdir / "remote"
        self.fakebin = self.tmpdir / "fakebin"
        for d in (self.home, self.project_dir, self.remote_dir, self.fakebin):
            d.mkdir(parents=True)

        # Deliberately omits REPORTS_ROOT/DB_HOST/DB_PORT -- this
        # fixture's own absence of those optional keys is exactly what
        # exercises the r0060 `|| true` pipefail fix below (see
        # BackupOptionalEnvReadPipefailTests for the same fix's static/
        # functional coverage).
        (self.project_dir / ".env").write_text(
            "DB_NAME=fakedb\nDB_USER=fakeuser\nDB_PASSWORD=fakepass\n",
            encoding="utf-8",
        )
        venv_bin = self.project_dir / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_executable(venv_bin / "python", _FAKE_VENV_PYTHON)
        (self.project_dir / "manage.py").write_text("# initial\n", encoding="utf-8")
        # venv/ is untracked scratch (the fake interpreter above) -- same
        # as the real checkout's own .gitignore, excluded here so a
        # freshly-committed fixture reads as CLEAN, not dirty merely
        # because a venv exists on disk.
        # Matches the real repo's own .gitignore (.env.* covers the
        # script's own .env.lock DB-maintenance-coordination lock file,
        # created before this fixture's git-state is ever read).
        (self.project_dir / ".gitignore").write_text("venv/\n.env.*\n", encoding="utf-8")

        _run_git(self.project_dir, "init", "-q")
        _run_git(self.project_dir, "config", "user.email", "test@example.invalid")
        _run_git(self.project_dir, "config", "user.name", "Test")
        _run_git(self.project_dir, "add", "manage.py", ".env", ".gitignore")
        _run_git(self.project_dir, "commit", "-q", "-m", "initial")
        self.git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.project_dir,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.git_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"], cwd=self.project_dir,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        (self.home / ".iasboxbu.cred").write_text(
            'BAK_HOST="localhost"\nBAK_USER="fake"\nBAK_PORT="22"\n'
            f'BAK_PATH="{self.remote_dir}"\nBAK_PASS="fakepass"\n',
            encoding="utf-8",
        )

        _write_executable(self.fakebin / "pg_dump", _FAKE_PG_DUMP)
        _write_executable(self.fakebin / "pg_restore", _FAKE_PG_RESTORE)
        _write_executable(self.fakebin / "sshpass", _FAKE_SSHPASS)
        _write_executable(self.fakebin / "sftp", _FAKE_SFTP)

        self.state_dir = self.home / ".local" / "state" / "isadoraair" / "backup-assurance"

    def _env(self, **overrides):
        env = {
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "PROJECT_DIR": str(self.project_dir),
            "FAKE_REMOTE_DIR": str(self.remote_dir),
        }
        env.update(overrides)
        return env

    def _run_backup(self, **env_overrides):
        return subprocess.run(
            ["bash", str(SCRIPT_PATH)],
            cwd=str(self.project_dir),
            env=self._env(**env_overrides),
            capture_output=True, text=True, timeout=60,
        )

    def _read_receipt(self, name):
        path = self.state_dir / name
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # -- successful run --------------------------------------------

    def test_successful_run_creates_success_and_attempt_receipts(self):
        result = self._run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)

        remote_files = list(self.remote_dir.glob("isadoraair-backup-*.tar.gz"))
        self.assertEqual(len(remote_files), 1, f"expected exactly one uploaded archive, found: {remote_files}")
        self.assertFalse(list(self.remote_dir.glob("*.partial")), "a .partial file must never survive a successful run")

        attempt = self._read_receipt("last-attempt.json")
        self.assertIsNotNone(attempt)
        self.assertEqual(attempt["outcome"], "success")
        self.assertEqual(attempt["git_sha"], self.git_sha)
        self.assertEqual(attempt["git_dirty"], False)

        success = self._read_receipt("last-success.json")
        self.assertIsNotNone(success)
        self.assertEqual(success["remote_filename"], remote_files[0].name)
        self.assertEqual(success["git_sha"], self.git_sha)
        self.assertEqual(success["git_branch"], self.git_branch)
        self.assertFalse(success["git_detached"])
        self.assertFalse(success["git_dirty"])
        self.assertTrue(success["validations"]["remote_promotion"])
        self.assertTrue(success["validations"]["database_catalog"])
        self.assertGreater(success["archive_bytes"], 0)
        self.assertEqual(len(success["archive_sha256"]), 64)

        # Never any secret material in either receipt.
        for receipt_text in (
            json.dumps(attempt), json.dumps(success),
        ):
            self.assertNotIn("fakepass", receipt_text)

    def test_dirty_checkout_recorded_as_dirty_not_aborted(self):
        (self.project_dir / "untracked.txt").write_text("scratch\n", encoding="utf-8")
        result = self._run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)

        success = self._read_receipt("last-success.json")
        self.assertTrue(success["git_dirty"])

        remote_files = list(self.remote_dir.glob("isadoraair-backup-*.tar.gz"))
        with tarfile.open(remote_files[0]) as outer:
            manifest = outer.extractfile("./MANIFEST.txt").read().decode("utf-8")
        self.assertIn("IsadoraAir Git Dirty:   true", manifest)
        self.assertNotIn("untracked.txt", manifest)

    # -- catalog-check failure (must abort BEFORE upload) -----------

    def test_pg_restore_catalog_failure_aborts_before_upload_no_success_receipt(self):
        result = self._run_backup(FAKE_PG_RESTORE_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pg_restore --list", result.stderr)

        self.assertEqual(list(self.remote_dir.iterdir()), [], "nothing should ever reach the remote on a catalog failure")
        self.assertIsNone(self._read_receipt("last-success.json"))

        attempt = self._read_receipt("last-attempt.json")
        self.assertEqual(attempt["outcome"], "failed")
        self.assertEqual(attempt["stage"], "catalog_check")
        self.assertEqual(attempt["exit_code"], result.returncode)

    # -- upload/rename failure ---------------------------------------

    def test_upload_failure_creates_failed_attempt_but_preserves_prior_success(self):
        first = self._run_backup()
        self.assertEqual(first.returncode, 0, first.stderr)
        prior_success = self._read_receipt("last-success.json")
        self.assertIsNotNone(prior_success)

        second = self._run_backup(FAKE_SFTP_FAIL_STAGE="rename")
        self.assertNotEqual(second.returncode, 0)

        attempt = self._read_receipt("last-attempt.json")
        self.assertEqual(attempt["outcome"], "failed")
        self.assertEqual(attempt["stage"], "upload")
        self.assertEqual(attempt["exit_code"], second.returncode)

        # The earlier success receipt must be completely untouched by
        # the later failure -- a failed attempt must never manufacture
        # or overwrite a success record.
        self.assertEqual(self._read_receipt("last-success.json"), prior_success)

        # No truncated/partial file was ever promoted to the FINAL name
        # -- the sftp batch stops at the failed `rename`, so a stray
        # `.partial` from this failed run may still be sitting on the
        # remote (a real, pre-existing, documented limitation of the
        # rename-only-after-full-upload design -- see this script's own
        # "a human should notice and investigate it" comment), but it
        # must never be mistaken for a second valid FINAL-named archive.
        remote_files = sorted(p.name for p in self.remote_dir.glob("isadoraair-backup-*.tar.gz"))
        self.assertEqual(len(remote_files), 1)
        self.assertEqual(remote_files[0], prior_success["remote_filename"])

    # -- DRY_RUN must never mutate assurance state --------------------

    def test_dry_run_never_touches_receipts(self):
        self.assertFalse(self.state_dir.exists())
        result = self._run_backup(DRY_RUN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state_dir.exists(), "DRY_RUN must never create/touch backup-assurance state")
        self.assertEqual(list(self.remote_dir.iterdir()), [])
