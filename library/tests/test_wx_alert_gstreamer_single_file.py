"""r0137: real GStreamer evidence that ONE mutable WxAlert/wx_alert.flac is safe for playout.

A deck is built exactly as engine._create_deck builds one (filesrc -> decodebin -> audioconvert ->
audioresample -> fixed caps, a bin added to an already-PLAYING live audiomixer pipeline and started with
sync_state_with_parent) and its decoded audio is classified per window by tone: generation A is 440 Hz,
generation B 880 Hz.  Synthetic FLACs in a private temporary directory; nothing reaches any audio device.

Proves:
  * the file is opened synchronously inside sync_state_with_parent (an fd on the path exists when it returns);
  * an atomic replacement (new inode) after that leaves the open deck's audio and length unchanged;
  * a replacement before the open is what the deck plays (why the open must be inside the publication lock);
  * an IN-PLACE rewrite would mix generations (reads are incremental) -- the negative control for
    publish_weather_asset's rename-only contract;
  * with the engine's shared publication lock held from authorization through the open, a publication
    (exclusive lock) cannot slip in between -- and once the deck has the file, a publication cannot alter it.

Requires GStreamer (gi, filesrc/decodebin/flac/audiomixer) and ffmpeg; skipped otherwise.
"""
import fcntl
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair import wx_alert_state

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    _GST_OK = all(Gst.ElementFactory.find(name) for name in
                  ("filesrc", "decodebin", "audiomixer", "audiotestsrc", "flacparse", "flacdec"))
except Exception:  # noqa: BLE001
    _GST_OK = False

RATE = 44100
WINDOW = 0.25


def _ffmpeg_tone(path, hz, seconds):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={hz}:duration={seconds}:sample_rate={RATE}", "-ac", "2",
                    "-c:a", "flac", str(path)], check=True, timeout=30, stdin=subprocess.DEVNULL)


