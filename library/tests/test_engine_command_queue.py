import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from isadoraair import engine_commands
from isadoraair.engine_commands import enqueue_engine_command
from library.services import engine as engine_module


class EngineCommandConsumerTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.queue_dir = root / "engine_cmd.d"
        self.lock_path = root / "engine_cmd.lock"
        self.legacy_path = root / "engine_cmd.json"
        self.patchers = [
            patch.object(engine_commands, "ENGINE_COMMAND_QUEUE_DIR", self.queue_dir),
            patch.object(engine_commands, "ENGINE_COMMAND_LOCK_PATH", self.lock_path),
            patch.object(engine_module, "CMD_PATH", self.legacy_path),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _enqueue(self, command, **fields):
        return enqueue_engine_command({"command": command, **fields})

    def _engine_with_collector(self, side_effect=None):
        engine = object.__new__(engine_module.PlaybackEngine)
        engine._dispatch_engine_command = MagicMock(side_effect=side_effect)
        return engine

    def test_one_queued_command_dispatches_and_is_removed(self):
        engine = object.__new__(engine_module.PlaybackEngine)
        engine._set_mic_ptt = MagicMock()
        path = self._enqueue("mic_ptt", active=True)
        engine._check_commands()
        engine._set_mic_ptt.assert_called_once_with(True)
        self.assertFalse(path.exists())

    def test_multiple_commands_dispatch_fifo_preserving_boolean_edges(self):
        engine = self._engine_with_collector()
        self._enqueue("remote_dj_gate", active=True)
        self._enqueue("remote_dj_gate", active=False)
        self._enqueue("remote_dj_gate", active=True)
        engine._check_commands()
        self.assertEqual(
            [call.args[0]["active"] for call in engine._dispatch_engine_command.call_args_list],
            [True, False, True],
        )

    def test_per_poll_batch_bound_and_later_poll_progress(self):
        engine = self._engine_with_collector()
        for index in range(5):
            self._enqueue("test", index=index)
        with patch.object(engine_module, "ENGINE_COMMAND_BATCH_SIZE", 2):
            engine._check_commands()
            self.assertEqual(engine._dispatch_engine_command.call_count, 2)
            self.assertEqual(len(list(self.queue_dir.glob("*.json"))), 3)
            engine._check_commands()
            self.assertEqual(engine._dispatch_engine_command.call_count, 4)
            self.assertEqual(len(list(self.queue_dir.glob("*.json"))), 1)
            engine._check_commands()
        self.assertEqual(engine._dispatch_engine_command.call_count, 5)
        self.assertEqual(list(self.queue_dir.glob("*.json")), [])

    def test_malformed_committed_entry_does_not_block_later_valid_entry(self):
        self.queue_dir.mkdir()
        malformed = self.queue_dir / (
            "cmd-00000000000000000001-123-" + ("d" * 32) + ".json"
        )
        malformed.write_text("{not-json", encoding="utf-8")
        valid = self._enqueue("valid")
        engine = self._engine_with_collector()
        engine._check_commands()
        engine._dispatch_engine_command.assert_called_once_with({"command": "valid"})
        self.assertFalse(malformed.exists())
        self.assertFalse(valid.exists())

    def test_handler_exception_does_not_block_later_command(self):
        seen = []

        def dispatch(payload):
            seen.append(payload["command"])
            if payload["command"] == "broken":
                raise RuntimeError("handler failed")

        engine = self._engine_with_collector(side_effect=dispatch)
        first = self._enqueue("broken")
        second = self._enqueue("later")
        engine._check_commands()
        self.assertEqual(seen, ["broken", "later"])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())

    def test_committed_command_survives_engine_absence_until_later_poll(self):
        path = self._enqueue("waiting")
        self.assertTrue(path.exists())
        engine = self._engine_with_collector()
        engine._check_commands()
        engine._dispatch_engine_command.assert_called_once_with({"command": "waiting"})


class LegacyEngineCommandCompatibilityTests(SimpleTestCase):
    setUp = EngineCommandConsumerTests.setUp
    _enqueue = EngineCommandConsumerTests._enqueue
    _engine_with_collector = EngineCommandConsumerTests._engine_with_collector

    def _write_legacy(self, payload):
        self.legacy_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_legacy_command_dispatches_and_is_unlinked(self):
        engine = self._engine_with_collector()
        self._write_legacy({"command": "legacy"})
        engine._check_commands()
        engine._dispatch_engine_command.assert_called_once_with({"command": "legacy"})
        self.assertFalse(self.legacy_path.exists())

    def test_malformed_legacy_does_not_block_queue(self):
        engine = self._engine_with_collector()
        self.legacy_path.write_text("{broken", encoding="utf-8")
        self._enqueue("queued")
        engine._check_commands()
        engine._dispatch_engine_command.assert_called_once_with({"command": "queued"})
        self.assertFalse(self.legacy_path.exists())

    def test_legacy_is_dispatched_before_fifo_queue_batch(self):
        engine = self._engine_with_collector()
        self._write_legacy({"command": "legacy"})
        self._enqueue("queued-one")
        self._enqueue("queued-two")
        engine._check_commands()
        self.assertEqual(
            [call.args[0]["command"] for call in engine._dispatch_engine_command.call_args_list],
            ["legacy", "queued-one", "queued-two"],
        )

    def test_repeated_legacy_arrivals_cannot_starve_queue(self):
        engine = self._engine_with_collector()
        for index in range(3):
            self._enqueue("queued", index=index)
        with patch.object(engine_module, "ENGINE_COMMAND_BATCH_SIZE", 1):
            for index in range(3):
                self._write_legacy({"command": "legacy", "index": index})
                engine._check_commands()
        commands = [
            call.args[0]["command"]
            for call in engine._dispatch_engine_command.call_args_list
        ]
        self.assertEqual(commands, ["legacy", "queued"] * 3)

    def test_queue_backlog_cannot_starve_legacy(self):
        engine = self._engine_with_collector()
        for index in range(4):
            self._enqueue("queued", index=index)
        self._write_legacy({"command": "legacy"})
        with patch.object(engine_module, "ENGINE_COMMAND_BATCH_SIZE", 1):
            engine._check_commands()
        first_two = [
            call.args[0]["command"]
            for call in engine._dispatch_engine_command.call_args_list
        ]
        self.assertEqual(first_two, ["legacy", "queued"])

    def test_multiple_legacy_writes_remain_last_write_wins(self):
        engine = self._engine_with_collector()
        self._write_legacy({"command": "first"})
        self._write_legacy({"command": "second"})
        engine._check_commands()
        engine._dispatch_engine_command.assert_called_once_with({"command": "second"})
