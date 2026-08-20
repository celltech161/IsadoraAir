"""Tests for aircheck/services/recorder.py: Start/Stop/finalization
(baseline coverage -- none existed before this module) plus the idle
working-buffer maintenance feature and its shared AIRCHECK_LOCK_PATH
flock.

AIRCHECK_LOCK_PATH and AIRCHECK_CURRENT_PATH are hard-coded /run/isadoraair
paths in production. Every test here redirects both to a per-test
tempdir via patch.object -- these must NEVER touch the real production
paths, since this suite runs on the same box that serves production.
"""
import json
import os
import struct
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings

from aircheck.models import AircheckConfig, AircheckSession
from aircheck.services import recorder
from library.management.commands import analyze_tracks
from library.models import Category, CategoryKind, Track
from monitoring.models import SystemEvent


def _fake_mono_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n = seconds * sample_rate
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _fake_stereo_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n_frames = seconds * sample_rate
    return struct.pack(f"<{n_frames * 2}h", *([amplitude] * (n_frames * 2)))


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
        self.buffer_state_path = str(tmp / "aircheck_buffer_state.json")
        self.out_dir = tmp / "out"

        self.enterContext(patch.object(recorder, "AIRCHECK_LOCK_PATH", self.lock_path))
        self.enterContext(patch.object(recorder, "AIRCHECK_CURRENT_PATH", str(self.working_path)))
        # Real production value is /run/isadoraair (a DIFFERENT tmpfs
        # mount than this test's own tempdir) -- _finalize_he_aac_async
        # stages the intermediate with a plain Path.rename(), which
        # requires same-filesystem, so this must be redirected too or
        # every real he_aac finalize in a test silently fails at the
        # "could not stage intermediate" cross-device-rename step
        # before ever reaching the remux thread.
        self.enterContext(patch.object(recorder, "REMUX_INTERMEDIATE_DIR", tmp))
        self.enterContext(patch.object(recorder, "AIRCHECK_BUFFER_STATE_PATH", self.buffer_state_path))

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