@unittest.skipUnless(_GST_OK and shutil.which("ffmpeg"), "GStreamer FLAC playback or ffmpeg unavailable")
class SingleFileDeckTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxalert-gst-")
        root = Path(cls._tmp.name)
        cls.gen_a, cls.gen_b = root / "a.flac", root / "b.flac"
        _ffmpeg_tone(cls.gen_a, 440, 3.0)
        _ffmpeg_tone(cls.gen_b, 880, 1.5)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="isadoraair-wxalert-gst-case-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dest = wx_alert_state.audio_path(self.root)
        self.dest.parent.mkdir(parents=True)
        self.lock_path = self.dest.parent / f".{self.dest.name}.publish.lock"
        self.atomic_replace(self.gen_a)

    # -- publication stand-ins -------------------------------------------------------------------
    def atomic_replace(self, source):
        staging = self.dest.parent / ".candidate"
        shutil.copy2(source, staging)
        staging.replace(self.dest)

    def rewrite_in_place(self, source):
        with open(source, "rb") as src, open(self.dest, "r+b") as dst:
            dst.write(src.read())
            dst.truncate()

    # -- the deck, built as engine._create_deck builds it ---------------------------------------
    def start_pipeline(self):
        pipe = Gst.Pipeline.new("main")
        live = Gst.ElementFactory.make("audiotestsrc")
        live.set_property("wave", "silence")
        live.set_property("is-live", True)
        self.mixer = Gst.ElementFactory.make("audiomixer")
        caps = Gst.ElementFactory.make("capsfilter")
        caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={RATE},channels=2"))
        sink = Gst.ElementFactory.make("fakesink")
        sink.set_property("sync", True)
        for element in (live, self.mixer, caps, sink):
            pipe.add(element)
        live.link(self.mixer)
        self.mixer.link(caps)
        caps.link(sink)
        pipe.set_state(Gst.State.PLAYING)
        pipe.get_state(5 * Gst.SECOND)
        self.addCleanup(pipe.set_state, Gst.State.NULL)
        self.pipe = pipe

    def build_deck(self):
        src = Gst.ElementFactory.make("filesrc")
        src.set_property("location", str(self.dest))
        decode = Gst.ElementFactory.make("decodebin")
        convert = Gst.ElementFactory.make("audioconvert")
        resample = Gst.ElementFactory.make("audioresample")
        outcaps = Gst.ElementFactory.make("capsfilter")
        outcaps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format=S16LE,rate={RATE},channels=2"))
        deck = Gst.Bin.new("deck")
        for element in (src, decode, convert, resample, outcaps):
            deck.add(element)
        src.link(decode)
        convert.link(resample)
        resample.link(outcaps)
        deck.add_pad(Gst.GhostPad.new("src", outcaps.get_static_pad("src")))
        decode.connect("pad-added", lambda _el, pad: pad.link(convert.get_static_pad("sink"))
                       if pad.get_current_caps().get_structure(0).get_name().startswith("audio") else None)
        self.samples, self.eos = [], threading.Event()

        def tap(pad, info):
            if info.type & Gst.PadProbeType.BUFFER:
                buf = info.get_buffer()
                ok, mapped = buf.map(Gst.MapFlags.READ)
                if ok:
                    frames = struct.unpack("<%dh" % (mapped.size // 2), mapped.data)
                    self.samples.append((buf.pts / Gst.SECOND, frames[0::2]))
                    buf.unmap(mapped)
            elif info.type & Gst.PadProbeType.EVENT_DOWNSTREAM:
                event = info.get_event()
                if event is not None and event.type == Gst.EventType.EOS:
                    self.eos.set()
            return Gst.PadProbeReturn.OK
        outcaps.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.EVENT_DOWNSTREAM, tap)
        return deck

    def open_deck(self, deck):
        """engine._create_deck's tail: add, link to the live mixer, offset, sync."""
        self.pipe.add(deck)
        deck.get_static_pad("src").link(self.mixer.request_pad_simple("sink_%u"))
        clock = self.pipe.get_clock()
        deck.get_static_pad("src").set_offset(clock.get_time() - self.pipe.get_base_time())
        deck.sync_state_with_parent()

    def fds_on_dest(self):
        out = []
        for fd in os.listdir("/proc/self/fd"):
            try:
                if os.readlink(f"/proc/self/fd/{fd}") == str(self.dest):
                    out.append(os.fstat(int(fd)).st_ino)
            except OSError:
                pass
        return out

    def heard(self, mutate_after=None, mutate=None):
        started = time.monotonic()
        mutated = False
        while not self.eos.is_set() and time.monotonic() - started < 15:
            if mutate is not None and not mutated and time.monotonic() - started >= mutate_after:
                mutate()
                mutated = True
            time.sleep(0.02)
        self.assertTrue(self.eos.is_set(), "deck never reached EOS")
        windows = {}
        for pts, left in self.samples:
            for i, value in enumerate(left):
                windows.setdefault(int((pts + i / RATE) / WINDOW), []).append(value)
        labels = []
        for key in sorted(windows):
            values = windows[key]
            crossings = sum(1 for a, b in zip(values, values[1:]) if (a < 0) != (b < 0))
            hz = crossings / 2 / (len(values) / RATE) if values else 0
            labels.append("A" if 400 < hz < 480 else "B" if 840 < hz < 920 else "?")
        seconds = sum(len(left) for _, left in self.samples) / RATE
        return "".join(labels), seconds

    # -- tests -----------------------------------------------------------------------------------
    def test_the_file_is_open_when_sync_state_with_parent_returns(self):
        self.start_pipeline()
        deck = self.build_deck()
        self.assertEqual(self.fds_on_dest(), [])
        self.open_deck(deck)
        self.assertEqual(self.fds_on_dest(), [os.stat(self.dest).st_ino])
        self.atomic_replace(self.gen_b)                  # immediately, same thread
        labels, seconds = self.heard()
        self.assertEqual(set(labels), {"A"})
        self.assertAlmostEqual(seconds, 3.0, delta=0.1)

    def test_atomic_replacement_while_playing_leaves_the_open_deck_unchanged(self):
        self.start_pipeline()
        self.open_deck(self.build_deck())
        labels, seconds = self.heard(mutate_after=1.0, mutate=lambda: self.atomic_replace(self.gen_b))
        self.assertEqual(set(labels), {"A"}, labels)
        self.assertAlmostEqual(seconds, 3.0, delta=0.1)

    def test_a_replacement_before_the_open_is_what_the_deck_plays(self):
        self.start_pipeline()
        deck = self.build_deck()                          # claimed, built, not yet opened
        self.atomic_replace(self.gen_b)
        self.open_deck(deck)
        labels, seconds = self.heard()
        self.assertEqual(set(labels), {"B"}, labels)
        self.assertAlmostEqual(seconds, 1.5, delta=0.1)

    def test_negative_control_an_in_place_rewrite_mixes_generations(self):
        self.start_pipeline()
        self.open_deck(self.build_deck())
        labels, seconds = self.heard(mutate_after=0.5, mutate=lambda: self.rewrite_in_place(self.gen_b))
        self.assertIn("A", labels)
        self.assertNotEqual(set(labels), {"A"}, "an in-place rewrite should have reached the open deck")
        self.assertLess(seconds, 2.9)

    def test_the_deck_open_lock_excludes_publication_until_the_file_is_open(self):
        self.start_pipeline()
        deck = self.build_deck()
        publisher = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        self.addCleanup(os.close, publisher)
        with wx_alert_state.shared_publication_lock(self.lock_path) as held:
            self.assertTrue(held)
            with self.assertRaises(BlockingIOError):      # publish_weather_asset would wait here
                fcntl.flock(publisher, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.open_deck(deck)
            self.assertEqual(len(self.fds_on_dest()), 1)
        fcntl.flock(publisher, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the publication proceeds after the open
        self.atomic_replace(self.gen_b)
        fcntl.flock(publisher, fcntl.LOCK_UN)
        labels, seconds = self.heard()
        self.assertEqual(set(labels), {"A"}, labels)
        self.assertAlmostEqual(seconds, 3.0, delta=0.1)


class EngineDeckConstructionUnchangedTests(SimpleTestCase):
    """The GStreamer evidence above mirrors engine._create_deck; fail loudly if that construction changes."""

    def test_create_deck_opens_with_filesrc_and_sync_state_with_parent_inside_deck_open_authority(self):
        import inspect

        import library.services.engine as eng_module
        source = inspect.getsource(eng_module.PlaybackEngine._create_deck)
        for needle in ('Gst.ElementFactory.make("filesrc", None)', 'src.set_property("location", filepath)',
                       'Gst.ElementFactory.make("decodebin", None)', "deck_bin.sync_state_with_parent()"):
            self.assertIn(needle, source)
        self.assertLess(source.index("self._claim_playback_occurrence(log_item)"),
                        source.index("deck_bin.sync_state_with_parent()"))
        wrapper = inspect.getsource(eng_module._require_deck_open_authority)
        self.assertIn("with _deck_open_authority(log_item)", wrapper)
        self.assertIn("return method(", wrapper)
