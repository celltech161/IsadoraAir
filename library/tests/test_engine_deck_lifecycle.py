"""P0 deck EOS/watchdog containment and resource-bound regressions.

The topology tests use real GStreamer bins, decodebin, silence-prime concat,
ghost-pad EOS probe, a dynamic audiomixer request pad, and the GLib idle
handoff. They are hardware-free and write media only beneath TemporaryDirectory.
"""

from __future__ import annotations

import json
import os
import gc
import inspect
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi
from django.test import SimpleTestCase

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

import library.services.engine as eng_module
from library.services import audio_recovery
from library.services.engine import DECK_STUCK_TIMEOUT_SECONDS, Deck, PlaybackEngine


Gst.init(None)


def _wait_until(predicate, timeout=3.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.001)
    return False


def _pipeline_child_count(pipeline):
    iterator = pipeline.iterate_elements()
    count = 0
    while True:
        result, _element = iterator.next()
        if result == Gst.IteratorResult.OK:
            count += 1
        elif result == Gst.IteratorResult.RESYNC:
            iterator.resync()
        else:
            return count


def _rss_bytes():
    fields = Path("/proc/self/statm").read_text().split()
    return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")


def _task_count():
    return len(tuple(Path("/proc/self/task").iterdir()))


def _write_wav(path, *, frames=441, sample_rate=44100):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        samples = [struct.pack("<hh", 500, -500) for _ in range(frames)]
        output.writeframes(b"".join(samples))


def _make_track(path, track_id, duration=0.01, title="Test Track"):
    track = MagicMock()
    track.id = track_id
    track.filepath = str(path)
    track.title = title
    track.duration_seconds = duration
    track.next_start_seconds = duration
    track.cue_in_seconds = 0.0
    track.outro_starts_seconds = None
    track.play_count = 0
    track.record_label = ""
    track.isrc = ""
    track.artist = MagicMock(name="artist")
    track.artist.name = "Test Artist"
    track.album = MagicMock(name="album")
    track.album.title = "Test Album"
    return track


def _make_log_item(track, item_id, playlist_log_id=None):
    item = MagicMock()
    item.id = item_id
    item.playlist_log_id = playlist_log_id  # None keeps _request_restart's int-check false
    item.track = track
    item.category_id = None
    item.category = None
    item.position = item_id
    return item


def _init_policy_restart_attrs(engine):
    """[P0] 1.8 -- engines built via object.__new__(PlaybackEngine) skip
    __init__, so the policy-restart bookkeeping attributes must be set
    explicitly. Kept in one helper so every fixture stays consistent
    with the real init order."""
    engine.restart_required = False
    engine._poison_skip_identities = []
    engine._poison_reported_identities = set()


def _make_real_engine():
    engine = object.__new__(PlaybackEngine)
    engine._lock = threading.RLock()
    engine.main_pipeline = Gst.Pipeline.new("deck-lifecycle-test")
    engine.mixer = Gst.ElementFactory.make("audiomixer", "mixer")
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)
    engine.main_pipeline.add(engine.mixer)
    engine.main_pipeline.add(sink)
    engine.mixer.link(sink)
    engine._test_output_buffers = 0

    def count_output(_pad, info):
        if info.type & Gst.PadProbeType.BUFFER:
            engine._test_output_buffers += 1
        return Gst.PadProbeReturn.OK

    engine.mixer.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, count_output)
    bus = engine.main_pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message::error", engine._on_main_bus_error)
    engine.pipeline_sample_rate = 44100
    engine.decks = {"A": None, "B": None}
    engine._deck_bin_map = {}
    engine._deck_generation_serial = 0
    engine._deck_teardowns = {
        slot: audio_recovery.BoundedTeardownCoordinator(
            f"deck-real-{slot.lower()}-test", timeout_s=1.0, queue_capacity=16
        )
        for slot in ("A", "B")
    }
    engine._deck_watchdog_times = eng_module.deque()
    engine._mic_bin = None
    engine._output_slots = {}
    engine._resume_hint = None
    engine._vt = {"phase": "idle", "outgoing_track_id": None}
    engine.manual_mode = False
    engine._manual_hold_pending = False
    engine._next_triggered = False
    engine._write_now_playing = lambda _track: None
    engine._write_rbds_category_state = lambda _track: None
    engine._start_next_track = lambda **_kwargs: None
    # loop is what _request_restart tries to quit(); MagicMock keeps the
    # try/except path a no-op instead of an AttributeError when a real
    # deck test happens to trigger the poison path.
    engine.loop = MagicMock()
    _init_policy_restart_attrs(engine)
    return engine


class RealDeckTopologyTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-deck-eos.")
        self.addCleanup(self.temp_dir.cleanup)
        self.wav_path = Path(self.temp_dir.name) / "short.wav"
        _write_wav(self.wav_path)
        # [P0] 1.8 -- redirect poison marker writes to a tmpdir so the
        # real-deck poison tests below never touch /run/isadoraair/.
        self._marker_tmpdir = tempfile.TemporaryDirectory(prefix="isa-p0-1.8-real.")
        self.addCleanup(self._marker_tmpdir.cleanup)
        self.marker_path = Path(self._marker_tmpdir.name) / "last_policy_restart.json"
        self.engine = _make_real_engine()
        self.addCleanup(self._cleanup_engine)
        self.patches = (
            patch.object(
                eng_module.Track.objects,
                "filter",
                new=lambda *_args, **_kwargs: SimpleNamespace(update=lambda **_fields: None),
            ),
            patch.object(
                eng_module.PlayEvent.objects,
                "create",
                new=lambda **_kwargs: SimpleNamespace(id=None),
            ),
            patch.object(eng_module, "mark_song_requests_aired", new=lambda *_args, **_kwargs: None),
            patch.object(eng_module, "emit_event", new=lambda **_kwargs: None),
            patch.object(eng_module, "POLICY_RESTART_MARKER_PATH", self.marker_path),
        )
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def _cleanup_engine(self):
        for coordinator in self.engine._deck_teardowns.values():
            coordinator.stop()
        self.engine.main_pipeline.set_state(Gst.State.NULL)

    def _create(
        self,
        generation,
        *,
        slot="A",
        primed=True,
        title="Test Track",
        path=None,
        duration=0.01,
    ):
        track = _make_track(
            path or self.wav_path,
            generation,
            duration=duration,
            title=title,
        )
        item = _make_log_item(track, generation)
        resume = None if primed else 0
        return self.engine._create_deck(slot, item, resume_position_ns=resume)

    def test_silence_primed_natural_eos_crosses_every_boundary(self):
        deck = self._create(1, title="Short Legal ID")
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(_wait_until(lambda: deck.finished))
        self.assertTrue(_wait_until(lambda: "O_NULL_TRANSITION_COMPLETE" in deck.eos_milestones))
        expected = {
            "A_DECODER_AUDIO_EOS",
            "B_REAL_LEG_EOS_BEFORE_CONCAT",
            "C_CONCAT_REAL_SINK_EOS",
            "D_CONCAT_SRC_EOS",
            "E_DECK_GHOST_SRC_EOS",
            "F_EOS_PROBE_ENTERED",
            "G_GLIB_IDLE_SCHEDULED",
            "H_EOS_IDLE_CALLBACK_ENTERED",
            "I_EOS_ACCEPTED",
            "J_HANDLE_DECK_FINISHED_ENTERED",
            "K_REMOVE_DECK_ENTERED",
            "L_MIXER_UNLINK_COMPLETE",
            "M_MIXER_REQUEST_PAD_RELEASE_COMPLETE",
            "N_BIN_REMOVAL_COMPLETE",
            "O_NULL_TRANSITION_COMPLETE",
        }
        self.assertTrue(expected.issubset(deck.eos_milestones), deck.milestone_snapshot())
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)

    def test_audiomixer_ignore_inactive_does_not_retire_previously_active_pad(self):
        prop = self.engine.mixer.find_property("ignore-inactive-pads")
        self.assertIsNotNone(prop)
        self.assertFalse(prop.default_value)
        self.assertFalse(self.engine.mixer.get_property("ignore-inactive-pads"))
        self.engine.mixer.set_property("ignore-inactive-pads", True)
        original_idle_add = GLib.idle_add

        def suppress_only_deck_completion(function, *args, **kwargs):
            if getattr(function, "__name__", "") == "_on_deck_eos_probed":
                return 0
            return original_idle_add(function, *args, **kwargs)

        bad = self._create(600)
        with patch.object(GLib, "idle_add", side_effect=suppress_only_deck_completion):
            self.engine.main_pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(_wait_until(lambda: bad.media_buffer_count > 0))
            self.assertTrue(_wait_until(lambda: "G_GLIB_IDLE_SCHEDULE_FAILED" in bad.eos_milestones))
        bad.finished = True
        self.engine.decks["A"] = None

        good = self._create(601)
        self.assertFalse(_wait_until(lambda: good.finished, timeout=0.1))
        self.engine._remove_deck(bad)
        self.assertTrue(_wait_until(lambda: good.finished))

    def test_unprimed_resumed_deck_uses_equivalent_path_without_concat(self):
        deck = self._create(2, primed=False)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(_wait_until(lambda: deck.finished))
        self.assertIn("A_DECODER_AUDIO_EOS", deck.eos_milestones)
        self.assertIn("B_REAL_LEG_EOS_BEFORE_CONCAT", deck.eos_milestones)
        self.assertNotIn("C_CONCAT_REAL_SINK_EOS", deck.eos_milestones)
        self.assertNotIn("D_CONCAT_SRC_EOS", deck.eos_milestones)
        self.assertIn("E_DECK_GHOST_SRC_EOS", deck.eos_milestones)

    def test_healthy_two_hundred_deck_churn_is_bounded(self):
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        last = None
        with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
            # Exclude one-time plugin/typefind/decoder allocator growth from
            # the leak slope. The acceptance measurement is the following
            # 200 generations after this explicit 25-generation warm-up.
            for generation in range(1, 26):
                last = self._create(generation)
                self.assertTrue(_wait_until(lambda deck=last: deck.finished, timeout=2.0))
            self.assertTrue(
                _wait_until(lambda: self.engine._deck_teardowns["A"].snapshot()["completed"] == 25)
            )
            rss_start = _rss_bytes()
            tasks_start = _task_count()
            threads_start = threading.active_count()
            for generation in range(26, 226):
                last = self._create(generation)
                self.assertTrue(
                    _wait_until(lambda deck=last: deck.finished, timeout=2.0),
                    f"generation {generation} did not complete",
                )
        self.assertTrue(
            _wait_until(lambda: self.engine._deck_teardowns["A"].snapshot()["completed"] == 225)
        )
        gc.collect()
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(self.engine.decks, {"A": None, "B": None})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), 2)
        self.assertEqual(self.engine._deck_teardowns["A"].snapshot()["worker_starts"], 1)
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        self.assertLessEqual(_task_count(), tasks_start + 2)
        self.assertLess(_rss_bytes() - rss_start, 16 * 1024 * 1024)
        print(
            "healthy-churn-resources "
            f"cycles=200 pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"map={len(self.engine._deck_bin_map)} task_delta={_task_count() - tasks_start} "
            f"thread_delta={threading.active_count() - threads_start} "
            f"rss_delta={_rss_bytes() - rss_start}",
            flush=True,
        )
        self.assertIn("E_DECK_GHOST_SRC_EOS", last.eos_milestones)

    def test_post_poison_real_deck_churn_releases_detached_generations(self):
        """A wedged NULL must not turn every later deck into another leak."""
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        release = threading.Event()
        coordinator = self.engine._deck_teardowns["A"]
        original_submit = coordinator.submit

        def wedge_first_null(generation, _worker_fn, **kwargs):
            return original_submit(generation, release.wait, **kwargs)

        with patch.object(coordinator, "submit", side_effect=wedge_first_null):
            bad = self._create(999_999, slot="A")
            self.assertTrue(_wait_until(lambda: bad.finished))
            self.assertTrue(
                _wait_until(lambda: coordinator.snapshot()["active_generation"] == bad.generation)
            )
        coordinator.tick(now=time.monotonic() + 2.0)
        self.engine._deck_teardown_tick()
        self.assertTrue(coordinator.snapshot()["poisoned"])

        # Warm the surviving B decoder/allocator path before measuring the
        # next 100 retirements, exactly as the healthy-churn baseline does.
        with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
            for index, generation in enumerate(range(900, 925), start=1):
                warm = self._create(generation, slot="B")
                self.assertTrue(_wait_until(lambda deck=warm: deck.finished))
                self.assertTrue(
                    _wait_until(
                        lambda expected=index: self.engine._deck_teardowns["B"].snapshot()["completed"]
                        == expected
                    )
                )
                del warm
        gc.collect()
        rss_start = _rss_bytes()
        tasks_start = _task_count()
        threads_start = threading.active_count()
        generation_refs = []
        resource_samples = []
        with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
            for index, generation in enumerate(range(1_000, 1_100), start=1):
                deck = self._create(generation, slot="B")
                generation_refs.append(weakref.ref(deck.pipeline))
                self.assertTrue(
                    _wait_until(lambda deck=deck: deck.finished, timeout=2.0),
                    f"generation {generation} did not complete",
                )
                self.assertTrue(
                    _wait_until(
                        lambda expected=index + 25: self.engine._deck_teardowns["B"].snapshot()["completed"]
                        == expected
                    )
                )
                del deck
                if generation % 20 == 19:
                    gc.collect()
                    resource_samples.append(
                        {
                            "cycle": index,
                            "tasks": _task_count(),
                            "rss": _rss_bytes(),
                            "live_wrappers": sum(
                                ref() is not None for ref in generation_refs
                            ),
                        }
                    )
        gc.collect()

        snapshot = coordinator.snapshot()
        self.assertEqual(snapshot["worker_starts"], 1)
        self.assertEqual(snapshot["active_generation"], bad.generation)
        self.assertEqual(self.engine._deck_teardowns["B"].snapshot()["completed"], 125)
        self.assertEqual(self.engine._deck_teardowns["B"].snapshot()["worker_starts"], 1)
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(self.engine.decks, {"A": None, "B": None})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), 2)
        # One additional long-lived worker belongs to the surviving B
        # teardown boundary; it services all 100 retirements.
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        self.assertLessEqual(_task_count(), tasks_start + 8)
        self.assertLessEqual(
            max(sample["tasks"] for sample in resource_samples[-3:])
            - min(sample["tasks"] for sample in resource_samples[-3:]),
            2,
        )
        self.assertLess(_rss_bytes() - rss_start, 32 * 1024 * 1024)
        self.assertLessEqual(sum(ref() is not None for ref in generation_refs), 1)
        print(
            "post-poison-resources "
            f"cycles=100 live_wrappers={sum(ref() is not None for ref in generation_refs)} "
            f"pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"children={_pipeline_child_count(self.engine.main_pipeline)} "
            f"map={len(self.engine._deck_bin_map)} "
            f"task_delta={_task_count() - tasks_start} "
            f"thread_delta={threading.active_count() - threads_start} "
            f"rss_delta={_rss_bytes() - rss_start} samples={resource_samples}",
            flush=True,
        )
        release.set()
        self.assertTrue(
            _wait_until(
                lambda: not any(t.name == "deck-real-a-test-worker" for t in threading.enumerate())
            )
        )
        bad.pipeline.set_state(Gst.State.NULL)

    def test_wedged_generations_are_bounded_to_one_per_slot(self):
        """Two poisoned slots fail closed with one isolated wedge apiece."""
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        release_a = threading.Event()
        release_b = threading.Event()
        coordinator_a = self.engine._deck_teardowns["A"]
        coordinator_b = self.engine._deck_teardowns["B"]
        original_submit_a = coordinator_a.submit
        original_submit_b = coordinator_b.submit
        bad_a = None
        bad_b = None

        def wedge_a(generation, _worker_fn, **kwargs):
            return original_submit_a(generation, release_a.wait, **kwargs)

        def wedge_b(generation, _worker_fn, **kwargs):
            return original_submit_b(generation, release_b.wait, **kwargs)

        try:
            with patch.object(eng_module, "emit_event") as emitted:
                # Poison A and prove that B remains a fully functional
                # playback/NULL boundary for substantial real deck churn.
                with patch.object(coordinator_a, "submit", side_effect=wedge_a):
                    bad_a = self._create(20_000, slot="A")
                    self.assertTrue(_wait_until(lambda: bad_a.finished))
                    self.assertTrue(
                        _wait_until(
                            lambda: coordinator_a.snapshot()["active_generation"]
                            == bad_a.generation
                        )
                    )
                coordinator_a.tick(now=time.monotonic() + 2.0)
                self.engine._deck_teardown_tick()
                self.assertFalse(self.engine._deck_slot_available("A"))
                self.assertTrue(self.engine._deck_slot_available("B"))

                with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
                    for index, generation in enumerate(range(20_001, 20_051), start=1):
                        healthy_b = self._create(generation, slot="B")
                        self.assertTrue(
                            _wait_until(lambda deck=healthy_b: deck.finished, timeout=2.0),
                            f"surviving B generation {generation} did not complete",
                        )
                        self.assertTrue(
                            _wait_until(
                                lambda expected=index: coordinator_b.snapshot()["completed"]
                                == expected
                            )
                        )
                self.assertEqual(coordinator_b.snapshot()["completed"], 50)
                self.assertEqual(coordinator_b.snapshot()["worker_starts"], 1)

                # Poison B only after it has demonstrated continued real
                # playout and NULL cleanup following A's isolation.
                with patch.object(coordinator_b, "submit", side_effect=wedge_b):
                    bad_b = self._create(20_051, slot="B")
                    self.assertTrue(_wait_until(lambda: bad_b.finished))
                    self.assertTrue(
                        _wait_until(
                            lambda: coordinator_b.snapshot()["active_generation"]
                            == bad_b.generation
                        )
                    )
                coordinator_b.tick(now=time.monotonic() + 2.0)
                self.engine._deck_teardown_tick()

                state = self.engine._deck_recovery_state()
                self.assertEqual(
                    state["health"], "RESTART_REQUIRED_NO_PLAYBACK_SLOTS"
                )
                self.assertEqual(state["poisoned_slots"], ["A", "B"])
                self.assertEqual(state["reusable_slots"], [])
                self.assertFalse(state["automated_playout_available"])
                self.assertTrue(state["terminal_degraded"])
                self.assertTrue(state["restart_required_before_automated_playout"])
                self.assertFalse(state["playout_continues_on_remaining_slot"])

                self.assertEqual(self.engine.decks, {"A": None, "B": None})
                self.assertEqual(self.engine._deck_bin_map, {})
                self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
                self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), 2)
                self.assertIsNone(bad_a.pipeline.get_parent())
                self.assertIsNone(bad_b.pipeline.get_parent())
                self.assertEqual(coordinator_a.snapshot()["worker_starts"], 1)
                self.assertEqual(coordinator_b.snapshot()["worker_starts"], 1)
                self.assertEqual(
                    coordinator_a.snapshot()["active_generation"], bad_a.generation
                )
                self.assertEqual(
                    coordinator_b.snapshot()["active_generation"], bad_b.generation
                )

                worker_names = {
                    "deck-real-a-test-worker",
                    "deck-real-b-test-worker",
                }
                self.assertEqual(
                    sum(t.name in worker_names for t in threading.enumerate()), 2
                )
                critical_before = len(
                    [
                        call
                        for call in emitted.call_args_list
                        if call.kwargs["level"] == "critical"
                    ]
                )
                # [P0] 1.8 -- 3 critical events, not 2: one "timed out"
                # per poisoned slot (A and B), PLUS one "Policy restart
                # requested" from _request_restart -- fired only once
                # per process (idempotent) even though both slots
                # poisoned.
                self.assertEqual(critical_before, 3)
                self.assertTrue(self.engine.restart_required)

                # With no reusable slot, repeated start attempts fail before
                # queue selection. They cannot allocate topology, increment a
                # generation, dispatch teardown work, or emit another event.
                self.engine._queue_cursor = 17
                generation_before = self.engine._deck_generation_serial
                tasks_before = _task_count()
                threads_before = threading.active_count()
                rss_before = _rss_bytes()
                with patch.object(self.engine, "_next_queue_item") as next_item:
                    for _attempt in range(100):
                        PlaybackEngine._start_next_track(self.engine)
                        self.engine._deck_teardown_tick()
                    next_item.assert_not_called()

                self.assertEqual(self.engine._queue_cursor, 17)
                self.assertEqual(self.engine._deck_generation_serial, generation_before)
                self.assertEqual(self.engine.decks, {"A": None, "B": None})
                self.assertEqual(self.engine._deck_bin_map, {})
                self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
                self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), 2)
                self.assertEqual(_task_count(), tasks_before)
                self.assertEqual(threading.active_count(), threads_before)
                self.assertLessEqual(_rss_bytes() - rss_before, 1024 * 1024)
                self.assertEqual(
                    sum(t.name in worker_names for t in threading.enumerate()), 2
                )
                self.assertEqual(coordinator_a.snapshot()["worker_starts"], 1)
                self.assertEqual(coordinator_b.snapshot()["worker_starts"], 1)
                critical_after = len(
                    [
                        call
                        for call in emitted.call_args_list
                        if call.kwargs["level"] == "critical"
                    ]
                )
                self.assertEqual(critical_after, critical_before)
                print(
                    "dual-slot-poison-resources "
                    f"isolated_generations=2 wedged_workers=2 "
                    f"pads={len(tuple(self.engine.mixer.sinkpads))} "
                    f"children={_pipeline_child_count(self.engine.main_pipeline)} "
                    f"map={len(self.engine._deck_bin_map)} "
                    f"task_delta={_task_count() - tasks_before} "
                    f"thread_delta={threading.active_count() - threads_before} "
                    f"rss_delta={_rss_bytes() - rss_before}",
                    flush=True,
                )
        finally:
            release_a.set()
            release_b.set()
            self.assertTrue(
                _wait_until(
                    lambda: not any(
                        t.name in {
                            "deck-real-a-test-worker",
                            "deck-real-b-test-worker",
                        }
                        for t in threading.enumerate()
                    )
                )
            )
            if bad_a is not None:
                bad_a.pipeline.set_state(Gst.State.NULL)
            if bad_b is not None:
                bad_b.pipeline.set_state(Gst.State.NULL)

    def test_silence_primed_stall_uses_real_buffer_progress_not_wall_clock(self):
        long_path = Path(self.temp_dir.name) / "stall-source.wav"
        _write_wav(long_path, frames=5 * 44100)
        self.engine.main_pipeline.get_by_name("sink").set_property("sync", True)
        with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
            deck = self._create(1_200, path=long_path, duration=5.0)
            self.engine.main_pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(_wait_until(lambda: deck.media_buffer_count >= 3, timeout=2.0))

            # Replace the production real-leg accounting probe with a
            # deterministic downstream stall after some genuine decoded
            # buffers. Wall-clock presentation continues, but the actual
            # media-progress timestamp remains frozen and EOS is suppressed.
            real_pad, progress_probe_id = deck.probe_handles[0]
            real_pad.remove_probe(progress_probe_id)
            deck.probe_handles.pop(0)

            def stall_real_leg(_pad, info):
                if info.type & Gst.PadProbeType.BUFFER:
                    return Gst.PadProbeReturn.DROP
                if info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
                    event = info.get_event()
                    if event is not None and event.type == Gst.EventType.EOS:
                        return Gst.PadProbeReturn.DROP
                return Gst.PadProbeReturn.OK

            stall_probe_id = real_pad.add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM,
                stall_real_leg,
            )
            deck.probe_handles.append((real_pad, stall_probe_id))
            frozen_buffer_count = deck.media_buffer_count
            deck.last_media_buffer_monotonic = (
                time.monotonic() - DECK_STUCK_TIMEOUT_SECONDS - 1
            )
            deck.started_at = time.time() - 100.0
            position_before = self.engine._get_deck_position(deck)
            time.sleep(0.01)
            position_after = self.engine._get_deck_position(deck)
            self.assertGreater(position_after, position_before)
            self.assertEqual(deck.media_buffer_count, frozen_buffer_count)

            self.engine._check_stuck_decks()
            self.assertTrue(deck.finished)
            self.assertIn("N_BIN_REMOVAL_COMPLETE", deck.eos_milestones)
            self.assertTrue(
                _wait_until(
                    lambda: "O_NULL_TRANSITION_COMPLETE" in deck.eos_milestones
                )
            )

            good = self._create(1_201, duration=0.01)
            self.assertTrue(_wait_until(lambda: good.finished, timeout=2.0))
            self.assertIn("I_EOS_ACCEPTED", good.eos_milestones)

    def test_short_metadata_does_not_watchdog_while_real_buffers_advance(self):
        long_path = Path(self.temp_dir.name) / "bad-metadata-source.wav"
        _write_wav(long_path, frames=5 * 44100)
        self.engine.main_pipeline.get_by_name("sink").set_property("sync", True)
        with patch.object(eng_module, "SILENCE_PRIME_SECONDS", 0.001):
            deck = self._create(1_300, path=long_path, duration=0.01)
            self.engine.main_pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(_wait_until(lambda: deck.media_buffer_count >= 3, timeout=2.0))
            deck.started_at = time.time() - 100.0
            before = deck.media_buffer_count
            self.engine._check_stuck_decks()
            self.assertFalse(deck.finished)
            self.assertTrue(
                _wait_until(lambda: deck.media_buffer_count > before, timeout=1.0)
            )
            self.engine._remove_deck(deck)

    def test_one_suppressed_idle_handoff_isolated_then_good_deck_finishes(self):
        original_idle_add = GLib.idle_add

        def suppress_only_deck_completion(function, *args, **kwargs):
            if getattr(function, "__name__", "") == "_on_deck_eos_probed":
                return 0
            return original_idle_add(function, *args, **kwargs)

        bad = self._create(10)
        with patch.object(GLib, "idle_add", side_effect=suppress_only_deck_completion):
            self.engine.main_pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(_wait_until(lambda: "G_GLIB_IDLE_SCHEDULE_FAILED" in bad.eos_milestones))
        self.assertFalse(bad.finished)
        self.assertIn("E_DECK_GHOST_SRC_EOS", bad.eos_milestones)
        self.assertNotIn("H_EOS_IDLE_CALLBACK_ENTERED", bad.eos_milestones)

        started = time.monotonic()
        self.assertTrue(self.engine._remove_deck(bad))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertTrue(bad.detached_from_mixer)
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)

        good = self._create(11, slot="A")
        self.assertTrue(_wait_until(lambda: good.finished))
        self.assertIn("I_EOS_ACCEPTED", good.eos_milestones)
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)

    def test_legacy_watchdog_abandonment_can_block_every_later_deck(self):
        """Execute the pre-fix watchdog exactly, then prove the cascade.

        Suppression is at boundary G: decoder/concat/ghost EOS all existed,
        but no GLib completion handoff was scheduled. Dropping ghost EOS means
        the still-requested mixer pad never receives EOS.
        """
        original_idle_add = GLib.idle_add

        def suppress_only_deck_completion(function, *args, **kwargs):
            if getattr(function, "__name__", "") == "_on_deck_eos_probed":
                return 0
            return original_idle_add(function, *args, **kwargs)

        bad = self._create(30)
        with patch.object(GLib, "idle_add", side_effect=suppress_only_deck_completion):
            self.engine.main_pipeline.set_state(Gst.State.PLAYING)
            self.assertTrue(_wait_until(lambda: "G_GLIB_IDLE_SCHEDULE_FAILED" in bad.eos_milestones))

        # Exact legacy watchdog semantics: logical slot only. The bin, map,
        # link, and mixer request pad deliberately remain live.
        bad.finished = True
        self.engine.decks["A"] = None
        self.engine._next_triggered = False
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)
        self.assertIs(self.engine._deck_bin_map[id(bad.pipeline)], bad)
        tasks_before = _task_count()
        rss_before = _rss_bytes()
        output_before = self.engine._test_output_buffers

        later = []
        legacy_watchdogs = []
        for index in range(110):
            deck = self._create(31 + index, slot="A")
            later.append(deck)
            if not _wait_until(lambda deck=deck: deck.finished, timeout=0.03):
                # Execute the same legacy watchdog abandonment for every
                # generation which the stale mixer pad prevents completing.
                deck.finished = True
                self.engine.decks["A"] = None
                self.engine._next_triggered = False
                legacy_watchdogs.append(deck)
        self.assertGreaterEqual(len(legacy_watchdogs), 100)
        self.assertTrue(all(deck.media_buffer_count > 0 for deck in legacy_watchdogs))
        self.assertTrue(all("A_DECODER_AUDIO_EOS" not in deck.eos_milestones for deck in legacy_watchdogs))
        self.assertTrue(all("D_CONCAT_SRC_EOS" not in deck.eos_milestones for deck in legacy_watchdogs))
        self.assertTrue(all("E_DECK_GHOST_SRC_EOS" not in deck.eos_milestones for deck in legacy_watchdogs))
        mixer_output_delta = self.engine._test_output_buffers - output_before
        # A small number of already-scheduled mixer buffers may drain before
        # the stale request pad blocks aggregation. Their exact count is a
        # GStreamer scheduling detail, not an architectural boundary. The
        # deterministic legacy-cascade proof is that fewer mixer buffers than
        # abandoned generations emerge while every decoded generation misses
        # all three downstream EOS boundaries and retains its pad/map entry.
        self.assertLess(mixer_output_delta, len(legacy_watchdogs))
        self.assertEqual(
            len(tuple(self.engine.mixer.sinkpads)),
            1 + len(legacy_watchdogs),
        )
        print(
            "legacy-cascade-resources "
            f"watchdogs={len(legacy_watchdogs)} pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"map={len(self.engine._deck_bin_map)} task_delta={_task_count() - tasks_before} "
            f"rss_delta={_rss_bytes() - rss_before} mixer_output_delta={mixer_output_delta}",
            flush=True,
        )

        # Detach every leaked generation, then prove a fresh good deck crosses
        # EOS normally. The production fix performs this on each watchdog, so
        # the accumulation never starts.
        completed_before_cleanup = self.engine._deck_teardowns["A"].snapshot()["completed"]
        for index, leaked in enumerate([bad, *legacy_watchdogs], start=1):
            self.engine._remove_deck(leaked)
            self.assertTrue(
                _wait_until(
                    lambda expected=completed_before_cleanup + index: self.engine._deck_teardowns["A"].snapshot()["completed"]
                    == expected
                )
            )
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
        self.assertEqual(self.engine._deck_bin_map, {})
        # GStreamer may retain stopped task-pool threads briefly after this
        # deliberately pathological 111-bin legacy teardown. Completion of
        # every explicit NULL operation and zero live topology are the stable
        # cleanup boundaries; current-code task slope is covered separately by
        # the contained-storm and dual-slot poison tests.
        later.clear()
        legacy_watchdogs.clear()
        gc.collect()
        final_good = self._create(500, slot="A")
        self.assertTrue(_wait_until(lambda: final_good.finished))

    def test_one_hundred_forced_misses_are_contained_without_resource_growth(self):
        original_idle_add = GLib.idle_add

        def suppress_only_deck_completion(function, *args, **kwargs):
            if getattr(function, "__name__", "") == "_on_deck_eos_probed":
                return 0
            return original_idle_add(function, *args, **kwargs)

        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        warmup = self._create(799)
        self.assertTrue(_wait_until(lambda: warmup.finished))
        self.assertTrue(_wait_until(lambda: self.engine._deck_teardowns["A"].snapshot()["completed"] == 1))
        rss_start = _rss_bytes()
        tasks_start = _task_count()
        threads_start = threading.active_count()
        output_start = self.engine._test_output_buffers
        with patch.object(GLib, "idle_add", side_effect=suppress_only_deck_completion):
            for index, generation in enumerate(range(800, 900), start=1):
                deck = self._create(generation)
                self.assertTrue(
                    _wait_until(lambda deck=deck: "G_GLIB_IDLE_SCHEDULE_FAILED" in deck.eos_milestones)
                )
                self.engine._remove_deck(deck)
                self.assertTrue(
                    _wait_until(
                        lambda expected=index: self.engine._deck_teardowns["A"].snapshot()["completed"]
                        == expected + 1
                    )
                )
        self.assertTrue(
            _wait_until(lambda: self.engine._deck_teardowns["A"].snapshot()["completed"] == 101)
        )
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(self.engine.decks, {"A": None, "B": None})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), 2)
        self.assertGreater(self.engine._test_output_buffers, output_start)
        self.assertEqual(self.engine._deck_teardowns["A"].snapshot()["worker_starts"], 1)
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        self.assertLessEqual(_task_count(), tasks_start + 2)
        self.assertLess(_rss_bytes() - rss_start, 32 * 1024 * 1024)
        print(
            "contained-storm-resources "
            f"watchdogs=100 pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"map={len(self.engine._deck_bin_map)} task_delta={_task_count() - tasks_start} "
            f"thread_delta={threading.active_count() - threads_start} "
            f"rss_delta={_rss_bytes() - rss_start}",
            flush=True,
        )
        good = self._create(901)
        self.assertTrue(_wait_until(lambda: good.finished))

    def test_crossfade_pair_and_pair_after_contained_watchdog_both_complete(self):
        first = self._create(20, slot="A")
        second = self._create(21, slot="B")
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(_wait_until(lambda: first.finished and second.finished))
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)

        bad = self._create(22, slot="A")
        self.engine._remove_deck(bad)
        after_a = self._create(23, slot="A")
        after_b = self._create(24, slot="B")
        self.assertTrue(_wait_until(lambda: after_a.finished and after_b.finished))
        self.assertEqual(self.engine._deck_bin_map, {})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 0)

    def test_decoder_error_is_isolated_and_does_not_contaminate_good_followup(self):
        malformed = Path(self.temp_dir.name) / "malformed.mp3"
        survivor_path = Path(self.temp_dir.name) / "survivor.wav"
        malformed.write_bytes(b"not an audio stream")
        _write_wav(survivor_path, frames=88200)
        self.engine.main_pipeline.get_by_name("sink").set_property("sync", True)
        survivor = self._create(699, slot="B", path=survivor_path)
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        self.assertTrue(_wait_until(lambda: survivor.media_buffer_count > 0))
        bad = self._create(700, path=malformed)
        self.assertTrue(_wait_until(lambda: bad.finished))
        self.assertIn("ERROR_OBSERVED", bad.eos_milestones)
        self.assertIn("N_BIN_REMOVAL_COMPLETE", bad.eos_milestones)
        good = self._create(701)
        self.assertTrue(_wait_until(lambda: good.finished), good.milestone_snapshot())
        self.assertIn("I_EOS_ACCEPTED", good.eos_milestones)
        self.assertEqual(set(self.engine._deck_bin_map.values()), {survivor})
        self.assertEqual(len(tuple(self.engine.mixer.sinkpads)), 1)
        self.engine._remove_deck(survivor)


