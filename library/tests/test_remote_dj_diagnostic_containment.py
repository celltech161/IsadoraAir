"""P2 1.13C: bounded Remote DJ text and raw-PCM diagnostics.

Tiny patched limits prove exact behavior without allocating MiB-scale files.
The PCM cases call the actual GStreamer pad-probe callback used by the engine.
"""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

import library.services.engine as engine_module


Gst.init(None)


class DiagnosticFixture:
    def setUp(self):
        super().setUp()
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        self.diag_path = root / "remote_dj_diag.log"
        self.pcm_path = root / "remote_dj_first_1s.pcm"
        patches = (
            ("DJ_DIAG_LOG", self.diag_path),
            ("DJ_DIAG_MAX_BYTES", 220),
            ("DJ_DIAG_MAX_LINE_BYTES", 96),
            ("DJ_DUMP_PCM", self.pcm_path),
            ("DJ_DUMP_PCM_MAX_BYTES", 10),
            ("DJ_DUMP_PROGRESS_BYTES", 4),
        )
        for name, value in patches:
            self.enterContext(patch.object(engine_module, name, value))

    def session(self):
        session = engine_module.RemoteDJSession()
        session.connection_attempt = SimpleNamespace(attempt_id="attempt-test")
        return session

    def open_diag(self, session=None):
        session = session or self.session()
        self.assertTrue(engine_module._reset_remote_dj_diagnostics(session))
        return session

    def force_rotation(self, session, prefix="entry"):
        for index in range(30):
            engine_module._dj_diag(session, f"{prefix}-{index}-" + "x" * 50)
            if engine_module._dj_diag_backup_path().exists():
                return
        self.fail("diagnostic log did not rotate")

    def minimal_engine(self, session):
        session.connection_attempt = None
        engine = object.__new__(engine_module.PlaybackEngine)
        engine.remote_dj_session = session
        engine._remote_dj_server = None
        engine._remote_dj_last_attempt = None
        engine.dj_slots = []
        return engine


