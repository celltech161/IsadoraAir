"""Tests for aircheck/services/recorder.py: Start/Stop/finalization
(baseline coverage -- none existed before this module) plus the idle
working-buffer maintenance feature and its shared AIRCHECK_LOCK_PATH
flock.

AIRCHECK_LOCK_PATH and AIRCHECK_CURRENT_PATH are hard-coded /run/isadoraair
paths in production. Every test here redirects both to a per-test
tempdir via patch.object -- these must NEVER touch the real production
paths, since this suite runs on the same box that serves production.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from aircheck.models import AircheckConfig, AircheckSession
from aircheck.services import recorder
from monitoring.models import SystemEvent


class AircheckRecorderTestBase(TestCase):
    """Redirects AIRCHECK_LOCK_PATH/AIRCHECK_CURRENT_PATH to a fresh
    tempdir per test, and gives a real AircheckConfig row pointing its
    output_directory at that same tempdir (never the real
    /srv/isadoraair/aircheck)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        tmp = Path(self._tmpdir.name)
        self.lock_path = str(tmp / "aircheck.lock")
        self.working_path = tmp / "aircheck-current.audio"
        self.out_dir = tmp / "out"

        self.enterContext(patch.object(recorder, "AIRCHECK_LOCK_PATH", self.lock_path))
        self.enterContext(patch.object(recorder, "AIRCHECK_CURRENT_PATH", str(self.working_path)))

        self.cfg = AircheckConfig.objects.create(
            pk=1, audio_format="mp3", bitrate="320k", source_device="airtap",
            output_directory=str(self.out_dir), filename_template="aircheck-test-%H%M%S",
        )

    def write_working_file(self, size_bytes):
        self.working_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.working_path, "wb") as f:
            f.write(b"\0" * size_bytes)


class IdleBufferMaintenanceTests(AircheckRecorderTestBase):
    """The 9 required regression scenarios for maintain_idle_buffer."""

    def test_missing_working_file_is_harmless_noop(self):
        self.assertFalse(self.working_path.exists())
        with patch.object(recorder, "_send_telnet") as telnet:
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "missing")
        telnet.assert_not_called()
        self.assertEqual(SystemEvent.objects.count(), 0)

    def test_below_limit_makes_no_telnet_call(self):
        self.write_working_file(50)
        with patch.object(recorder, "_send_telnet") as telnet:
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "below_limit")
        telnet.assert_not_called()

    def test_oversized_idle_file_issues_exactly_one_reopen(self):
        self.write_working_file(200)
        with patch.object(recorder, "_send_telnet") as telnet:
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "rolled")
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")

    def test_successful_idle_rollover_touches_no_session(self):
        self.write_working_file(200)
        self.assertEqual(AircheckSession.objects.count(), 0)
        with patch.object(recorder, "_send_telnet"):
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "rolled")
        self.assertEqual(AircheckSession.objects.count(), 0)

    def test_active_session_with_oversized_file_skips_rollover(self):
        AircheckSession.objects.create(
            filename="/tmp/whatever.mp3", audio_format="mp3", bitrate="320k",
            source_device="airtap", still_running=True,
        )
        self.write_working_file(200)
        with patch.object(recorder, "_send_telnet") as telnet:
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "active_session")
        telnet.assert_not_called()

    def test_lock_busy_skips_with_no_reopen(self):
        self.write_working_file(200)
        # Hold the real flock externally, from a second fd, simulating
        # a concurrent Start/Stop/maintenance run already in progress.
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with patch.object(recorder, "_send_telnet") as telnet:
                result = recorder.maintain_idle_buffer(max_bytes=100)
            self.assertEqual(result, "lock_busy")
            telnet.assert_not_called()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_start_and_stop_use_shared_serialization_path(self):
        """Start and Stop must both go through _aircheck_lock in
        BLOCKING mode (the same primitive maintenance uses
        non-blocking) -- verified by wrapping the real context manager
        and recording how each caller invokes it."""
        calls = []
        real_lock = recorder._aircheck_lock

        def spy(blocking):
            calls.append(blocking)
            return real_lock(blocking)

        with patch.object(recorder, "_aircheck_lock", side_effect=spy), \
             patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, err = recorder.start_recording()
            self.assertIsNone(err)
            _, stop_err = recorder.stop_recording()
            self.assertIsNone(stop_err)

        self.assertEqual(calls, [True, True])

    def test_telnet_failure_fails_safely_with_warning_event(self):
        self.write_working_file(200)
        with patch.object(recorder, "_send_telnet", side_effect=recorder.TelnetError("no route")):
            result = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(result, "error")
        # File left completely untouched -- no destructive action taken.
        self.assertTrue(self.working_path.exists())
        self.assertEqual(self.working_path.stat().st_size, 200)
        self.assertEqual(AircheckSession.objects.count(), 0)

        events = SystemEvent.objects.all()
        self.assertEqual(events.count(), 1)
        event = events.first()
        self.assertEqual(event.level, "warning")
        self.assertEqual(event.category, "aircheck")

        # A second failure within the coalesce window must not create
        # a second row -- no recurring-noise spam.
        with patch.object(recorder, "_send_telnet", side_effect=recorder.TelnetError("no route")):
            recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(SystemEvent.objects.count(), 1)

    def test_repeated_maintenance_below_threshold_is_idempotent(self):
        self.write_working_file(50)
        with patch.object(recorder, "_send_telnet") as telnet:
            first = recorder.maintain_idle_buffer(max_bytes=100)
            second = recorder.maintain_idle_buffer(max_bytes=100)
        self.assertEqual(first, "below_limit")
        self.assertEqual(second, "below_limit")
        telnet.assert_not_called()
        self.assertEqual(SystemEvent.objects.count(), 0)