class DeckGenerationAndWatchdogTests(SimpleTestCase):
    def setUp(self):
        # [P0] 1.8 -- keep _request_restart's atomic marker write out of
        # the real /run/isadoraair/. Also lets each test observe/assert
        # the marker file directly without cross-test contamination.
        self._marker_tmpdir = tempfile.TemporaryDirectory(prefix="isa-p0-1.8-marker.")
        self.addCleanup(self._marker_tmpdir.cleanup)
        self.marker_path = Path(self._marker_tmpdir.name) / "last_policy_restart.json"
        self._marker_patch = patch.object(eng_module, "POLICY_RESTART_MARKER_PATH", self.marker_path)
        self._marker_patch.start()
        self.addCleanup(self._marker_patch.stop)

    def _deck(self, *, generation=1, duration=60.0, slot="A",
              log_item_id=None, playlist_log_id=None):
        track = MagicMock(id=generation, title=f"Track {generation}", duration_seconds=duration)
        pipeline = MagicMock()
        pipeline.query_duration.return_value = (False, Gst.CLOCK_TIME_NONE)
        pipeline.set_state.return_value = Gst.StateChangeReturn.SUCCESS
        pipeline.get_static_pad.return_value.unlink.return_value = True
        log_item = MagicMock()
        # Concrete ints when caller wants marker identity to survive the
        # isinstance(int) checks in _request_restart; None otherwise.
        log_item.id = log_item_id if log_item_id is not None else generation * 100
        log_item.playlist_log_id = playlist_log_id
        return Deck(
            slot,
            track,
            log_item,
            pipeline,
            MagicMock(),
            generation=generation,
        )

    def _engine(self, deck):
        engine = object.__new__(PlaybackEngine)
        engine._lock = threading.RLock()
        engine.decks = {"A": deck if deck.slot == "A" else None, "B": deck if deck.slot == "B" else None}
        engine._deck_bin_map = {id(deck.pipeline): deck}
        engine._deck_watchdog_times = eng_module.deque()
        engine._deck_teardowns = {
            slot: audio_recovery.BoundedTeardownCoordinator(
                f"deck-mock-{slot.lower()}-test", timeout_s=0.05, queue_capacity=2
            )
            for slot in ("A", "B")
        }
        engine.mixer = MagicMock()
        engine.mixer.sinkpads = [deck.mixer_pad]
        engine.main_pipeline = MagicMock()
        engine.main_pipeline.remove.return_value = True
        engine.manual_mode = False
        engine._next_triggered = True
        engine._start_next_track = MagicMock()
        engine._vt = {"phase": "idle", "outgoing_track_id": None}
        # loop is what _request_restart tries to quit(); MagicMock keeps
        # the try/except path a no-op instead of an AttributeError.
        engine.loop = MagicMock()
        _init_policy_restart_attrs(engine)
        return engine

    def test_stale_late_callback_cannot_retire_replacement_generation(self):
        old = self._deck(generation=1)
        new = self._deck(generation=2)
        engine = self._engine(new)
        engine._on_deck_eos_probed(old.pipeline, old)
        self.assertIs(engine.decks["A"], new)
        self.assertFalse(new.finished)
        self.assertIn("H_STALE_EOS_CALLBACK_IGNORED", old.eos_milestones)

    def test_duplicate_eos_callback_is_harmless(self):
        deck = self._deck()
        engine = self._engine(deck)
        engine._remove_deck = MagicMock(side_effect=lambda value: setattr(value, "finished", True))
        engine._on_deck_eos_probed(deck.pipeline, deck)
        engine._on_deck_eos_probed(deck.pipeline, deck)
        engine._remove_deck.assert_called_once_with(deck)

    def test_duration_more_than_thirty_seconds_short_does_not_cut_live_buffers(self):
        deck = self._deck(duration=10.0)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=41.0)
        deck.last_media_buffer_monotonic = time.monotonic()
        engine._remove_deck = MagicMock()
        with patch.object(eng_module, "emit_event") as emitted:
            engine._check_stuck_decks()
        engine._remove_deck.assert_not_called()

    def test_correct_slightly_short_too_long_and_missing_duration_do_not_false_watchdog(self):
        cases = (
            (60.0, 59.0),
            (55.0, 59.0),
            (600.0, 91.0),
            (None, 91.0),
        )
        for duration, position in cases:
            with self.subTest(duration=duration, position=position):
                deck = self._deck(duration=duration)
                engine = self._engine(deck)
                engine._get_deck_position = MagicMock(return_value=position)
                deck.last_media_buffer_monotonic = time.monotonic() - 120
                engine._remove_deck = MagicMock()
                with patch.object(eng_module, "emit_event"):
                    engine._check_stuck_decks()
                engine._remove_deck.assert_not_called()

    def test_decoder_reported_duration_overrides_too_short_metadata(self):
        deck = self._deck(duration=10.0)
        deck.pipeline.query_duration.return_value = (True, 60 * Gst.SECOND)
        deck.last_media_buffer_monotonic = time.monotonic() - 120
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=41.0)
        engine._remove_deck = MagicMock()
        with patch.object(eng_module, "emit_event"):
            engine._check_stuck_decks()
        engine._remove_deck.assert_not_called()
        self.assertEqual(deck.observed_duration_seconds, 60.0)

    def test_duration_overrun_plus_no_progress_is_contained(self):
        deck = self._deck(duration=10.0)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=41.0)
        deck.last_media_buffer_monotonic = time.monotonic() - DECK_STUCK_TIMEOUT_SECONDS - 1
        engine._remove_deck = MagicMock(side_effect=lambda value: setattr(value, "finished", True))
        with patch.object(eng_module, "emit_event") as emitted:
            engine._check_stuck_decks()
        engine._remove_deck.assert_called_once_with(deck)
        self.assertEqual(emitted.call_args.kwargs["detail"]["reason"], "duration_overrun_without_progress")

    def test_watchdog_persists_exact_incident_only_after_retire_and_advance(self):
        deck = self._deck(duration=10.0)
        deck.track.filepath = "/srv/music/exact-suspect.wav"
        deck.last_media_buffer_monotonic = time.monotonic() - DECK_STUCK_TIMEOUT_SECONDS - 1
        deck.mark_milestone("A_DECODER_AUDIO_EOS")
        engine = self._engine(deck)
        engine._runtime_commit = "runtime-commit"
        engine._media_validation_worker = MagicMock()
        engine._get_deck_position = MagicMock(return_value=41.0)
        order = []

        def retire(value):
            order.append("retire")
            value.finished = True
            engine.decks[value.slot] = None

        engine._remove_deck = MagicMock(side_effect=retire)
        engine._start_next_track = MagicMock(side_effect=lambda **_kwargs: order.append("advance"))
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "create_incident", side_effect=lambda evidence: order.append("persist") or SimpleNamespace(pk=1)) as create:
            engine._check_stuck_decks()
        self.assertEqual(order, ["retire", "advance", "persist"])
        evidence = create.call_args.args[0]
        self.assertEqual(evidence["filepath_snapshot"], "/srv/music/exact-suspect.wav")
        self.assertEqual(evidence["deck_generation"], deck.generation)
        self.assertEqual(evidence["trigger"], "watchdog_stall")
        self.assertIn("A_DECODER_AUDIO_EOS", evidence["eos_snapshot"]["milestones"])
        engine._media_validation_worker.wake.assert_called_once()

    def test_deck_error_retirement_precedes_incident_persistence(self):
        deck = self._deck()
        deck.track.filepath = "/srv/music/decode-error.mp3"
        engine = self._engine(deck)
        engine._runtime_commit = "runtime-commit"
        engine._media_validation_worker = MagicMock()
        message = MagicMock()
        message.parse_error.return_value = (RuntimeError("decode failed"), "decoder debug")
        scheduled = {}

        def capture_idle(callback, *args):
            scheduled.update(callback=callback, args=args)
            return 1

        with patch.object(eng_module.GLib, "idle_add", side_effect=capture_idle), \
             patch.object(eng_module, "emit_event"):
            self.assertTrue(engine._on_deck_error(None, message, deck))
        evidence = scheduled["args"][1]
        self.assertEqual(evidence["trigger"], "deck_pipeline_error")
        self.assertIn("decode failed", evidence["gstreamer_error"])
        order = []
        engine._handle_deck_finished = MagicMock(side_effect=lambda value: order.append("retire"))
        with patch.object(eng_module, "create_incident", side_effect=lambda value: order.append("persist") or SimpleNamespace(pk=2)):
            scheduled["callback"](*scheduled["args"])
        self.assertEqual(order, ["retire", "persist"])

    def test_only_watchdog_and_exact_deck_error_capture_media_incidents(self):
        source = Path(eng_module.__file__).read_text()
        self.assertEqual(source.count("capture_deck_evidence("), 2)
        self.assertIn("capture_deck_evidence(", inspect.getsource(PlaybackEngine._check_stuck_decks))
        self.assertIn("capture_deck_evidence(", inspect.getsource(PlaybackEngine._on_deck_error))
        for handler in (
            PlaybackEngine._on_main_bus_error,
            PlaybackEngine._on_mic_error,
            PlaybackEngine._on_output_error,
        ):
            self.assertNotIn("capture_deck_evidence", inspect.getsource(handler))

    def test_missing_metadata_can_recover_from_observed_unhandled_eos(self):
        deck = self._deck(duration=None)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        deck.mark_milestone(
            "D_CONCAT_SRC_EOS",
            now=time.monotonic() - DECK_STUCK_TIMEOUT_SECONDS - 1,
        )
        engine._remove_deck = MagicMock(side_effect=lambda value: setattr(value, "finished", True))
        with patch.object(eng_module, "emit_event") as emitted:
            engine._check_stuck_decks()
        engine._remove_deck.assert_called_once_with(deck)
        self.assertEqual(emitted.call_args.kwargs["detail"]["reason"], "unhandled_eos")

    def test_repeated_watchdogs_escalate_with_bounded_structured_state(self):
        first = self._deck(generation=1, duration=10.0)
        engine = self._engine(first)
        engine._get_deck_position = MagicMock(return_value=41.0)
        with patch.object(eng_module, "emit_event") as emitted:
            for generation in range(1, 4):
                deck = first if generation == 1 else self._deck(generation=generation, duration=10.0)
                deck.last_media_buffer_monotonic = time.monotonic() - 60
                engine.decks["A"] = deck
                engine._deck_bin_map[id(deck.pipeline)] = deck
                engine._remove_deck = MagicMock(side_effect=lambda value: setattr(value, "finished", True))
                engine._check_stuck_decks()
        details = [call.kwargs["detail"] for call in emitted.call_args_list]
        self.assertEqual([item["watchdogs_in_window"] for item in details], [1, 2, 3])
        self.assertFalse(details[1]["restart_recommended"])
        self.assertTrue(details[2]["restart_recommended"])
        self.assertIn("eos", details[2])
        self.assertIn("active_deck_generations", details[2])
        self.assertIn("mixer_sink_pad_count", details[2])

    def test_detach_first_returns_while_null_worker_is_deliberately_wedged(self):
        # [P0] 1.8 -- pass concrete int identities so _request_restart's
        # isinstance(int) marker-persistence guard survives, letting the
        # test observe the exact (playlist_log_id, log_item_id) that was
        # written to the anti-replay marker file.
        deck = self._deck(playlist_log_id=999, log_item_id=1234)
        engine = self._engine(deck)
        release = threading.Event()
        deck.pipeline.set_state.side_effect = release.wait
        with patch.object(eng_module, "emit_event") as emitted:
            started = time.monotonic()
            engine._remove_deck(deck)
            self.assertLess(time.monotonic() - started, 0.05)
            self.assertNotIn(id(deck.pipeline), engine._deck_bin_map)
            self.assertIsNone(engine.decks["A"])
            engine.mixer.release_request_pad.assert_called_once()
            engine.main_pipeline.remove.assert_called_once_with(deck.pipeline)
            self.assertTrue(
                _wait_until(lambda: engine._deck_teardowns["A"].snapshot()["active_generation"] == 1)
            )
            engine._deck_teardowns["A"].tick(now=time.monotonic() + 1.0)
            engine.running = True
            engine._start_next_track.reset_mock()
            engine._deck_teardown_tick()
            self.assertTrue(
                any(
                    call.kwargs["level"] == "critical"
                    and "timed out" in call.kwargs["title"]
                    and call.kwargs["detail"]["restart_recommended"]
                    for call in emitted.call_args_list
                )
            )
            # [P0] 1.8 -- restart requested, marker atomically written,
            # loop quit invoked exactly once.
            self.assertTrue(engine.restart_required)
            self.assertTrue(self.marker_path.exists(),
                            "poison marker must be written for the next process to consume")
            engine.loop.quit.assert_called_once()
            marker = json.loads(self.marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["reason"], "deck_teardown_poisoned")
            self.assertEqual(marker["last_event"]["slot"], "A")
            self.assertEqual(marker["last_event"]["log_item_id"], 1234)
            self.assertEqual(marker["last_event"]["playlist_log_id"], 999)
            self.assertEqual(
                marker["skip"],
                [{"playlist_log_id": 999, "log_item_id": 1234}],
            )
            self.assertIn((999, 1234), engine._poison_skip_identities)
            self.assertFalse(engine._deck_slot_available("A"))
            self.assertTrue(engine._deck_slot_available("B"))
            for index, generation in enumerate(range(2, 102), start=1):
                later = self._deck(generation=generation, slot="B")
                engine.decks["B"] = later
                engine._deck_bin_map[id(later.pipeline)] = later
                engine._remove_deck(later)
                self.assertTrue(
                    _wait_until(
                        lambda expected=index: engine._deck_teardowns["B"].snapshot()["completed"]
                        == expected
                    )
                )
            # [P0] 1.8 -- repeated _deck_teardown_tick observations of
            # the same poison must not double-request restart.
            for _ in range(5):
                engine._deck_teardown_tick()
            snapshot = engine._deck_teardowns["A"].snapshot()
            self.assertEqual(snapshot["worker_starts"], 1)
            self.assertEqual(snapshot["active_generation"], 1)
            self.assertEqual(engine._deck_teardowns["B"].snapshot()["completed"], 100)
            engine.loop.quit.assert_called_once()  # still exactly one
            critical_events = [
                call for call in emitted.call_args_list
                if call.kwargs["level"] == "critical"
            ]
            # Two critical events: one "timed out" from the coordinator
            # drain, one "Policy restart requested" from _request_restart.
            self.assertEqual(len(critical_events), 2)
            self.assertTrue(any(
                "Policy restart requested" in call.kwargs["title"]
                for call in critical_events
            ))
            state = engine._deck_recovery_state()
            self.assertEqual(state["health"], "RESTART_REQUIRED_GENERATION_ISOLATED")
            self.assertEqual(state["poisoned_slots"], ["A"])
            self.assertTrue(state["playout_continues_on_remaining_slot"])
            self.assertTrue(state["restart_required_to_reclaim_generation"])
            engine._start_next_track.assert_called_once_with()
            self.assertEqual(engine._deck_bin_map, {})
            self.assertEqual(engine.decks, {"A": None, "B": None})
        release.set()
        self.assertTrue(
            _wait_until(
                lambda: not any(t.name == "deck-mock-a-test-worker" for t in threading.enumerate())
            )
        )
        engine._deck_teardowns["B"].stop()

    # ==================================================================
    # [P0] 1.8 WRJE follow-up -- deferred post-seek-EOS re-check tests.
    # ==================================================================

    def _seeked_deck(self, *, generation=1, duration=180.0, slot="A",
                    seek_seconds_ago=1.0, media_buffer_count=0):
        deck = self._deck(generation=generation, duration=duration, slot=slot)
        deck.seeked_at = time.time() - seek_seconds_ago
        deck.media_buffer_count = media_buffer_count
        return deck

    def _reject_eos_and_capture_deferred(self, engine, deck):
        """Invoke _on_deck_eos_probed under a GLib.timeout_add_seconds
        capture so we can drive the deferred callback synchronously.
        Returns (callback, expected_generation)."""
        captured = []

        def capture(_delay_s, fn, *args, **_kwargs):
            captured.append((fn, args))
            return 1  # a non-zero source id keeps the caller happy

        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            engine._on_deck_eos_probed(deck.pipeline, deck)
        return captured

    def test_premature_post_seek_eos_with_buffer_recovery_lets_deck_continue(self):
        # Stale duration_seconds=180 vs. pos=5 -> looks implausibly early.
        deck = self._seeked_deck(duration=180.0, media_buffer_count=10)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = self._reject_eos_and_capture_deferred(engine, deck)
        self.assertEqual(len(captured), 1,
            "one deferred re-check scheduled on rejection")
        self.assertTrue(deck.deferred_seek_eos_pending)
        self.assertEqual(deck.deferred_seek_eos_baseline, 10)
        self.assertIn("I_EOS_REJECTED_POST_SEEK", deck.eos_milestones)

        # Real media buffers arrive AFTER the rejection -- parser hiccup
        # confirmed transient. Deferred callback must leave the deck alive.
        deck.media_buffer_count = 42
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_not_called()
        self.assertFalse(deck.finished)
        self.assertFalse(deck.completion_claimed)
        self.assertFalse(deck.deferred_seek_eos_pending)
        self.assertIn("I_EOS_POST_SEEK_RECOVERED", deck.eos_milestones)

    def test_post_seek_eos_without_buffer_recovery_completes_after_guard(self):
        deck = self._seeked_deck(duration=180.0, media_buffer_count=10)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = self._reject_eos_and_capture_deferred(engine, deck)
        self.assertEqual(len(captured), 1)

        # No new buffers arrived -- deferred callback must accept EOS
        # as genuine and retire the deck via the ordinary finished path.
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event") as emitted:
            fn(*args)
        engine._handle_deck_finished.assert_called_once_with(deck)
        self.assertTrue(deck.completion_claimed)
        self.assertEqual(deck.completion_reason, "eos_deferred")
        self.assertIn("I_EOS_DEFERRED_ACCEPTED", deck.eos_milestones)
        deferred_events = [
            call for call in emitted.call_args_list
            if "Deferred post-seek EOS" in call.kwargs.get("title", "")
        ]
        self.assertEqual(len(deferred_events), 1)

    def test_stale_metadata_duration_does_not_delay_completion_to_duration_plus_thirty(self):
        """WRJE scenario: Track.duration_seconds=7194 (stale), the real
        audio is much shorter, deck gets to genuine EOS at pos~=60.
        Without the deferred re-check the EOS would be rejected and the
        deck would only complete when _check_stuck_decks fires ~30s past
        the METADATA duration -- effectively minutes/hours of dead
        deck. With the fix, completion happens inside ~SEEK_EOS_GUARD_SECONDS."""
        deck = self._seeked_deck(duration=7194.0, media_buffer_count=200)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=60.0)
        engine._handle_deck_finished = MagicMock()

        captured = self._reject_eos_and_capture_deferred(engine, deck)
        # Sanity: the classic guard math WAS triggered (60 < 7194 - 30).
        self.assertIn("I_EOS_REJECTED_POST_SEEK", deck.eos_milestones)
        # Deferred callback fires ~6s later (SEEK_EOS_GUARD_SECONDS+1),
        # not ~7194s later. Buffers did not advance -> completion runs.
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_called_once_with(deck)
        self.assertTrue(deck.completion_claimed)

    def test_deferred_callback_is_harmless_when_generation_replaced(self):
        old = self._seeked_deck(generation=1, duration=180.0)
        engine = self._engine(old)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = self._reject_eos_and_capture_deferred(engine, old)
        fn, args = captured[0]

        # Between rejection and the deferred callback the slot was
        # replaced: old deck retired, new generation installed.
        old.retirement_started = True
        engine._deck_bin_map.pop(id(old.pipeline), None)
        new = self._deck(generation=2, slot="A")
        engine.decks["A"] = new
        engine._deck_bin_map[id(new.pipeline)] = new

        with patch.object(eng_module, "emit_event"):
            result = fn(*args)
        # Return False so GLib auto-removes the timeout source.
        self.assertFalse(result)
        engine._handle_deck_finished.assert_not_called()
        self.assertFalse(new.completion_claimed,
            "the replacement generation must NEVER be affected by the old deck's deferred callback")
        self.assertNotIn("I_EOS_DEFERRED_ACCEPTED", new.eos_milestones)

    def test_deferred_callback_does_not_double_complete_when_racing_watchdog(self):
        deck = self._seeked_deck(duration=180.0)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = self._reject_eos_and_capture_deferred(engine, deck)
        fn, args = captured[0]

        # Between rejection and deferred callback, a watchdog / error /
        # EOS race retires the deck first and claims completion.
        self.assertTrue(deck.claim_completion("watchdog"))
        # Then the deferred callback fires. It must NOT call
        # _handle_deck_finished a second time.
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_not_called()
        self.assertEqual(deck.completion_reason, "watchdog",
            "the earlier winner's completion_reason must be preserved")

    def test_second_eos_rejection_in_window_does_not_stack_deferred_callbacks(self):
        deck = self._seeked_deck(duration=180.0, media_buffer_count=10)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)

        captured_all = []

        def capture(_delay_s, fn, *args, **_kwargs):
            captured_all.append((fn, args))
            return 1

        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            engine._on_deck_eos_probed(deck.pipeline, deck)
            # Second spurious EOS inside the window -- must NOT schedule
            # a second deferred callback.
            engine._on_deck_eos_probed(deck.pipeline, deck)
            engine._on_deck_eos_probed(deck.pipeline, deck)
        self.assertEqual(len(captured_all), 1)

    def test_second_eos_after_apparent_recovery_still_detects_genuine_end(self):
        """WRJE follow-up hole: EOS#1 rejected (baseline=10), buffers
        appear to recover (count=42), then a GENUINE EOS#2 arrives
        still inside the same seek-guard window. Without the per-
        rejection baseline refresh, the deferred callback would compare
        current=42 against baseline=10 and incorrectly conclude the
        deck recovered."""
        deck = self._seeked_deck(duration=180.0, media_buffer_count=10)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = []

        def capture(_delay_s, fn, *args, **_kwargs):
            captured.append((fn, args))
            return 1

        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            # EOS#1 -- baseline snapped at 10, one callback scheduled.
            engine._on_deck_eos_probed(deck.pipeline, deck)
            self.assertEqual(deck.deferred_seek_eos_baseline, 10)
            # Buffers appear to recover.
            deck.media_buffer_count = 42
            # EOS#2 (genuine end of a much-shorter-than-metadata track)
            # inside the same window. Baseline MUST refresh to 42; no
            # second callback is scheduled.
            engine._on_deck_eos_probed(deck.pipeline, deck)
        self.assertEqual(len(captured), 1)
        self.assertEqual(deck.deferred_seek_eos_baseline, 42)

        # No new buffers after EOS#2 -- deferred callback must complete
        # the deck instead of being fooled by the pre-EOS#1 recovery.
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_called_once_with(deck)
        self.assertTrue(deck.completion_claimed)
        self.assertEqual(deck.completion_reason, "eos_deferred")
        self.assertIn("I_EOS_DEFERRED_ACCEPTED", deck.eos_milestones)

    def test_second_eos_after_recovery_with_ongoing_recovery_leaves_deck_running(self):
        """Companion to the previous test: EOS#1 rejected, buffers
        recover, EOS#2 rejected (baseline refreshed), buffers keep
        advancing after EOS#2 -- deferred callback observes real
        forward progress and must leave the deck alive."""
        deck = self._seeked_deck(duration=180.0, media_buffer_count=10)
        engine = self._engine(deck)
        engine._get_deck_position = MagicMock(return_value=5.0)
        engine._handle_deck_finished = MagicMock()

        captured = []

        def capture(_delay_s, fn, *args, **_kwargs):
            captured.append((fn, args))
            return 1

        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            engine._on_deck_eos_probed(deck.pipeline, deck)
            self.assertEqual(deck.deferred_seek_eos_baseline, 10)
            deck.media_buffer_count = 42
            engine._on_deck_eos_probed(deck.pipeline, deck)
        self.assertEqual(deck.deferred_seek_eos_baseline, 42)
        self.assertEqual(len(captured), 1)

        # Real buffers advance past the refreshed baseline before the
        # deferred callback fires -- confirmed still-recovering, deck
        # must remain alive.
        deck.media_buffer_count = 60
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_not_called()
        self.assertFalse(deck.completion_claimed)
        self.assertIn("I_EOS_POST_SEEK_RECOVERED", deck.eos_milestones)


