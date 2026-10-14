"""D2B fixtures: native active messages through the ORDINARY library/queue/deck.

Isolation: a private LIBRARY_ROOT and WAVEFORMS_DIR (temporary directories),
engine commands captured instead of written to /run, the media root isolated
by production's mixin, the test database. The native-airing gate is opened
ONLY here, by an explicit test patch of services.NATIVE_AIRING_AVAILABLE plus
the policy switch -- production code keeps it closed.
"""
from __future__ import annotations

import tempfile
import threading
import wave
from datetime import date
from pathlib import Path
from unittest import mock

from django.test import override_settings
from django.utils import timezone

import library.services.engine as eng_module
from active_messages import playout, services, worker
from active_messages.models import ActiveBroadcastExecution
from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track

from .d2a_support import PA_HZ, URGENT_HZ, RealAudio


def write_wav(path: Path, seconds=1.0, rate=44100):
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00\x10\x00" * frames)


def engine_stand_in(current_log=None, log_items=None, cursor=0):
    """A bare PlaybackEngine (no __init__/GStreamer) for queue-level tests --
    the same technique the existing urgent-insertion tests use."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj.running = True
    obj.decks = {"A": None, "B": None}
    obj.manual_mode = False
    obj._lock = threading.RLock()
    obj._next_triggered = False
    obj._deck_bin_map = {}
    obj._forced_next_items = []
    obj._urgent_retry_counts = {}
    obj.current_log = current_log
    obj.log_items = list(log_items or [])
    obj._queue_cursor = cursor
    obj._next_hour_peek = None
    obj._next_hour_peek_at = 0.0
    obj._resume_item = None
    obj._resume_hint = None
    return obj


class Playout(RealAudio):
    """Gate open (test-only), isolated library/waveforms, captured commands."""

    def setUp(self):
        super().setUp()
        self._dirs = tempfile.TemporaryDirectory(prefix="am-d2b-")
        self.addCleanup(self._dirs.cleanup)
        root = Path(self._dirs.name)
        self.library_root, self.waveforms = root / "music", root / "waveforms"
        self.library_root.mkdir()
        self.waveforms.mkdir()
        override = override_settings(LIBRARY_ROOT=str(self.library_root), WAVEFORMS_DIR=str(self.waveforms))
        override.enable()
        self.addCleanup(override.disable)
        self.commands = []
        for target, value in (("active_messages.services.NATIVE_AIRING_AVAILABLE", True),
                              ("active_messages.playout._send", self.commands.append)):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        services.set_native_airing(True, actor=self.admin)
        music_kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.music = Category.objects.create(code="AMTESTMUS", name="AM test music", kind=music_kind)
        self.song_artist = Artist.objects.create(name="Song Artist")
        self.urgentpa = Category.objects.get(code="UrgentPA")
        self.wxalert = Category.objects.get(code="WxAlert")
        self._serial = 0

    # -- domain ---------------------------------------------------------------------------
    def ready_execution(self, *, kind="urgent", seconds=1.0, frequency=URGENT_HZ, actor=None):
        """A real READY aggregate (real render + validation), not yet published."""
        self.real_active(actor or self.producer, kind=kind, seconds=seconds, frequency=frequency)
        execution = services.materialize_due_execution()
        outcome = worker.process(execution)
        assert outcome.result in ("ready", "reused"), outcome
        return ActiveBroadcastExecution.objects.get(pk=execution.pk)

    def published(self, **kwargs):
        execution = self.ready_execution(**kwargs)
        return execution, playout.publish(execution)

    def committing(self):
        """Run on-commit callbacks (the queued command) under either test base:
        a TestCase captures them; a TransactionTestCase runs them on commit."""
        from contextlib import nullcontext

        from django.test import TestCase
        return self.captureOnCommitCallbacks(execute=True) if isinstance(self, TestCase) else nullcontext()

    def dispatched(self, **kwargs):
        execution, publication = self.published(**kwargs)
        with self.committing():
            playout.dispatch(execution)
        return ActiveBroadcastExecution.objects.get(pk=execution.pk), publication, self.commands[-1]

    # -- library / queue ------------------------------------------------------------------
    def song(self, title, *, category=None, seconds=1.0):
        self._serial += 1
        path = self.library_root / (category or self.music).code / f"song-{self._serial}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, seconds)
        return Track.objects.create(title=title, artist=self.song_artist, category=category or self.music,
                                    ready2air=True, filepath=str(path), filename=path.name,
                                    duration_seconds=seconds, next_start_seconds=seconds)

    def playlist(self, *titles, hour=10):
        log = PlaylistLog.objects.create(date=date(2027, 9, 13), hour=hour, status="approved")
        items = [LogItem.objects.create(playlist_log=log, position=index, scheduled_time=timezone.now(),
                                        track=track, track_title=track.title, track_artist=track.artist.name,
                                        category=track.category)
                 for index, track in enumerate(self.song(title) for title in titles)]
        return log, items

    def weather_item(self, log, position):
        track = self.song("Weather Alert", category=self.wxalert)
        return LogItem.objects.create(playlist_log=log, position=position, scheduled_time=timezone.now(),
                                      track=track, track_title=track.title, category=self.wxalert)


__all__ = ["Playout", "engine_stand_in", "write_wav", "PA_HZ", "URGENT_HZ"]
