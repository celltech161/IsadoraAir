"""Real-process regressions for orderly engine signal delivery."""

from __future__ import annotations

from datetime import date
import inspect
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst

from library.models import (
    Artist,
    Category,
    CategoryKind,
    LogItem,
    PlayEvent,
    PlaylistLog,
    Track,
)
from library.services.engine import Deck, PlaybackEngine
from library.tests.test_engine_deck_lifecycle import _write_wav


Gst.init(None)


class _SignalProbeMixin:
    helper = Path(__file__).with_name("_engine_signal_probe.py")
    repository_root = Path(__file__).resolve().parents[2]

    def _environment(self, *, database=False):
        env = os.environ.copy()
        pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(self.repository_root) + (
            os.pathsep + pythonpath if pythonpath else ""
        )
        if database:
            settings = connection.settings_dict
            env.update(
                DB_NAME=str(settings["NAME"]),
                DB_USER=str(settings["USER"]),
                DB_PASSWORD=str(settings["PASSWORD"] or ""),
                DB_HOST=str(settings["HOST"]),
                DB_PORT=str(settings["PORT"]),
            )
        return env

    def _start_probe(self, mode, *extra, database=False):
        temp_dir = tempfile.TemporaryDirectory(
            prefix=f"isadoraair-signal-{mode}."
        )
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        process = subprocess.Popen(
            [sys.executable, "-u", str(self.helper), mode, str(root), *extra],
            cwd=self.repository_root,
            env=self._environment(database=database),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def terminate_probe():
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)

        self.addCleanup(terminate_probe)
        return process, root

    def _wait_for(self, process, predicate, description, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            returncode = process.poll()
            if returncode is not None:
                output, _unused = process.communicate()
                self.fail(
                    f"probe exited {returncode} before {description}:\n{output}"
                )
            time.sleep(0.005)
        self.fail(f"probe did not reach {description} within {timeout}s")

    def _wait_for_file(self, process, path, timeout=10.0):
        self._wait_for(process, path.exists, path.name, timeout=timeout)

    def _read_progress(self, path):
        try:
            return int(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return -1

    def _assert_progress_after_signal(self, process, progress_path, previous):
        self._wait_for(
            process,
            lambda: self._read_progress(progress_path) > previous,
            "post-signal Python callback progress",
        )

    def _finish_probe(self, process, root, timeout=10.0):
        output, _unused = process.communicate(timeout=timeout)
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("Shutting down... (signal)", output)
        self.assertIn("Engine stopped.", output)
        self.assertNotIn("Force quit.", output)
        stop_lines = (root / "stop-count").read_text(encoding="utf-8").splitlines()
        self.assertEqual(stop_lines, ["stop"])
        return output


class RealSignalDeliveryTests(_SignalProbeMixin, SimpleTestCase):
    def test_idle_main_loop_sigterm_exits_through_orderly_stop(self):
        process, root = self._start_probe("idle")
        self._wait_for_file(process, root / "ready")
        os.kill(process.pid, signal.SIGTERM)
        output = self._finish_probe(process, root)
        self.assertLess(
            output.index("Shutting down..."), output.index("Engine stopped.")
        )

    def test_sigterm_during_python_glib_callback_waits_for_callback_return(self):
        process, root = self._start_probe("callback")
        self._wait_for_file(process, root / "callback-entered")
        progress_path = root / "progress"
        self._wait_for(
            process,
            lambda: self._read_progress(progress_path) >= 0,
            "progress",
        )
        before_signal = self._read_progress(progress_path)

        os.kill(process.pid, signal.SIGTERM)
        self._assert_progress_after_signal(process, progress_path, before_signal)
        (root / "release-callback").touch()

        self._finish_probe(process, root)
        self.assertTrue((root / "callback-completed").exists())

    def test_sigterm_after_registration_but_before_loop_run_is_pending(self):
        process, root = self._start_probe("preloop")
        self._wait_for_file(process, root / "preloop-ready")
        progress_path = root / "progress"
        self._wait_for(
            process,
            lambda: self._read_progress(progress_path) >= 0,
            "progress",
        )
        before_signal = self._read_progress(progress_path)

        os.kill(process.pid, signal.SIGTERM)
        self._assert_progress_after_signal(process, progress_path, before_signal)
        (root / "enter-loop").touch()

        self._finish_probe(process, root)

    def test_ctrl_c_sigint_uses_the_same_orderly_path(self):
        process, root = self._start_probe("idle")
        self._wait_for_file(process, root / "ready")
        os.kill(process.pid, signal.SIGINT)
        self._finish_probe(process, root)

    def test_glib_sources_are_registered_before_startup_without_python_handler(self):
        source = inspect.getsource(PlaybackEngine.start)
        self.assertNotIn("signal.signal", source)
        self.assertLess(
            source.index("GLib.unix_signal_add"), source.index("_read_resume_hint")
        )

    def test_orderly_shutdown_request_is_idempotent(self):
        engine = object.__new__(PlaybackEngine)
        engine._orderly_shutdown_requested = False
        engine.loop = MagicMock()

        with patch("builtins.print") as output:
            engine._request_orderly_shutdown("signal")
            engine._request_orderly_shutdown("duplicate")

        engine.loop.quit.assert_called_once_with()
        output.assert_called_once_with("Shutting down... (signal)")

    def test_stop_is_idempotent(self):
        engine = object.__new__(PlaybackEngine)
        engine._lock = threading.RLock()
        engine._stop_started = False
        engine.running = True
        engine._write_state = MagicMock()
        engine._media_validation_worker = MagicMock()
        engine.decks = {"A": None, "B": None}
        engine._deck_teardowns = {}
        engine.remote_dj_session = None
        engine.main_pipeline = MagicMock()
        engine.loop = MagicMock()
        engine.loop.is_running.return_value = False

        engine.stop()
        engine.stop()

        engine._write_state.assert_called_once_with(transport="STOPPED")
        engine._media_validation_worker.stop.assert_called_once_with()
        engine.main_pipeline.set_state.assert_called_once_with(Gst.State.NULL)


class RealGStreamerSignalAccountingTests(_SignalProbeMixin, TransactionTestCase):
    def setUp(self):
        self.kind = CategoryKind.objects.create(code="signal", name="Signal Music")
        self.category = Category.objects.create(
            code="SIGNAL", name="Signal Music", kind=self.kind
        )
        self.artist = Artist.objects.create(name="Signal Artist")
        self.log = PlaylistLog.objects.create(
            date=date(2027, 9, 14), hour=21, status="approved"
        )
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="isadoraair-real-signal-gstreamer."
        )
        self.addCleanup(self.temp_dir.cleanup)
        media_path = Path(self.temp_dir.name) / "signal.wav"
        _write_wav(media_path, frames=44100 * 5)
        self.track = Track.objects.create(
            filepath=str(media_path),
            filename=media_path.name,
            title="Signal Track",
            artist=self.artist,
            category=self.category,
            ready2air=True,
            duration_seconds=5,
            next_start_seconds=4.5,
        )
        self.item = LogItem.objects.create(
            playlist_log=self.log,
            position=1,
            scheduled_time=timezone.now(),
            track=self.track,
            track_title=self.track.title,
            track_artist=self.artist.name,
            category=self.category,
        )

    def test_real_sigterm_closes_deck_then_same_occurrence_finishes_complete(self):
        process, root = self._start_probe(
            "gstreamer", str(self.item.id), database=True
        )
        self._wait_for_file(process, root / "ready", timeout=15.0)
        os.kill(process.pid, signal.SIGTERM)
        self._finish_probe(process, root, timeout=15.0)

        close_old_connections()
        event = PlayEvent.objects.get(log_item_id_snapshot=self.item.id)
        original_event_id = event.id
        first = event.duration_segments.get()
        self.assertEqual(first.evidence_state, "complete")
        self.assertEqual(first.termination_reason, "clean_shutdown")
        self.assertIsNotNone(first.ended_at)
        self.assertGreater(first.confirmed_duration_seconds, 0.0)
        self.assertEqual(event.duration_evidence_state, "active")
        self.assertIsNone(event.ended_at)

        self.item.refresh_from_db()
        original_played_at = self.item.played_at
        continuation_engine = object.__new__(PlaybackEngine)
        continuation_engine._lock = threading.RLock()
        continuation = Deck(
            "A",
            self.track,
            self.item,
            MagicMock(),
            MagicMock(),
            generation=2,
            air_start_eligible=False,
            continuation_reason="auto_resume",
        )
        continuation_engine.decks = {"A": continuation, "B": None}
        started_at = timezone.now()
        continuation.activate_duration_segment(started_at, "auto_resume")
        continuation_engine._record_continuation_segment_start(
            "A",
            2,
            self.item.id,
            continuation.duration_generation_id,
            started_at,
            "auto_resume",
        )
        for seconds in (50, 50, 50, 47.39):
            buffer = Gst.Buffer.new()
            buffer.duration = int(seconds * Gst.SECOND)
            continuation.mark_program_buffer(buffer)
        continuation_engine._persist_deck_duration(
            continuation,
            close_segment=True,
            occurrence_terminal=True,
            termination_reason="natural_eos",
        )

        event.refresh_from_db()
        self.item.refresh_from_db()
        self.track.refresh_from_db()
        segments = list(event.duration_segments.order_by("started_at", "id"))
        self.assertEqual(event.id, original_event_id)
        self.assertEqual(self.item.played_at, original_played_at)
        self.assertEqual(self.track.play_count, 1)
        self.assertEqual(PlayEvent.objects.count(), 1)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1].evidence_state, "complete")
        self.assertEqual(segments[1].termination_reason, "natural_eos")
        self.assertAlmostEqual(
            event.duration_played_seconds,
            sum(segment.confirmed_duration_seconds for segment in segments),
        )
        self.assertEqual(event.duration_evidence_state, "complete")
        self.assertIsNotNone(event.ended_at)