class RealWedgedDetachOperationTests(SimpleTestCase):
    helper = Path(__file__).with_name("_deck_detach_probe.py")

    def test_unlink_release_and_remove_are_bounded_but_null_can_hang(self):
        for operation in ("unlink", "release", "remove"):
            result = subprocess.run(
                [sys.executable, str(self.helper), operation],
                capture_output=True,
                text=True,
                timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            elapsed = float(result.stdout.split()[1])
            self.assertLess(elapsed, 0.1, f"{operation} was not bounded")
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(
                [sys.executable, str(self.helper), "null"],
                capture_output=True,
                text=True,
                timeout=0.5,
            )


class MediaTerminationClassificationTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-deck-media.")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _classify(self, path, timeout=2.0):
        pipeline = Gst.parse_launch(
            f'filesrc location="{path}" ! decodebin ! audioconvert ! fakesink sync=false'
        )
        try:
            pipeline.set_state(Gst.State.PLAYING)
            message = pipeline.get_bus().timed_pop_filtered(
                int(timeout * Gst.SECOND),
                Gst.MessageType.ERROR | Gst.MessageType.EOS,
            )
            if message is None:
                return "STALL"
            return "ERROR" if message.type == Gst.MessageType.ERROR else "EOS"
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_valid_and_very_short_wav_end_cleanly(self):
        valid = self.root / "valid.wav"
        very_short = self.root / "very-short.wav"
        _write_wav(valid, frames=441)
        _write_wav(very_short, frames=1)
        self.assertEqual(self._classify(valid), "EOS")
        self.assertEqual(self._classify(very_short), "EOS")

    def test_zero_length_and_malformed_input_report_error(self):
        empty = self.root / "empty.wav"
        malformed = self.root / "malformed.mp3"
        empty.write_bytes(b"")
        malformed.write_bytes(b"this is not media")
        self.assertEqual(self._classify(empty), "ERROR")
        self.assertEqual(self._classify(malformed), "ERROR")

    def test_truncated_wav_and_mp3_terminate_instead_of_stalling(self):
        wav_path = self.root / "source.wav"
        truncated_wav = self.root / "truncated.wav"
        mp3_path = self.root / "source.mp3"
        truncated_mp3 = self.root / "truncated.mp3"
        _write_wav(wav_path, frames=44100)
        wav_bytes = wav_path.read_bytes()
        truncated_wav.write_bytes(wav_bytes[: len(wav_bytes) // 2])
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav_path), str(mp3_path),
            ],
            check=True,
            timeout=10,
        )
        mp3_bytes = mp3_path.read_bytes()
        truncated_mp3.write_bytes(mp3_bytes[: len(mp3_bytes) // 2])
        self.assertEqual(self._classify(truncated_wav), "EOS")
        self.assertEqual(self._classify(mp3_path), "EOS")
        self.assertEqual(self._classify(truncated_mp3), "EOS")


# ==========================================================================
# [P0] 1.8 -- policy-restart / poisoned-slot anti-replay tests.
# ==========================================================================


class PolicyRestartMechanismTests(SimpleTestCase):
    """Marker-shape, atomic-replace, bounded-set, per-process report
    dedupe, and idempotency of _request_restart. In-memory only (no
    real GStreamer, no real DB) -- the mechanism is exercised through
    the same object.__new__(PlaybackEngine) fixture the other unit-
    level lifecycle tests use."""

    def setUp(self):
        self._marker_tmpdir = tempfile.TemporaryDirectory(prefix="isa-p0-1.8-mech.")
        self.addCleanup(self._marker_tmpdir.cleanup)
        self.marker_path = Path(self._marker_tmpdir.name) / "last_policy_restart.json"
        self._marker_patch = patch.object(eng_module, "POLICY_RESTART_MARKER_PATH", self.marker_path)
        self._marker_patch.start()
        self.addCleanup(self._marker_patch.stop)

    def _bare(self):
        engine = object.__new__(PlaybackEngine)
        _init_policy_restart_attrs(engine)
        engine.loop = MagicMock()
        # Mirror __init__'s marker consumption (the real __init__ builds
        # a full pipeline, which is too heavy for these mechanism tests).
        engine._poison_skip_identities = engine._load_policy_restart_marker()
        return engine

    def test_load_marker_returns_empty_list_when_file_missing(self):
        eng = self._bare()
        self.assertEqual(eng._load_policy_restart_marker(), [])

    def test_load_marker_malformed_json_yields_empty_guard_no_raise(self):
        self.marker_path.write_text("{not valid json at all", encoding="utf-8")
        eng = self._bare()
        self.assertEqual(eng._load_policy_restart_marker(), [])

    def test_load_marker_missing_skip_key_yields_empty_guard(self):
        self.marker_path.write_text(json.dumps({"reason": "x"}), encoding="utf-8")
        eng = self._bare()
        self.assertEqual(eng._load_policy_restart_marker(), [])

    def test_load_marker_non_int_entries_are_ignored(self):
        self.marker_path.write_text(json.dumps({
            "skip": [
                {"playlist_log_id": "not-int", "log_item_id": 5},
                {"playlist_log_id": 7, "log_item_id": None},
                {"playlist_log_id": 8, "log_item_id": 9},
            ],
        }), encoding="utf-8")
        eng = self._bare()
        self.assertEqual(eng._load_policy_restart_marker(), [(8, 9)])

    def test_request_restart_writes_atomic_marker_via_same_dir_tmp(self):
        eng = self._bare()
        original_replace = eng_module.os.replace
        replace_calls = []

        def track_replace(src, dst):
            self.assertEqual(Path(src).parent, Path(dst).parent,
                "tmp and dst MUST be in the same directory for POSIX atomicity")
            replace_calls.append((str(src), str(dst)))
            return original_replace(src, dst)

        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.os, "replace", side_effect=track_replace):
            eng._request_restart(
                reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": 42, "playlist_log_id": 7},
            )
        self.assertEqual(len(replace_calls), 1)
        self.assertTrue(eng.restart_required)
        # No stray tmp file left behind.
        self.assertFalse(self.marker_path.with_suffix(".json.tmp").exists())
        result = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.assertEqual(result["reason"], "deck_teardown_poisoned")
        self.assertEqual(result["skip"], [{"playlist_log_id": 7, "log_item_id": 42}])
        eng.loop.quit.assert_called_once()

    def test_request_restart_is_idempotent_second_call_is_noop(self):
        eng = self._bare()
        with patch.object(eng_module, "emit_event"):
            eng._request_restart(reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": 42, "playlist_log_id": 7})
            first_mtime = self.marker_path.stat().st_mtime_ns
            time.sleep(0.005)
            eng._request_restart(reason="deck_teardown_poisoned",
                detail={"slot": "B", "generation": 2,
                        "log_item_id": 99, "playlist_log_id": 7})
        # Second call was a full no-op: marker unchanged.
        self.assertEqual(self.marker_path.stat().st_mtime_ns, first_mtime)
        result = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.assertEqual(result["skip"], [{"playlist_log_id": 7, "log_item_id": 42}])
        eng.loop.quit.assert_called_once()

    def test_request_restart_unions_with_existing_marker_and_bounds_to_max(self):
        # Seed marker with 15 identities (below the cap).
        seed = [{"playlist_log_id": 100, "log_item_id": i} for i in range(15)]
        self.marker_path.write_text(json.dumps({"skip": seed}), encoding="utf-8")
        eng = self._bare()
        self.assertEqual(len(eng._poison_skip_identities), 15)
        # Poison a 16th identity: reaches but does not exceed the cap.
        with patch.object(eng_module, "emit_event"):
            eng._request_restart(reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": 999, "playlist_log_id": 100})
        result = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.assertEqual(len(result["skip"]),
            eng_module.POLICY_RESTART_MAX_SKIP_IDENTITIES)
        self.assertEqual(result["skip"][-1],
            {"playlist_log_id": 100, "log_item_id": 999})

    def test_request_restart_over_cap_keeps_newest_identities(self):
        # Seed marker with the cap already reached.
        cap = eng_module.POLICY_RESTART_MAX_SKIP_IDENTITIES
        seed = [{"playlist_log_id": 100, "log_item_id": i} for i in range(cap)]
        self.marker_path.write_text(json.dumps({"skip": seed}), encoding="utf-8")
        eng = self._bare()
        # Add one MORE identity than the cap allows.
        with patch.object(eng_module, "emit_event"):
            eng._request_restart(reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": 999_999, "playlist_log_id": 100})
        result = json.loads(self.marker_path.read_text(encoding="utf-8"))
        skip_ids = [(e["playlist_log_id"], e["log_item_id"]) for e in result["skip"]]
        self.assertEqual(len(skip_ids), cap)
        # Oldest identity (i=0) was dropped; newest (999_999) survives.
        self.assertNotIn((100, 0), skip_ids)
        self.assertIn((100, 999_999), skip_ids)
        self.assertEqual(skip_ids[-1], (100, 999_999))

    def test_report_hit_once_deduplicates_per_process(self):
        eng = self._bare()
        eng._poison_skip_identities = [(7, 42)]
        item = MagicMock(id=42, playlist_log_id=7)
        item.track.id = 1
        item.track.title = "T"
        with patch.object(eng_module, "emit_event") as emitted:
            for _ in range(5):
                eng._report_poison_hit_once(item, "test")
        self.assertEqual(emitted.call_count, 1)
        # A different identity emits its own event.
        item2 = MagicMock(id=43, playlist_log_id=7)
        item2.track.id = 1
        item2.track.title = "T"
        eng._poison_skip_identities.append((7, 43))
        with patch.object(eng_module, "emit_event") as emitted2:
            eng._report_poison_hit_once(item2, "test")
        self.assertEqual(emitted2.call_count, 1)

    def test_is_poison_guarded_short_circuits_when_no_identities(self):
        eng = self._bare()
        item = MagicMock(id=42, playlist_log_id=7)
        self.assertFalse(eng._is_poison_guarded(item))
        # None-item is also safe.
        self.assertFalse(eng._is_poison_guarded(None))

    def test_apply_poison_skip_removes_matching_identities_only(self):
        eng = self._bare()
        eng._poison_skip_identities = [(7, 42)]
        keep1 = MagicMock(id=41, playlist_log_id=7)
        drop = MagicMock(id=42, playlist_log_id=7)
        keep2 = MagicMock(id=42, playlist_log_id=8)  # same LogItem id, DIFFERENT playlist
        for m in (keep1, drop, keep2):
            m.track.id = 1
            m.track.title = "T"
        with patch.object(eng_module, "emit_event"):
            filtered = eng._apply_poison_skip([keep1, drop, keep2])
        self.assertEqual([it.id for it in filtered], [41, 42])
        # keep2 stays because its playlist_log_id differs.
        self.assertIs(filtered[1], keep2)


class PolicyRestartForcedItemGateTests(SimpleTestCase):
    """[P0] 1.8 -- the authoritative gate: a guarded LogItem placed
    directly onto _forced_next_items must NEVER reach _start_next_track,
    even though _forced_next_items bypasses the DB-materialization
    filter entirely."""

    def _engine_with_guard(self, guarded_identity):
        engine = object.__new__(PlaybackEngine)
        _init_policy_restart_attrs(engine)
        engine._poison_skip_identities = [guarded_identity]
        engine.log_items = []
        engine._queue_cursor = 0
        engine._forced_next_items = []
        engine.decks = {"A": None, "B": None}
        engine._lock = threading.RLock()
        engine.current_log = None
        return engine

    def _make_item(self, item_id, playlist_log_id, playable=True):
        item = MagicMock()
        item.id = item_id
        item.playlist_log_id = playlist_log_id
        item.position = item_id
        item.category_id = None
        item.category = None
        item.track = MagicMock()
        item.track.id = item_id * 10
        item.track.title = f"Track {item_id}"
        item.track.filepath = "/dev/null" if playable else None
        return item

    def test_forced_item_matching_guard_is_skipped_next_healthy_selected(self):
        guarded = self._make_item(item_id=42, playlist_log_id=7)
        healthy = self._make_item(item_id=43, playlist_log_id=7)
        engine = self._engine_with_guard((7, 42))
        engine._forced_next_items = [guarded, healthy]
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "_log_item_playable",
                          new=lambda it: (True, None)):
            item, forced = engine._next_queue_item()
        self.assertIs(item, healthy)
        self.assertTrue(forced)
        # Guarded item was consumed off _forced_next_items -- proves it
        # didn't just skip in place.
        self.assertNotIn(guarded, engine._forced_next_items)

    def test_forced_item_same_track_different_logitem_still_playable(self):
        # Same Track.id (10*4=40 on both items via _make_item's convention).
        guarded = self._make_item(item_id=4, playlist_log_id=7)
        sibling = self._make_item(item_id=44, playlist_log_id=7)  # different LogItem PK, may or may not share Track
        # Force them to share the same Track id -- the guard MUST NOT
        # blacklist by Track.
        sibling.track.id = guarded.track.id
        engine = self._engine_with_guard((7, 4))
        engine._forced_next_items = [guarded, sibling]
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "_log_item_playable",
                          new=lambda it: (True, None)):
            item, forced = engine._next_queue_item()
        self.assertIs(item, sibling)
        self.assertTrue(forced)

    def test_peek_playable_at_cursor_also_gates_forced_items(self):
        guarded = self._make_item(item_id=42, playlist_log_id=7)
        healthy = self._make_item(item_id=43, playlist_log_id=7)
        engine = self._engine_with_guard((7, 42))
        engine._forced_next_items = [guarded, healthy]
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "_log_item_playable",
                          new=lambda it: (True, None)):
            peeked = engine._peek_playable_at_cursor()
        self.assertIs(peeked, healthy)
        # peek does not consume forced items -- guarded still present.
        self.assertIn(guarded, engine._forced_next_items)

    def test_upcoming_preview_hides_guarded_forced_and_queued_items(self):
        guarded_forced = self._make_item(item_id=42, playlist_log_id=7)
        healthy_forced = self._make_item(item_id=43, playlist_log_id=7)
        guarded_queued = self._make_item(item_id=44, playlist_log_id=7)
        healthy_queued = self._make_item(item_id=45, playlist_log_id=7)
        engine = self._engine_with_guard((7, 42))
        # Two guards this time.
        engine._poison_skip_identities.append((7, 44))
        engine._forced_next_items = [guarded_forced, healthy_forced]
        engine.log_items = [guarded_queued, healthy_queued]
        engine._peek_next_hour = lambda: None
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module, "_log_item_playable",
                          new=lambda it: (True, None)):
            preview = engine._get_upcoming_preview()
        ids = [it.id for it in preview]
        # Guarded items must be absent from the preview altogether.
        self.assertNotIn(42, ids)
        self.assertNotIn(44, ids)
        # Healthy items surface in the expected order (forced first).
        self.assertEqual(ids, [43, 45])


