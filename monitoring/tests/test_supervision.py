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
        prior, armed = supervision.record_new_invocation(runtime_commit="a" * 40)
        self.assertIsNone(prior)
        self.assertTrue(armed)
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
        prior, armed = supervision.record_new_invocation(runtime_commit="d" * 40)
        self.assertIsNotNone(prior)
        self.assertTrue(armed)
        self.assertTrue(prior["clean_shutdown"])
        self.assertFalse(supervision.prior_invocation_was_unclean(prior))

    def test_unclean_prior_stop_is_detected_on_next_start(self):
        """Simulates a watchdog SIGABRT / OOM-kill / crash: a marker
        was written at start, but mark_clean_shutdown() was NEVER
        called before the next process starts."""
        supervision.record_new_invocation(runtime_commit="e" * 40)
        # No mark_clean_shutdown() call here -- simulates the death.
        prior, armed = supervision.record_new_invocation(runtime_commit="f" * 40)
        self.assertIsNotNone(prior)
        self.assertTrue(armed)
        self.assertFalse(prior["clean_shutdown"])
        self.assertTrue(supervision.prior_invocation_was_unclean(prior))

    def test_unclean_prior_carries_useful_evidence_fields(self):
        supervision.record_new_invocation(runtime_commit="prior-commit")
        prior, _armed = supervision.record_new_invocation(runtime_commit="new-commit")
        self.assertIn("pid", prior)
        self.assertIn("started_at", prior)
        self.assertEqual(prior["runtime_commit"], "prior-commit")

    def test_malformed_marker_file_treated_as_no_prior(self):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text("{not valid json", encoding="utf-8")
        prior, armed = supervision.record_new_invocation(runtime_commit="g" * 40)
        self.assertIsNone(prior)
        self.assertTrue(armed)
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


class SupervisionMarkerBestEffortTests(SimpleTestCase):
    """Safety correction: marker persistence must be strictly best-
    effort. An OSError writing the marker (unwritable/full
    /run/isadoraair, a permissions regression, ...) must never raise
    out of record_new_invocation() or mark_clean_shutdown() -- this is
    supplemental evidence only, never allowed to affect whether
    Monitoring itself starts or stops."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.marker_path = Path(self.tmp_dir.name) / "monitoring_supervision.json"
        patcher = patch.object(supervision, "MARKER_PATH", self.marker_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_new_invocation_never_raises_when_write_fails(self):
        with patch.object(supervision, "_write_marker", side_effect=OSError("disk full")):
            prior, armed = supervision.record_new_invocation(runtime_commit="x" * 40)
        self.assertIsNone(prior)  # nothing was ever written, so no prior to read either
        self.assertFalse(armed)

    def test_record_new_invocation_reports_unarmed_on_write_failure(self):
        """The read of the PRIOR marker still succeeds even though the
        write of a fresh one for THIS invocation fails -- armed=False
        reflects the write outcome specifically, not the read."""
        supervision.record_new_invocation(runtime_commit="genuinely-prior")
        supervision.mark_clean_shutdown()
        with patch.object(supervision, "_write_marker", side_effect=OSError("permission denied")):
            prior, armed = supervision.record_new_invocation(runtime_commit="y" * 40)
        self.assertIsNotNone(prior)
        self.assertEqual(prior["runtime_commit"], "genuinely-prior")
        self.assertFalse(armed)

    def test_caller_must_not_report_incident_when_unarmed(self):
        """Direct proof of the "avoid repeated false incidents" contract:
        an unclean prior IS present, but since THIS invocation's own
        write failed (armed=False), the correct caller behavior --
        exactly what MonitorManager.start() does -- is to gate the
        SystemEvent on `armed`, not just on prior_invocation_was_unclean."""
        supervision.record_new_invocation(runtime_commit="dead-prior")
        # No mark_clean_shutdown() -- this prior really was unclean.
        with patch.object(supervision, "_write_marker", side_effect=OSError("disk full")):
            prior, armed = supervision.record_new_invocation(runtime_commit="new")
        self.assertTrue(supervision.prior_invocation_was_unclean(prior))
        self.assertFalse(armed)
        # The caller contract: `armed and prior_invocation_was_unclean(prior)`
        # -- with armed False, this must evaluate to a falsy "do not report."
        self.assertFalse(armed and supervision.prior_invocation_was_unclean(prior))

    def test_persistently_broken_storage_never_repeats_the_same_incident(self):
        """If /run/isadoraair stays unwritable across MANY consecutive
        restarts, every single one of them must report armed=False --
        never re-surface the one real stale incident on every cycle."""
        supervision.record_new_invocation(runtime_commit="dead-prior")
        with patch.object(supervision, "_write_marker", side_effect=OSError("disk full")):
            for i in range(10):
                prior, armed = supervision.record_new_invocation(runtime_commit=f"attempt-{i}")
                self.assertFalse(armed, f"attempt {i} should report unarmed")

    def test_mark_clean_shutdown_still_never_raises_when_write_fails(self):
        supervision.record_new_invocation(runtime_commit="z" * 40)
        with patch.object(supervision, "_write_marker", side_effect=OSError("disk full")):
            supervision.mark_clean_shutdown()  # must not raise

    def test_marker_directory_creation_failure_is_contained(self):
        """The write path's own mkdir(parents=True, exist_ok=True) can
        itself raise OSError (e.g. /run/isadoraair's parent is
        read-only) -- must be caught by the SAME try/except as the
        write itself, not just a bare file-write OSError."""
        with patch.object(
            supervision.Path, "mkdir", side_effect=OSError("read-only file system"),
        ):
            prior, armed = supervision.record_new_invocation(runtime_commit="w" * 40)
        self.assertIsNone(prior)
        self.assertFalse(armed)
