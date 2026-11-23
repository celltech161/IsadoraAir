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


# The fixture's permanent topology: mixer, deck_bus_caps (roadmap 1.21 A2, as production) and sink. Every
# deck generation must be gone again after churn.
_PERMANENT_CHILDREN = 3


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


def _task_ids():
    return frozenset(task.name for task in Path("/proc/self/task").iterdir())


# GStreamer's default task pool is a non-exclusive GThreadPool. A streaming
# thread whose task stopped idles briefly (~0.5 s) in that pool, then parks in
# GLib's global unused-thread list (GLib.ThreadPool.get_num_unused_threads(),
# capped by get_max_unused_threads()) or exits. An instantaneous
# /proc/self/task count taken mid-churn is therefore "deck generation rate x
# pool idle window" plus whatever an earlier test's pool threads have not yet
# drained: it swings by dozens with CPU speed and load and is not an
# IsadoraAir-owned quantity (r0104/1.18 measured 112-207 mid-churn settling to
# the same 15 every run). A real per-generation leak is different: its
# threads stay in use, so it survives settling and scales with retirements.
POOL_IDLE_QUIET_SECONDS = 0.75


def _settled_owned_task_count(timeout=5.0):
    """OS tasks once pool threads have gone idle, minus GLib's parked cache.

    Waits (bounded) until neither the task count nor GLib's parked
    unused-thread count has changed for POOL_IDLE_QUIET_SECONDS, which is
    longer than the pool's idle window, then returns the threads that are
    still doing something: the process's own threads, live pipelines'
    streaming threads, and anything leaked.
    """

    def sample():
        return _task_count(), GLib.ThreadPool.get_num_unused_threads()

    observed = {"sample": sample(), "since": time.monotonic()}

    def settled():
        current = sample()
        now = time.monotonic()
        if current != observed["sample"]:
            observed.update(sample=current, since=now)
            return False
        return now - observed["since"] >= POOL_IDLE_QUIET_SECONDS

    if not _wait_until(settled, timeout=timeout):
        raise AssertionError(
            f"OS task count did not settle within {timeout}s "
            f"(last tasks/parked={observed['sample']})"
        )
    tasks, parked = observed["sample"]
    return tasks - parked


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
    engine.mixer, _force_live = eng_module.make_deck_mixer("mixer")   # as production (1.21 Phase A)
    engine.deck_bus_caps = eng_module.make_deck_bus_caps(44100)       # as production (1.21 Phase A2)
    sink = Gst.ElementFactory.make("fakesink", "sink")
    sink.set_property("sync", False)
    engine.main_pipeline.add(engine.mixer)
    engine.main_pipeline.add(engine.deck_bus_caps)
    engine.main_pipeline.add(sink)
    assert engine.mixer.link(engine.deck_bus_caps) and engine.deck_bus_caps.link(sink)
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
    engine._quarantined_seek_generations = 0
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
    engine.manual_mode = False
    engine._manual_hold_pending = False
    engine._next_triggered = False
    engine._write_now_playing = lambda _track: None
    engine._write_rbds_category_state = lambda _track: None
    # Real-topology fixtures use MagicMock LogItems rather than persisted
    # occurrences. Keep their focus on GStreamer lifecycle; Phase B's claim
    # and transactional accounting have dedicated real-DB tests.
    def claim_fixture_occurrence(item):
        item.playback_claimed_at = eng_module.timezone.now()
        item.played_at = None
        return True

    engine._claim_playback_occurrence = claim_fixture_occurrence
    engine._schedule_occurrence_air_start_from_probe = lambda _deck: None
    # Phase C's DB ledger has its own PostgreSQL-backed integration coverage;
    # these SimpleTestCase fixtures remain focused on GStreamer topology.
    engine._persist_deck_duration = lambda *_args, **_kwargs: True
    engine._schedule_continuation_segment_start = (
        lambda deck, reason: deck.activate_duration_segment(
            eng_module.timezone.now(), reason
        )
    )
    engine._start_next_track = lambda **_kwargs: None
    # r0063 -- lets a test call the real engine.stop() directly (e.g. for
    # shutdown-vs-in-flight-seek collision tests) without needing to set
    # up Remote DJ or the on-disk engine-state writer, neither relevant
    # to deck lifecycle.
    engine.remote_dj_session = None
    engine._write_state = lambda **_kwargs: None
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
        # stop() deliberately never joins (production must not wait on a
        # hung NULL). Tests must: every test's coordinators reuse the same
        # worker thread names, so a worker that outlives its test -- e.g. a
        # deliberately wedged one whose release was skipped by an earlier
        # assertion failure -- otherwise surfaces later as another test's
        # "3 != 2" worker count. Tests that wedge a worker release it via
        # addCleanup/finally, which runs before this cleanup.
        for coordinator in self.engine._deck_teardowns.values():
            worker = coordinator._worker
            if worker is not None:
                worker.join(timeout=5.0)
                self.assertFalse(
                    worker.is_alive(),
                    f"{worker.name} outlived its test: {coordinator.snapshot()}",
                )

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

    def test_audiomixer_stale_inactive_pad_cannot_block_a_healthy_successor(self):
        """A deck whose completion never reaches GLib leaves a requested, EOS-less mixer pad. The r0145 non-live
        mixer (even with ignore-inactive-pads) waited on it and the next deck could not finish until the stale
        generation was explicitly removed. The live deck mixer (1.21 A/A2) does not wait: the healthy successor
        finishes while the stale pad still exists; explicit removal still releases the stale pad and map entry, and
        a further successor is unaffected."""
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
        stale_pad = bad.mixer_pad
        self.assertIn(stale_pad, tuple(self.engine.mixer.sinkpads))
        self.assertTrue(_wait_until(lambda: good.finished), "the stale pad cannot block a healthy successor")
        self.engine._remove_deck(bad)
        self.assertTrue(_wait_until(lambda: stale_pad not in tuple(self.engine.mixer.sinkpads)))
        self.assertNotIn(id(bad.pipeline), self.engine._deck_bin_map)
        after = self._create(602)
        self.assertTrue(_wait_until(lambda: after.finished))

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
            tasks_start = _settled_owned_task_count()
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
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), _PERMANENT_CHILDREN)
        self.assertEqual(self.engine._deck_teardowns["A"].snapshot()["worker_starts"], 1)
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        tasks_end = _settled_owned_task_count()
        self.assertLessEqual(tasks_end, tasks_start + 2)
        self.assertLess(_rss_bytes() - rss_start, 16 * 1024 * 1024)
        print(
            "healthy-churn-resources "
            f"cycles=200 pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"map={len(self.engine._deck_bin_map)} task_delta={tasks_end - tasks_start} "
            f"thread_delta={threading.active_count() - threads_start} "
            f"rss_delta={_rss_bytes() - rss_start}",
            flush=True,
        )
        self.assertIn("E_DECK_GHOST_SRC_EOS", last.eos_milestones)

    def test_post_poison_real_deck_churn_releases_detached_generations(self):
        """A wedged NULL must not turn every later deck into another leak."""
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)
        release = threading.Event()
        # Release the deliberately wedged A worker even if an assertion below
        # fails, so it cannot outlive this test (see _cleanup_engine).
        self.addCleanup(release.set)
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
        tasks_start = _settled_owned_task_count()
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
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), _PERMANENT_CHILDREN)
        # One additional long-lived worker belongs to the surviving B
        # teardown boundary; it services all 100 retirements.
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        # Thread growth is judged on settled, IsadoraAir-relevant counts, not
        # on the mid-churn samples above: those track GStreamer's pool idle
        # window (see POOL_IDLE_QUIET_SECONDS) and are kept for the printed
        # evidence only. A leak of even one streaming thread per detached
        # generation would put the settled delta near 100.
        tasks_end = _settled_owned_task_count()
        self.assertLessEqual(tasks_end, tasks_start + 2)
        self.assertLess(_rss_bytes() - rss_start, 32 * 1024 * 1024)
        self.assertLessEqual(sum(ref() is not None for ref in generation_refs), 1)
        print(
            "post-poison-resources "
            f"cycles=100 live_wrappers={sum(ref() is not None for ref in generation_refs)} "
            f"pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"children={_pipeline_child_count(self.engine.main_pipeline)} "
            f"map={len(self.engine._deck_bin_map)} "
            f"task_delta={tasks_end - tasks_start} "
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
                self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), _PERMANENT_CHILDREN)
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
                wedged_workers = {coordinator_a._worker, coordinator_b._worker}
                # Exactly this engine's two wedged workers -- by identity, so
                # the assertion is about these coordinators, not about how
                # many same-named threads exist process-wide.
                self.assertEqual(len(wedged_workers), 2)
                self.assertTrue(all(worker.is_alive() for worker in wedged_workers))
                self.assertEqual(
                    {t for t in threading.enumerate() if t.name in worker_names},
                    wedged_workers,
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
                task_ids_before = _task_ids()
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
                self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), _PERMANENT_CHILDREN)
                # Not one OS thread was created by 100 refused starts. (A
                # count comparison here also failed when an earlier test's
                # idle pool threads exited inside this window.)
                new_task_ids = _task_ids() - task_ids_before
                self.assertEqual(new_task_ids, frozenset())
                self.assertEqual(threading.active_count(), threads_before)
                self.assertLessEqual(_rss_bytes() - rss_before, 1024 * 1024)
                self.assertEqual(
                    {t for t in threading.enumerate() if t.name in worker_names},
                    wedged_workers,
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
                    f"new_tasks={len(new_task_ids)} "
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

    def test_legacy_watchdog_abandonment_no_longer_blocks_later_decks(self):
        """Execute the pre-fix watchdog exactly (logical slot only: bin, map, link and mixer request pad stay live).

        Suppression is at boundary G: decoder/concat/ghost EOS all existed, but no GLib completion handoff was
        scheduled. Under the r0145 non-live deck mixer that stale pad blocked every later generation (110 of 110
        abandoned in turn, 111 pads leaked). Under the live deck mixer (1.21 A/A2) later generations are not blocked;
        retained: every later deck crosses all EOS boundaries and is released, only the stale generation's pad/map
        entry remains until it is explicitly detached, teardown is bounded, and a fresh deck still completes.
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
            if not _wait_until(lambda deck=deck: deck.finished, timeout=0.5):
                # The legacy abandonment for a generation the stale pad blocked (none expected now).
                deck.finished = True
                self.engine.decks["A"] = None
                self.engine._next_triggered = False
                legacy_watchdogs.append(deck)
        self.assertEqual(legacy_watchdogs, [], "the stale pad blocks no later generation")
        self.assertTrue(all(deck.media_buffer_count > 0 for deck in later))
        for boundary in ("A_DECODER_AUDIO_EOS", "D_CONCAT_SRC_EOS", "E_DECK_GHOST_SRC_EOS"):
            self.assertTrue(all(boundary in deck.eos_milestones for deck in later), boundary)
        mixer_output_delta = self.engine._test_output_buffers - output_before
        self.assertGreater(mixer_output_delta, 0, "the deck mixer kept producing (clock-paced: ~39 x 10 ms here)")
        # Every later generation was released by the current code; only the stale (abandoned) one remains.
        self.assertTrue(_wait_until(lambda: len(tuple(self.engine.mixer.sinkpads)) == 1))
        self.assertEqual(set(self.engine._deck_bin_map), {id(bad.pipeline)})
        coordinator = self.engine._deck_teardowns["A"]
        self.assertTrue(_wait_until(lambda: coordinator.snapshot().get("active_generation") is None
                                    and not coordinator.snapshot().get("queue_depth")), "teardown is bounded")
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
        tasks_start = _settled_owned_task_count()
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
        self.assertEqual(_pipeline_child_count(self.engine.main_pipeline), _PERMANENT_CHILDREN)
        self.assertGreater(self.engine._test_output_buffers, output_start)
        self.assertEqual(self.engine._deck_teardowns["A"].snapshot()["worker_starts"], 1)
        self.assertLessEqual(threading.active_count(), threads_start + 1)
        tasks_end = _settled_owned_task_count()
        self.assertLessEqual(tasks_end, tasks_start + 2)
        self.assertLess(_rss_bytes() - rss_start, 32 * 1024 * 1024)
        print(
            "contained-storm-resources "
            f"watchdogs=100 pads={len(tuple(self.engine.mixer.sinkpads))} "
            f"map={len(self.engine._deck_bin_map)} task_delta={tasks_end - tasks_start} "
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

    def _expire_deferred_window(self, deck):
        """Backdate the most-recent-rejection timestamp past
        DEFERRED_SEEK_EOS_OBSERVATION_SECONDS so a captured callback
        decides immediately when invoked synchronously in a test,
        instead of rescheduling itself (real wall-clock time barely
        moves between scheduling and invocation in these tests)."""
        deck.deferred_seek_eos_last_rejected_monotonic -= (
            eng_module.DEFERRED_SEEK_EOS_OBSERVATION_SECONDS + 1
        )

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
        self._expire_deferred_window(deck)
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
        self._expire_deferred_window(deck)
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
        # Deferred callback fires ~6s later (DEFERRED_SEEK_EOS_OBSERVATION_
        # SECONDS), not ~7194s later. Buffers did not advance -> completion.
        self._expire_deferred_window(deck)
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
        # Expire the window so the callback actually reaches its
        # claim_completion race instead of short-circuiting via a
        # reschedule (which would also leave _handle_deck_finished
        # uncalled, but wouldn't exercise the real race).
        self._expire_deferred_window(deck)
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
        self._expire_deferred_window(deck)
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
        self._expire_deferred_window(deck)
        fn, args = captured[0]
        with patch.object(eng_module, "emit_event"):
            fn(*args)
        engine._handle_deck_finished.assert_not_called()
        self.assertFalse(deck.completion_claimed)
        self.assertIn("I_EOS_POST_SEEK_RECOVERED", deck.eos_milestones)

    def test_second_eos_shortly_before_deadline_reschedules_full_window(self):
        """[P0] 1.8 timing follow-up: the deferred callback deadline was
        previously anchored to the FIRST rejection. A second rejection
        arriving late in the window (shortly before that first deadline)
        would only get whatever time was left -- shorter than the
        known ~2.7s parser-recovery case -- before the ORIGINAL
        callback decided. Fixed by having the callback reschedule
        itself for the remaining time whenever
        deferred_seek_eos_last_rejected_monotonic was refreshed after
        the callback was originally scheduled."""
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
            # EOS#1 -- one GLib source scheduled, baseline=10.
            engine._on_deck_eos_probed(deck.pipeline, deck)
            self.assertEqual(len(captured), 1)
            # Buffers appear to recover.
            deck.media_buffer_count = 42
            # EOS#2 arrives shortly before EOS#1's callback deadline --
            # still well within DEFERRED_SEEK_EOS_OBSERVATION_SECONDS
            # of real wall-clock time, so no NEW GLib source is created
            # (deferred_seek_eos_pending is already True); only the
            # baseline and timestamp refresh.
            engine._on_deck_eos_probed(deck.pipeline, deck)
        self.assertEqual(len(captured), 1, "still only the original GLib source at this point")
        self.assertEqual(deck.deferred_seek_eos_baseline, 42)

        # The ORIGINAL scheduled callback now fires (simulating the real
        # GLib timeout elapsing). Because the timestamp was refreshed by
        # EOS#2 moments ago, this call must defer -- reschedule itself
        # for the remaining time -- rather than deciding on a partial
        # observation window.
        fn1, args1 = captured[0]
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            result = fn1(*args1)
        self.assertFalse(result, "one-shot GLib callback must return False regardless of outcome")
        self.assertEqual(len(captured), 2, "callback rescheduled itself for the remaining window")
        engine._handle_deck_finished.assert_not_called()
        self.assertTrue(deck.deferred_seek_eos_pending,
            "still pending -- the deadline was deferred, not decided")

        # After the FULL interval from EOS#2 (not EOS#1) has elapsed,
        # with no new buffers -> genuine EOS accepted.
        self._expire_deferred_window(deck)
        fn2, args2 = captured[1]
        with patch.object(eng_module, "emit_event"):
            fn2(*args2)
        engine._handle_deck_finished.assert_called_once_with(deck)
        self.assertTrue(deck.completion_claimed)
        self.assertEqual(deck.completion_reason, "eos_deferred")

    def test_second_eos_shortly_before_deadline_then_recovery_leaves_deck_running(self):
        """Companion to the previous test: same reschedule, but real
        media buffers advance past the refreshed baseline before the
        rescheduled callback fires -- deck must survive."""
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
            deck.media_buffer_count = 42
            engine._on_deck_eos_probed(deck.pipeline, deck)
        self.assertEqual(len(captured), 1)
        self.assertEqual(deck.deferred_seek_eos_baseline, 42)

        fn1, args1 = captured[0]
        with patch.object(eng_module, "emit_event"), \
             patch.object(eng_module.GLib, "timeout_add_seconds", side_effect=capture):
            fn1(*args1)
        self.assertEqual(len(captured), 2)
        engine._handle_deck_finished.assert_not_called()

        # Real buffers advance past the refreshed baseline (42) before
        # the rescheduled callback fires.
        deck.media_buffer_count = 100
        self._expire_deferred_window(deck)
        fn2, args2 = captured[1]
        with patch.object(eng_module, "emit_event"):
            fn2(*args2)
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
        item.played_at = None  # an unaired occurrence (a bare MagicMock attribute would read as aired)
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


class OrdinaryFirstAudioDeadlineTests(SimpleTestCase):
    """Roadmap 1.21-B1 -- the ordinary first-audio deadline and its one exact-occurrence retry.

    Deterministic: the engine's real queue, start, completion and retirement code runs; only deck
    construction (a fresh Deck around a MagicMock bin, claimed as _claim_playback_occurrence claims), the
    GLib timer registration (captured, fired by the test) and the database re-check at hand-out (covered by
    the PostgreSQL-backed accounting tests) are substituted. The real-GStreamer never-real-buffer failure and
    its recovery are in test_engine_playback_boundary_evidence.FirstAudioDeadlineRealGstTests."""

    def setUp(self):
        from library.models import LogItem
        from library.services import vt_transition

        self.LogItem = LogItem
        self.vt = vt_transition
        marker_dir = tempfile.TemporaryDirectory(prefix="isa-b1-marker.")
        self.addCleanup(marker_dir.cleanup)
        self.events = []
        self.timers = []
        self.idle = []
        for active in (
            patch.object(eng_module, "POLICY_RESTART_MARKER_PATH", Path(marker_dir.name) / "marker.json"),
            patch.object(eng_module, "emit_event", side_effect=lambda **kw: self.events.append(kw)),
            patch.object(eng_module, "_log_item_playable", return_value=(True, None)),
            patch.object(eng_module, "maybe_schedule_song_request", return_value=None),
            patch.object(eng_module.GLib, "timeout_add",
                         side_effect=lambda ms, fn, *args: self.timers.append((ms, fn, args)) or 1),
            patch.object(eng_module.GLib, "idle_add", side_effect=lambda fn, *args: self.idle.append((fn, args)) or 1),
        ):
            active.start()
            self.addCleanup(active.stop)
        self.engine = self._engine()
        self.addCleanup(self._no_uncaught)

    # -- fixture ---------------------------------------------------------------------------------------
    def _engine(self):
        engine = object.__new__(PlaybackEngine)
        engine._lock = threading.RLock()
        engine.decks = {"A": None, "B": None}
        engine._deck_bin_map = {}
        engine._deck_generation_serial = 0
        engine._deck_watchdog_times = eng_module.deque()
        engine._deck_teardowns = {
            slot: audio_recovery.BoundedTeardownCoordinator(f"deck-b1-{slot.lower()}-test", timeout_s=0.5,
                                                            queue_capacity=2)
            for slot in ("A", "B")
        }
        for coordinator in engine._deck_teardowns.values():
            self.addCleanup(coordinator.stop)
        engine.mixer = MagicMock()
        engine.main_pipeline = MagicMock()
        engine.main_pipeline.remove.return_value = True
        engine.manual_mode = False
        engine._manual_hold_pending = False
        engine._next_triggered = False
        engine.running = True
        engine.loop = MagicMock()
        engine._engine_session_id = "session-b1"
        engine.current_log = SimpleNamespace(id=7)
        engine.log_items = []
        engine._queue_cursor = 0
        engine._forced_next_items = []
        engine._resume_item = None
        engine._resume_hint = None
        engine._persist_deck_duration = lambda *_args, **_kwargs: True
        engine._maybe_insert_dedication_intro = lambda item: item
        engine._on_log_exhausted = MagicMock()
        engine._roll_over_to_next_hour = lambda: False
        engine._restore_followup_for_intro = lambda item: True
        # The database re-check at hand-out is exercised against PostgreSQL in
        # test_engine_playback_accounting_semantics.FirstAudioRetryAccountingTests.
        engine._first_audio_retry_refusal = lambda item, record: None
        _init_policy_restart_attrs(engine)
        self.opened = []

        def create_deck(slot, log_item, *_args, **_kwargs):
            # As _claim_playback_occurrence: one-way, the original timestamp survives every reopen.
            if log_item.playback_claimed_at is None:
                log_item.playback_claimed_at = eng_module.timezone.now()
            engine._deck_generation_serial += 1
            pipeline = MagicMock()
            pipeline.set_state.return_value = Gst.StateChangeReturn.SUCCESS
            pipeline.get_static_pad.return_value.unlink.return_value = True
            deck = Deck(slot, log_item.track, log_item, pipeline, MagicMock(), silence_primed=True,
                        generation=engine._deck_generation_serial,
                        air_start_eligible=log_item.played_at is None)
            engine.decks[slot] = deck
            engine._deck_bin_map[id(pipeline)] = deck
            self.opened.append(deck)
            return deck

        engine._create_deck = create_deck
        return engine

    def _item(self, item_id, *, origin="default", category_code=None, playlist_log_id=7):
        track = MagicMock()
        track.id = item_id * 10
        track.title = f"B1 track {item_id}"
        track.filepath = f"/b1/{item_id}.wav"
        track.duration_seconds = 180.0
        item = MagicMock()
        item.id = item_id
        item.playlist_log_id = playlist_log_id
        item.position = item_id
        item.track = track
        item.track_id = track.id
        item.played_at = None
        item.playback_claimed_at = None
        item.origin = self.LogItem.ORIGIN_GENERATED_SCHEDULE if origin == "default" else origin
        item.category_id = 1 if category_code else None
        item.category = SimpleNamespace(code=category_code) if category_code else None
        return item

    def _queue(self, *items):
        self.engine.log_items = list(items)
        self.engine._queue_cursor = 0

    def _start(self, kind="automatic", slot="A"):
        with self.engine._vtt_starting(kind):
            self.engine._start_next_track(slot=slot)

    def _deadlines(self):
        return [(args, fn) for _ms, fn, args in self.timers if fn == self.engine._first_audio_deadline]

    def _fire_deadline(self, deck):
        matching = [(ms, fn, args) for ms, fn, args in self.timers
                    if fn == self.engine._first_audio_deadline and args[1] == deck.generation]
        self.assertEqual(len(matching), 1, "exactly one deadline per armed generation")
        ms, fn, args = matching[0]
        self.assertEqual(ms, int(eng_module.ORDINARY_FIRST_AUDIO_GRACE_S * 1000))
        self.assertEqual(args, (deck.slot, deck.generation, deck.log_item.id))
        return fn(*args)

    def _titles(self, level=None):
        return [e["title"] for e in self.events if level is None or e.get("level") == level]

    def _no_uncaught(self):
        self.assertFalse([t for t in self._titles() if t.startswith("Uncaught exception")], self.events)

    def _settle(self):
        """Every unoccupied slot's previous bounded NULL has returned (the mock bins return at once)."""
        self.assertTrue(_wait_until(lambda: all(
            self.engine.decks[slot] is not None or self.engine._deck_slot_available(slot) for slot in ("A", "B")
        )))

    def _dispatch_first_real(self, deck):
        # Exactly what the streaming-thread boundary probe does (its GLib hand-off is captured, not run).
        deck.mark_milestone(eng_module.FIRST_REAL_POST_PRIMER_MILESTONE)
        PlaybackEngine._schedule_occurrence_air_start_from_probe(self.engine, deck)

    # -- 1, 2: healthy and slower-but-legitimate starts --------------------------------------------------
    def test_grace_is_one_second_and_a_healthy_start_makes_the_deadline_inert(self):
        self.assertEqual(eng_module.ORDINARY_FIRST_AUDIO_GRACE_S, 1.0)
        item = self._item(1)
        self._queue(item, self._item(2))
        self._start()
        deck = self.opened[0]
        self.assertEqual(deck.first_audio_attempt, 1)
        self._dispatch_first_real(deck)                       # first real buffer well inside the grace
        self.assertEqual(len(self.idle), 1)                   # the authoritative air-start hand-off, once
        self._fire_deadline(deck)
        self.assertIs(self.engine.decks["A"], deck)
        self.assertFalse(deck.finished or deck.completion_claimed or deck.first_audio_fenced)
        self.assertIn("FIRST_AUDIO_DEADLINE_INERT_FIRST_REAL_WON", deck.eos_milestones)
        self.assertEqual(self.engine._forced_next_items, [])
        self.assertEqual(self.events, [])

    def test_slower_but_legitimate_first_audio_at_the_last_moment_is_not_retried(self):
        """The deadline races dispatch evidence, never a database commit or a wall-clock guess: a first real
        buffer dispatched any time before the timeout runs (here: just before) wins outright, even with its
        PlayEvent transaction still pending (played_at None)."""
        self._queue(self._item(1))
        self._start()
        deck = self.opened[0]
        deck.mark_media_buffer()
        self._dispatch_first_real(deck)
        self.assertIsNone(deck.log_item.played_at)
        self._fire_deadline(deck)
        self.assertFalse(deck.finished)
        self.assertNotIn(1, self.engine.__dict__.get("_first_audio_retries", {}))

    # -- 3, 4, 13: timeout -> exact same occurrence retried once ------------------------------------------
    def test_timeout_fences_retires_and_retries_the_exact_occurrence_once(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        original = self.opened[0]
        claimed_at = item.playback_claimed_at
        self.assertIsNotNone(claimed_at)
        self._fire_deadline(original)

        self._no_uncaught()
        self.assertTrue(original.first_audio_fenced)
        self.assertEqual(original.completion_reason, "first_audio_deadline")
        self.assertTrue(original.finished and original.retirement_started)
        self.assertIn("M_MIXER_REQUEST_PAD_RELEASE_COMPLETE", original.eos_milestones)
        self.engine.mixer.release_request_pad.assert_called()
        # The retry opened immediately on the free slot: the SAME in-memory LogItem, never a new row.
        self.assertEqual(len(self.opened), 2)
        retry = self.opened[1]
        self.assertIs(retry.log_item, item)
        self.assertEqual(retry.first_audio_attempt, 2)
        self.assertIs(item.playback_claimed_at, claimed_at)        # the claim timestamp is untouched
        self.assertEqual(self.engine._queue_cursor, 1)             # never rewound
        self.assertEqual(self.engine._forced_next_items, [])
        self.assertEqual([e["level"] for e in self.events], ["warning"])
        detail = self.events[0]["detail"]
        self.assertEqual((detail["log_item_id"], detail["original_generation"], detail["attempt"]),
                         (1, original.generation, 1))
        self.assertFalse(detail["first_real_dispatched"])
        self.assertEqual(self.idle, [])                            # no air-start hand-off: nothing recorded

    def test_retry_success_records_one_air_start_and_releases_the_record(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        self._fire_deadline(self.opened[0])
        retry = self.opened[1]
        self._dispatch_first_real(retry)
        self._dispatch_first_real(retry)                   # a duplicate probe call is still one hand-off
        self.assertEqual(len(self.idle), 1)
        self.assertEqual(self.idle[0][1][:3], (retry.slot, retry.generation, 1))
        self._fire_deadline(retry)
        self.assertFalse(retry.finished)
        self.assertNotIn(1, self.engine._first_audio_retries)
        self.assertTrue(self.engine._occurrence_handed_out(retry.log_item))
        self.assertEqual(self._titles("critical"), [])

    # -- 6, 7, 21: retry failure is terminal, reported once, and playout advances -----------------------
    def test_second_timeout_is_terminal_reported_once_and_the_successor_plays(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        self._fire_deadline(self.opened[0])
        retry = self.opened[1]
        self._settle()
        self._fire_deadline(retry)
        self._no_uncaught()
        self.assertTrue(retry.finished and retry.first_audio_fenced)
        critical = [e for e in self.events if e["level"] == "critical"]
        self.assertEqual(len(critical), 1)
        detail = critical[0]["detail"]
        self.assertEqual(critical[0]["dedupe_key"], "engine|first-audio-exhausted|1")
        self.assertEqual(
            (detail["log_item_id"], detail["track_id"], detail["engine_session_id"], detail["attempts"],
             detail["original_generation"], detail["retry_generation"], detail["reason"]),
            (1, 10, "session-b1", 2, self.opened[0].generation, retry.generation,
             "ordinary_first_audio_deadline_exhausted"))
        self.assertFalse(detail["first_real_dispatched"] or detail["occurrence_aired"]
                         or detail["played_at_recorded"])
        # Exactly two generations of the occurrence, ever; the healthy successor opened next.
        self.assertEqual([d.log_item.id for d in self.opened], [1, 1, 2])
        successor = self.opened[2]
        self.assertEqual(successor.first_audio_attempt, 1)
        # A duplicate / stale deadline firing changes nothing and reports nothing more.
        self._fire_deadline(retry)
        self.assertEqual(len([e for e in self.events if e["level"] == "critical"]), 1)
        # The cursor can never hand the abandoned occurrence out again, even if a reload rewinds onto it.
        self.engine.decks = {"A": None, "B": None}
        self.engine._queue_cursor = 0
        selected, forced = self.engine._next_queue_item()
        self.assertIs(selected, nxt)
        self.assertNotIn(1, self.engine._first_audio_retries)
        self.assertIn(1, self.engine._first_audio_spent)

    # -- 8, 9: stale callbacks and the first-buffer / timeout race ---------------------------------------
    def test_late_first_real_buffer_of_a_fenced_generation_never_dispatches(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        original = self.opened[0]
        self._fire_deadline(original)
        self._dispatch_first_real(original)
        self.assertEqual([a for f, a in self.idle], [])
        self.assertFalse(original.air_start_dispatched)
        self.assertIn("FIRST_REAL_AFTER_FIRST_AUDIO_FENCE", original.eos_milestones)

    def test_stale_deadline_cannot_touch_a_newer_generation_in_the_same_slot(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        old = self.opened[0]
        self.engine._remove_deck(old, occurrence_terminal=False, termination_reason="test")
        self.engine.decks["A"] = None
        replacement = self.engine._create_deck("A", self._item(3))
        self.assertTrue(_wait_until(lambda: self.engine._deck_slot_available("A")))
        self._fire_deadline(old)
        self.assertIs(self.engine.decks["A"], replacement)
        self.assertFalse(replacement.finished or replacement.completion_claimed or replacement.first_audio_fenced)
        self.assertEqual(self.engine._forced_next_items, [])
        self.assertEqual(self.events, [])

    def test_first_buffer_and_deadline_racing_have_exactly_one_winner(self):
        outcomes = {"first_real": 0, "deadline": 0}
        switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)                 # interleave the two threads as finely as CPython allows
        self.addCleanup(sys.setswitchinterval, switch_interval)
        for round_ in range(300):
            self.timers.clear()
            self.idle.clear()
            self.engine = self._engine()
            self.engine._handle_deck_finished = MagicMock()       # retirement itself is covered above
            self._queue(self._item(1000 + round_))
            self._start()
            deck = self.opened[-1]
            barrier = threading.Barrier(2)

            # Stagger which side reaches the shared lock first: 0, ~50 and ~100 microseconds of lead.
            lead = (round_ % 3) * 0.00005

            def spin(seconds):
                end = time.perf_counter() + seconds
                while time.perf_counter() < end:
                    pass

            def streaming_thread():
                barrier.wait()
                self._dispatch_first_real(deck)

            worker = threading.Thread(target=streaming_thread)
            worker.start()
            barrier.wait()
            spin(lead)
            self._fire_deadline(deck)
            worker.join(5)
            dispatched, fenced = deck.air_start_dispatched, deck.first_audio_fenced
            self.assertNotEqual(dispatched, fenced, f"round {round_}: both or neither won")
            if dispatched:
                outcomes["first_real"] += 1
                self.assertEqual(len(self.idle), 1)
                self.assertFalse(deck.completion_claimed)
                self.engine._handle_deck_finished.assert_not_called()
            else:
                outcomes["deadline"] += 1
                self.assertEqual(self.idle, [])
                self.assertEqual(deck.completion_reason, "first_audio_deadline")
                self.engine._handle_deck_finished.assert_called_once_with(deck)
        print(f"b1-race-outcomes {outcomes}", flush=True)
        self.assertTrue(outcomes["first_real"] and outcomes["deadline"], outcomes)

    # -- 12: already aired / already dispatched occurrences ----------------------------------------------
    def test_already_aired_or_dispatched_occurrences_never_get_a_deadline_or_retry(self):
        aired = self._item(1)
        aired.played_at = eng_module.timezone.now()
        self.engine._forced_next_items = []
        self._queue(aired, self._item(2))
        self._start()
        # The cursor never hands out an aired row (r0135); the next one opens.
        self.assertEqual([d.log_item.id for d in self.opened], [2])
        deck = self.opened[0]
        deck.air_start_dispatched = True                       # dispatched, commit still pending
        self._fire_deadline(deck)
        self.assertFalse(deck.finished)

    # -- eligibility and exclusions ----------------------------------------------------------------------
    def test_only_fresh_ordinary_unattended_scheduled_generations_are_armed(self):
        K = self.vt.StartKind
        armed_cases = {
            "automatic": dict(kind=K.AUTOMATIC),
            "return_to_auto_handoff": dict(kind=K.MANUAL_TO_AUTO_HANDOFF),
            "unattended_unkinded": dict(kind=None, unattended=True),
            "live_fill": dict(origin=self.LogItem.ORIGIN_LIVE_FILL),
            "build_fill": dict(origin=self.LogItem.ORIGIN_GENERATED_FILL),
            "refill_installed": dict(origin=self.LogItem.ORIGIN_REFILL_INSTALLED),
            "dst_replay": dict(origin=self.LogItem.ORIGIN_DST_REPLAY),
        }
        excluded_cases = {
            "start_next_or_reload_unkinded": dict(kind=None),
            "manual_start": dict(kind=K.MANUAL),
            "start_next_kind": dict(kind=K.START_NEXT),
            "eject_replacement": dict(kind=K.EJECT_REPLACEMENT),
            "resume": dict(kind=K.RESUME),
            "continuation": dict(kind=K.CONTINUATION),
            "manual_mode": dict(manual=True),
            "operator_inserted": dict(origin=self.LogItem.ORIGIN_OPERATOR_INSERTED),
            "engine_inserted": dict(origin=self.LogItem.ORIGIN_ENGINE_INSERTED),
            "no_origin": dict(origin=None),
            "weather_amber": dict(category="WxAlert"),
            "dedication_intro": dict(category="Dedications"),
            "forced_followup": dict(forced=True),
            "active_message": dict(native=True),
            "not_primed": dict(deck=dict(silence_primed=False)),
            "scheduled_vt_admission": dict(deck=dict(admission_state="preparing")),
            "seek_continuation": dict(deck=dict(continuation_reason="manual_seek_or_resume")),
            "start_failed": dict(deck=dict(start_failed=True)),
        }
        for name, case in {**{k: (True, v) for k, v in armed_cases.items()},
                           **{k: (False, v) for k, v in excluded_cases.items()}}.items():
            with self.subTest(name):
                expect, spec = case
                self.timers.clear()
                self.engine = self._engine()
                self.engine.manual_mode = spec.get("manual", False)
                origin = spec.get("origin", "default")
                item = self._item(5, origin=origin, category_code=spec.get("category"))
                deck_overrides = spec.get("deck", {})
                original_create = self.engine._create_deck

                def create(slot, log_item, *a, _orig=original_create, _o=deck_overrides, **k):
                    deck = _orig(slot, log_item)
                    for attr, value in _o.items():
                        setattr(deck, attr, value)
                    return deck

                self.engine._create_deck = create
                if spec.get("forced"):
                    self.engine._forced_next_items = [item]
                    self.engine.log_items = []
                else:
                    self._queue(item)
                if spec.get("native"):
                    self.engine._active_message_held = {item.id: "attempt-1"}
                kind = spec.get("kind", K.AUTOMATIC)
                if spec.get("unattended"):
                    with self.engine._first_audio_unattended():
                        self._start(kind=kind)
                elif kind is None:
                    self.engine._start_next_track(slot="A")
                else:
                    self._start(kind=kind)
                deck = self.opened[-1]
                self.assertEqual(bool(self._deadlines()), expect, name)
                self.assertEqual(deck.first_audio_attempt, 1 if expect else 0, name)

    def test_unattended_marker_is_scoped_and_restored(self):
        self.assertFalse(self.engine._first_audio_start_unattended())
        with self.engine._first_audio_unattended():
            self.assertTrue(self.engine._first_audio_start_unattended())
            with self.engine._vtt_starting(self.vt.StartKind.START_NEXT):
                self.assertFalse(self.engine._first_audio_start_unattended())
        self.assertFalse(self.engine._first_audio_start_unattended())

    # -- 14: Manual / microphone / Remote DJ hold ---------------------------------------------------------
    def test_manual_or_mic_hold_keeps_the_retry_for_the_next_authorized_start(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        original = self.opened[0]
        self.engine.manual_mode = True        # operator Manual, or the mic/Remote DJ auto-hold
        self._fire_deadline(original)
        self.assertTrue(original.finished)
        self.assertEqual(len(self.opened), 1, "nothing is forced to play over the hold")
        self.assertTrue(self.engine._manual_hold_pending)
        self.assertEqual(self.engine._forced_next_items, [item])
        self.assertEqual(self.engine._get_upcoming_preview()[:2], [item, nxt])   # the console shows it next
        # Return to Auto releases the deferred handoff: the retry, with its own deadline.
        self.assertTrue(_wait_until(lambda: self.engine._free_slot() is not None))
        self.engine._set_manual_mode(False)
        self.assertEqual(len(self.opened), 2)
        self.assertIs(self.opened[1].log_item, item)
        self.assertEqual(self.opened[1].first_audio_attempt, 2)

    # -- 15: urgent and dedication priority ---------------------------------------------------------------
    def test_retry_is_queued_behind_forced_items_and_pending_cursor_weather_urgents(self):
        item = self._item(1)
        followup, native = self._item(2), self._item(3)
        urgent = self._item(4, origin=self.LogItem.ORIGIN_ENGINE_INSERTED, category_code="WxAlert")
        ordinary = self._item(5)
        self._queue(item, urgent, ordinary)
        self._start()
        original = self.opened[0]
        self.assertEqual(self.engine._queue_cursor, 1)          # the cursor now sits on the pending urgent
        self.engine._forced_next_items = [followup, native]      # e.g. a dedication follow-up, an active message
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)   # an outgoing still plays
        self._fire_deadline(original)
        self.assertEqual(len(self.opened), 1)                    # the outgoing still owns the next start
        self.assertEqual(self.engine._forced_next_items, [followup, native, urgent, item])
        # An urgent inserted while the retry is pending goes first, as _insert_urgent_next_inner does.
        later_urgent = self._item(6, origin=self.LogItem.ORIGIN_ENGINE_INSERTED, category_code="WxAlert")
        self.engine._forced_next_items.insert(0, later_urgent)
        order = []
        for _ in range(6):
            selected, _forced = self.engine._next_queue_item()
            if selected is None:
                break
            order.append(selected.id)
            self.engine.decks["A"] = MagicMock(log_item=selected, finished=False)   # "on a deck now"
        self.assertEqual(order, [6, 2, 3, 4, 1, 5])
        self.assertEqual(self.engine._first_audio_retries[1]["state"], "handed_out")

    # -- 16: Start Next / Eject ---------------------------------------------------------------------------
    def test_eject_before_the_deadline_owns_the_completion(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        original = self.opened[0]
        self.engine._start_next_cancel_for_operator_command = lambda *a: None
        self.engine._vtt_operator_command = lambda *a: None
        self.engine._eject_deck("A", expected_log_item_id=1)
        self.assertEqual(original.completion_reason, "eject")
        self._fire_deadline(original)
        self.assertFalse(original.first_audio_fenced)
        self.assertNotIn(1, self.engine.__dict__.get("_first_audio_retries", {}))
        self.assertEqual([d.log_item.id for d in self.opened], [1, 2])     # Eject replacement: the next item

    def test_start_next_fading_the_deck_owns_it(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        original = self.opened[0]
        self.engine._start_next_active = {"outgoing_deck": original}
        self._fire_deadline(original)
        self.assertFalse(original.completion_claimed or original.first_audio_fenced)
        self.assertIn("FIRST_AUDIO_DEADLINE_INERT_OTHER_OWNER", original.eos_milestones)

    def test_start_next_taking_the_queued_retry_opens_it_without_a_deadline_once(self):
        item = self._item(1)
        self._queue(item, self._item(2))
        self._start()
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)
        self._fire_deadline(self.opened[0])
        self.assertEqual(self.engine._forced_next_items, [item])
        self.engine.decks["B"] = None
        self.assertTrue(_wait_until(lambda: self.engine._free_slot() is not None))
        self.engine._start_next_track()          # Start Next's incoming start (no start kind)
        retry = self.opened[-1]
        self.assertIs(retry.log_item, item)
        self.assertEqual(retry.first_audio_attempt, 0)
        self.assertIn("FIRST_AUDIO_RETRY_OPENED_WITHOUT_DEADLINE", retry.eos_milestones)
        self.assertNotIn(1, self.engine._first_audio_retries)
        self.assertTrue(self.engine._occurrence_handed_out(item))

    # -- 17, 18: reload and log / hour change ------------------------------------------------------------
    def test_explicit_reload_withdraws_the_pending_retry(self):
        item = self._item(1)
        self._queue(item, self._item(2))
        self._start()
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)
        self._fire_deadline(self.opened[0])
        self.engine.decks["B"] = None
        self.engine._vtt_abort = lambda *a, **k: None
        self.engine._start_next_cancel_for_reload = lambda: None
        self.engine._finish_reload_and_restart_current_log = MagicMock()
        self.engine._reload_and_restart_current_log()
        self.assertEqual(self.engine._forced_next_items, [])
        self.assertNotIn(1, self.engine._first_audio_retries)
        withdrawn = [e for e in self.events if e["title"] == "First-audio retry withdrawn"]
        self.assertEqual(len(withdrawn), 1)
        self.assertEqual(withdrawn[0]["detail"]["reason"], "explicit_reload")
        self.assertTrue(self.engine._occurrence_handed_out(item))

    def test_a_new_active_log_withdraws_the_retry_at_hand_out(self):
        del self.engine._first_audio_retry_refusal                 # the real re-check (log test is DB-free)
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)
        self._fire_deadline(self.opened[0])
        self.engine.current_log = SimpleNamespace(id=8)             # rollover / takeover / new approved log
        new_hour = self._item(20, playlist_log_id=8)
        self._queue(new_hour)
        selected, _forced = self.engine._next_queue_item()
        self.assertIs(selected, new_hour)
        withdrawn = [e for e in self.events if e["title"] == "First-audio retry withdrawn"]
        self.assertEqual(withdrawn[0]["detail"]["reason"], "active_log_changed")

    def test_retry_is_not_queued_when_the_active_log_changed_before_the_deadline(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        self.engine.current_log = SimpleNamespace(id=8)
        self.engine.log_items = []
        self._fire_deadline(self.opened[0])
        critical = [e for e in self.events if e["level"] == "critical"]
        self.assertEqual([c["detail"]["reason"] for c in critical], ["active_log_changed"])
        self.assertEqual(self.engine._forced_next_items, [])

    # -- 19: VT and ordinary crossfade compatibility ----------------------------------------------------
    def test_crossfade_outgoing_keeps_playing_and_the_trigger_takes_the_retry(self):
        item = self._item(1)
        self._queue(item, self._item(2))
        outgoing = MagicMock(log_item=self._item(99), finished=False)
        self.engine.decks["B"] = outgoing
        self._start(slot="A")
        self._fire_deadline(self.opened[0])
        self.assertIs(self.engine.decks["B"], outgoing)            # the outgoing is never touched
        self.assertFalse(self.engine._next_triggered)              # the ordinary trigger re-fires
        self.assertIs(self.engine._peek_playable_at_cursor(), item)
        self.assertEqual(self.engine._next_queue_item_id(), item.id)

    def test_vt_outgoing_finish_hand_over_still_applies(self):
        self._queue(self._item(1), self._item(2))
        self._start()
        original = self.opened[0]
        self.engine._vtt_outgoing_finished = MagicMock(return_value=True)
        self._fire_deadline(original)
        self.engine._vtt_outgoing_finished.assert_called_once()
        self.assertEqual(len(self.opened), 1, "the VT transition owns what starts next")

    # -- 20: a poisoned teardown slot is never reused; a healthy successor still plays --------------------
    def test_poisoned_slot_is_not_reused_and_the_retry_uses_the_healthy_slot(self):
        self._queue(self._item(1), self._item(2))
        self._start(slot="A")
        original = self.opened[0]
        release = threading.Event()
        original.pipeline.set_state.side_effect = lambda _state: release.wait(5) or Gst.StateChangeReturn.SUCCESS
        self.addCleanup(release.set)
        self._fire_deadline(original)
        retry = self.opened[1]
        self.assertEqual(retry.slot, "B")
        time.sleep(0.6)                                             # past the coordinator's 0.5 s bound
        self.engine._deck_teardowns["A"].tick()                     # what _deck_teardown_tick does each poll
        self.assertTrue(self.engine._deck_teardowns["A"].snapshot()["poisoned"])
        self.assertFalse(self.engine._deck_slot_available("A"))
        self._fire_deadline(retry)                                  # the retry also fails: terminal
        self.assertTrue(_wait_until(lambda: self.engine._deck_slot_available("B"), timeout=3))
        if len(self.opened) == 2:
            # B was still returning from NULL when the terminal path asked for a slot: the existing healthy
            # teardown resume starts the successor (and, A having timed out, requests the 1.8 policy restart).
            self.engine._deck_teardown_tick()
            self.assertTrue(self.engine.restart_required)
        self.assertEqual(len(self.opened), 3)
        self.assertEqual(self.opened[2].first_audio_attempt, 1)
        self.assertEqual(self.opened[2].slot, "B")                  # the healthy successor, never slot A
        self.assertEqual(self.opened[2].log_item.id, 2)

    # ======================================================================================================
    # B1-R1 -- the deferred claim-contention callback keeps the unattended-start attribution.
    # ======================================================================================================
    def _contend_creates(self, count, *, only_item_id=None):
        """The next ``count`` _create_deck calls (of ``only_item_id``, else any unclaimed item) lose the claim to a
        transient row-lock conflict exactly as _claim_playback_occurrence reports it: no claim, no deck, the
        contended id recorded for _start_next_track."""
        inner = self.engine._create_deck
        state = {"left": count}

        def create(slot, log_item, *args, **kwargs):
            if (
                state["left"] > 0 and log_item.playback_claimed_at is None
                and only_item_id in (None, log_item.id)
            ):
                state["left"] -= 1
                self.engine._claim_contended_item_id = log_item.id
                return None
            return inner(slot, log_item, *args, **kwargs)

        self.engine._create_deck = create

    def _contention_timers(self):
        return [(ms, args) for ms, fn, args in self.timers if fn == self.engine._retry_contended_occurrence]

    def _fire_contention(self):
        """Run the newest scheduled claim-contention callback exactly as its GLib one-shot would."""
        timers = self._contention_timers()
        self.assertTrue(timers, "no deferred claim-contention retry is scheduled")
        return self.engine._retry_contended_occurrence(*timers[-1][1])

    def test_contended_unattended_start_is_armed_when_its_deferred_callback_opens_it(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._contend_creates(1)
        self._start()                                    # an unattended automatic start; its claim is contended
        self.assertEqual(self.opened, [])
        self.assertEqual(self._deadlines(), [], "a contended claim opened nothing, so nothing is armed")
        ((ms, args),) = self._contention_timers()
        self.assertEqual((ms, args), (eng_module.CLAIM_CONTENTION_RETRY_MS, ((1, 1),)))
        self.assertEqual(self.engine._queue_cursor, 0)   # the same occurrence stays next, exactly where it was

        self.engine._retry_contended_occurrence((1, 1))  # the real GLib one-shot, now past every guard
        self.assertEqual([d.log_item.id for d in self.opened], [1])
        deck = self.opened[0]
        self.assertEqual(deck.first_audio_attempt, 1)
        self.assertEqual([args for args, _fn in self._deadlines()], [(deck.slot, deck.generation, 1)])
        self.assertIsNone(self.engine._claim_contention)
        self.assertFalse(self.engine.__dict__.get("_first_audio_unattended_start", False))      # the context is not leaked
        # Contention is not B1 state: no retry record, nothing spent, no event.
        self.assertEqual((self.engine._first_audio_retries, self.engine.__dict__.get("_first_audio_spent")), ({}, None))
        self.assertEqual(self.events, [])
        self.assertEqual(self.engine._queue_cursor, 1)

    def test_contention_record_carries_the_start_attribution(self):
        self._queue(self._item(1), self._item(2))
        self._contend_creates(1)
        self._start()
        self.assertIs(self.engine._claim_contention["first_audio_unattended"], True)
        self.engine._claim_contention = None
        self.timers.clear()
        self.engine = self._engine()
        self._queue(self._item(1), self._item(2))
        self._contend_creates(1)
        self._start(kind=self.vt.StartKind.START_NEXT)
        self.assertIs(self.engine._claim_contention["first_audio_unattended"], False)

    def test_contended_then_stalled_occurrence_recovers_with_exactly_one_retry(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._contend_creates(1)
        self._start()
        self.engine._retry_contended_occurrence((1, 1))
        original = self.opened[0]
        claimed_at = item.playback_claimed_at
        self.assertIsNotNone(claimed_at)
        self._fire_deadline(original)                                      # first-audio timeout of the attempt
        self.assertEqual([d.log_item.id for d in self.opened], [1, 1])
        retry = self.opened[1]
        self.assertEqual(retry.first_audio_attempt, 2)
        self.assertIs(item.playback_claimed_at, claimed_at)                # claim identity survives
        self._settle()
        self._fire_deadline(retry)                                         # the retry stalls too: terminal
        self.assertEqual([d.log_item.id for d in self.opened], [1, 1, 2])  # never a third generation
        self.assertEqual(len([e for e in self.events if e["level"] == "critical"]), 1)
        self.assertEqual(self.opened[2].first_audio_attempt, 1)            # the successor is armed in its own right
        self.assertEqual(self.idle, [])

    def test_contention_retries_do_not_consume_or_pre_empt_the_audio_retry_budget(self):
        """Two contended claims, then success: the audio retry is still the single one B1 grants."""
        item = self._item(1)
        self._queue(item, self._item(2))
        self._contend_creates(2)
        self._start()
        self._fire_contention()
        self.assertEqual(self.engine._claim_contention["attempts"], 2)
        self._fire_contention()
        self.assertEqual([d.log_item.id for d in self.opened], [1])
        self.assertEqual(self.opened[0].first_audio_attempt, 1)
        self.assertEqual(self.engine._first_audio_retries, {})
        self.assertNotIn(1, self.engine.__dict__.get("_first_audio_spent", {}))
        self._fire_deadline(self.opened[0])
        self.assertEqual(self.opened[1].first_audio_attempt, 2)

    def test_persistent_contention_still_skips_with_its_own_event_and_no_b1_state(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._contend_creates(eng_module.CLAIM_CONTENTION_MAX_ATTEMPTS + 5, only_item_id=1)
        self._start()
        for _ in range(eng_module.CLAIM_CONTENTION_MAX_ATTEMPTS):
            self._fire_contention()
        skipped = [e for e in self.events if e["title"] == "Occurrence skipped after persistent claim contention"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["detail"]["attempts"], eng_module.CLAIM_CONTENTION_MAX_ATTEMPTS)
        self.assertEqual([d.log_item.id for d in self.opened], [2])        # existing behaviour: it moves on
        self.assertEqual(self.engine._first_audio_retries, {})
        self.assertEqual(self.engine.__dict__.get("_first_audio_spent"), None)
        self.assertEqual([t for t in self._titles() if "first-audio" in t.lower() or "audio" in t.lower()], [])

    def test_contention_callback_of_an_operator_start_is_not_armed(self):
        for name, kind in (("start_next", self.vt.StartKind.START_NEXT), ("manual", self.vt.StartKind.MANUAL),
                           ("eject", self.vt.StartKind.EJECT_REPLACEMENT), ("unkinded", None)):
            with self.subTest(name):
                self.timers.clear()
                self.engine = self._engine()
                self.opened.clear()
                self._queue(self._item(1), self._item(2))
                self._contend_creates(1)
                if kind is None:
                    self.engine._start_next_track(slot="A")
                else:
                    self._start(kind=kind)
                self.engine._retry_contended_occurrence((1, 1))
                self.assertEqual([d.log_item.id for d in self.opened], [1])
                self.assertEqual(self.opened[0].first_audio_attempt, 0)
                self.assertEqual(self._deadlines(), [])

    def test_a_timer_firing_alone_never_arms_manual_stale_or_excluded_occurrences(self):
        # Manual mode at callback time: inert, nothing opens, nothing is armed; the occurrence stays next.
        self._queue(self._item(1), self._item(2))
        self._contend_creates(1)
        self._start()
        self.engine.manual_mode = True
        self.assertFalse(self.engine._retry_contended_occurrence((1, 1)))
        self.assertEqual((self.opened, self._deadlines()), ([], []))
        self.assertEqual(self.engine._next_queue_item_id(), 1)
        self.assertFalse(self.engine.__dict__.get("_first_audio_unattended_start", False))

        # Stale tokens: a superseded attempt, an unknown item, a cleared contention and a stopped engine.
        self.engine.manual_mode = False
        self.assertFalse(self.engine._retry_contended_occurrence((1, 99)))
        self.assertFalse(self.engine._retry_contended_occurrence((777, 1)))
        self.engine.running = False
        self.assertFalse(self.engine._retry_contended_occurrence((1, 1)))
        self.engine.running = True
        self.assertEqual((self.opened, self._deadlines()), ([], []))
        self.engine._claim_contention = None
        self.assertFalse(self.engine._retry_contended_occurrence((1, 1)))
        self.assertEqual((self.opened, self._deadlines()), ([], []))

    def test_an_older_contention_token_is_inert_once_a_newer_attempt_is_pending(self):
        self._queue(self._item(1), self._item(2))
        self._contend_creates(2)
        self._start()
        self._fire_contention()                                            # attempt 1 -> contended again
        self.assertEqual(self.engine._claim_contention["attempts"], 2)
        self.assertFalse(self.engine._retry_contended_occurrence((1, 1)))  # the stale first token
        self.assertEqual(self.opened, [])
        self.engine._retry_contended_occurrence((1, 2))
        self.assertEqual([d.first_audio_attempt for d in self.opened], [1])

    def test_contended_excluded_occurrences_stay_unarmed_through_the_callback(self):
        U = self.LogItem
        cases = {
            "weather_amber": dict(item=dict(origin=U.ORIGIN_ENGINE_INSERTED, category_code="WxAlert")),
            "dedication_intro": dict(item=dict(category_code="Dedications")),
            "operator_inserted": dict(item=dict(origin=U.ORIGIN_OPERATOR_INSERTED)),
            "no_origin": dict(item=dict(origin=None)),
            "forced": dict(forced=True),
            "active_message": dict(native=True),
        }
        for name, spec in cases.items():
            with self.subTest(name):
                self.timers.clear()
                self.engine = self._engine()
                self.opened.clear()
                item = self._item(5, **spec.get("item", {}))
                if spec.get("forced"):
                    self.engine._forced_next_items = [item]
                    self.engine.log_items = []
                else:
                    self._queue(item)
                if spec.get("native"):
                    self.engine._active_message_held = {item.id: "attempt-1"}
                self._contend_creates(1)
                self._start()
                self.assertEqual(self.opened, [])
                ((_ms, args),) = self._contention_timers()
                self.engine._retry_contended_occurrence(*args)
                self.assertEqual([d.log_item.id for d in self.opened], [5], name)
                self.assertEqual(self.opened[0].first_audio_attempt, 0, name)
                self.assertEqual(self._deadlines(), [], name)

    def test_already_claimed_occurrence_is_never_deferred_so_the_retry_open_keeps_its_own_path(self):
        """A retry (its claim is already held) that loses a row lock is not a contention deferral: it ends the
        occurrence once, with the underlying cause reported (B1-R3)."""
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        original = self.opened[0]
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)   # retry waits in the queue
        self._fire_deadline(original)
        self.engine.decks["B"] = None
        self.assertTrue(_wait_until(lambda: self.engine._free_slot() is not None))
        inner = self.engine._create_deck

        def contended_create(slot, log_item, *args, **kwargs):
            if log_item.id == 1:
                self.engine._claim_contended_item_id = 1
                return None
            return inner(slot, log_item, *args, **kwargs)

        self.engine._create_deck = contended_create
        self.engine._start_next_track()
        self.assertEqual(self._contention_timers(), [])                    # claimed rows are never deferred
        critical = [e for e in self.events if e["level"] == "critical"]
        self.assertEqual(len(critical), 1)
        detail = critical[0]["detail"]
        self.assertEqual(detail["reason"], "first_audio_retry_claim_contended")     # not the generic reason
        self.assertEqual((detail["attempts"], detail["retry_attempted"], detail["retry_opened"],
                          detail["retry_generation"]), (1, True, False, None))
        self.assertEqual([d.log_item.id for d in self.opened if d.log_item.id == 1], [1])   # one generation only
        self.assertEqual(self.opened[-1].log_item.id, 2)                    # playout advanced

    def test_retry_that_cannot_open_for_another_reason_keeps_the_generic_reason_and_accurate_counts(self):
        item, nxt = self._item(1), self._item(2)
        self._queue(item, nxt)
        self._start()
        self.engine.decks["B"] = MagicMock(log_item=self._item(99), finished=False)
        self._fire_deadline(self.opened[0])
        self.engine.decks["B"] = None
        self.assertTrue(_wait_until(lambda: self.engine._free_slot() is not None))
        inner = self.engine._create_deck
        self.engine._create_deck = lambda slot, log_item, *a, **k: (
            None if log_item.id == 1 else inner(slot, log_item, *a, **k))
        self.engine._start_next_track()
        critical = [e["detail"] for e in self.events if e["level"] == "critical"]
        self.assertEqual([c["reason"] for c in critical], ["first_audio_retry_not_opened"])
        self.assertEqual((critical[0]["attempts"], critical[0]["retry_opened"]), (1, False))

    # ======================================================================================================
    # B1-R2 -- consumed retry authority is retained for the life of its programming authority.
    # ======================================================================================================
    def _log_item_set(self, first_id, count, playlist_log_id):
        return [self._item(first_id + n, playlist_log_id=playlist_log_id) for n in range(count)]

    def _exhaust_in_order(self, count):
        """Drive the real queue, start, deadline, retry and retirement code through ``count`` consecutive
        occurrences that each stall twice: every one of them ends spent."""
        for expected in range(1, count + 1):
            original = self.opened[-1]
            self.assertEqual(original.log_item.id, expected)
            self._fire_deadline(original)
            retry = self.opened[-1]
            self.assertEqual((retry.log_item.id, retry.first_audio_attempt), (expected, 2))
            self._settle()
            self._fire_deadline(retry)
            self._settle()
            self.assertEqual(self.opened[-1].log_item.id, expected + 1)

    def test_more_than_the_old_fifo_limit_of_spent_occurrences_all_stay_suppressed_across_reload(self):
        total = 290                                  # > 256, the limit of the reviewed candidate's FIFO
        items = self._log_item_set(1, total + 10, playlist_log_id=7)
        self._queue(*items)
        self._start()
        self._exhaust_in_order(total)                # items 1..290 each spend their one retry; 291 is open
        spent = self.engine._first_audio_spent
        self.assertGreater(len(spent), 256)
        self.assertEqual(sorted(spent), list(range(1, total + 1)))
        self.assertEqual(len([e for e in self.events if e["level"] == "critical"]), total)
        oldest = items[0]
        self.assertTrue(self.engine._occurrence_handed_out(oldest))

        # Explicit reload of the SAME active log: fresh rows, same ids, nothing aired, cursor back at the top.
        fresh = self._log_item_set(1, total + 10, playlist_log_id=7)
        log = SimpleNamespace(id=7, date=None, hour=3)
        with patch.object(self.engine, "_approved_log_queue", return_value=(log, fresh)), patch.object(
            self.engine, "_log_queue_eligibility", return_value=(True, None)
        ):
            self.assertTrue(self.engine._load_log_for(None, 3))
        self.assertEqual(self.engine._queue_cursor, 0)
        self.assertEqual(len(self.engine._first_audio_spent), total)        # reload releases nothing
        for stale in (fresh[0], fresh[100], fresh[total - 1]):
            self.assertTrue(self.engine._occurrence_handed_out(stale), stale.id)
            self.assertTrue(self.engine._first_audio_owns(stale), stale.id)

        # Normal cursor traversal: every spent occurrence is passed over; unspent ones are not (the live deck is
        # cleared first, so occurrence 291 -- unspent, merely open -- is the first one offered).
        self.engine.decks = {"A": None, "B": None}
        handed_out = []
        while True:
            item, _forced = self.engine._next_queue_item()
            if item is None:
                break
            handed_out.append(item.id)
        self.assertEqual(handed_out, list(range(total + 1, total + 11)))
        self.assertTrue(set(handed_out).isdisjoint(self.engine._first_audio_spent))

    def test_a_spent_occurrence_is_never_restored_by_an_old_fifo_style_eviction(self):
        """Recording many more spends than the old limit never forgets an earlier one while its log is active."""
        self._queue(*self._log_item_set(1, 5, playlist_log_id=7))
        for item_id in range(1, 1500):
            self.engine._first_audio_spend(item_id, 7)
        self.assertEqual(len(self.engine._first_audio_spent), 1499)
        self.assertTrue(self.engine._first_audio_owns(self._item(1)))

    def test_consumed_authority_is_reclaimed_only_when_its_log_is_unreachable(self):
        old_items = self._log_item_set(1, 3, playlist_log_id=7)
        self._queue(*old_items)
        for item in old_items:
            self.engine._first_audio_spend(item.id, 7)
        engine = self.engine
        new_items = self._log_item_set(100, 3, playlist_log_id=8)

        # A different log becomes active, but the old log's occurrence is still held by a live deck.
        live = MagicMock(log_item=old_items[0], finished=False)
        engine.decks["A"] = live
        engine.current_log = SimpleNamespace(id=8)
        engine.log_items = list(new_items)
        self.assertEqual(engine._first_audio_prune_spent(), 0)
        self.assertEqual(sorted(engine._first_audio_spent), [1, 2, 3])
        engine.decks["A"] = None
        # ...or queued in the forced queue (a pending retry / carried urgent)...
        engine._forced_next_items = [old_items[1]]
        self.assertEqual(engine._first_audio_prune_spent(), 0)
        engine._forced_next_items = []
        # ...or held by a VT hold, or by a live retry record.
        engine._vtt_held_occurrence = {"log_item": old_items[2], "slot": "A", "reason": "x"}
        self.assertEqual(engine._first_audio_prune_spent(), 0)
        engine._vtt_held_occurrence = None
        retries = engine.__dict__.setdefault("_first_audio_retries", {})
        retries[3] = {"state": "pending", "log_item": old_items[2], "playlist_log_id": 7}
        self.assertEqual(engine._first_audio_prune_spent(), 0)
        retries.clear()
        self.assertEqual(sorted(engine._first_audio_spent), [1, 2, 3])

        # Nothing reaches the old log any more: its consumed authority is released, and only it.
        engine._first_audio_spend(100, 8)                                   # the active log's own entry
        self.assertEqual(sorted(engine._first_audio_spent), [100])

    def test_without_an_active_log_nothing_is_provable_so_nothing_is_released(self):
        self.engine._first_audio_spend(1, 7)
        self.engine.current_log = None
        self.assertEqual(self.engine._first_audio_prune_spent(), 0)
        self.assertEqual(list(self.engine._first_audio_spent), [1])

    def test_a_new_log_install_releases_the_old_logs_authority_and_new_occurrences_are_eligible(self):
        total = 40
        items = self._log_item_set(1, total + 2, playlist_log_id=7)
        self._queue(*items)
        self._start()
        self._exhaust_in_order(total)
        self.assertEqual(len(self.engine._first_audio_spent), total)
        # Hour transition: item 41 is still on a deck (the outgoing track), so log 7 stays reachable...
        fresh = self._log_item_set(1, 4, playlist_log_id=8)        # ids COLLIDE with the old log's: reuse
        new_log = SimpleNamespace(id=8, date=None, hour=4)
        with patch.object(self.engine, "_approved_log_queue", return_value=(new_log, fresh)), patch.object(
            self.engine, "_log_queue_eligibility", return_value=(True, None)
        ):
            self.engine._load_log_for(None, 4)
        self.assertEqual(len(self.engine._first_audio_spent), total)
        for row in fresh:        # an unrelated occurrence reusing an old id inherits nothing
            self.assertFalse(self.engine._first_audio_owns(row), row.id)
            self.assertFalse(self.engine._occurrence_handed_out(row), row.id)
        # ...and once the old log is unreachable the next lifecycle point (here another install) frees it.
        self.engine.decks = {"A": None, "B": None}
        with patch.object(self.engine, "_approved_log_queue", return_value=(new_log, fresh)), patch.object(
            self.engine, "_log_queue_eligibility", return_value=(True, None)
        ):
            self.engine._load_log_for(None, 4)
        self.assertEqual(self.engine._first_audio_spent, {})
        self.assertEqual(self.engine._queue_cursor, 0)
        item, _forced = self.engine._next_queue_item()
        self.assertEqual(item.id, 1)                                # id 1 of log 8 is eligible again
