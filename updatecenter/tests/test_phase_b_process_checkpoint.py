import datetime as dt
import json
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.checkpoint import create_checkpoint, prune_checkpoints, verify_checkpoint
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.process import CommandRunner, ProcessResult


class ProcessBoundsTests(SimpleTestCase):
    def test_stdout_capture_is_actually_bounded(self):
        runner = CommandRunner()
        result = runner.run(
            ["/usr/bin/python3", "-c", "import sys; sys.stdout.write('x'*100000)"],
            timeout=10, output_limit=1024,
        )
        self.assertEqual(len(result.stdout), 1024)
        self.assertTrue(result.output_truncated)
        self.assertFalse(result.ok)

    def test_timeout_kills_and_reaps(self):
        result = CommandRunner().run(["/usr/bin/python3", "-c", "import time; time.sleep(30)"], timeout=0.05)
        self.assertTrue(result.timed_out)
        self.assertIsNotNone(result.returncode)

    def test_shell_metacharacters_remain_plain_argv(self):
        result = CommandRunner().run(["/usr/bin/printf", "%s", "$(touch /tmp/must-not-exist-phase-b)"], timeout=5)
        self.assertEqual(result.stdout, b"$(touch /tmp/must-not-exist-phase-b)")


class FakeDumpRunner(CommandRunner):
    def __init__(self, content=b"valid-dump", *, success=True):
        super().__init__(runuser_path="/usr/sbin/runuser")
        self.content = content
        self.success = success
        self.calls = []

    def run_to_file(self, argv, destination, **kwargs):
        self.calls.append((list(argv), dict(kwargs)))
        if self.content:
            Path(destination).write_bytes(self.content)
        return ProcessResult(tuple(argv), 0 if self.success else 1, b"", b"dump failed" if not self.success else b"")


class CheckpointTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, runner=None, job_id="123e4567-e89b-42d3-a456-426614174000"):
        return create_checkpoint(
            self.config, runner or FakeDumpRunner(), job_id=job_id,
            installed_release="r0002", installed_commit="a" * 40,
            target_release="r0003", target_commit="b" * 40,
        )

    def test_valid_checkpoint_has_hash_size_metadata_and_private_mode(self):
        metadata = self._create()
        self.assertTrue(verify_checkpoint(self.config.checkpoint_root, metadata))
        dump = self.config.checkpoint_root / metadata["dump_file"]
        self.assertEqual(dump.stat().st_mode & 0o777, 0o600)
        self.assertEqual(metadata["size_bytes"], len(b"valid-dump"))
        self.assertEqual(len(metadata["sha256"]), 64)

    def test_credentials_never_enter_argv(self):
        data = config_dict(self.root, str(self.root / "upstream.git"))
        data["database"]["pgpass_file"] = str(self.root / "home" / ".pgpass")
        Path(data["database"]["pgpass_file"]).parent.mkdir(parents=True)
        Path(data["database"]["pgpass_file"]).write_text("secret-password", encoding="utf-8")
        config = validate_config_dict(data, allow_local_repository=True)
        runner = FakeDumpRunner()
        create_checkpoint(
            config, runner, job_id="123e4567-e89b-42d3-a456-426614174000",
            installed_release="r0002", installed_commit="a" * 40,
            target_release="r0003", target_commit="b" * 40,
        )
        argv, kwargs = runner.calls[0]
        self.assertNotIn("secret-password", " ".join(argv))
        self.assertEqual(kwargs["env"], {"PGPASSFILE": data["database"]["pgpass_file"]})

    def test_failed_or_empty_dump_is_not_promoted(self):
        for runner in (FakeDumpRunner(success=False), FakeDumpRunner(content=b"")):
            with self.assertRaises(Exception):
                self._create(runner)
            self.assertFalse(list(self.config.checkpoint_root.glob("*.json")))

    def test_tampering_invalidates_checkpoint(self):
        metadata = self._create()
        dump = self.config.checkpoint_root / metadata["dump_file"]
        dump.write_bytes(b"tampered")
        self.assertFalse(verify_checkpoint(self.config.checkpoint_root, metadata))

    def test_retention_max_five_and_newest_always_preserved(self):
        root = self.config.checkpoint_root
        root.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        for index in range(7):
            dump = root / f"d{index}.dump"
            dump.write_bytes(f"dump-{index}".encode())
            import hashlib
            created = now - dt.timedelta(days=index)
            metadata = {
                "valid": True, "created_at": created.isoformat(), "dump_file": dump.name,
                "size_bytes": dump.stat().st_size, "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            }
            (root / f"d{index}.json").write_text(json.dumps(metadata), encoding="utf-8")
        prune_checkpoints(root, now=now)
        self.assertEqual(len(list(root.glob("*.json"))), 5)
        self.assertTrue((root / "d0.json").exists())

    def test_only_old_newest_is_preserved(self):
        root = self.config.checkpoint_root
        root.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        dump = root / "old.dump"
        dump.write_bytes(b"old")
        import hashlib
        (root / "old.json").write_text(json.dumps({
            "valid": True, "created_at": (now - dt.timedelta(days=100)).isoformat(),
            "dump_file": dump.name, "size_bytes": 3,
            "sha256": hashlib.sha256(b"old").hexdigest(),
        }), encoding="utf-8")
        prune_checkpoints(root, now=now)
        self.assertTrue(dump.exists())