class BufferHeartbeatTests(AircheckRecorderTestBase):
    """record_buffer_heartbeat: the state written for the /monitoring/
    card to read. Pure reporting -- never influences maintain_idle_buffer's
    own rollover decision, which none of these tests touch."""

    def _read_state(self):
        with open(self.buffer_state_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_first_heartbeat_with_no_prior_file(self):
        self.assertFalse(Path(self.buffer_state_path).exists())
        recorder.record_buffer_heartbeat("below_limit", 1234, recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES)

        state = self._read_state()
        self.assertEqual(state["result"], "below_limit")
        self.assertEqual(state["size_bytes"], 1234)
        self.assertEqual(state["max_bytes"], recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES)
        self.assertIsInstance(state["checked_at"], (int, float))
        self.assertIsNone(state["last_rollover_at"])

    def test_normal_below_limit_heartbeat(self):
        recorder.record_buffer_heartbeat("below_limit", 500, 1000)
        state = self._read_state()
        self.assertEqual(state["result"], "below_limit")
        self.assertEqual(state["size_bytes"], 500)
        self.assertEqual(state["max_bytes"], 1000)
        self.assertIsNone(state["last_rollover_at"])

    def test_rolled_heartbeat_records_last_rollover_at(self):
        recorder.record_buffer_heartbeat("rolled", 200, 1000)
        state = self._read_state()
        self.assertEqual(state["result"], "rolled")
        self.assertIsNotNone(state["last_rollover_at"])
        self.assertEqual(state["last_rollover_at"], state["checked_at"])

    def test_later_below_limit_heartbeat_preserves_last_rollover_at(self):
        recorder.record_buffer_heartbeat("rolled", 200, 1000)
        rolled_state = self._read_state()
        rollover_at = rolled_state["last_rollover_at"]
        self.assertIsNotNone(rollover_at)

        recorder.record_buffer_heartbeat("below_limit", 300, 1000)
        later_state = self._read_state()
        self.assertEqual(later_state["result"], "below_limit")
        self.assertEqual(later_state["last_rollover_at"], rollover_at)
        # checked_at itself still advances even though last_rollover_at didn't.
        self.assertGreaterEqual(later_state["checked_at"], rolled_state["checked_at"])

    def test_malformed_prior_state_fails_safely(self):
        Path(self.buffer_state_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.buffer_state_path, "w", encoding="utf-8") as f:
            f.write("{not valid json::")

        recorder.record_buffer_heartbeat("below_limit", 42, 1000)  # must not raise

        state = self._read_state()
        self.assertEqual(state["result"], "below_limit")
        self.assertIsNone(state["last_rollover_at"])  # malformed prior treated as "no prior state"

    def test_atomic_write_leaves_no_tmp_file_and_valid_json(self):
        recorder.record_buffer_heartbeat("rolled", 10, 1000)
        recorder.record_buffer_heartbeat("below_limit", 20, 1000)

        tmp_path = Path(self.buffer_state_path).with_suffix(".tmp")
        self.assertFalse(tmp_path.exists())
        # If this parses, the final file was never left half-written.
        state = self._read_state()
        self.assertEqual(state["result"], "below_limit")


class FinalizationClassificationTests(AircheckRecorderTestBase):
    """classify_finalization -- the recorder's own semantics for what
    the /monitoring/ card should show about the most recently stopped
    session, reused by aircheck.views rather than duplicated there."""

    def _make(self, still_running, exit_note=""):
        return AircheckSession.objects.create(
            filename="/tmp/x.m4a", audio_format="he_aac", bitrate="64k",
            source_device="airtap", still_running=still_running, exit_note=exit_note,
        )

    def test_none_session(self):
        self.assertIsNone(recorder.classify_finalization(None))

    def test_still_running_is_recording(self):
        session = self._make(True)
        self.assertEqual(recorder.classify_finalization(session), "recording")

    def test_remux_pending_is_finalizing(self):
        session = self._make(False, exit_note=recorder.REMUX_PENDING_NOTE)
        self.assertEqual(recorder.classify_finalization(session), "finalizing")

    def test_remux_failed_is_error(self):
        session = self._make(False, exit_note="remux failed: ffmpeg exit 1: bad input")
        self.assertEqual(recorder.classify_finalization(session), "error")

    def test_missing_working_file_at_stop_is_error(self):
        session = self._make(False, exit_note="working file /run/isadoraair/aircheck-current.audio was missing at Stop -- no audio to remux")
        self.assertEqual(recorder.classify_finalization(session), "error")

    def test_stage_intermediate_failure_is_error(self):
        session = self._make(False, exit_note="could not stage intermediate: [Errno 13] Permission denied")
        self.assertEqual(recorder.classify_finalization(session), "error")

    def test_telnet_only_note_is_warning_not_error(self):
        # A telnet hiccup AT Stop that didn't stop the move/remux from
        # succeeding -- worth flagging, but not "finalization failed".
        session = self._make(False, exit_note="telnet reopen failed at Stop: cannot reach liquidsoap telnet")
        self.assertEqual(recorder.classify_finalization(session), "warning")

    def test_clean_stop_no_note_is_complete(self):
        session = self._make(False, exit_note="")
        self.assertEqual(recorder.classify_finalization(session), "complete")


class LibrarySyncTests(AircheckRecorderFixtureMixin, TransactionTestCase):
    """_sync_finalized_recording_to_library: best-effort indexing of a
    finalized recording into the library, only when AircheckConfig's
    output_directory (here, self.out_dir, redirected per-test) happens
    to live under LIBRARY_ROOT with a matching Category. ffmpeg decode
    is mocked (same idiom as library/tests/test_sync_track_file.py).

    TransactionTestCase (not TestCase): the function under test calls
    close_old_connections() (it's designed to run in a background
    thread in production -- see its own docstring), which is
    incompatible with TestCase's outer per-test atomic transaction on
    the main thread's connection ("connection already closed" if
    called synchronously inline there). Dispatching it via a real
    thread + join, same as production, sidesteps that entirely and
    matches this file's own ConcurrentStartTests precedent."""

    def _sync(self, dest):
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            t = threading.Thread(target=recorder._sync_finalized_recording_to_library, args=(dest,))
            t.start()
            t.join(timeout=10)

    def test_dest_outside_library_root_is_silent_noop(self):
        # self.out_dir (a random tempdir) is never under the real,
        # unpatched settings.LIBRARY_ROOT -- exactly the default/most
        # common case (output_directory left at its original value).
        dest = self.out_dir / "aircheck-20260101-000000.mp3"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"audio")

        self._sync(dest)  # must not raise

        self.assertEqual(Track.objects.count(), 0)
        self.assertEqual(SystemEvent.objects.count(), 0)

    def test_dest_under_library_root_with_category_creates_track(self):
        with tempfile.TemporaryDirectory() as lib_root:
            lib_root = Path(lib_root)
            with override_settings(LIBRARY_ROOT=str(lib_root)):
                kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
                Category.objects.create(code="Aircheck", name="Aircheck", kind=kind)

                dest_dir = lib_root / "Aircheck"
                dest_dir.mkdir(parents=True)
                dest = dest_dir / "aircheck-20260101-000000.mp3"
                dest.write_bytes(b"audio")

                self._sync(dest)

                track = Track.objects.get(filepath=str(dest))
                self.assertFalse(track.ready2air, "Aircheck recordings must not default to ready2air=True")
                self.assertEqual(track.category.code, "Aircheck")
                self.assertEqual(SystemEvent.objects.count(), 0)

    def test_dest_under_library_root_without_category_warns(self):
        with tempfile.TemporaryDirectory() as lib_root:
            lib_root = Path(lib_root)
            with override_settings(LIBRARY_ROOT=str(lib_root)):
                dest_dir = lib_root / "NoSuchCategory"
                dest_dir.mkdir(parents=True)
                dest = dest_dir / "aircheck-20260101-000000.mp3"
                dest.write_bytes(b"audio")

                self._sync(dest)  # must not raise

                self.assertEqual(Track.objects.count(), 0)
                events = SystemEvent.objects.all()
                self.assertEqual(events.count(), 1)
                self.assertEqual(events.first().level, "warning")
                self.assertEqual(events.first().category, "aircheck")


