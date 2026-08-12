"""1.7 release/version-skew visibility -- EncoderManager's own runtime-
commit capture (encoder_manager.py __init__) and its inclusion in every
per-group state file _write_group_state() produces.

No process-wide heartbeat exists for this service (only per-group
files, see __init__'s own comment) -- these tests confirm the same
single captured value is stamped into a freshly-constructed manager's
group state even before any group has ever been launched, since
_group_meta/_current/etc. all use dict.get()-with-defaults rather than
requiring a prior _start_group() call."""
from unittest.mock import patch

import encoders.services.encoder_manager as em
from django.test import TransactionTestCase

from encoders.tests.test_encoder_manager import EncoderManagerFixtureMixin


class RuntimeCommitGroupStateTests(EncoderManagerFixtureMixin, TransactionTestCase):
    def test_runtime_commit_captured_once_and_stamped_into_group_state(self):
        with patch.object(em, "capture_runtime_commit", return_value="c" * 40):
            manager = em.EncoderManager()
        self.assertEqual(manager._runtime_commit, "c" * 40)
        manager._write_group_state("airtap")
        state = self.read_group_state("airtap")
        self.assertEqual(state["runtime_commit"], "c" * 40)

    def test_redundant_across_groups_by_design(self):
        """Every group's state file carries the SAME value -- they all
        describe this one process, so the monitoring side can read any
        one of them."""
        with patch.object(em, "capture_runtime_commit", return_value="d" * 40):
            manager = em.EncoderManager()
        manager._write_group_state("airtap")
        manager._write_group_state("linein")
        self.assertEqual(self.read_group_state("airtap")["runtime_commit"], "d" * 40)
        self.assertEqual(self.read_group_state("linein")["runtime_commit"], "d" * 40)

    def test_git_unavailable_writes_none_not_a_crash(self):
        with patch.object(em, "capture_runtime_commit", return_value=None):
            manager = em.EncoderManager()
        manager._write_group_state("airtap")
        state = self.read_group_state("airtap")
        self.assertIsNone(state["runtime_commit"])

    def test_not_re_derived_on_every_state_write(self):
        with patch.object(em, "capture_runtime_commit", return_value="e" * 40) as mock_capture:
            manager = em.EncoderManager()
            manager._write_group_state("airtap")
            manager._write_group_state("airtap")
        mock_capture.assert_called_once()
