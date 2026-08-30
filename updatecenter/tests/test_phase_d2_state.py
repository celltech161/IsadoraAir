"""D2-E: runtime state schema. D2-F: its durable atomic writer, with
fault-injection tests at each meaningful boundary rather than fragile
timing sleeps."""
import os
from pathlib import Path
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.activation import ActivationPhase
from isadoraair_updater_bootstrap.slots import Slot
from isadoraair_updater_bootstrap.state import (
    ActivationTransaction, RuntimeState, StateError, parse_runtime_state_dict,
    read_runtime_state, write_runtime_state_atomically,
)

VALID_UUID = "12345678-1234-4123-8123-123456789abc"


def _valid_state_dict(**overrides):
    data = {
        "schema_version": 1,
        "active_slot": "A",
        "active_generation": 4,
        "active_descriptor_sha256": "a" * 64,
        "previous_slot": None,
        "previous_generation": None,
        "previous_descriptor_sha256": None,
        "activation": None,
    }
    data.update(overrides)
    return data


class ParseRuntimeStateDictTests(SimpleTestCase):
    def test_valid_idle_state_parses(self):
        state = parse_runtime_state_dict(_valid_state_dict())
        self.assertIs(state.active_slot, Slot.A)
        self.assertEqual(state.active_generation, 4)
        self.assertIsNone(state.activation)

    def test_valid_state_with_previous_lkg_parses(self):
        state = parse_runtime_state_dict(_valid_state_dict(
            previous_slot="B", previous_generation=3, previous_descriptor_sha256="b" * 64,
        ))
        self.assertIs(state.previous_slot, Slot.B)
        self.assertEqual(state.previous_generation, 3)

    def test_unknown_top_level_field_rejected(self):
        data = _valid_state_dict()
        data["extra"] = 1
        with self.assertRaises(StateError):
            parse_runtime_state_dict(data)

    def test_missing_field_rejected(self):
        data = _valid_state_dict()
        del data["previous_slot"]
        with self.assertRaises(StateError):
            parse_runtime_state_dict(data)

    def test_bad_slot_value_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(active_slot="C"))

    def test_zero_active_generation_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(active_generation=0))

    def test_bad_sha_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(active_descriptor_sha256="not-hex"))

    def test_partial_previous_fields_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(previous_slot="B"))

    def test_previous_slot_equal_to_active_slot_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(
                previous_slot="A", previous_generation=3, previous_descriptor_sha256="b" * 64,
            ))

    def test_previous_generation_not_older_than_active_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(
                previous_slot="B", previous_generation=4, previous_descriptor_sha256="b" * 64,
            ))
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(
                previous_slot="B", previous_generation=5, previous_descriptor_sha256="b" * 64,
            ))

    def test_valid_activation_transaction_parses(self):
        state = parse_runtime_state_dict(_valid_state_dict(activation={
            "transaction_id": VALID_UUID, "candidate_slot": "B", "candidate_generation": 5,
            "candidate_descriptor_sha256": "c" * 64, "phase": "candidate_staged",
        }))
        self.assertEqual(state.activation.transaction_id, VALID_UUID)
        self.assertIs(state.activation.phase, ActivationPhase.CANDIDATE_STAGED)

    def test_activation_candidate_slot_equal_to_active_slot_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(activation={
                "transaction_id": VALID_UUID, "candidate_slot": "A", "candidate_generation": 5,
                "candidate_descriptor_sha256": "c" * 64, "phase": "candidate_staged",
            }))

    def test_activation_candidate_generation_not_newer_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(activation={
                "transaction_id": VALID_UUID, "candidate_slot": "B", "candidate_generation": 4,
                "candidate_descriptor_sha256": "c" * 64, "phase": "candidate_staged",
            }))

    def test_activation_bad_uuid_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(activation={
                "transaction_id": "not-a-uuid", "candidate_slot": "B", "candidate_generation": 5,
                "candidate_descriptor_sha256": "c" * 64, "phase": "candidate_staged",
            }))

    def test_activation_uppercase_uuid_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(activation={
                "transaction_id": VALID_UUID.upper(), "candidate_slot": "B", "candidate_generation": 5,
                "candidate_descriptor_sha256": "c" * 64, "phase": "candidate_staged",
            }))

    def test_activation_bad_phase_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(activation={
                "transaction_id": VALID_UUID, "candidate_slot": "B", "candidate_generation": 5,
                "candidate_descriptor_sha256": "c" * 64, "phase": "not_a_real_phase",
            }))

    def test_unsupported_schema_version_rejected(self):
        with self.assertRaises(StateError):
            parse_runtime_state_dict(_valid_state_dict(schema_version=2))


class AtomicStateWriterTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime-state.json"

    def _state(self, **overrides) -> RuntimeState:
        base = dict(
            schema_version=1, active_slot=Slot.A, active_generation=4,
            active_descriptor_sha256="a" * 64, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        )
        base.update(overrides)
        return RuntimeState(**base)

    def test_write_then_read_round_trips(self):
        state = self._state()
        write_runtime_state_atomically(self.path, state)
        reloaded = read_runtime_state(self.path)
        self.assertEqual(reloaded, state)

    def test_written_file_is_mode_0600(self):
        write_runtime_state_atomically(self.path, self._state())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_no_temp_file_left_behind_on_success(self):
        write_runtime_state_atomically(self.path, self._state())
        leftovers = [p for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [])

    def test_second_write_fully_replaces_first(self):
        write_runtime_state_atomically(self.path, self._state())
        second = self._state(active_generation=5, activation=None)
        write_runtime_state_atomically(self.path, second)
        reloaded = read_runtime_state(self.path)
        self.assertEqual(reloaded.active_generation, 5)

    def test_failure_during_write_leaves_no_partial_temp_file(self):
        # Fault injection at the "write complete bytes" boundary --
        # os.fdopen's handle.write() raising mid-way must not leave a
        # temp file behind, and must never touch the real destination.
        with mock.patch("os.fdopen") as fdopen:
            handle = mock.MagicMock()
            handle.write.side_effect = OSError("simulated disk full")
            fdopen.return_value.__enter__.return_value = handle
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        leftovers = list(self.path.parent.iterdir())
        self.assertEqual(leftovers, [])

    def test_failure_during_fsync_leaves_no_partial_temp_file(self):
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        leftovers = list(self.path.parent.iterdir())
        self.assertEqual(leftovers, [])

    def test_failure_during_replace_leaves_no_partial_temp_file_and_no_destination(self):
        with mock.patch("os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        leftovers = list(self.path.parent.iterdir())
        self.assertEqual(leftovers, [])

    def test_failure_after_replace_before_directory_fsync_still_leaves_file_durable_on_disk(self):
        # os.replace() already happened by the time directory fsync is
        # attempted -- the RENAME is done at the filesystem level even
        # if this process then fails to fsync the parent (a real crash
        # here risks losing durability of the rename on SOME mount
        # options, which is exactly why this step exists at all -- but
        # the file itself, as observed by THIS still-running process,
        # is already present).
        original_fsync = os.fsync
        call_count = {"n": 0}

        def fsync_second_call_fails(fd):
            call_count["n"] += 1
            if call_count["n"] == 2:  # first call is inside the write; second is the directory fsync
                raise OSError("simulated directory fsync failure")
            return original_fsync(fd)

        with mock.patch("os.fsync", side_effect=fsync_second_call_fails):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertTrue(self.path.exists())

    def test_existing_destination_untouched_if_write_fails_before_replace(self):
        write_runtime_state_atomically(self.path, self._state())
        original_bytes = self.path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state(active_generation=99))
        self.assertEqual(self.path.read_bytes(), original_bytes)