class FinalizeLibrarySyncWiringTests(AircheckRecorderFixtureMixin, TransactionTestCase):
    """Confirms _finalize_direct_move and _remux_worker's success path
    actually dispatch to _sync_finalized_recording_to_library -- and
    only when the file genuinely landed at dest, never on a failed
    move/remux. Patches _sync_finalized_recording_to_library itself
    (already covered directly above) so these stay fast/deterministic.

    TransactionTestCase: the he_aac cases exercise the REAL
    _remux_worker background thread (only _sync_finalized_recording_to_
    library is mocked), which needs to see the AircheckSession row
    Start/Stop created on the main thread -- impossible under
    TestCase's uncommitted per-test transaction, same reasoning as
    LibrarySyncTests above."""

    def _capturing_thread_patch(self):
        """threading.enumerate()-by-name is racy (a fast/all-mocked
        thread can finish -- or not yet even be scheduled -- before the
        test gets to look for it). Patching recorder.threading.Thread
        to record every real Thread object it constructs lets the test
        .join() the exact instance deterministically instead."""
        created = []
        real_thread_cls = threading.Thread

        def capturing(*args, **kwargs):
            t = real_thread_cls(*args, **kwargs)
            created.append(t)
            return t

        return created, patch.object(recorder.threading, "Thread", side_effect=capturing)

    def test_direct_move_success_dispatches_library_sync(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()
        self.write_working_file(4321)

        created_threads, thread_patch = self._capturing_thread_patch()
        with patch.object(recorder, "_send_telnet", return_value="reopened"), \
             thread_patch, \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync_mock:
            stopped, err = recorder.stop_recording()
            for t in created_threads:
                t.join(timeout=5)

        self.assertIsNone(err)
        sync_mock.assert_called_once()
        called_dest = sync_mock.call_args[0][0]
        self.assertEqual(str(called_dest), stopped.filename)

    def test_direct_move_missing_working_file_does_not_dispatch_sync(self):
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            recorder.start_recording()
        # No write_working_file() call -- working file stays missing.

        with patch.object(recorder, "_send_telnet", return_value="reopened"), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync_mock:
            stopped, err = recorder.stop_recording()

        self.assertIsNone(err)
        self.assertIn("no audio to move", stopped.exit_note)
        sync_mock.assert_not_called()

    def test_he_aac_remux_success_dispatches_library_sync(self):
        self.cfg.audio_format = "he_aac"
        self.cfg.bitrate = "64k"
        self.cfg.save()
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()
        self.write_working_file(999)

        dest = Path(session.filename)

        def fake_ffmpeg(*args, **kwargs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"remuxed m4a")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        created_threads, thread_patch = self._capturing_thread_patch()
        with patch.object(recorder, "_send_telnet", return_value="reopened"), \
             thread_patch, \
             patch("subprocess.run", side_effect=fake_ffmpeg), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync_mock:
            stopped, err = recorder.stop_recording()
            for t in created_threads:
                t.join(timeout=5)

        self.assertIsNone(err)
        sync_mock.assert_called_once_with(dest)

    def test_he_aac_remux_failure_does_not_dispatch_sync(self):
        self.cfg.audio_format = "he_aac"
        self.cfg.bitrate = "64k"
        self.cfg.save()
        with patch.object(recorder, "_send_telnet", return_value="reopened"):
            session, _ = recorder.start_recording()
        self.write_working_file(999)

        created_threads, thread_patch = self._capturing_thread_patch()
        with patch.object(recorder, "_send_telnet", return_value="reopened"), \
             thread_patch, \
             patch("subprocess.run", side_effect=OSError("ffmpeg not found")), \
             patch.object(recorder, "_sync_finalized_recording_to_library") as sync_mock:
            stopped, err = recorder.stop_recording()
            for t in created_threads:
                t.join(timeout=5)

        self.assertIsNone(err)
        sync_mock.assert_not_called()
