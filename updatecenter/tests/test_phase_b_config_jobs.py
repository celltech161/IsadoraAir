import json
import os
from pathlib import Path
import tempfile
import uuid

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import ConfigError, load_config, validate_config_dict
from isadoraair_updater.jobs import JobError, JobStore


class RootConfigTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = config_dict(self.root, str(self.root / "upstream.git"))

    def tearDown(self):
        self.temp.cleanup()

    def test_local_upstream_requires_explicit_test_mode(self):
        with self.assertRaises(ConfigError):
            validate_config_dict(self.data)
        config = validate_config_dict(self.data, allow_local_repository=True)
        self.assertEqual(config.trusted_branch, "main")

    def test_unknown_command_or_path_field_rejected(self):
        for field in ("command", "systemctl", "destination", "target_sha"):
            changed = dict(self.data)
            changed[field] = "/bin/sh"
            with self.assertRaises(ConfigError):
                validate_config_dict(changed, allow_local_repository=True)

    def test_protected_path_overlap_with_app_rejected(self):
        self.data["jobs_root"] = str(Path(self.data["application_root"]) / "jobs")
        with self.assertRaises(ConfigError):
            validate_config_dict(self.data, allow_local_repository=True)

    def test_root_controlled_paths_cannot_overlap_each_other(self):
        self.data["jobs_root"] = str(Path(self.data["trusted_repository"]) / "jobs")
        with self.assertRaises(ConfigError):
            validate_config_dict(self.data, allow_local_repository=True)

    def test_non_loopback_health_rejected(self):
        self.data["gunicorn_health_url"] = "https://example.com/"
        with self.assertRaises(ConfigError):
            validate_config_dict(self.data, allow_local_repository=True)

    def test_symlink_config_refused(self):
        real = self.root / "real.json"
        real.write_text(json.dumps(self.data), encoding="utf-8")
        link = self.root / "station.json"
        link.symlink_to(real)
        with self.assertRaises(ConfigError):
            load_config(link, enforce_protection=False, allow_local_repository=True)

    def test_group_writable_root_config_rejected(self):
        path = self.root / "station.json"
        path.write_text(json.dumps(self.data), encoding="utf-8")
        path.chmod(0o620)
        with self.assertRaises(ConfigError):
            load_config(path, enforce_protection=True, allow_local_repository=True)


class DurableJobTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = JobStore(root / "jobs", root / "logs", acquire_daemon_lock=False)
        self.job = str(uuid.uuid4())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_same_job_is_idempotent(self):
        first, created = self.store.accept(self.job, "r0003", "a" * 64)
        second, created_again = self.store.accept(self.job, "r0003", "a" * 64)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["created_at"], second["created_at"])

    def test_same_id_different_facts_rejected(self):
        self.store.accept(self.job, "r0003", "a" * 64)
        with self.assertRaises(JobError):
            self.store.accept(self.job, "r0004", "b" * 64)

    def test_different_concurrent_job_rejected(self):
        self.store.accept(self.job, "r0003", "a" * 64)
        with self.assertRaises(JobError):
            self.store.accept(str(uuid.uuid4()), "r0003", "a" * 64)

    def test_terminal_job_releases_root_side_concurrency(self):
        self.store.accept(self.job, "r0003", "a" * 64)
        self.store.succeed(self.job)
        other, created = self.store.accept(str(uuid.uuid4()), "r0004", "b" * 64)
        self.assertTrue(created)

    def test_state_survives_store_reload(self):
        self.store.accept(self.job, "r0003", "a" * 64)
        self.store.milestone(self.job, "target_staged")
        reloaded = JobStore(self.store.jobs_root, self.store.logs_root, acquire_daemon_lock=False)
        try:
            self.assertIn("target_staged", reloaded.load(self.job)["milestones"])
        finally:
            reloaded.close()

    def test_log_retrieval_is_tail_bounded(self):
        self.store.accept(self.job, "r0003", "a" * 64)
        for _ in range(30):
            self.store.append_log(self.job, "x" * 100)
        self.assertLessEqual(len(self.store.tail_log(self.job, 128).encode()), 128)

    def test_job_paths_cannot_be_supplied(self):
        with self.assertRaises(JobError):
            self.store.load("../../etc/passwd")
