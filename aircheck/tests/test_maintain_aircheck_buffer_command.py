"""Tests for the maintain_aircheck_buffer management command.

Specifically guards against a real bug found while wiring up the
buffer-maintenance heartbeat: importing AIRCHECK_CURRENT_PATH/
AIRCHECK_IDLE_BUFFER_MAX_BYTES as bare names at module import time
(`from aircheck.services.recorder import AIRCHECK_CURRENT_PATH`) binds
a local copy that a test's `patch.object(recorder, "AIRCHECK_CURRENT_PATH",
...)` never reaches -- the command would silently keep statting the
real production path for its heartbeat's size_bytes. Fixed by reading
these as recorder.X at call time instead; these tests would have
failed against the old bare-import version.
"""
import json
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from aircheck.services import recorder
from aircheck.tests.test_recorder import AircheckRecorderTestBase


class MaintainAircheckBufferCommandTests(AircheckRecorderTestBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("maintain_aircheck_buffer", *args, stdout=out)
        return out.getvalue()

    def _read_heartbeat(self):
        with open(self.buffer_state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_heartbeat_uses_the_patched_working_file_not_the_real_one(self):
        self.write_working_file(500)
        output = self.run_command("--max-bytes", "1000")
        self.assertIn("below_limit", output)

        state = self._read_heartbeat()
        self.assertEqual(state["result"], "below_limit")
        # This is the regression check: size_bytes must come from the
        # per-test tempdir file, never the real /run/isadoraair path.
        self.assertEqual(state["size_bytes"], 500)
        self.assertEqual(state["max_bytes"], 1000)

    def test_rolled_heartbeat_reflects_post_rollover_size(self):
        self.write_working_file(2000)
        self.run_command("--max-bytes", "1000")
        state = self._read_heartbeat()
        self.assertEqual(state["result"], "rolled")
        self.assertIsNotNone(state["last_rollover_at"])

    def test_default_max_bytes_uses_patched_production_constant(self):
        self.write_working_file(100)
        self.run_command()  # no --max-bytes: must use recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES
        state = self._read_heartbeat()
        self.assertEqual(state["max_bytes"], recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES)

    def test_missing_working_file_records_null_size(self):
        self.assertFalse(self.working_path.exists())
        self.run_command("--max-bytes", "1000")
        state = self._read_heartbeat()
        self.assertEqual(state["result"], "missing")
        self.assertIsNone(state["size_bytes"])

    def test_non_positive_max_bytes_is_rejected(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command("maintain_aircheck_buffer", "--max-bytes", "0")
