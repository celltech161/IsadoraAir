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
import threading
from pathlib import Path
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase

from aircheck.models import AircheckConfig, AircheckSession
from aircheck.services import recorder
from monitoring.models import SystemEvent


class AircheckRecorderFixtureMixin:
    """Redirects AIRCHECK_LOCK_PATH/AIRCHECK_CURRENT_PATH to a fresh
    tempdir per test, and gives a real AircheckConfig row pointing its
    output_directory at that same tempdir (never the real
    /srv/isadoraair/aircheck). Split out from AircheckRecorderTestBase so
    it can also back a TransactionTestCase for the real-thread
    concurrency test below, which needs actual committing DB
    connections rather than TestCase's outer per-test atomic wrapper."""

    def setUp(self):
        super().setUp()
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


class AircheckRecorderTestBase(AircheckRecorderFixtureMixin, TestCase):
    pass


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


class DoubleStartIdempotencyTests(AircheckRecorderTestBase):
    """start_recording() must be genuinely idempotent: a second Start
    while a session is already running returns that SAME session
    untouched -- no telnet call, no new row, no mutation of any kind.
    Regression coverage for the double-Start bug (a prior version of
    start_recording() unconditionally reaped every still_running row
    before checking for one, so this branch was unreachable and a
    second Start silently cut the in-progress recording)."""

    def test_start_with_existing_active_session_returns_it_unchanged(self):
        existing = AircheckSession.objects.create(
            filename="/tmp/already-running.mp3", audio_format="mp3", bitrate="320k",
            source_device="airtap", still_running=True,
        )
        with patch.object(recorder, "_send_telnet") as telnet:
            session, note = recorder.start_recording()

        self.assertEqual(session.id, existing.id)
        self.assertEqual(note, "already recording")
        telnet.assert_not_called()
        self.assertEqual(AircheckSession.objects.count(), 1)

    def test_double_start_issues_exactly_one_reopen_total(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened") as telnet:
            first_session, first_note = recorder.start_recording()
            second_session, second_note = recorder.start_recording()

        self.assertIsNone(first_note)
        self.assertEqual(second_note, "already recording")
        self.assertEqual(first_session.id, second_session.id)
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        self.assertEqual(AircheckSession.objects.count(), 1)

    def test_second_start_does_not_mutate_the_existing_session(self):
        existing = AircheckSession.objects.create(
            filename="/tmp/untouched.mp3", audio_format="mp3", bitrate="320k",
            source_device="airtap", still_running=True,
        )
        before = {
            "filename": existing.filename, "audio_format": existing.audio_format,
            "bitrate": existing.bitrate, "source_device": existing.source_device,
            "started_at": existing.started_at,
        }

        with patch.object(recorder, "_send_telnet") as telnet:
            recorder.start_recording()

        existing.refresh_from_db()
        telnet.assert_not_called()
        self.assertTrue(existing.still_running)
        self.assertIsNone(existing.ended_at)
        self.assertEqual(existing.exit_note, "")
        self.assertEqual(existing.filename, before["filename"])
        self.assertEqual(existing.audio_format, before["audio_format"])
        self.assertEqual(existing.bitrate, before["bitrate"])
        self.assertEqual(existing.source_device, before["source_device"])
        self.assertEqual(existing.started_at, before["started_at"])

    def test_stop_after_idempotent_second_start_finalizes_original_session(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            first_session, first_note = recorder.start_recording()
        with patch.object(recorder, "_send_telnet") as telnet:
            second_session, second_note = recorder.start_recording()
        self.assertIsNone(first_note)
        self.assertEqual(second_note, "already recording")
        self.assertEqual(second_session.id, first_session.id)
        telnet.assert_not_called()

        self.write_working_file(999)
        with patch.object(recorder, "_send_telnet", return_value="reopened") as stop_telnet:
            stopped, stop_err = recorder.stop_recording()

        self.assertIsNone(stop_err)
        self.assertEqual(stopped.id, first_session.id)
        self.assertFalse(stopped.still_running)
        stop_telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        dest = Path(stopped.filename)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.stat().st_size, 999)


class ConcurrentStartTests(AircheckRecorderFixtureMixin, TransactionTestCase):
    """Real inter-thread concurrency on the shared AIRCHECK_LOCK_PATH
    flock -- TransactionTestCase (not TestCase) because each thread
    needs its own genuinely committing DB connection; TestCase's outer
    per-test atomic transaction would hide one thread's committed
    session from the other and defeat the point of this test. The lock
    itself (not test timing) is what must make this safe -- a
    threading.Barrier is used only to maximize the odds both callers
    actually contend, never to fake or bypass the lock."""

    def test_concurrent_start_callers_produce_exactly_one_session(self):
        start_barrier = threading.Barrier(2)
        outcomes = []
        outcomes_lock = threading.Lock()

        def call_start():
            close_old_connections()
            try:
                start_barrier.wait(timeout=5)
                session, note = recorder.start_recording()
                with outcomes_lock:
                    outcomes.append((session.id if session else None, note))
            finally:
                close_old_connections()

        with patch.object(recorder, "_send_telnet", return_value="reopened") as telnet:
            threads = [threading.Thread(target=call_start) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(len(outcomes), 2)
        session_ids = {sid for sid, _ in outcomes}
        self.assertEqual(len(session_ids), 1, f"expected exactly one session id across both callers, got {outcomes}")
        notes = sorted(note or "" for _, note in outcomes)
        self.assertEqual(notes, ["", "already recording"])
        telnet.assert_called_once_with(f"{recorder.AIRCHECK_OUTPUT_ID}.reopen")
        self.assertEqual(AircheckSession.objects.count(), 1)