class StartStopFinalizationTests(AircheckRecorderTestBase):
    """Baseline Start/Stop/finalization coverage -- confirms the
    AIRCHECK_LOCK_PATH wrapping added around these functions did not
    change their pre-existing observable behavior."""

    def test_start_recording_creates_session_and_reopens_once(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened") as telnet:
            session, err = recorder.start_recording()
        self.assertIsNone(err)
        self.assertIsNotNone(session)
        self.assertTrue(session.still_running)
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        self.assertEqual(AircheckSession.objects.filter(still_running=True).count(), 1)

    def test_start_recording_reaps_stale_session_under_the_lock(self):
        stale = AircheckSession.objects.create(
            filename="/tmp/stale.mp3", audio_format="mp3", bitrate="320k",
            source_device="airtap", still_running=True,
        )
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, err = recorder.start_recording()
        self.assertIsNone(err)
        stale.refresh_from_db()
        self.assertFalse(stale.still_running)
        self.assertIn("reaped stale row", stale.exit_note)
        self.assertNotEqual(session.id, stale.id)

    def test_stop_recording_direct_move_finalizes_mp3(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()
        self.write_working_file(1234)

        with patch.object(recorder, "_send_telnet", return_value="reopened") as telnet:
            stopped, err = recorder.stop_recording()

        self.assertIsNone(err)
        self.assertEqual(stopped.id, session.id)
        self.assertFalse(stopped.still_running)
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        dest = Path(stopped.filename)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.stat().st_size, 1234)
        self.assertFalse(self.working_path.exists())  # moved, not copied
        self.assertEqual(stopped.size_bytes, 1234)

    def test_stop_recording_no_active_session(self):
        session, err = recorder.stop_recording()
        self.assertIsNone(session)
        self.assertEqual(err, "no active session")

    def test_stop_recording_he_aac_dispatches_async_finalizer(self):
        self.cfg.audio_format = "he_aac"
        self.cfg.bitrate = "64k"
        self.cfg.save()
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()

        with patch.object(recorder, "_send_telnet", return_value="reopened"), \
             patch.object(recorder, "_finalize_he_aac_async") as finalize:
            stopped, err = recorder.stop_recording()

        self.assertIsNone(err)
        finalize.assert_called_once()
        args = finalize.call_args[0]
        self.assertEqual(args[0].id, session.id)
