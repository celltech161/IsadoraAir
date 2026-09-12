"""P1 1.11 -- monitoring/services/supervision.py, the tiny process-
supervision marker used to distinguish a clean Monitoring restart
(Update Center / systemctl restart / normal reboot) from an unclean
one (watchdog SIGABRT, OOM-kill, crash) after the fact. MARKER_PATH is
redirected to a temp file for every test -- never touches the real
/run/isadoraair/monitoring_supervision.json a live isadoraair-
monitoring service may also be writing to."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from monitoring.services import supervision


class SupervisionMarkerTests(SimpleTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.marker_path = Path(self.tmp_dir.name) / "monitoring_supervision.json"
        patcher = patch.object(supervision, "MARKER_PATH", self.marker_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_invocation_since_boot_reports_no_prior_and_is_not_unclean(self):
        """No marker file at all (freshly cleared /run tmpfs after a
        real reboot, or a station's very first start) must NEVER be
        treated as an incident."""
        prior = supervision.record_new_invocation(runtime_commit="a" * 40)
        self.assertIsNone(prior)
        self.assertFalse(supervision.prior_invocation_was_unclean(prior))

    def test_marker_is_written_with_expected_fields(self):
        supervision.record_new_invocation(runtime_commit="b" * 40)
        data = json.loads(self.marker_path.read_text())
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["runtime_commit"], "b" * 40)
        self.assertFalse(data["clean_shutdown"])
        self.assertIn("invocation_id", data)
        self.assertIn("started_at", data)

    def test_clean_shutdown_then_restart_reports_no_incident(self):
        supervision.record_new_invocation(runtime_commit="c" * 40)
        supervision.mark_clean_shutdown()
        prior = supervision.record_new_invocation(runtime_commit="d" * 40)
        self.assertIsNotNone(prior)
        self.assertTrue(prior["clean_shutdown"])
        self.assertFalse(supervision.prior_invocation_was_unclean(prior))

    def test_unclean_prior_stop_is_detected_on_next_start(self):
        """Simulates a watchdog SIGABRT / OOM-kill / crash: a marker
        was written at start, but mark_clean_shutdown() was NEVER
        called before the next process starts."""
        supervision.record_new_invocation(runtime_commit="e" * 40)
        # No mark_clean_shutdown() call here -- simulates the death.
        prior = supervision.record_new_invocation(runtime_commit="f" * 40)
        self.assertIsNotNone(prior)
        self.assertFalse(prior["clean_shutdown"])
        self.assertTrue(supervision.prior_invocation_was_unclean(prior))

    def test_unclean_prior_carries_useful_evidence_fields(self):
        supervision.record_new_invocation(runtime_commit="prior-commit")
        prior = supervision.record_new_invocation(runtime_commit="new-commit")
        self.assertIn("pid", prior)
        self.assertIn("started_at", prior)
        self.assertEqual(prior["runtime_commit"], "prior-commit")

    def test_malformed_marker_file_treated_as_no_prior(self):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text("{not valid json", encoding="utf-8")
        prior = supervision.record_new_invocation(runtime_commit="g" * 40)
        self.assertIsNone(prior)
        self.assertFalse(supervision.prior_invocation_was_unclean(prior))

    def test_mark_clean_shutdown_is_a_noop_with_no_marker(self):
        # Must never raise even if record_new_invocation was never called.
        supervision.mark_clean_shutdown()
        self.assertFalse(self.marker_path.exists())

    def test_mark_clean_shutdown_is_a_noop_for_a_different_pid(self):
        """A marker written by some OTHER pid (a stale leftover this
        process didn't create) must not be silently claimed/rewritten
        as if we were the ones who started it."""
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(
            json.dumps({"pid": os.getpid() + 12345, "clean_shutdown": False}),
            encoding="utf-8",
        )
        supervision.mark_clean_shutdown()
        data = json.loads(self.marker_path.read_text())
        self.assertFalse(data["clean_shutdown"])

    def test_marker_write_is_atomic_tmp_then_rename(self):
        supervision.record_new_invocation(runtime_commit="h" * 40)
        self.assertFalse(self.marker_path.with_suffix(".tmp").exists())
        self.assertTrue(self.marker_path.exists())

    def test_repeated_unclean_cycles_still_never_grow_the_marker(self):
        """No restart HISTORY accumulates -- exactly one JSON object,
        no matter how many unclean cycles happen in a row."""
        for i in range(5):
            supervision.record_new_invocation(runtime_commit=f"commit-{i}")
        data = json.loads(self.marker_path.read_text())
        self.assertEqual(data["runtime_commit"], "commit-4")
        self.assertLess(self.marker_path.stat().st_size, 500)
