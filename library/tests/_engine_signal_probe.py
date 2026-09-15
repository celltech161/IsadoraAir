"""Subprocess probe for PlaybackEngine's real POSIX/GLib shutdown wiring."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import django


django.setup()

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import library.services.engine as engine_module
from library.services.engine import PlaybackEngine


Gst.init(None)


class _NoopWorker:
    def start(self):
        return None

    def stop(self):
        return None


class _StagedMainLoop:
    """Expose deterministic points around one real GLib.MainLoop.run()."""

    def __init__(self, root: Path, mode: str, ready_predicate=None):
        self._root = root
        self._mode = mode
        self._ready_predicate = ready_predicate
        self._loop = GLib.MainLoop()

    def _touch(self, name: str):
        (self._root / name).write_text("ready\n", encoding="utf-8")

    def _write_progress(self, value: int):
        (self._root / "progress").write_text(str(value), encoding="utf-8")

    def _wait_for_release(self, release_name: str):
        progress = 0
        release = self._root / release_name
        while not release.exists():
            progress += 1
            self._write_progress(progress)
            # The loop deliberately remains inside ordinary Python callback
            # code. A Python signal exception would interrupt this section;
            # GLib's low-level handler only wakes the main context.
            time.sleep(0.002)

    def _idle_ready(self):
        self._touch("ready")
        return GLib.SOURCE_REMOVE

    def _blocking_callback(self):
        self._touch("callback-entered")
        self._wait_for_release("release-callback")
        self._touch("callback-completed")
        return GLib.SOURCE_REMOVE

    def _gstreamer_ready(self):
        if self._ready_predicate and self._ready_predicate():
            self._touch("ready")
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def run(self):
        if self._mode == "preloop":
            self._touch("preloop-ready")
            self._wait_for_release("enter-loop")
        elif self._mode == "callback":
            GLib.idle_add(self._blocking_callback)
        elif self._mode == "gstreamer":
            GLib.timeout_add(5, self._gstreamer_ready)
        else:
            GLib.idle_add(self._idle_ready)
        self._loop.run()

    def quit(self):
        self._loop.quit()

    def is_running(self):
        return self._loop.is_running()


class _SignalProbeEngine(PlaybackEngine):
    """Minimal engine shell that exercises production start/stop methods."""

    def __init__(self, root: Path, mode: str):
        self._root = root
        self._lock = threading.RLock()
        self.loop = _StagedMainLoop(root, mode)
        self.log_items = []
        self._forced_next_items = []
        self._media_validation_worker = _NoopWorker()
        self._deck_teardowns = {}
        self.decks = {"A": None, "B": None}
        self.remote_dj_session = None
        self.main_pipeline = None
        self.running = False
        self.restart_required = False

    def _read_resume_hint(self):
        return None

    def _build_main_pipeline(self):
        return None

    def _load_current_hour_log(self):
        return None

    def _apply_resume_hint_queue_rewind(self):
        return None

    def _restore_dedication_sequence_from_resume_hint(self):
        return None

    def _reconcile_playback_duration_state(self):
        return None

    def _write_state(self, **_kwargs):
        with (self._root / "stop-count").open("a", encoding="utf-8") as output:
            output.write("stop\n")

    def _poll_position(self):
        return GLib.SOURCE_CONTINUE

    _ensure_upcoming_logs = _poll_position
    _mic_recovery_tick = _poll_position
    _mic_presence_probe_tick = _poll_position
    _output_recovery_tick = _poll_position
    _output_presence_probe_tick = _poll_position
    _deck_teardown_tick = _poll_position
    _deck_seek_tick = _poll_position
    _checkpoint_playback_durations = _poll_position


def _disable_startup_callbacks(engine):
    engine._read_resume_hint = lambda: None
    engine._build_main_pipeline = lambda: None
    engine._load_current_hour_log = lambda: None
    engine._apply_resume_hint_queue_rewind = lambda: None
    engine._restore_dedication_sequence_from_resume_hint = lambda: None
    engine._reconcile_playback_duration_state = lambda: None
    engine._poll_position = lambda: GLib.SOURCE_CONTINUE
    engine._ensure_upcoming_logs = lambda: GLib.SOURCE_CONTINUE
    engine._mic_recovery_tick = lambda: GLib.SOURCE_CONTINUE
    engine._mic_presence_probe_tick = lambda: GLib.SOURCE_CONTINUE
    engine._output_recovery_tick = lambda: GLib.SOURCE_CONTINUE
    engine._output_presence_probe_tick = lambda: GLib.SOURCE_CONTINUE
    engine._deck_teardown_tick = lambda: GLib.SOURCE_CONTINUE
    engine._deck_seek_tick = lambda: GLib.SOURCE_CONTINUE
    engine._checkpoint_playback_durations = lambda: GLib.SOURCE_CONTINUE
    engine._media_validation_worker = _NoopWorker()
    engine.log_items = []
    engine._forced_next_items = []


def _run_minimal(root: Path, mode: str):
    engine = _SignalProbeEngine(root, mode)
    with patch.object(
        engine_module.RemoteDJConfig,
        "load",
        return_value=SimpleNamespace(enabled=False),
    ):
        engine.start()
    return 0


def _run_gstreamer(root: Path, log_item_id: int):
    from library.models import LogItem
    from library.tests.test_engine_deck_lifecycle import _make_real_engine

    engine = _make_real_engine()
    for method_name in (
        "_claim_playback_occurrence",
        "_schedule_occurrence_air_start_from_probe",
        "_persist_deck_duration",
        "_schedule_continuation_segment_start",
    ):
        engine.__dict__.pop(method_name, None)

    _disable_startup_callbacks(engine)
    engine._write_state = lambda **_kwargs: (
        root / "stop-count"
    ).write_text("stop\n", encoding="utf-8")
    item = LogItem.objects.select_related(
        "track__artist", "track__album", "category"
    ).get(pk=log_item_id)
    deck = engine._create_deck("A", item)
    if deck is None:
        print("Failed to construct test deck", flush=True)
        return 3

    sink = engine.main_pipeline.get_by_name("sink")
    sink.set_property("sync", True)
    engine.main_pipeline.set_state(Gst.State.PLAYING)

    def contributing():
        snapshot = deck.duration_snapshot()
        return (
            deck.play_event_id is not None
            and snapshot["confirmed_seconds"] > 0.0
        )

    engine.loop = _StagedMainLoop(root, "gstreamer", contributing)
    with patch.object(
        engine_module.RemoteDJConfig,
        "load",
        return_value=SimpleNamespace(enabled=False),
    ):
        engine.start()
    return 0


def main(argv):
    mode = argv[1]
    root = Path(argv[2])
    if mode == "gstreamer":
        return _run_gstreamer(root, int(argv[3]))
    return _run_minimal(root, mode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
