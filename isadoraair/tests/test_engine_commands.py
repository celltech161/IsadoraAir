import fcntl
import json
import multiprocessing
import os
import tempfile
from pathlib import Path
from unittest import TestCase

from isadoraair.engine_commands import (
    EngineCommandQueueFull,
    EngineCommandValidationError,
    consume_engine_command_file,
    enqueue_engine_command,
    list_committed_engine_commands,
)


def _enqueue_in_process(queue_dir, lock_path, index, start, results, max_depth=64):
    start.wait()
    try:
        path = enqueue_engine_command(
            {"command": "test", "index": index},
            queue_dir=queue_dir,
            lock_path=lock_path,
            max_depth=max_depth,
        )
        results.put(("ok", index, path.name))
    except EngineCommandQueueFull:
        results.put(("full", index, None))
    except Exception as exc:  # pragma: no cover - reported by parent assertion
        results.put(("error", index, repr(exc)))


def _exit_while_holding_lock(lock_path, ready):
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()
    os._exit(0)


class EngineCommandWriterTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue_dir = self.root / "engine_cmd.d"
        self.lock_path = self.root / "engine_cmd.lock"

    def enqueue(self, payload, **kwargs):
        return enqueue_engine_command(
            payload,
            queue_dir=self.queue_dir,
            lock_path=self.lock_path,
            **kwargs,
        )

    def list(self, **kwargs):
        return list_committed_engine_commands(
            queue_dir=self.queue_dir, lock_path=self.lock_path, **kwargs
        )

    def test_valid_payload_is_one_committed_json_object(self):
        payload = {"command": "seek", "position": 12.5}
        path = self.enqueue(payload)
        self.assertEqual(self.list(), [path])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
        self.assertEqual(list(self.queue_dir.glob("*.json")), [path])

    def test_queue_directory_creation_is_idempotent(self):
        first = self.enqueue({"command": "one"})
        second = self.enqueue({"command": "two"})
        self.assertTrue(self.queue_dir.is_dir())
        self.assertEqual(self.list(), [first, second])

    def test_incomplete_temp_is_never_visible_as_committed(self):
        self.queue_dir.mkdir()
        temp = self.queue_dir / (
            ".cmd-00000000000000000001-123-" + ("a" * 32) + ".tmp"
        )
        temp.write_text('{"command":', encoding="utf-8")
        self.assertEqual(self.list(), [])

    def test_oversized_payload_is_rejected_without_artifact(self):
        with self.assertRaises(EngineCommandValidationError):
            self.enqueue({"command": "x", "value": "0123456789"}, max_payload_bytes=10)
        self.assertEqual(self.list(), [])

    def test_non_dict_payload_is_rejected(self):
        with self.assertRaises(EngineCommandValidationError):
            self.enqueue(["not", "an", "object"])

    def test_missing_or_blank_command_is_rejected(self):
        for payload in ({}, {"command": ""}, {"command": "   "}, {"command": 3}):
            with self.subTest(payload=payload):
                with self.assertRaises(EngineCommandValidationError):
                    self.enqueue(payload)

    def test_exact_depth_limit_is_enforced(self):
        self.enqueue({"command": "one"}, max_depth=2)
        self.enqueue({"command": "two"}, max_depth=2)
        with self.assertRaises(EngineCommandQueueFull):
            self.enqueue({"command": "three"}, max_depth=2)
        self.assertEqual(len(self.list()), 2)

    def test_full_queue_rejects_new_without_deleting_old(self):
        old = self.enqueue({"command": "old"}, max_depth=1)
        with self.assertRaises(EngineCommandQueueFull):
            self.enqueue({"command": "new"}, max_depth=1)
        self.assertEqual(self.list(), [old])
        self.assertEqual(json.loads(old.read_text()), {"command": "old"})

    def test_temp_artifacts_do_not_count_toward_capacity(self):
        self.queue_dir.mkdir()
        temp = self.queue_dir / (
            ".cmd-00000000000000000001-123-" + ("b" * 32) + ".tmp"
        )
        temp.write_text("partial")
        path = self.enqueue({"command": "valid"}, max_depth=1)
        self.assertEqual(self.list(), [path])

    def test_stale_owned_temp_is_cleaned(self):
        self.queue_dir.mkdir()
        temp = self.queue_dir / (
            ".cmd-00000000000000000001-123-" + ("c" * 32) + ".tmp"
        )
        temp.write_text("partial")
        self.enqueue({"command": "valid"})
        self.assertFalse(temp.exists())

    def test_unknown_file_is_not_deleted(self):
        self.queue_dir.mkdir()
        unknown = self.queue_dir / "operator-note.txt"
        unknown.write_text("keep me")
        self.enqueue({"command": "valid"})
        self.list()
        self.assertEqual(unknown.read_text(), "keep me")

    def test_consumer_rejects_unknown_name_without_deleting_it(self):
        self.queue_dir.mkdir()
        unknown = self.queue_dir / "not-ours.json"
        unknown.write_text('{"command":"x"}')
        with self.assertRaises(EngineCommandValidationError):
            consume_engine_command_file(unknown)
        self.assertTrue(unknown.exists())

    def test_scanner_does_not_follow_committed_name_symlink(self):
        self.queue_dir.mkdir()
        target = self.root / "outside.json"
        target.write_text('{"command":"outside"}', encoding="utf-8")
        link = self.queue_dir / (
            "cmd-00000000000000000001-123-" + ("e" * 32) + ".json"
        )
        link.symlink_to(target)
        self.assertEqual(self.list(), [])
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), '{"command":"outside"}')


class EngineCommandConcurrencyTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.queue_dir = self.root / "engine_cmd.d"
        self.lock_path = self.root / "engine_cmd.lock"
        self.context = multiprocessing.get_context("fork")

    def _run_writers(self, count, max_depth=64):
        start = self.context.Event()
        results = self.context.Queue()
        processes = [
            self.context.Process(
                target=_enqueue_in_process,
                args=(
                    str(self.queue_dir), str(self.lock_path), index,
                    start, results, max_depth,
                ),
            )
            for index in range(count)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        return [results.get(timeout=2) for _ in processes]

    def test_simultaneous_writers_commit_unique_files_without_overwrite(self):
        results = self._run_writers(12)
        self.assertEqual({result[0] for result in results}, {"ok"})
        paths = list_committed_engine_commands(
            queue_dir=self.queue_dir, lock_path=self.lock_path
        )
        self.assertEqual(len(paths), 12)
        self.assertEqual(len({path.name for path in paths}), 12)
        payload_indexes = {
            json.loads(path.read_text(encoding="utf-8"))["index"] for path in paths
        }
        self.assertEqual(payload_indexes, set(range(12)))

    def test_committed_sequence_is_strictly_increasing(self):
        self._run_writers(10)
        paths = list_committed_engine_commands(
            queue_dir=self.queue_dir, lock_path=self.lock_path
        )
        sequences = [int(path.name.split("-", 2)[1]) for path in paths]
        self.assertEqual(sequences, sorted(sequences))
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_capacity_race_cannot_exceed_limit(self):
        results = self._run_writers(12, max_depth=3)
        self.assertEqual(sum(result[0] == "ok" for result in results), 3)
        self.assertEqual(sum(result[0] == "full" for result in results), 9)
        self.assertNotIn("error", {result[0] for result in results})
        self.assertEqual(
            len(list_committed_engine_commands(
                queue_dir=self.queue_dir, lock_path=self.lock_path
            )),
            3,
        )

    def test_process_exit_releases_flock_for_future_writer(self):
        ready = self.context.Event()
        process = self.context.Process(
            target=_exit_while_holding_lock, args=(str(self.lock_path), ready)
        )
        process.start()
        self.assertTrue(ready.wait(5))
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        path = enqueue_engine_command(
            {"command": "after_crash"},
            queue_dir=self.queue_dir,
            lock_path=self.lock_path,
        )
        self.assertTrue(path.exists())