class RunEngineExitStatusTests(SimpleTestCase):
    """[P0] 1.8 -- the run_engine management command exits with a
    non-zero status when the engine sets restart_required, so systemd's
    Restart=on-failure fires. Uses the actual Command's handle() to
    prove the wiring, not a re-implementation."""

    def test_exit_status_is_non_zero_when_restart_required(self):
        from library.management.commands.run_engine import Command
        cmd = Command()
        fake_engine = MagicMock()
        fake_engine.restart_required = True
        # start() returns None (like the real one after the loop quits).
        fake_engine.start.return_value = None
        with patch("library.services.engine.PlaybackEngine",
                   return_value=fake_engine):
            with self.assertRaises(SystemExit) as cm:
                cmd.handle()
        self.assertEqual(cm.exception.code, eng_module.POLICY_RESTART_EXIT_STATUS)
        self.assertNotEqual(cm.exception.code, 0)
        fake_engine.start.assert_called_once()

    def test_exit_is_clean_when_restart_not_required(self):
        from library.management.commands.run_engine import Command
        cmd = Command()
        fake_engine = MagicMock()
        fake_engine.restart_required = False
        fake_engine.start.return_value = None
        with patch("library.services.engine.PlaybackEngine",
                   return_value=fake_engine):
            # handle() returns None; no SystemExit raised.
            self.assertIsNone(cmd.handle())


