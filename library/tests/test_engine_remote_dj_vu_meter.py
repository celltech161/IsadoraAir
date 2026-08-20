"""Roadmap 4.1 -- "Make Remote Mic PTT fill act as a live VU meter while
ON." Engine-side backend half: the new gain-adjusted `session.dj_level_
sample` capture in `_on_element_message`'s existing dj_level branch, the
`_remote_dj_level_payload()` None/valid/stale resolution, and the
existing output_level handler's LEVELS_PATH payload gaining exactly one
new "remote_dj" key while its four existing top-level keys (ts/rms/peak/
decay) stay unchanged.

Real GStreamer `level` elements on synthetic audiotestsrc pipelines are
used wherever a real "level" bus message is needed, matching this
suite's established convention (test_engine_mic_recovery.py,
test_remote_dj_monitor_mixer_timeline.py: exercise real Gst behavior,
don't reimplement its message shape) -- no real ALSA/webrtc hardware is
opened anywhere in this file. The one exception is
MalformedDjLevelStructureFailsSafeTests, which needs a structure that
fails in a way a real level element never would; that uses a small
hand-built stand-in, not GStreamer.

object.__new__(PlaybackEngine) bypasses __init__ (which builds a real
full pipeline and calls Gst.init itself), same technique as every other
engine test in this suite."""
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.test import SimpleTestCase, TestCase

import library.services.engine as eng_module
from hardware.models import RemoteDJAudioInput

Gst.init(None)


def _build_level_pipeline():
    """A tiny real pipeline: audiotestsrc -> level -> fakesink. Returns
    (pipeline, level_element)."""
    pipeline = Gst.Pipeline.new(None)
    src = Gst.ElementFactory.make("audiotestsrc", None)
    src.set_property("wave", "sine")
    src.set_property("volume", 0.5)
    level = Gst.ElementFactory.make("level", None)
    level.set_property("interval", 20_000_000)  # 20ms -- fast, keeps tests quick
    level.set_property("post-messages", True)
    sink = Gst.ElementFactory.make("fakesink", None)
    for el in (src, level, sink):
        pipeline.add(el)
    src.link(level)
    level.link(sink)
    return pipeline, level


def _get_level_messages(pipeline, level_element, count=1, timeout_s=5.0):
    """Sets the pipeline PLAYING, pulls the bus until `count` real
    "level" element messages from level_element have arrived (all
    within one PLAYING session), tears the pipeline back down to NULL,
    and returns them as a list."""
    bus = pipeline.get_bus()
    pipeline.set_state(Gst.State.PLAYING)
    try:
        collected = []
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and len(collected) < count:
            msg = bus.timed_pop_filtered(int(0.5 * Gst.SECOND), Gst.MessageType.ELEMENT)
            if msg is None:
                continue
            structure = msg.get_structure()
            if structure is not None and structure.get_name() == "level" and msg.src is level_element:
                collected.append(msg)
        if len(collected) < count:
            raise AssertionError(f"only {len(collected)}/{count} level messages received within timeout")
        return collected
    finally:
        pipeline.set_state(Gst.State.NULL)


def _get_one_level_message(pipeline, level_element, timeout_s=5.0):
    return _get_level_messages(pipeline, level_element, count=1, timeout_s=timeout_s)[0]


