"""r0060 Phase 6 -- isadoraair/backup_assurance.py: atomic receipt I/O,
strict schema validation, git-cleanliness collection, DRY_RUN isolation,
and the Monitoring evaluate_backup_health() authority."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair import backup_assurance as ba


def _run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class TempStateDirMixin:
    def setUp(self):
        super().setUp()
        self.state_dir = Path(tempfile.mkdtemp(prefix="isadoraair-backup-assurance-"))
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.state_dir, ignore_errors=True)


CLEAN_GIT = {"sha": "a" * 40, "branch": "main", "detached": False, "dirty": False}


class AtomicWriteReadTests(TempStateDirMixin, SimpleTestCase):
    def test_state_dir_created_mode_0700(self):
        ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)

    def test_receipt_file_mode_0600(self):
        ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        path = self.state_dir / ba.ATTEMPT_FILE
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_round_trip_attempt(self):
        record = ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        read_back = ba.read_last_attempt(state_dir=self.state_dir)
        self.assertEqual(record, read_back)

    def test_missing_receipt_reads_as_none(self):
        self.assertIsNone(ba.read_last_attempt(state_dir=self.state_dir))
        self.assertIsNone(ba.read_last_success(state_dir=self.state_dir))
        self.assertIsNone(ba.read_roundtrip_last_attempt(state_dir=self.state_dir))
        self.assertIsNone(ba.read_roundtrip_last_success(state_dir=self.state_dir))

    def test_malformed_json_raises_assurance_error(self):
        self.state_dir.mkdir(exist_ok=True)
        (self.state_dir / ba.ATTEMPT_FILE).write_text("{not json", encoding="utf-8")
        with self.assertRaises(ba.AssuranceError):
            ba.read_last_attempt(state_dir=self.state_dir)

    def test_wrong_schema_version_rejected(self):
        record = ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        record["schema_version"] = 999
        (self.state_dir / ba.ATTEMPT_FILE).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ba.AssuranceError):
            ba.read_last_attempt(state_dir=self.state_dir)

    def test_missing_field_rejected(self):
        record = ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        del record["stage"]
        (self.state_dir / ba.ATTEMPT_FILE).write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaises(ba.AssuranceError):
            ba.read_last_attempt(state_dir=self.state_dir)

    def test_atomic_write_leaves_no_temp_files_behind(self):
        ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        names = os.listdir(self.state_dir)
        self.assertEqual(names, [ba.ATTEMPT_FILE])


class NoSecretFieldsTests(TempStateDirMixin, SimpleTestCase):
    """Every receipt this module can produce must be free of anything
    that looks like a secret -- passwords, .env/.pgpass contents, host/
    path information, raw git porcelain output."""

    FORBIDDEN_SUBSTRINGS = ("PASSWORD", "hunter2", ".pgpass", "BAK_HOST", "porcelain")

    def test_attempt_and_success_receipts_carry_no_secret_markers(self):
        ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir)
        ba.record_success(
            remote_filename="isadoraair-backup-20260910-033000.tar.gz",
            archive_bytes=123, archive_sha256="f" * 64,
            backup_script_version="3.2.0", archive_format_version="3.0.0",
            recovery_class="self_contained_v3", retention_days=30,
            validations={"database_catalog": True, "archive_integrity": True,
                         "runtime_extractable": True, "recovery_policy_satisfied": True,
                         "remote_promotion": True},
            git_state=CLEAN_GIT, state_dir=self.state_dir,
        )
        blob = (self.state_dir / ba.ATTEMPT_FILE).read_text() + (self.state_dir / ba.SUCCESS_FILE).read_text()
        for marker in self.FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(marker, blob)

    def test_schema_field_lists_never_grew_a_secret_looking_field(self):
        for fields in (ba._ATTEMPT_FIELDS, ba._SUCCESS_FIELDS, ba._ROUNDTRIP_ATTEMPT_FIELDS, ba._ROUNDTRIP_SUCCESS_FIELDS):
            for field in fields:
                lowered = field.lower()
                self.assertNotIn("password", lowered)
                self.assertNotIn("secret", lowered)
                self.assertNotIn("pgpass", lowered)
                self.assertNotIn("host", lowered)


class SuccessGatingTests(TempStateDirMixin, SimpleTestCase):
    def _validations(self, **overrides):
        base = {
            "database_catalog": True, "archive_integrity": True,
            "runtime_extractable": True, "recovery_policy_satisfied": True,
            "remote_promotion": True,
        }
        base.update(overrides)
        return base

    def test_success_requires_remote_promotion_true(self):
        with self.assertRaises(ba.AssuranceError):
            ba.record_success(
                remote_filename="x.tar.gz", archive_bytes=1, archive_sha256="a" * 64,
                backup_script_version="3.2.0", archive_format_version="3.0.0",
                recovery_class="self_contained_v3", retention_days=30,
                validations=self._validations(remote_promotion=False),
                git_state=CLEAN_GIT, state_dir=self.state_dir,
            )
        self.assertIsNone(ba.read_last_success(state_dir=self.state_dir))

    def test_success_written_after_remote_promotion_true(self):
        ba.record_success(
            remote_filename="x.tar.gz", archive_bytes=1, archive_sha256="a" * 64,
            backup_script_version="3.2.0", archive_format_version="3.0.0",
            recovery_class="self_contained_v3", retention_days=30,
            validations=self._validations(),
            git_state=CLEAN_GIT, state_dir=self.state_dir,
        )
        self.assertIsNotNone(ba.read_last_success(state_dir=self.state_dir))

    def test_roundtrip_success_requires_matching_hash(self):
        with self.assertRaises(ba.AssuranceError):
            ba.record_roundtrip_success(
                remote_filename="x.tar.gz", expected_sha256="a" * 64, observed_sha256="b" * 64,
                backup_git_sha="c" * 40, inspector_result="pass",
                pg_restore_catalog_result="pass", main_ancestry_result="pass",
                state_dir=self.state_dir,
            )

    def test_roundtrip_success_requires_every_stage_pass(self):
        with self.assertRaises(ba.AssuranceError):
            ba.record_roundtrip_success(
                remote_filename="x.tar.gz", expected_sha256="a" * 64, observed_sha256="a" * 64,
                backup_git_sha="c" * 40, inspector_result="fail",
                pg_restore_catalog_result="pass", main_ancestry_result="pass",
                state_dir=self.state_dir,
            )


class FailurePreservesExitSemanticsTests(TempStateDirMixin, SimpleTestCase):
    def test_attempt_result_preserves_started_at_and_git_state_from_start(self):
        start = ba.record_attempt_start("pg_dump", git_state=CLEAN_GIT, state_dir=self.state_dir)
        result = ba.record_attempt_result(
            start["attempt_id"], "failed", "catalog_check", exit_code=1, state_dir=self.state_dir,
        )
        self.assertEqual(result["started_at"], start["started_at"])
        self.assertEqual(result["git_sha"], CLEAN_GIT["sha"])
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["exit_code"], 1)

    def test_mismatched_attempt_id_still_writes_a_standalone_failure_record(self):
        """A missing/mismatched 'running' record must never cause the
        original failure to go unrecorded."""
        result = ba.record_attempt_result(
            "some-other-attempt-id", "failed", "pg_dump", exit_code=7, state_dir=self.state_dir,
        )
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual(result["exit_code"], 7)
        self.assertEqual(ba.read_last_attempt(state_dir=self.state_dir)["exit_code"], 7)


class DryRunNeverMutatesTests(TempStateDirMixin, SimpleTestCase):
    def test_attempt_start_dry_run_writes_nothing(self):
        ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir, dry_run=True)
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_success_dry_run_writes_nothing(self):
        ba.record_success(
            remote_filename="x.tar.gz", archive_bytes=1, archive_sha256="a" * 64,
            backup_script_version="3.2.0", archive_format_version="3.0.0",
            recovery_class="self_contained_v3", retention_days=30,
            validations={"database_catalog": True, "archive_integrity": True,
                         "runtime_extractable": True, "recovery_policy_satisfied": True,
                         "remote_promotion": True},
            git_state=CLEAN_GIT, state_dir=self.state_dir, dry_run=True,
        )
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_dry_run_returns_the_record_it_would_have_written(self):
        record = ba.record_attempt_start("starting", git_state=CLEAN_GIT, state_dir=self.state_dir, dry_run=True)
        self.assertEqual(record["outcome"], "running")


@unittest.skipUnless(__import__("shutil").which("git"), "git not installed")
class GitCleanlinessTests(SimpleTestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="isadoraair-git-cleanliness-"))
        self.addCleanup(self._rmtree)
        _run_git(self.repo, "init", "-q")
        _run_git(self.repo, "config", "user.email", "test@example.invalid")
        _run_git(self.repo, "config", "user.name", "Test")

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_clean_checkout(self):
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _run_git(self.repo, "add", "a.txt")
        _run_git(self.repo, "commit", "-q", "-m", "init")
        state = ba.collect_git_state(self.repo)
        self.assertEqual(state["dirty"], False)
        self.assertFalse(state["detached"])
        self.assertIsNotNone(state["sha"])

    def test_dirty_tracked_file(self):
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _run_git(self.repo, "add", "a.txt")
        _run_git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "a.txt").write_text("modified", encoding="utf-8")
        self.assertTrue(ba.collect_git_state(self.repo)["dirty"])

    def test_dirty_untracked_file(self):
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _run_git(self.repo, "add", "a.txt")
        _run_git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "untracked.txt").write_text("scratch", encoding="utf-8")
        self.assertTrue(ba.collect_git_state(self.repo)["dirty"])

    def test_detached_head(self):
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _run_git(self.repo, "add", "a.txt")
        _run_git(self.repo, "commit", "-q", "-m", "init")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        _run_git(self.repo, "checkout", "-q", sha)
        state = ba.collect_git_state(self.repo)
        self.assertTrue(state["detached"])
        self.assertIsNone(state["branch"])

    def test_git_unavailable_or_not_a_repo_reads_as_unknown(self):
        not_a_repo = self.repo / "not-a-repo"
        not_a_repo.mkdir()
        state = ba.collect_git_state(not_a_repo)
        self.assertEqual(state, {"sha": None, "branch": None, "detached": None, "dirty": None})

    def test_never_returns_raw_porcelain_text(self):
        """collect_git_state()'s return value must never contain
        anything resembling raw `git status --porcelain` output (e.g. a
        leading ' M '/'??' status code or a filename) -- only the
        derived boolean."""
        (self.repo / "a.txt").write_text("x", encoding="utf-8")
        _run_git(self.repo, "add", "a.txt")
        _run_git(self.repo, "commit", "-q", "-m", "init")
        (self.repo / "secret-named-file.txt").write_text("x", encoding="utf-8")
        state = ba.collect_git_state(self.repo)
        blob = json.dumps(state)
        self.assertNotIn("secret-named-file.txt", blob)
        self.assertNotIn("??", blob)


class EvaluateBackupHealthTests(TempStateDirMixin, SimpleTestCase):
    def _success(self, **overrides):
        from datetime import datetime, timezone
        now = overrides.pop("now", datetime.now(timezone.utc))
        fields = {
            "remote_filename": "isadoraair-backup-20260910-033000.tar.gz",
            "archive_bytes": 100, "archive_sha256": "a" * 64,
            "backup_script_version": "3.2.0", "archive_format_version": "3.0.0",
            "recovery_class": "self_contained_v3", "retention_days": 30,
            "validations": {"database_catalog": True, "archive_integrity": True,
                             "runtime_extractable": True, "recovery_policy_satisfied": True,
                             "remote_promotion": True},
            "git_state": CLEAN_GIT, "state_dir": self.state_dir, "now": now,
        }
        fields.update(overrides)
        return ba.record_success(**fields)

    def _rt_success(self, **overrides):
        from datetime import datetime, timezone
        now = overrides.pop("now", datetime.now(timezone.utc))
        fields = {
            "remote_filename": "isadoraair-backup-20260910-033000.tar.gz",
            "expected_sha256": "a" * 64, "observed_sha256": "a" * 64,
            "backup_git_sha": "a" * 40, "inspector_result": "pass",
            "pg_restore_catalog_result": "pass", "main_ancestry_result": "pass",
            "state_dir": self.state_dir, "now": now,
        }
        fields.update(overrides)
        return ba.record_roundtrip_success(**fields)

    def test_uninitialized_is_unknown(self):
        status, detail = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "unknown")

    def test_fresh_clean_backup_and_fresh_roundtrip_is_ok(self):
        self._success()
        self._rt_success()
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "ok")

    def test_28_hour_threshold_is_warning(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=29))
        self._rt_success(now=now)
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "warning")

    def test_36_hour_threshold_is_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=37))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_newest_failed_attempt_newer_than_success_is_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        start = ba.record_attempt_start("pg_dump", git_state=CLEAN_GIT, state_dir=self.state_dir, now=now - timedelta(minutes=5))
        ba.record_attempt_result(start["attempt_id"], "failed", "pg_dump", exit_code=1, state_dir=self.state_dir, now=now)
        status, detail = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_subsequent_success_clears_earlier_failure(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = ba.record_attempt_start("pg_dump", git_state=CLEAN_GIT, state_dir=self.state_dir, now=now - timedelta(hours=2))
        ba.record_attempt_result(start["attempt_id"], "failed", "pg_dump", exit_code=1, state_dir=self.state_dir, now=now - timedelta(hours=2))
        self._success(now=now - timedelta(minutes=5))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertIn(status, ("ok", "warning"))  # never critical -- the failure is now stale

    def test_dirty_success_is_warning(self):
        dirty_git = dict(CLEAN_GIT, dirty=True)
        self._success(git_state=dirty_git)
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "warning")

    def test_unknown_dirty_is_warning(self):
        unknown_git = dict(CLEAN_GIT, dirty=None)
        self._success(git_state=unknown_git)
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "warning")

    def test_stuck_running_over_2h_is_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        ba.record_attempt_start("pg_dump", git_state=CLEAN_GIT, state_dir=self.state_dir, now=now - timedelta(hours=3))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_fresh_running_with_valid_prior_success_is_not_false_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        ba.record_attempt_start("pg_dump", git_state=CLEAN_GIT, state_dir=self.state_dir, now=now - timedelta(minutes=2))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertNotEqual(status, "critical")

    def test_weekly_8_day_warning(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        self._rt_success(now=now - timedelta(days=9))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "warning")

    def test_weekly_14_day_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        self._rt_success(now=now - timedelta(days=15))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_explicit_weekly_failure_is_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now - timedelta(hours=1))
        self._rt_success(now=now - timedelta(days=2))
        start = ba.record_roundtrip_attempt_start("downloading", state_dir=self.state_dir, now=now - timedelta(minutes=10))
        ba.record_roundtrip_attempt_result(start["attempt_id"], "failed", "hash_verify", exit_code=1, state_dir=self.state_dir, now=now)
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_malformed_initialized_receipt_is_critical(self):
        self._success()
        (self.state_dir / ba.SUCCESS_FILE).write_text('{"broken": true}', encoding="utf-8")
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "critical")

    def test_future_timestamp_is_critical(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        self._success(now=now + timedelta(hours=6))
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir, now=now)
        self.assertEqual(status, "critical")

    def test_no_roundtrip_yet_is_warning_not_critical(self):
        self._success()
        status, _ = ba.evaluate_backup_health(state_dir=self.state_dir)
        self.assertEqual(status, "warning")
