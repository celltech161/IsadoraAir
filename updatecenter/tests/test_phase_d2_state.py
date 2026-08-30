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
    ActivationTransaction, IndeterminateStateWriteError, RuntimeState, StateError,
    parse_runtime_state_dict, read_runtime_state, write_runtime_state_atomically,
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

    # ---- D2 corrective review, Correction 2: 7 distinguished boundaries ----

    def test_1_failure_before_temporary_write_completion(self):
        # os.fdopen's handle.write() raising mid-way -- destination
        # provably untouched (os.replace() never ran at all).
        with mock.patch("os.fdopen") as fdopen:
            handle = mock.MagicMock()
            handle.write.side_effect = OSError("simulated disk full")
            fdopen.return_value.__enter__.return_value = handle
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_2_file_fsync_failure(self):
        # The FIRST fsync call (the temp file's own bytes) -- also
        # strictly before os.replace(), so the destination is provably
        # untouched, same guarantee as case 1.
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_2b_file_fsync_failure_does_not_raise_indeterminate(self):
        # Specifically NOT IndeterminateStateWriteError -- that type is
        # reserved for a post-replace failure only; a pre-replace
        # failure of any kind is an ordinary, safely-retryable OSError.
        with mock.patch("os.fsync", side_effect=OSError("simulated fsync failure")):
            try:
                write_runtime_state_atomically(self.path, self._state())
            except IndeterminateStateWriteError:
                self.fail("a pre-replace failure must never raise IndeterminateStateWriteError")
            except OSError:
                pass

    def test_3_replace_failure(self):
        with mock.patch("os.replace", side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state())
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_3b_existing_destination_provably_untouched_if_replace_itself_fails(self):
        write_runtime_state_atomically(self.path, self._state())
        original_bytes = self.path.read_bytes()
        with mock.patch("os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state(active_generation=99))
        self.assertEqual(self.path.read_bytes(), original_bytes)

    def test_4_parent_directory_fsync_failure_after_replace_is_indeterminate(self):
        # os.replace() ALREADY succeeded by the time directory fsync is
        # attempted -- this is the case Correction 2 exists for. Must
        # raise the DISTINCT IndeterminateStateWriteError, never a bare
        # OSError indistinguishable from cases 1-3, and this function
        # must never claim (by return value, by exception type, or by
        # any side effect) that the OLD destination content is "still
        # the current state" -- it is not; the rename already happened.
        original_fsync = os.fsync
        call_count = {"n": 0}

        def fsync_second_call_fails(fd):
            call_count["n"] += 1
            if call_count["n"] == 2:  # first call is inside the write; second is the directory fsync
                raise OSError("simulated directory fsync failure")
            return original_fsync(fd)

        with mock.patch("os.fsync", side_effect=fsync_second_call_fails):
            with self.assertRaises(IndeterminateStateWriteError) as ctx:
                write_runtime_state_atomically(self.path, self._state())
        # The new bytes ARE visible to this process (the rename really
        # did happen) -- but this test asserts only what the exception
        # itself communicates, never treats file presence as proof of
        # crash-survivability.
        self.assertEqual(ctx.exception.path, self.path)
        self.assertIsInstance(ctx.exception.original_error, OSError)

    def test_4b_indeterminate_error_never_raised_for_a_clean_write(self):
        # Sanity: the distinct exception type is not accidentally
        # raised on the ordinary success path.
        try:
            write_runtime_state_atomically(self.path, self._state())
        except IndeterminateStateWriteError:
            self.fail("a fully successful write must never raise IndeterminateStateWriteError")

    def test_5_restart_with_old_complete_state_present(self):
        # Simulates: a write never even started (or failed cleanly per
        # cases 1-3) -- on "restart," the file on disk is exactly the
        # previously-written, complete, valid state.
        original = self._state(active_generation=4)
        write_runtime_state_atomically(self.path, original)
        with mock.patch("os.replace", side_effect=OSError("simulated failure -- write never landed")):
            with self.assertRaises(OSError):
                write_runtime_state_atomically(self.path, self._state(active_generation=99))
        reloaded = read_runtime_state(self.path)
        self.assertEqual(reloaded, original)

    def test_6_restart_with_new_complete_state_present(self):
        # Simulates: a write fully succeeded (all 7 steps) before
        # "restart" -- the file on disk is exactly the NEW state.
        write_runtime_state_atomically(self.path, self._state(active_generation=4))
        new_state = self._state(active_generation=5)
        write_runtime_state_atomically(self.path, new_state)
        reloaded = read_runtime_state(self.path)
        self.assertEqual(reloaded, new_state)

    def test_7_malformed_or_inconsistent_resulting_state_never_reconstructed_as_valid(self):
        # Simulates a genuinely corrupted/foreign file at the state
        # path (never produced by write_runtime_state_atomically
        # itself, which is atomic by construction -- but read_runtime_
        # state()/parse_runtime_state_dict() must still fail closed
        # against ANY malformed content reaching that path by some
        # other means, rather than attempting to salvage a partial
        # parse into a "best guess" RuntimeState).
        self.path.write_text('{"schema_version": 1, "active_slot": "A"', encoding="utf-8")  # truncated JSON
        with self.assertRaises(ValueError):
            read_runtime_state(self.path)

    def test_7b_structurally_valid_json_but_semantically_inconsistent_state_rejected(self):
        # Valid JSON, but internally contradictory (previous_slot ==
        # active_slot) -- parse_runtime_state_dict's own consistency
        # checks (ParseRuntimeStateDictTests above) must still apply
        # when reached via the file-reading path, not just the
        # dict-in-memory path.
        import json
        contradictory = {
            "schema_version": 1, "active_slot": "A", "active_generation": 4,
            "active_descriptor_sha256": "a" * 64, "previous_slot": "A",
            "previous_generation": 3, "previous_descriptor_sha256": "b" * 64, "activation": None,
        }
        self.path.write_text(json.dumps(contradictory), encoding="utf-8")
        with self.assertRaises(StateError):
            read_runtime_state(self.path)