class TextDiagnosticContainmentTests(DiagnosticFixture, SimpleTestCase):
    def test_new_session_starts_with_fresh_current_log(self):
        self.diag_path.write_text("previous-session\n")
        session = self.open_diag()
        engine_module._dj_diag(session, "session_start")
        text = self.diag_path.read_text()
        self.assertNotIn("previous-session", text)
        self.assertIn("session_start", text)

    def test_new_session_removes_previous_rotated_backup(self):
        backup = engine_module._dj_diag_backup_path()
        backup.write_text("older-session\n")
        self.open_diag()
        self.assertFalse(backup.exists())

    def test_ordinary_messages_append_normally_below_limit(self):
        session = self.open_diag()
        engine_module._dj_diag(session, "first")
        engine_module._dj_diag(session, "second")
        text = self.diag_path.read_text()
        self.assertLessEqual(self.diag_path.stat().st_size, engine_module.DJ_DIAG_MAX_BYTES)
        self.assertIn("first", text)
        self.assertIn("second", text)

    def test_crossing_limit_rotates_current_to_backup(self):
        session = self.open_diag()
        self.force_rotation(session)
        backup = engine_module._dj_diag_backup_path()
        self.assertTrue(backup.is_file())
        self.assertGreater(backup.stat().st_size, 0)
        self.assertIn("diagnostic_log_rotated", self.diag_path.read_text())

    def test_logging_continues_in_fresh_current_after_rotation(self):
        session = self.open_diag()
        self.force_rotation(session)
        engine_module._dj_diag(session, "post-rotation-evidence")
        self.assertIn("post-rotation-evidence", self.diag_path.read_text())

    def test_second_rotation_replaces_backup_without_numbered_growth(self):
        session = self.open_diag()
        self.force_rotation(session, "first-cycle")
        first_backup = engine_module._dj_diag_backup_path().read_text()
        for index in range(30):
            engine_module._dj_diag(session, f"second-cycle-{index}-" + "y" * 50)
            if engine_module._dj_diag_backup_path().read_text() != first_backup:
                break
        self.assertNotEqual(engine_module._dj_diag_backup_path().read_text(), first_backup)
        self.assertEqual(list(self.diag_path.parent.glob("remote_dj_diag.log.[2-9]*")), [])

    def test_total_text_footprint_stays_within_two_file_bound(self):
        session = self.open_diag()
        for index in range(100):
            engine_module._dj_diag(session, f"message-{index}-" + "z" * 100)
        paths = [self.diag_path, engine_module._dj_diag_backup_path()]
        sizes = [path.stat().st_size for path in paths if path.exists()]
        self.assertLessEqual(len(sizes), 2)
        self.assertTrue(all(size <= engine_module.DJ_DIAG_MAX_BYTES for size in sizes))
        self.assertLessEqual(sum(sizes), 2 * engine_module.DJ_DIAG_MAX_BYTES)

    def test_one_large_message_is_truncated_inside_bound(self):
        session = self.open_diag()
        engine_module._dj_diag(session, "huge=" + "q" * 10_000)
        text = self.diag_path.read_text()
        self.assertLessEqual(self.diag_path.stat().st_size, engine_module.DJ_DIAG_MAX_BYTES)
        self.assertIn("diagnostic_message_truncated", text)

    def test_rotation_failure_is_nonfatal_and_disables_diagnostics(self):
        session = self.open_diag()
        engine_module._dj_diag(session, "seed-" + "x" * 50)
        with patch.object(engine_module.os, "replace", side_effect=OSError("read-only")):
            for index in range(10):
                engine_module._dj_diag(session, f"trigger-{index}-" + "y" * 50)
        self.assertIsNone(session.diag_fh)
        self.assertEqual(session.connection_attempt.attempt_id, "attempt-test")

    def test_session_stop_closes_handle_and_leaves_bounded_evidence(self):
        session = self.open_diag()
        engine_module._dj_diag(session, "before-stop")
        engine = self.minimal_engine(session)
        self.assertFalse(engine._remote_dj_session_stop())
        self.assertIsNone(session.diag_fh)
        self.assertTrue(self.diag_path.is_file())
        self.assertIn("session_stop", self.diag_path.read_text())
        self.assertLessEqual(self.diag_path.stat().st_size, engine_module.DJ_DIAG_MAX_BYTES)


class _BufferInfo:
    def __init__(self, payload):
        self.buffer = Gst.Buffer.new_wrapped(payload)

    def get_buffer(self):
        return self.buffer