class EngineServiceUnitDeclaresCircuitBreakerTests(SimpleTestCase):
    """[P0] 1.8 -- the systemd unit file must declare the explicit
    circuit breaker under [Unit], not rely on systemd's 10s/5-burst
    default (which cannot catch a poison loop because one poison cycle
    itself exceeds 10s)."""

    def test_deploy_service_file_has_start_limits_under_unit_section(self):
        import configparser
        unit = Path(__file__).resolve().parents[2] / "deploy" / "isadoraair-engine.service"
        # configparser can read systemd unit syntax for this narrow purpose.
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str  # preserve case
        parser.read(unit, encoding="utf-8")
        self.assertIn("Unit", parser.sections())
        self.assertEqual(parser["Unit"].get("StartLimitIntervalSec"), "10min")
        self.assertEqual(parser["Unit"].get("StartLimitBurst"), "3")
        # Failure-restart plumbing must remain intact.
        self.assertIn("Service", parser.sections())
        self.assertEqual(parser["Service"].get("Restart"), "on-failure")
        self.assertEqual(parser["Service"].get("RestartSec"), "5")


class PoisonMarkerCrossRestartTests(SimpleTestCase):
    """[P0] 1.8 -- full cross-restart integrity of the anti-replay guard
    with real DB rows: proves the guard survives even when the poison
    LogItem's `played_at` failed to persist AND after
    _reload_queue_if_changed re-reads from PostgreSQL. Uses real
    Track/LogItem/PlaylistLog rows so `_apply_poison_skip` operates on
    ORM objects the same way it does in production."""

    databases = {"default"}

    def setUp(self):
        # Real DB fixture -- SimpleTestCase does not roll back, so we
        # clean up in tearDown.
        from django.utils import timezone as _tz
        from datetime import date
        from library.models import (
            Artist, Category, CategoryKind, LogItem, PlaylistLog, Track,
        )
        self._tz = _tz
        self._models = SimpleNamespace(
            Artist=Artist, Category=Category, CategoryKind=CategoryKind,
            LogItem=LogItem, PlaylistLog=PlaylistLog, Track=Track,
        )

        self._marker_tmpdir = tempfile.TemporaryDirectory(prefix="isa-p0-1.8-xr.")
        self.addCleanup(self._marker_tmpdir.cleanup)
        self.marker_path = Path(self._marker_tmpdir.name) / "last_policy_restart.json"
        self._marker_patch = patch.object(
            eng_module, "POLICY_RESTART_MARKER_PATH", self.marker_path,
        )
        self._marker_patch.start()
        self.addCleanup(self._marker_patch.stop)

        self._media_tmpdir = tempfile.TemporaryDirectory(prefix="isa-p0-1.8-media.")
        self.addCleanup(self._media_tmpdir.cleanup)

        self.kind = CategoryKind.objects.create(code="p018", name="P0 1.8 test")
        self.category = Category.objects.create(code="P018CAT", name="P0 1.8", kind=self.kind)
        self.artist, _ = Artist.get_or_create_ci("P0 1.8 Test Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 6, 1), hour=10, status="approved")

        # Two Tracks; item_bad and item_shared_again reference the same Track.
        self.track_shared = self._make_track("shared.mp3")
        self.track_middle = self._make_track("middle.mp3")
        self.item_bad = self._make_item(self.track_shared, position=1)
        self.item_good = self._make_item(self.track_middle, position=2)
        self.item_shared_again = self._make_item(self.track_shared, position=3)
        # Sanity: same Track, different LogItem PKs.
        self.assertEqual(self.item_bad.track_id, self.item_shared_again.track_id)
        self.assertNotEqual(self.item_bad.id, self.item_shared_again.id)

        self.addCleanup(self._teardown_db)

    def _teardown_db(self):
        # Ordered teardown so FK constraints don't complain.
        m = self._models
        m.LogItem.objects.filter(playlist_log=self.log).delete()
        self.log.delete()
        m.Track.objects.filter(id__in=[self.track_shared.id, self.track_middle.id]).delete()
        self.artist.delete()
        self.category.delete()
        self.kind.delete()

    def _make_track(self, filename):
        real_path = Path(self._media_tmpdir.name) / filename
        real_path.touch()
        return self._models.Track.objects.create(
            filepath=str(real_path), filename=filename,
            title=filename, artist=self.artist, category=self.category,
            ready2air=True, duration_seconds=100.0, next_start_seconds=100.0,
            cue_in_seconds=0.0,
        )

    def _make_item(self, track, position):
        return self._models.LogItem.objects.create(
            playlist_log=self.log, position=position,
            scheduled_time=self._tz.now(),
            track=track, category=self.category,
        )

    def _bare_engine(self):
        eng = object.__new__(PlaybackEngine)
        _init_policy_restart_attrs(eng)
        eng.loop = MagicMock()
        eng._lock = threading.RLock()
        eng.current_log = None
        eng.log_items = []
        eng._queue_cursor = 0
        eng._forced_next_items = []
        eng.decks = {"A": None, "B": None}
        # Attributes _reload_queue_if_changed touches:
        eng._last_queue_reload = 0.0
        eng._next_hour_peek = None
        eng._next_hour_peek_at = 0.0
        # Poll_position/state helpers _load_log_for and friends may call
        eng._peek_next_hour = lambda: None
        # Mirror __init__'s marker consumption -- the real __init__
        # builds a full pipeline, which is too heavy for these tests.
        eng._poison_skip_identities = eng._load_policy_restart_marker()
        return eng

    # (1) poison LogItem has played_at=NULL after the simulated save failure.
    def test_poison_item_played_at_stays_null_on_save_failure(self):
        from django.db.utils import DatabaseError
        with patch.object(type(self.item_bad), "save",
                          side_effect=DatabaseError("simulated write failure")):
            try:
                self.item_bad.played_at = self._tz.now()
                self.item_bad.save(update_fields=["played_at"])
            except DatabaseError:
                pass
        self.item_bad.refresh_from_db()
        self.assertIsNone(self.item_bad.played_at)

    # (2)+(3)+(4)+(5)+(6) -- the central cross-restart proof.
    def test_reload_after_load_does_not_reintroduce_poison_item(self):
        # played_at MUST be NULL for this test's premise to hold.
        self.item_bad.refresh_from_db()
        self.assertIsNone(self.item_bad.played_at)

        # Previous process: _request_restart wrote a marker.
        prev = self._bare_engine()
        with patch.object(eng_module, "emit_event"):
            prev._request_restart(
                reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 42,
                        "log_item_id": self.item_bad.id,
                        "playlist_log_id": self.log.id,
                        "track_id": self.item_bad.track_id,
                        "track_title": self.item_bad.track.title},
            )
        self.assertTrue(prev.restart_required)
        self.assertTrue(self.marker_path.exists())

        # Fresh process: __init__ consumes the marker WITHOUT deleting it.
        fresh = self._bare_engine()
        self.assertEqual(
            fresh._poison_skip_identities,
            [(self.log.id, self.item_bad.id)],
        )
        self.assertTrue(self.marker_path.exists(),
            "marker must survive __init__ so a crash before load doesn't lose the guard")

        # (2) initial _load_log_for skips it.
        with patch.object(eng_module, "emit_event"):
            fresh._load_log_for(self.log.date, self.log.hour)
        first_load_ids = [it.id for it in fresh.log_items]
        self.assertNotIn(self.item_bad.id, first_load_ids)
        self.assertIn(self.item_good.id, first_load_ids)               # (5)
        self.assertIn(self.item_shared_again.id, first_load_ids)       # (6)

        # (3) _reload_queue_if_changed runs afterward.
        fresh._last_queue_reload = 0.0
        with patch.object(eng_module, "emit_event"):
            fresh._reload_queue_if_changed()

        # (4) poison item STILL does not reappear.
        reload_ids = [it.id for it in fresh.log_items]
        self.assertNotIn(self.item_bad.id, reload_ids,
            "reload must not reintroduce the poison item -- its played_at is NULL")
        self.assertIn(self.item_good.id, reload_ids)
        self.assertIn(self.item_shared_again.id, reload_ids)

        # Guard survives multiple reloads.
        for _ in range(3):
            fresh._last_queue_reload = 0.0
            with patch.object(eng_module, "emit_event"):
                fresh._reload_queue_if_changed()
            self.assertNotIn(self.item_bad.id, [it.id for it in fresh.log_items])

    # (7) replacement engine crashing before queue load must not lose guard.
    def test_marker_survives_replacement_startup_crash(self):
        prev = self._bare_engine()
        with patch.object(eng_module, "emit_event"):
            prev._request_restart(
                reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": self.item_bad.id,
                        "playlist_log_id": self.log.id},
            )
        self.assertTrue(self.marker_path.exists())

        # Simulate: replacement process starts, reads marker, then
        # crashes before touching any queue.
        crashed = self._bare_engine()
        self.assertIn((self.log.id, self.item_bad.id), crashed._poison_skip_identities)
        # Do NOT call _load_log_for. Just verify the marker is still on
        # disk for the NEXT process to read.
        self.assertTrue(self.marker_path.exists())
        next_next = self._bare_engine()
        self.assertIn((self.log.id, self.item_bad.id), next_next._poison_skip_identities)

    def test_marker_persists_across_clean_stop_no_expiry(self):
        # Preexisting marker.
        prev = self._bare_engine()
        with patch.object(eng_module, "emit_event"):
            prev._request_restart(
                reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": self.item_bad.id,
                        "playlist_log_id": self.log.id},
            )
        # Marker file was written; NO age-based expiry, NO clean-stop
        # cleanup -- once the poison LogItem might still be replayable
        # from DB, the guard MUST remain for the boot lifetime.
        # Backdate mtime by 24h to prove there is no age check.
        past = time.time() - 86400
        os.utime(self.marker_path, (past, past))
        fresh = self._bare_engine()
        self.assertIn((self.log.id, self.item_bad.id), fresh._poison_skip_identities,
            "no 5-minute expiry -- guard must persist for the boot lifetime")

    # (9) preview matches playback selection.
    def test_upcoming_preview_matches_selection_after_load(self):
        prev = self._bare_engine()
        with patch.object(eng_module, "emit_event"):
            prev._request_restart(
                reason="deck_teardown_poisoned",
                detail={"slot": "A", "generation": 1,
                        "log_item_id": self.item_bad.id,
                        "playlist_log_id": self.log.id},
            )
        fresh = self._bare_engine()
        with patch.object(eng_module, "emit_event"):
            fresh._load_log_for(self.log.date, self.log.hour)
        preview_ids = [it.id for it in fresh._get_upcoming_preview()]
        self.assertNotIn(self.item_bad.id, preview_ids,
            "the preview must not show items playback will refuse")
        self.assertIn(self.item_good.id, preview_ids)
        self.assertIn(self.item_shared_again.id, preview_ids)
