"""P0 deck EOS/watchdog containment and resource-bound regressions.

The topology tests use real GStreamer bins, decodebin, silence-prime concat,
ghost-pad EOS probe, a dynamic audiomixer request pad, and the GLib idle
handoff. They are hardware-free and write media only beneath TemporaryDirectory.
"""

from __future__ import annotations

import os
import gc
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


def _make_log_item(track, item_id):
    item = MagicMock()
    item.id = item_id
    item.track = track
    item.category_id = None
    item.category = None
    item.position = item_id
    return item


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
    return engine


class RealDeckTopologyTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="isadoraair-deck-eos.")
        self.addCleanup(self.temp_dir.cleanup)
        self.wav_path = Path(self.temp_dir.name) / "short.wav"
        _write_wav(self.wav_path)
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
                self.assertEqual(critical_before, 2)

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
    def _deck(self, *, generation=1, duration=60.0, slot="A"):
        track = MagicMock(id=generation, title=f"Track {generation}", duration_seconds=duration)
        pipeline = MagicMock()
        pipeline.query_duration.return_value = (False, Gst.CLOCK_TIME_NONE)
        pipeline.set_state.return_value = Gst.StateChangeReturn.SUCCESS
        pipeline.get_static_pad.return_value.unlink.return_value = True
        return Deck(
            slot,
            track,
            MagicMock(),
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
        deck = self._deck()
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
            engine._deck_teardown_tick()
            snapshot = engine._deck_teardowns["A"].snapshot()
            self.assertEqual(snapshot["worker_starts"], 1)
            self.assertEqual(snapshot["active_generation"], 1)
            self.assertEqual(engine._deck_teardowns["B"].snapshot()["completed"], 100)
            critical_events = [
                call for call in emitted.call_args_list
                if call.kwargs["level"] == "critical"
            ]
            self.assertEqual(len(critical_events), 1)
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