class RemoteDjLevelSampleCaptureTests(SimpleTestCase):
    """_on_element_message's dj_level branch: gain-adjusted sample
    capture, alongside (not instead of) the pre-existing diagnostic
    logging."""

    def setUp(self):
        self.pipeline, self.level = _build_level_pipeline()
        self.addCleanup(self.pipeline.set_state, Gst.State.NULL)

    def _stand_in(self, session):
        obj = object.__new__(eng_module.PlaybackEngine)
        obj.remote_dj_session = session
        obj.output_level = object()  # a distinct object -- message.src must never match this
        return obj

    def test_gain_is_applied_to_captured_sample(self):
        msg = _get_one_level_message(self.pipeline, self.level)
        structure = msg.get_structure()
        raw_rms = list(structure.get_value("rms"))
        raw_peak = list(structure.get_value("peak"))
        raw_decay = list(structure.get_value("decay"))

        session = eng_module.RemoteDJSession()
        session.dj_level = self.level
        session.dj_gain_db = 6.0
        stand_in = self._stand_in(session)

        result = stand_in._on_element_message(None, msg)

        self.assertTrue(result)
        self.assertIsNotNone(session.dj_level_sample)
        for ch in range(len(raw_rms)):
            self.assertAlmostEqual(session.dj_level_sample["rms"][ch], raw_rms[ch] + 6.0, places=6)
            self.assertAlmostEqual(session.dj_level_sample["peak"][ch], raw_peak[ch] + 6.0, places=6)
            self.assertAlmostEqual(session.dj_level_sample["decay"][ch], raw_decay[ch] + 6.0, places=6)
        self.assertLessEqual(abs(time.time() - session.dj_level_sample["ts"]), 2.0)

    def test_zero_gain_leaves_values_unchanged(self):
        msg = _get_one_level_message(self.pipeline, self.level)
        structure = msg.get_structure()
        raw_rms = list(structure.get_value("rms"))

        session = eng_module.RemoteDJSession()
        session.dj_level = self.level
        session.dj_gain_db = 0.0
        stand_in = self._stand_in(session)

        stand_in._on_element_message(None, msg)

        for ch in range(len(raw_rms)):
            self.assertAlmostEqual(session.dj_level_sample["rms"][ch], raw_rms[ch], places=6)

    def test_diagnostic_logging_is_unaffected(self):
        """The pre-existing dj_level diag-log line must still be written
        exactly as before -- this feature rides alongside it, never
        replaces it."""
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        diag_path = Path(tmp_dir.name) / "diag.log"

        msg = _get_one_level_message(self.pipeline, self.level)
        session = eng_module.RemoteDJSession()
        session.dj_level = self.level
        session.dj_gain_db = 6.0
        session.diag_fh = open(diag_path, "w")
        self.addCleanup(session.diag_fh.close)
        stand_in = self._stand_in(session)

        stand_in._on_element_message(None, msg)
        session.diag_fh.flush()

        self.assertIn("dj_level peak=", diag_path.read_text())
        # And the new capture happened too -- proves the two blocks
        # coexist rather than one silently replacing the other.
        self.assertIsNotNone(session.dj_level_sample)


class RemoteDjLevelPayloadResolutionTests(SimpleTestCase):
    """_remote_dj_level_payload()'s None/valid/stale resolution --
    pure logic, no GStreamer involved."""

    def _stand_in(self, session):
        obj = object.__new__(eng_module.PlaybackEngine)
        obj.remote_dj_session = session
        return obj

    def test_no_active_session_returns_none(self):
        obj = self._stand_in(None)
        self.assertIsNone(obj._remote_dj_level_payload())

    def test_session_with_no_sample_yet_returns_none(self):
        session = eng_module.RemoteDJSession()
        obj = self._stand_in(session)
        self.assertIsNone(obj._remote_dj_level_payload())

    def test_fresh_sample_is_returned(self):
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {"ts": time.time(), "rms": [-20.0], "peak": [-18.0], "decay": [-19.0]}
        obj = self._stand_in(session)
        self.assertEqual(obj._remote_dj_level_payload(), session.dj_level_sample)

    def test_stale_sample_returns_none(self):
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {
            "ts": time.time() - (eng_module.REMOTE_DJ_LEVEL_STALE_S + 0.5),
            "rms": [-20.0], "peak": [-18.0], "decay": [-19.0],
        }
        obj = self._stand_in(session)
        self.assertIsNone(obj._remote_dj_level_payload())

    def test_sample_just_under_threshold_is_still_returned(self):
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {
            "ts": time.time() - (eng_module.REMOTE_DJ_LEVEL_STALE_S - 0.2),
            "rms": [-20.0], "peak": [-18.0], "decay": [-19.0],
        }
        obj = self._stand_in(session)
        self.assertIsNotNone(obj._remote_dj_level_payload())

    def test_disconnect_clears_immediately_even_with_a_fresh_sample(self):
        """Mirrors _remote_dj_session_stop's own first line
        (self.remote_dj_session = None, BEFORE any other teardown) --
        a perfectly fresh sample must resolve to None the instant the
        session reference itself is gone. This is what makes a
        disconnect clear within one ~50ms output_level tick rather than
        waiting out REMOTE_DJ_LEVEL_STALE_S."""
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {"ts": time.time(), "rms": [-10.0], "peak": [-8.0], "decay": [-9.0]}
        obj = self._stand_in(session)
        self.assertIsNotNone(obj._remote_dj_level_payload())
        obj.remote_dj_session = None
        self.assertIsNone(obj._remote_dj_level_payload())