class PcmDiagnosticContainmentTests(DiagnosticFixture, SimpleTestCase):
    def open_pcm(self, session=None):
        session = self.open_diag(session)
        self.assertTrue(engine_module._reset_remote_dj_pcm(session, True))
        engine = SimpleNamespace(remote_dj_session=session)
        return session, engine

    def probe(self, engine, session, payload):
        return engine_module._remote_dj_pcm_dump_probe(
            None, _BufferInfo(payload), (engine, session)
        )

    def test_pcm_dump_remains_disabled_by_default(self):
        self.assertIs(engine_module.DJ_DUMP_PCM_ENABLED, False)

    def test_disabled_path_creates_no_pcm_file_and_probe_is_noop(self):
        session = self.open_diag()
        self.assertFalse(engine_module._reset_remote_dj_pcm(session, False))
        engine = SimpleNamespace(remote_dj_session=session)
        result = self.probe(engine, session, b"audio")
        self.assertEqual(result, Gst.PadProbeReturn.REMOVE)
        self.assertFalse(self.pcm_path.exists())
        self.assertEqual(session.dump_bytes_written, 0)

    def test_enabled_path_opens_a_fresh_file(self):
        self.pcm_path.write_bytes(b"old-session")
        session, _engine = self.open_pcm()
        self.assertIsNotNone(session.dump_fh)
        self.assertEqual(self.pcm_path.stat().st_size, 0)

    def test_buffers_accumulate_normally_below_cap(self):
        session, engine = self.open_pcm()
        self.assertEqual(self.probe(engine, session, b"1234"), Gst.PadProbeReturn.OK)
        self.assertEqual(self.probe(engine, session, b"56"), Gst.PadProbeReturn.OK)
        session.dump_fh.flush()
        self.assertEqual(session.dump_bytes_written, 6)
        self.assertEqual(self.pcm_path.read_bytes(), b"123456")

    def test_final_buffer_is_sliced_to_exact_remaining_bytes(self):
        session, engine = self.open_pcm()
        self.probe(engine, session, b"1234567")
        result = self.probe(engine, session, b"ABCDEFG")
        self.assertEqual(result, Gst.PadProbeReturn.REMOVE)
        self.assertEqual(self.pcm_path.read_bytes(), b"1234567ABC")

    def test_resulting_file_size_is_exactly_at_most_cap(self):
        session, engine = self.open_pcm()
        self.probe(engine, session, b"x" * 100)
        self.assertEqual(self.pcm_path.stat().st_size, engine_module.DJ_DUMP_PCM_MAX_BYTES)
        self.assertLessEqual(self.pcm_path.stat().st_size, engine_module.DJ_DUMP_PCM_MAX_BYTES)

    def test_later_buffers_add_zero_bytes_after_cap(self):
        session, engine = self.open_pcm()
        self.probe(engine, session, b"x" * 10)
        before = self.pcm_path.read_bytes()
        self.assertEqual(self.probe(engine, session, b"later"), Gst.PadProbeReturn.REMOVE)
        self.assertEqual(self.pcm_path.read_bytes(), before)
        self.assertEqual(session.dump_bytes_written, 10)

    def test_cap_reached_diagnostic_is_emitted_exactly_once(self):
        session, engine = self.open_pcm()
        self.probe(engine, session, b"x" * 10)
        self.probe(engine, session, b"later")
        text = self.diag_path.read_text()
        self.assertEqual(text.count("pcm_dump_cap_reached bytes=10"), 1)
        self.assertTrue(session.dump_capped)

    def test_progress_logging_stops_after_cap(self):
        session, engine = self.open_pcm()
        self.probe(engine, session, b"1234")
        self.probe(engine, session, b"567890")
        before = self.diag_path.read_text()
        self.probe(engine, session, b"later")
        after = self.diag_path.read_text()
        self.assertEqual(before, after)
        self.assertEqual(after.count("dump_progress"), 1)

    def test_session_stop_safely_handles_already_capped_closed_dump(self):
        session, engine_ref = self.open_pcm()
        self.probe(engine_ref, session, b"x" * 10)
        engine = self.minimal_engine(session)
        self.assertFalse(engine._remote_dj_session_stop())
        self.assertIsNone(session.dump_fh)
        self.assertEqual(self.pcm_path.stat().st_size, 10)

    def test_next_session_resets_state_and_can_capture_again(self):
        first, first_engine = self.open_pcm()
        self.probe(first_engine, first, b"x" * 10)
        second = self.session()
        second, second_engine = self.open_pcm(second)
        self.assertFalse(second.dump_capped)
        self.assertEqual(second.dump_bytes_written, 0)
        self.assertEqual(self.probe(second_engine, second, b"new"), Gst.PadProbeReturn.OK)
        second.dump_fh.flush()
        self.assertEqual(self.pcm_path.read_bytes(), b"new")

    def test_pcm_oserror_disables_future_writes_without_escaping(self):
        session, engine = self.open_pcm()

        class FailingFile:
            def __init__(self):
                self.write_calls = 0

            def write(self, _payload):
                self.write_calls += 1
                raise OSError("simulated full tmpfs")

            def close(self):
                pass

        session.dump_fh.close()
        failing = FailingFile()
        session.dump_fh = failing
        self.assertEqual(self.probe(engine, session, b"audio"), Gst.PadProbeReturn.REMOVE)
        self.assertEqual(self.probe(engine, session, b"again"), Gst.PadProbeReturn.REMOVE)
        self.assertEqual(failing.write_calls, 1)
        self.assertTrue(session.dump_failed)
        self.assertIsNone(session.dump_fh)
        self.assertEqual(self.diag_path.read_text().count("pcm_dump_write_failed"), 1)