class OutputLevelPayloadRemoteDjKeyTests(SimpleTestCase):
    """The existing output_level handler's LEVELS_PATH write -- exactly
    one new "remote_dj" key, existing keys/contract untouched."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.levels_path = Path(tmp_dir.name) / "levels.json"
        self.levels_tmp_path = Path(tmp_dir.name) / "levels.json.tmp"
        for name, val in (("LEVELS_PATH", self.levels_path), ("LEVELS_TMP_PATH", self.levels_tmp_path)):
            patcher = mock.patch.object(eng_module, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pipeline, self.level = _build_level_pipeline()
        self.addCleanup(self.pipeline.set_state, Gst.State.NULL)

    def _write_once(self, remote_dj_session):
        msg = _get_one_level_message(self.pipeline, self.level)
        stand_in = object.__new__(eng_module.PlaybackEngine)
        stand_in.output_level = self.level
        stand_in.remote_dj_session = remote_dj_session
        result = stand_in._on_element_message(None, msg)
        self.assertTrue(result)
        return json.loads(self.levels_path.read_text())

    def test_existing_top_level_keys_unchanged_and_remote_dj_null_with_no_session(self):
        payload = self._write_once(None)
        self.assertEqual(set(payload.keys()), {"ts", "rms", "peak", "decay", "remote_dj"})
        self.assertIsInstance(payload["ts"], float)
        self.assertIsInstance(payload["rms"], list)
        self.assertIsInstance(payload["peak"], list)
        self.assertIsInstance(payload["decay"], list)
        self.assertIsNone(payload["remote_dj"])

    def test_remote_dj_key_embeds_valid_session_sample(self):
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {
            "ts": time.time(), "rms": [-12.0, -14.0], "peak": [-10.0, -11.0], "decay": [-11.0, -12.0],
        }
        payload = self._write_once(session)
        self.assertEqual(payload["remote_dj"], session.dj_level_sample)

    def test_remote_dj_key_null_when_session_sample_is_stale(self):
        session = eng_module.RemoteDJSession()
        session.dj_level_sample = {
            "ts": time.time() - (eng_module.REMOTE_DJ_LEVEL_STALE_S + 0.5),
            "rms": [-12.0], "peak": [-10.0], "decay": [-11.0],
        }
        payload = self._write_once(session)
        self.assertIsNone(payload["remote_dj"])


class MalformedDjLevelStructureFailsSafeTests(SimpleTestCase):
    """A level structure that fails in ways a real GStreamer level
    element never would -- get_value() raising, or returning something
    non-iterable. _on_element_message must never raise out of these,
    and must never corrupt a previously-good sample."""

    class _RaisingStructure:
        def get_name(self):
            return "level"

        def get_value(self, key):
            raise ValueError(f"simulated malformed level structure ({key})")

    class _EmptyValueStructure:
        def get_name(self):
            return "level"

        def get_value(self, key):
            return []

    class _FakeMessage:
        def __init__(self, src, structure):
            self.src = src
            self._structure = structure

        def get_structure(self):
            return self._structure

    def _stand_in(self, session):
        obj = object.__new__(eng_module.PlaybackEngine)
        obj.remote_dj_session = session
        obj.output_level = object()
        return obj

    def test_raising_get_value_does_not_raise_and_leaves_sample_none(self):
        session = eng_module.RemoteDJSession()
        fake_dj_level = object()
        session.dj_level = fake_dj_level
        message = self._FakeMessage(fake_dj_level, self._RaisingStructure())
        stand_in = self._stand_in(session)

        result = stand_in._on_element_message(None, message)

        self.assertTrue(result)
        self.assertIsNone(session.dj_level_sample)

    def test_raising_get_value_preserves_a_previously_good_sample(self):
        session = eng_module.RemoteDJSession()
        previous = {"ts": time.time(), "rms": [-5.0], "peak": [-3.0], "decay": [-4.0]}
        session.dj_level_sample = previous
        fake_dj_level = object()
        session.dj_level = fake_dj_level
        message = self._FakeMessage(fake_dj_level, self._RaisingStructure())
        stand_in = self._stand_in(session)

        stand_in._on_element_message(None, message)

        self.assertEqual(session.dj_level_sample, previous)

    def test_empty_value_lists_produce_a_well_formed_empty_sample(self):
        """Empty (not raising) is a distinct, also-safe case: a
        well-formed sample with empty channel lists, not a crash and
        not a silently-preserved stale value."""
        session = eng_module.RemoteDJSession()
        session.dj_gain_db = 6.0
        fake_dj_level = object()
        session.dj_level = fake_dj_level
        message = self._FakeMessage(fake_dj_level, self._EmptyValueStructure())
        stand_in = self._stand_in(session)

        result = stand_in._on_element_message(None, message)

        self.assertTrue(result)
        self.assertEqual(session.dj_level_sample["rms"], [])
        self.assertEqual(session.dj_level_sample["peak"], [])
        self.assertEqual(session.dj_level_sample["decay"], [])


class RemoteDjSessionStartAppliesGainOnceTests(TestCase):
    """_remote_dj_session_start's gain wiring: session.dj_gain_db and
    slot.remote_gain's volume must derive from the SAME single
    RemoteDJAudioInput.load() read -- no per-meter-message DB query.
    Uses a real DB row (TestCase, not SimpleTestCase) since
    RemoteDJAudioInput.load() is a real ORM call this method is not
    stubbed around -- that IS the thing under test.

    _remote_dj_build_session (real webrtcbin/ICE setup) is stubbed to a
    no-op -- out of scope here and exactly the kind of heavy real-network
    setup this suite's other engine tests already avoid calling
    end-to-end (see test_engine_runtime_commit.py's own docstring)."""

    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        for name, suffix in (("DJ_DIAG_LOG", "diag.log"), ("DJ_DUMP_PCM", "dump.pcm")):
            patcher = mock.patch.object(eng_module, name, Path(tmp_dir.name) / suffix)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _stand_in(self):
        remote_gain = Gst.ElementFactory.make("volume", None)
        remote_gate = Gst.ElementFactory.make("volume", None)
        slot = eng_module.RemoteDJSlot(
            slot_id=0, selector=None, silence_pad=None, webrtc_pad=None,
            remote_gain=remote_gain, remote_gate=remote_gate,
            master_mixer_pad=None, silence_src=None,
        )
        obj = object.__new__(eng_module.PlaybackEngine)
        obj.remote_dj_session = None
        obj.remote_dj_tee = object()  # just needs to be not-None ("feature is built")
        obj.dj_slots = [slot]
        obj._remote_dj_build_session = lambda session: None
        return obj, slot

    def test_session_gain_matches_configured_value(self):
        RemoteDJAudioInput.objects.update_or_create(pk=1, defaults={"gain_db": 9.5})
        stand_in, slot = self._stand_in()

        # _remote_dj_session_start is a GLib.idle_add callback (see
        # library/services/remote_dj_signaling.py) -- its return value
        # is the idle-source protocol ("don't reschedule me"), NOT a
        # success/failure signal. Success is observed via the resulting
        # state (self.remote_dj_session/slot.remote_gain), not the
        # return value.
        stand_in._remote_dj_session_start()

        self.assertIsNotNone(stand_in.remote_dj_session)
        self.assertEqual(stand_in.remote_dj_session.dj_gain_db, 9.5)
        self.assertAlmostEqual(slot.remote_gain.get_property("volume"), 10 ** (9.5 / 20.0), places=6)

    def test_gain_read_exactly_once_per_session_start_not_per_meter_message(self):
        RemoteDJAudioInput.objects.update_or_create(pk=1, defaults={"gain_db": 3.0})
        stand_in, slot = self._stand_in()

        with mock.patch.object(RemoteDJAudioInput, "load", wraps=RemoteDJAudioInput.load) as mock_load:
            stand_in._remote_dj_session_start()
            self.assertEqual(mock_load.call_count, 1)

            # Simulate several meter messages arriving on the already-
            # started session -- must not touch the DB at all.
            session = stand_in.remote_dj_session
            pipeline, level = _build_level_pipeline()
            self.addCleanup(pipeline.set_state, Gst.State.NULL)
            session.dj_level = level
            stand_in.output_level = object()
            for msg in _get_level_messages(pipeline, level, count=3):
                stand_in._on_element_message(None, msg)
            self.assertEqual(mock_load.call_count, 1)
