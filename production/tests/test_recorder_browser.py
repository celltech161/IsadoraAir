"""2.22B -- the browser recorder/editor, exercised in headless Chromium.

Hardware-free by construction: Chromium gets a FAKE capture device fed from a
generated tone file (--use-fake-device-for-media-stream, --use-file-for-fake-
audio-capture), a fake audio output (--disable-audio-output, --mute-audio), and
an environment in which ALSA has an empty configuration and PulseAudio points
nowhere -- so no station audio device can ever be opened. The page is the real
workstation served by a live test server, mounted for the scratch (non-
VoiceTrack) consumer, so this also proves the browser side is domain-neutral.
"""
import functools
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connections
from django.test import Client, override_settings

from production.models import ProductionMedia
from production.recorder import registry
from production.services import validation

from .support import IsolatedMediaRootMixin, ffmpeg_available
from .test_recorder_core import URLS, ScratchPadAdapter

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

User = get_user_model()
TONE_DBFS = -12.0

# Counts live requestAnimationFrame callbacks (one preview loop, never two).
FRAME_COUNTER = """(() => {
  const raf = window.requestAnimationFrame.bind(window);
  const caf = window.cancelAnimationFrame.bind(window);
  window.__iportalActiveFrames = new Set();
  window.requestAnimationFrame = (callback) => {
    let id = 0;
    id = raf((time) => { window.__iportalActiveFrames.delete(id); callback(time); });
    window.__iportalActiveFrames.add(id);
    return id;
  };
  window.cancelAnimationFrame = (id) => { window.__iportalActiveFrames.delete(id); caf(id); };
})()"""

# Everything a navigation gesture must leave alone (r0114).
EDITOR_SNAPSHOT = """() => {
  const S = IPortalWorkstation, ch = S.pcm.channels[0];
  let sum = 0;
  for (let i = 0; i < ch.length; i += 97) sum += ch[i];
  return {pcmSum: sum, length: ch.length, dirty: S.dirty, ops: S.ops.slice(), undo: S.undo.items.length,
          baseRevision: S.baseRevision, selection: S.selection, playhead: S.playhead, view: S.view};
}"""


def exercise_shared_editor(test, page):
    """r0114: the shared workstation's meter scale, preview transport and wheel
    navigation behave identically in every recorder domain (scratch, produced
    content REGULAR and TEST, VoiceTrack). Uses browser-local PCM only and
    never saves: the domain's save/authorization path is not touched."""
    page.evaluate("""() => {
      const A = window.IPortalAudio, S = window.IPortalWorkstation;
      const rate = 48000, samples = new Float32Array(rate * 2);
      for (let i = 0; i < samples.length; i++) samples[i] = 0.2 * Math.sin(2 * Math.PI * 440 * i / rate);
      S.pcm = A.make(rate, [samples]); S.sourceKind = 'recording'; S.selection = null; S.playhead = 0;
      S.view = {start: 0, end: 2}; const fit = document.getElementById('ipZoomFit'); fit.disabled = false; fit.click();
      document.getElementById('ipPunch').dispatchEvent(new Event('change'));   // the page's own refreshControls
    }""")
    meter = page.evaluate("() => { const b = document.getElementById('ipMeter'); "
                          "return [b.style.getPropertyValue('--ip-vu-yellow'), b.style.getPropertyValue('--ip-vu-red')]; }")
    test.assertEqual(meter, ["50%", "85%"])
    before = page.evaluate(EDITOR_SNAPSHOT)
    page.click("#ipPlay")
    page.wait_for_timeout(200)
    test.assertEqual(page.evaluate("IPortalWorkstation.transport"), "playing")
    page.click("#ipPlay")
    paused = page.evaluate("IPortalWorkstation.playhead")
    test.assertEqual((page.evaluate("IPortalWorkstation.transport"), page.text_content("#ipPlay")),
                     ("paused", "\u25B6 Resume"))
    test.assertGreater(paused, 0.1)
    page.click("#ipPlayStop")
    test.assertEqual((page.evaluate("IPortalWorkstation.transport"), page.evaluate("IPortalWorkstation.playhead")),
                     ("stopped", 0))
    box = page.locator("#ipWave").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
    page.mouse.wheel(0, -300)
    page.wait_for_timeout(50)
    view = page.evaluate("IPortalWorkstation.view")
    test.assertLess(view["end"] - view["start"], 2)
    test.assertAlmostEqual(view["start"] + 0.5 * (view["end"] - view["start"]), 1.0, delta=0.01)
    after = page.evaluate(EDITOR_SNAPSHOT)
    for key in ("pcmSum", "length", "dirty", "ops", "undo", "baseRevision", "selection"):
        test.assertEqual(after[key], before[key], key)
    test.assertEqual(page.errors, [])
    test.assertFalse([url for url in page.requests if "/api/engine" in url or "/api/fx" in url])


# A deterministic stand-in for the recorder's AudioContext: the test sets the
# clock and ends sources itself (r0114 transport tests). Nothing is audible.
FAKE_AUDIO = """() => {
  const ac = {state: 'running', currentTime: 0, destination: {},
    createBuffer(channels, length, rate) {
      const data = Array.from({length: channels}, () => new Float32Array(length));
      return {length, sampleRate: rate, numberOfChannels: channels, getChannelData: (i) => data[i]};
    },
    createBufferSource() {
      if (window.__fake.failNext) { window.__fake.failNext = false; throw new Error('no source'); }
      const source = {started: null, stopped: false, onended: null, connect() {},
        start(when, offset, duration) { this.started = {offset, duration}; },
        stop() { this.stopped = true; },
        end() { if (this.onended) this.onended(); }};
      window.__fake.sources.push(source);
      return source;
    },
    resume() { if (window.__fake.rejectResume) return Promise.reject(new Error('blocked'));
               this.state = 'running'; return Promise.resolve(); },
  };
  window.__fake = {ac, sources: [], failNext: false, rejectResume: false};
  window.IPortalWorkstation.recorder = {state: 'idle', release() {}, _context() { return ac; },
                                        elapsedSeconds() { return 0; }};
}"""


def in_browser(test):
    """Run one test inside its own Playwright session (the established pattern,
    see library.tests.test_schedule_ui_async_3_1d): the browser closes and every
    DB connection opened inside the session is closed BEFORE the test ends, so
    the destructive-test database guard can still verify the test database."""
    @functools.wraps(test)
    def wrapper(self):
        with sync_playwright() as pw:
            self.browser = pw.chromium.launch(env=self.browser_env, args=self.browser_args)
            try:
                return test(self)
            finally:
                try:
                    self.browser.close()
                except Exception:  # noqa: BLE001
                    pass
                connections.close_all()
    return wrapper


def _tone(path: Path, seconds: float, amplitude: float = 0.25):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    f"aevalsrc={amplitude}*sin(2*PI*440*t):s=48000:d={seconds}", "-ac", "1", "-c:a", "pcm_s16le",
                    str(path)], check=True)


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(ROOT_URLCONF=URLS, SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False,
                   CSRF_COOKIE_SECURE=False)
class RecorderBrowserTests(IsolatedMediaRootMixin, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        registry.register(ScratchPadAdapter())
        cls.assets = tempfile.TemporaryDirectory()
        assets = Path(cls.assets.name)
        cls.mic_tone = assets / "mic.wav"
        _tone(cls.mic_tone, 30)
        cls.long_file = assets / "long.wav"
        _tone(cls.long_file, 40)
        (assets / "empty-alsa.conf").write_text("")
        cls.browser_env = {**os.environ, "ALSA_CONFIG_PATH": str(assets / "empty-alsa.conf"),
                           "PULSE_SERVER": "unix:/nonexistent/pulse", "PULSE_RUNTIME_PATH": str(assets)}
        cls.browser_args = [
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={cls.mic_tone}",
            "--disable-audio-output", "--mute-audio", "--autoplay-policy=no-user-gesture-required",
        ]

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()
        registry._ADAPTERS.pop("scratch-pad", None)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        # Playwright's sync API runs an event loop; the test thread's own ORM
        # assertions beside it are safe (established pattern, scoped per test).
        patcher = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        ScratchPadAdapter.committed = {}
        self.user = User.objects.create_user("producer", password="x", is_staff=True)
        client = Client()
        client.force_login(self.user)
        self.session_cookie = client.cookies["sessionid"].value

    # -- harness -----------------------------------------------------------------
    def open(self, init_script=None, touch=False):
        context = self.browser.new_context(accept_downloads=True, has_touch=touch)
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        if init_script:
            context.add_init_script(init_script)
        page = context.new_page()
        page.errors, page.requests = [], []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.on("request", lambda req: page.requests.append(req.url))
        page.goto(f"{self.live_server_url}/scratch/?slot=a")
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        return page

    def arm(self, page):
        page.click("#ipArm")
        page.wait_for_function("window.IPortalWorkstation.recorder && window.IPortalWorkstation.recorder.state === 'armed'")

    def record(self, page, seconds):
        page.click("#ipRecord")
        page.wait_for_timeout(int(seconds * 1000))
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.recorder.state !== 'stopping' && window.IPortalWorkstation.pcm")

    def duration(self, page):
        return page.evaluate("IPortalAudio.duration(window.IPortalWorkstation.pcm)")

    def load_browser_pcm(self, page, seconds=1.2):
        """Install deterministic browser-local PCM without opening capture.

        This exercises the real editor canvas and Web Audio preview path while
        keeping the focused cursor tests quick and independent of microphone
        scheduling.
        """
        page.evaluate("""seconds => {
          const A = window.IPortalAudio, S = window.IPortalWorkstation;
          const rate = 48000, samples = new Float32Array(Math.round(rate * seconds));
          for (let i = 0; i < samples.length; i++) samples[i] = 0.2 * Math.sin(2 * Math.PI * 440 * i / rate);
          S.pcm = A.make(rate, [samples]);
          S.sourceKind = 'recording'; S.selection = null; S.playhead = 0;
          S.view = {start: 0, end: A.duration(S.pcm)};
          const fit = document.getElementById('ipZoomFit'); fit.disabled = false; fit.click();
          document.getElementById('ipPunch').dispatchEvent(new Event('change'));   // the page's own refreshControls
        }""", seconds)

    def assert_clean(self, page):
        self.assertEqual(page.errors, [])
        self.assertFalse([url for url in page.requests if "/api/engine" in url or "/api/fx" in url],
                         "the recorder must never touch the station output")

    # -- 1. pure audio operations --------------------------------------------------
    @in_browser
    def test_audio_core_operations_are_exact(self):
        page = self.open()
        r = page.evaluate("""() => {
          const A = window.IPortalAudio, out = {};
          out.clamp = [A.clampGainDb(-20), A.clampGainDb(16), A.clampGainDb(3.26), A.clampGainDb('x')];
          out.setLevel = [A.setLevelGain(0, 0.1), A.setLevelGain(0, 0.005), A.setLevelGain(10, 0.01), A.setLevelGain(0, 0)];
          const sr = 1000, ramp = new Float32Array(3000);
          for (let i = 0; i < ramp.length; i++) ramp[i] = ((i % 100) / 100) * 0.5;
          const pcm = A.make(sr, [ramp, ramp.map(v => -v)]);
          out.trim = [A.length(A.trim(pcm, 0.5, 1.5, 'keep')), A.length(A.trim(pcm, 0.5, 1.5, 'delete')),
                      A.length(A.trim(pcm, 1.5, 0.5, 'keep'))];
          out.normPeak = A.peak(A.normalize(pcm).pcm);
          out.silentNorm = A.normalize(A.make(sr, [new Float32Array(10)])).applied;
          out.gain = [A.peak(A.applyGain(A.make(sr, [Float32Array.of(0.1)]), 6)),
                      A.peak(A.applyGain(A.make(sr, [Float32Array.of(0.01)]), 40))];
          const ones = A.make(sr, [new Float32Array(3000).fill(1)]), half = A.make(sr, [new Float32Array(500).fill(0.5)]);
          const ins = A.punchIn(ones, half, 1.0, 'insert'), rep = A.punchIn(ones, half, 1.0, 'replace');
          const over = A.punchIn(ones, half, 2.8, 'insert');
          out.punch = [A.length(ins), ins.channels[0][999], ins.channels[0][1000], ins.channels[0][1499],
                       ins.channels[0][1500], A.length(rep), rep.channels[0][1499], A.length(over)];
          out.concat = A.length(A.concat(half, half));
          const sig = new Float32Array(100).fill(0.3);
          out.collapse = [A.collapseSilentChannel(A.make(sr, [sig, new Float32Array(100)])).channels.length,
                          A.collapseSilentChannel(A.make(sr, [new Float32Array(100), sig])).channels.length,
                          A.collapseSilentChannel(A.make(sr, [sig, sig])).channels.length];
          const wav = A.encodeWav(A.make(8000, [Float32Array.of(0, 1, -1, 0.5)]));
          const dv = new DataView(wav.buffer);
          out.wav = [String.fromCharCode(...wav.slice(0, 4)), String.fromCharCode(...wav.slice(8, 12)), wav.length,
                     dv.getUint16(20, true), dv.getUint16(22, true), dv.getUint32(24, true), dv.getUint16(34, true),
                     dv.getUint32(40, true), dv.getInt16(44, true), dv.getInt16(46, true), dv.getInt16(48, true),
                     dv.getInt16(50, true)];
          const stack = new A.UndoStack();
          for (let i = 0; i < 7; i++) stack.push(half, 'op' + i);
          const tiny = new A.UndoStack(10, 5000);
          for (let i = 0; i < 3; i++) tiny.push(half, 'x');
          out.undo = [stack.items.length, stack.pop().label, tiny.items.length];
          out.peaks = A.peaksForRange(pcm, 0, 3, 50).length;
          return out;
        }""")
        self.assertEqual(r["clamp"], [-15, 15, 3.5, 0])
        self.assertEqual([x["ok"] for x in r["setLevel"]], [True, False, True, False])
        self.assertEqual(r["setLevel"][0]["gainDb"], 14)          # -20 dBFS -> +14 dB lands at -6
        self.assertEqual(r["setLevel"][2]["gainDb"], 15)          # clamped to +15
        self.assertEqual(r["trim"], [1000, 2000, 1000])
        self.assertAlmostEqual(r["normPeak"], 0.95, places=5)
        self.assertFalse(r["silentNorm"])
        self.assertAlmostEqual(r["gain"][0], 0.1 * 10 ** (6 / 20), places=5)
        self.assertAlmostEqual(r["gain"][1], 0.01 * 10 ** (15 / 20), places=5)   # +40 requested -> +15
        self.assertEqual(r["punch"], [3000, 1, 0.5, 0.5, 1, 1500, 0.5, 3300])
        self.assertEqual(r["concat"], 1000)
        self.assertEqual(r["collapse"], [1, 1, 2])
        self.assertEqual(r["wav"], ["RIFF", "WAVE", 52, 1, 1, 8000, 16, 8, 0, 32767, -32768, 16384])
        self.assertEqual(r["undo"], [5, "op6", 2])
        self.assertEqual(r["peaks"], 50)
        self.assert_clean(page)

    # -- 2. capture, Set Level, gain, pause/resume, save --------------------------------
    @in_browser
    def test_record_pause_resume_set_level_and_save(self):
        page = self.open()
        self.arm(page)
        page.wait_for_function("parseFloat(document.getElementById('ipMeter').dataset.rmsPct) > 0")
        devices = page.eval_on_selector_all("#ipDevice option", "els => els.map(e => e.textContent)")
        self.assertTrue(any("Fake" in label for label in devices), devices)
        result = page.evaluate("window.IPortalWorkstation.recorder.setLevel(1.5)")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["observedDbfs"], TONE_DBFS, delta=0.6)
        self.assertAlmostEqual(result["gainDb"], -6 - TONE_DBFS, delta=0.5)        # +6 dB
        page.eval_on_selector("#ipGain", "el => { el.value = '30'; el.dispatchEvent(new Event('input')); }")
        self.assertEqual(page.text_content("#ipGainLabel"), "15.0 dB")
        page.eval_on_selector("#ipGain", "el => { el.value = '0'; el.dispatchEvent(new Event('input')); }")
        page.click("#ipRecord")
        page.wait_for_timeout(1200)
        page.click("#ipPause")
        page.wait_for_timeout(900)
        self.assertEqual(page.text_content("#ipPause"), "Resume")
        page.click("#ipPause")
        page.wait_for_timeout(1000)
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.pcm")
        self.assertTrue(1.8 <= self.duration(page) <= 2.9, self.duration(page))      # the pause is not captured
        peak = page.evaluate("IPortalAudio.peak(window.IPortalWorkstation.pcm)")
        self.assertAlmostEqual(peak, 0.25, delta=0.05)                                 # 0 dB gain, lossless capture
        page.click("#ipSave")
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Saved", timeout=60000)
        media = ProductionMedia.objects.get()
        self.assertEqual((media.kind, media.validation_state, media.container), ("recording", "valid", "wav"))
        self.assertEqual(ScratchPadAdapter.committed["a"], media.pk)
        self.assert_clean(page)

    # -- 3. editor ----------------------------------------------------------------------
    @in_browser
    def test_editor_trim_normalize_gain_undo_punch_in_and_export(self):
        page = self.open()
        self.arm(page)
        self.record(page, 3.0)
        total = self.duration(page)
        page.evaluate("() => { const S = window.IPortalWorkstation; S.selection = {start: 0.5, end: 1.5}; }")
        page.evaluate("() => document.getElementById('ipKeep').disabled = false")
        page.click("#ipKeep")
        self.assertAlmostEqual(self.duration(page), 1.0, delta=0.01)
        page.click("#ipUndo")
        self.assertAlmostEqual(self.duration(page), total, delta=0.01)
        page.click("#ipNormalize")
        self.assertAlmostEqual(page.evaluate("IPortalAudio.peak(window.IPortalWorkstation.pcm)"), 0.95, delta=0.001)
        page.fill("#ipEditGain", "-6")
        page.click("#ipApplyGain")
        self.assertAlmostEqual(page.evaluate("IPortalAudio.peak(window.IPortalWorkstation.pcm)"),
                               0.95 * 10 ** (-6 / 20), delta=0.002)
        self.assertEqual(page.evaluate("window.IPortalWorkstation.ops"), ["normalize", "gain:-6.0"])
        # Punch-in (replace) at 1.0 s with a 1 s overdub -> ~2.0 s.
        page.evaluate("() => { window.IPortalWorkstation.playhead = 1.0; }")
        page.check("#ipPunch")
        page.check("#ipPunchReplace")
        self.record(page, 1.0)
        self.assertAlmostEqual(self.duration(page), 2.0, delta=0.35)
        self.assertEqual(page.evaluate("window.IPortalWorkstation.ops")[-1], "punch-replace")
        with page.expect_download() as download:
            page.click("#ipExport")
        self.assertEqual(Path(download.value.path()).read_bytes()[:4], b"RIFF")
        self.assertFalse(ProductionMedia.objects.exists())          # nothing is saved by editing or export
        self.assert_clean(page)

    @in_browser
    def test_waveform_cursor_style_click_and_drag_selection_remain_editor_state(self):
        page = self.open()
        self.load_browser_pcm(page, 2.0)
        page.evaluate("""() => {
          const S = window.IPortalWorkstation;
          S.playhead = 1.0;
          document.getElementById('ipZoomFit').click();
        }""")
        pixels = page.evaluate("""() => {
          const canvas = document.getElementById('ipWave'), S = window.IPortalWorkstation;
          const x = Math.round((S.playhead - S.view.start) / (S.view.end - S.view.start) * canvas.width);
          const ctx = canvas.getContext('2d'), h = canvas.height;
          const white = (y) => {
            const data = ctx.getImageData(x - 5, y, 11, 1).data;
            let count = 0;
            for (let i = 0; i < data.length; i += 4) {
              if (data[i] > 240 && data[i + 1] > 240 && data[i + 2] > 240 && data[i + 3] > 0) count++;
            }
            return count;
          };
          const middle = Array.from(ctx.getImageData(x, Math.floor(h / 2), 1, 1).data);
          return {top: white(0), bottom: white(h - 1), middle};
        }""")
        self.assertGreaterEqual(pixels["top"], 6)       # triangle is visibly wider than the 2 px line
        self.assertGreaterEqual(pixels["bottom"], 6)
        self.assertTrue(all(channel > 240 for channel in pixels["middle"][:3]), pixels)

        box = page.locator("#ipWave").bounding_box()
        y = box["y"] + box["height"] / 2
        page.mouse.click(box["x"] + box["width"] * 0.25, y)
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), 0.5, delta=0.03)
        self.assertIsNone(page.evaluate("IPortalWorkstation.selection"))

        page.mouse.move(box["x"] + box["width"] * 0.25, y)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] * 0.60, y, steps=5)
        page.mouse.up()
        selection = page.evaluate("IPortalWorkstation.selection")
        self.assertAlmostEqual(selection["start"], 0.5, delta=0.03)
        self.assertAlmostEqual(selection["end"], 1.2, delta=0.03)
        self.assert_clean(page)

    @in_browser
    def test_preview_transport_play_pause_resume_stop_with_real_audio(self):
        """r0114: a conventional transport on the real Web Audio path -- one
        Play/Pause/Resume button, Stop (and a natural end) back to exactly 0,
        one moving playhead, one animation frame, one audible source."""
        page = self.open(init_script=FRAME_COUNTER)
        self.load_browser_pcm(page, 1.2)
        state = "() => ({t: IPortalWorkstation.transport, p: IPortalWorkstation.playhead, " \
                "label: document.getElementById('ipPlay').textContent, " \
                "stop: document.getElementById('ipPlayStop').disabled, frames: window.__iportalActiveFrames.size})"
        first = page.evaluate(state)
        self.assertEqual((first["t"], first["label"], first["stop"]), ("stopped", "\u25B6 Preview", True))
        page.evaluate("() => { IPortalWorkstation.playhead = 0.1; }")
        page.click("#ipPlay")
        page.wait_for_timeout(250)
        playing = page.evaluate(state)
        self.assertEqual((playing["t"], playing["label"], playing["stop"], playing["frames"]),
                         ("playing", "\u23F8 Pause", False, 1))
        self.assertGreater(playing["p"], 0.25)
        page.click("#ipPlay")                                                   # pause
        paused = page.evaluate(state)
        page.wait_for_timeout(200)
        self.assertEqual((paused["t"], paused["label"], paused["stop"], paused["frames"]),
                         ("paused", "\u25B6 Resume", False, 0))
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), paused["p"], delta=0.005)
        page.click("#ipPlay")                                                   # resume, not restart
        page.wait_for_timeout(120)
        resumed = page.evaluate(state)
        self.assertEqual(resumed["t"], "playing")
        self.assertGreater(resumed["p"], paused["p"])
        self.assertLess(resumed["p"], paused["p"] + 0.4)
        page.click("#ipPlayStop")                                               # Stop while playing
        stopped = page.evaluate(state)
        self.assertEqual((stopped["t"], stopped["p"], stopped["label"], stopped["stop"]),
                         ("stopped", 0, "\u25B6 Preview", True))
        page.wait_for_timeout(150)
        self.assertEqual(page.evaluate("IPortalWorkstation.playhead"), 0)
        self.assertEqual(page.evaluate("window.__iportalActiveFrames.size"), 0)

        page.click("#ipPlay")
        page.wait_for_timeout(150)
        page.click("#ipPlay")                                                   # pause, then Stop while paused
        self.assertEqual(page.evaluate("IPortalWorkstation.transport"), "paused")
        page.click("#ipPlayStop")
        self.assertEqual((page.evaluate("IPortalWorkstation.transport"), page.evaluate("IPortalWorkstation.playhead")),
                         ("stopped", 0))

        # A full preview's natural end returns to exactly 0 by itself.
        page.evaluate("() => { const S = IPortalWorkstation; S.selection = null; "
                      "S.playhead = IPortalAudio.duration(S.pcm) - 0.16; }")
        page.click("#ipPlay")
        page.wait_for_function("IPortalWorkstation.transport === 'stopped'")
        ended = page.evaluate(state)
        self.assertEqual((ended["p"], ended["label"], ended["stop"], ended["frames"]), (0, "\u25B6 Preview", True, 0))

        # A selection preview never passes the selection end; its end resets
        # the playhead and keeps the selection.
        page.evaluate("() => { const S = IPortalWorkstation; S.selection = {start: 0.2, end: 0.55}; S.playhead = 0; }")
        page.click("#ipPlay")
        page.wait_for_timeout(120)
        self.assertTrue(0.25 < page.evaluate("IPortalWorkstation.playhead") <= 0.55)
        page.wait_for_function("IPortalWorkstation.transport === 'stopped'")
        self.assertEqual(page.evaluate("IPortalWorkstation.playhead"), 0)
        self.assertEqual(page.evaluate("IPortalWorkstation.selection"), {"start": 0.2, "end": 0.55})

        # Punch-in consumes the paused playhead: an internal halt keeps it.
        page.evaluate("() => { IPortalWorkstation.selection = null; IPortalWorkstation.playhead = 0.3; }")
        page.click("#ipPlay")
        page.wait_for_timeout(150)
        page.click("#ipPlay")
        paused_at = page.evaluate("IPortalWorkstation.playhead")
        punch_at = page.evaluate("""() => {
          const S = IPortalWorkstation;
          S.recorder = {state: 'armed', start() { this.state = 'recording'; }, elapsedSeconds() { return 0; },
                        release() {}, _context: S.recorder._context.bind(S.recorder)};
          S.canRecord = true;
          document.getElementById('ipPunch').checked = true;
          document.getElementById('ipRecord').disabled = false;
          document.getElementById('ipRecord').click();
          return {at: S.punch.at, transport: S.transport, playhead: S.playhead};
        }""")
        self.assertAlmostEqual(punch_at["at"], paused_at, delta=0.001)
        self.assertEqual(punch_at["transport"], "stopped")
        self.assertAlmostEqual(punch_at["playhead"], paused_at, delta=0.001)
        self.assert_clean(page)

    @in_browser
    def test_preview_transport_state_machine_with_a_fake_audio_clock(self):
        """Deterministic: a fake AudioContext whose clock the test sets and
        whose sources the test ends. Elapsed time survives repeated pauses, a
        superseded source's onended can never touch newer playback, rapid
        presses never leave two sources audible, and every end path is exact."""
        page = self.open()
        self.load_browser_pcm(page, 2.0)
        page.evaluate(FAKE_AUDIO)
        click = "id => document.getElementById(id).click()"
        snap = "() => ({t: IPortalWorkstation.transport, p: IPortalWorkstation.playhead, " \
               "label: document.getElementById('ipPlay').textContent, stop: document.getElementById('ipPlayStop').disabled, " \
               "live: __fake.sources.filter(s => s.started && !s.stopped).length, n: __fake.sources.length})"
        def at(seconds):
            page.evaluate(f"__fake.ac.currentTime = {seconds}")
        page.evaluate(click, "ipPlay")
        self.assertEqual(page.evaluate("__fake.sources[0].started"), {"offset": 0, "duration": 2})
        at(0.5)
        stale = page.evaluate("() => { window.__h0 = __fake.sources[0].onended; return typeof window.__h0; }")
        self.assertEqual(stale, "function")
        page.evaluate(click, "ipPlay")                                          # pause at 0.5
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["p"], st["label"], st["stop"], st["live"]), ("paused", 0.5, "\u25B6 Resume", False, 0))
        self.assertIsNone(page.evaluate("__fake.sources[0].onended"))         # detached
        page.evaluate("() => window.__h0()")                                    # a stale onended anyway
        self.assertEqual(page.evaluate(snap)["t"], "paused")
        at(1.0)                                                                 # time passes while paused
        page.evaluate(click, "ipPlay")                                          # resume from 0.5
        self.assertEqual(page.evaluate("__fake.sources[1].started"), {"offset": 0.5, "duration": 1.5})
        at(1.3)
        page.evaluate(click, "ipPlay")                                          # pause at 0.8
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), 0.8, places=6)
        at(2.0)
        page.evaluate("() => { window.__h1 = __fake.sources[1].onended; }")
        page.evaluate(click, "ipPlay")
        self.assertEqual(page.evaluate("__fake.sources[2].started")["offset"], 0.8)
        at(2.4)
        page.evaluate("() => { __fake.sources[0].end(); __fake.sources[1].end(); }")   # stale ends
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["live"]), ("playing", 1))               # ignored: still playing
        self.assertTrue(0.8 <= st["p"] <= 1.2 + 1e-9, st["p"])                # 0.8 + elapsed; never reset
        page.evaluate("__fake.sources[2].end()")                                # the natural end
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["p"], st["label"], st["stop"], st["live"]), ("stopped", 0, "\u25B6 Preview", True, 0))

        # Stop while playing, and Stop while paused: exactly 0.
        page.evaluate(click, "ipPlay"); at(2.7)
        page.evaluate(click, "ipPlayStop")
        self.assertEqual(page.evaluate(snap)["p"], 0)
        page.evaluate(click, "ipPlay"); at(3.0); page.evaluate(click, "ipPlay")
        self.assertEqual(page.evaluate(snap)["t"], "paused")
        page.evaluate(click, "ipPlayStop")
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["p"], st["live"]), ("stopped", 0, 0))

        # A selection: plays exactly the range; pause/resume keeps to it; the end resets to 0.
        page.evaluate("() => { IPortalWorkstation.selection = {start: 0.5, end: 1.2}; }")
        at(10)
        page.evaluate(click, "ipPlay")
        n = page.evaluate("__fake.sources.length") - 1
        self.assertEqual(page.evaluate(f"__fake.sources[{n}].started"), {"offset": 0.5, "duration": 0.7})
        at(10.3); page.evaluate(click, "ipPlay"); at(11); page.evaluate(click, "ipPlay")
        started = page.evaluate(f"__fake.sources[{n + 1}].started")
        self.assertAlmostEqual(started["offset"], 0.8, places=6)
        self.assertAlmostEqual(started["duration"], 0.4, places=6)
        page.evaluate(f"__fake.sources[{n + 1}].end()")
        self.assertEqual((page.evaluate(snap)["p"], page.evaluate("IPortalWorkstation.selection")),
                         (0, {"start": 0.5, "end": 1.2}))
        page.evaluate("() => { IPortalWorkstation.selection = null; }")

        # Rapid presses: never two audible sources.
        page.evaluate("""() => { const b = document.getElementById('ipPlay');
          for (let i = 0; i < 7; i++) { __fake.ac.currentTime += 0.01; b.click(); } }""")
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["live"]), ("playing", 1))
        page.evaluate(click, "ipPlayStop")

        # A click on the waveform seeks while playing: a new source from there.
        page.evaluate(click, "ipPlay")
        box = page.locator("#ipWave").bounding_box()
        page.mouse.click(box["x"] + box["width"] * 0.75, box["y"] + box["height"] / 2)
        st = page.evaluate(snap)
        self.assertEqual((st["t"], st["live"]), ("playing", 1))
        self.assertAlmostEqual(page.evaluate("__fake.sources[__fake.sources.length - 1].started.offset"), 1.5, delta=0.03)
        page.evaluate(click, "ipPlayStop")

        # A failing start and a refused resume both leave a consistent stop.
        page.evaluate("() => { __fake.failNext = true; }")
        page.evaluate(click, "ipPlay")
        self.assertEqual(page.evaluate(snap)["t"], "stopped")
        self.assertIn("could not start", page.text_content("#ipMessages"))
        page.evaluate("() => { __fake.ac.state = 'suspended'; __fake.rejectResume = true; }")
        page.evaluate(click, "ipPlay")
        page.wait_for_function("IPortalWorkstation.transport === 'stopped'")
        self.assertIn("did not allow audio", page.text_content("#ipMessages"))
        self.assertEqual(page.evaluate(snap)["live"], 0)

        # An empty editor: nothing to play.
        page.evaluate("() => { IPortalWorkstation.pcm = null; document.getElementById('ipPlay').disabled = false; }")
        before = page.evaluate("__fake.sources.length")
        page.evaluate(click, "ipPlay")
        self.assertEqual((page.evaluate("__fake.sources.length"), page.evaluate("IPortalWorkstation.transport")),
                         (before, "stopped"))
        self.assertEqual(page.errors, [])

    # -- r0114: the calibrated input meter ------------------------------------------
    @in_browser
    def test_meter_math_is_the_consoles_dbfs_scale(self):
        page = self.open()
        r = page.evaluate("""() => {
          const A = IPortalAudio, pct = (db, min) => Math.round(A.vuPercent(db, min) * 100) / 100;
          const sine = new Float32Array(4800);
          for (let i = 0; i < sine.length; i++) sine[i] = 0.5 * Math.sin(2 * Math.PI * 1000 * i / 48000);
          const hold = new A.PeakHold();
          return {
            at40: [pct(-40, -40), pct(-20, -40), pct(-6, -40), pct(0, -40)],
            at60: [pct(-60, -60), pct(-20, -60), pct(-6, -60), pct(0, -60)],
            thresholds40: A.vuThresholds(-40), thresholds60: A.vuThresholds(-60),
            clamped: [pct(-90, -40), pct(6, -40), pct(-Infinity, -40), pct(NaN, -40), pct(null, -40), pct('x', -40)],
            minDb: [A.vuMinDb(-60), A.vuMinDb(null), A.vuMinDb('nope'), A.vuMinDb(-5e3), A.vuMinDb(0), A.vuMinDb(3)],
            dbfs: [A.levelDbfs(1), A.levelDbfs(0.1), A.levelDbfs(0), A.levelDbfs(-0.5), A.levelDbfs(NaN)],
            silence: A.measureLevels(new Float32Array(1024)),
            empty: A.measureLevels(null),
            garbage: A.measureLevels([NaN, Infinity, -Infinity, 0.5, -0.5]),
            sine: A.measureLevels(sine),
            hold: [hold.update(-10, 0), hold.update(-30, 100), hold.update(-30, 300), hold.update(-30, 800),
                   hold.update(NaN, 800), hold.update(-3, 900), hold.value(1200), hold.value(1700)],
          };
        }""")
        self.assertEqual(r["at40"], [0, 50, 85, 100])
        self.assertEqual(r["at60"], [0, 66.67, 90, 100])
        self.assertEqual((r["thresholds40"]["yellow"], r["thresholds40"]["red"]), (50, 85))
        self.assertAlmostEqual(r["thresholds60"]["yellow"], 66.6667, places=3)
        self.assertEqual(r["clamped"], [0, 100, 0, 0, 0, 0])
        self.assertEqual(r["minDb"], [-60, -40, -40, -40, -40, -40])
        self.assertEqual(r["dbfs"][0], 0)
        self.assertAlmostEqual(r["dbfs"][1], -20, places=9)
        self.assertEqual(r["dbfs"][2:], [float("-inf")] * 3)
        self.assertEqual(r["silence"], {"peak": 0, "rms": 0})
        self.assertEqual(r["empty"], {"peak": 0, "rms": 0})
        self.assertEqual(r["garbage"]["peak"], 0.5)
        self.assertAlmostEqual(r["garbage"]["rms"], (0.5 / (5 ** 0.5)) * (2 ** 0.5), places=9)
        self.assertAlmostEqual(r["sine"]["peak"], 0.5, places=3)
        self.assertAlmostEqual(r["sine"]["rms"], 0.5 / (2 ** 0.5), places=3)    # RMS is not peak
        # held for 300 ms, then falling at 20 dB/s; a higher peak re-arms; NaN is ignored
        self.assertEqual(r["hold"][:3], [-10, -10, -10])
        self.assertAlmostEqual(r["hold"][3], -10 - 20 * 0.5, places=6)
        self.assertAlmostEqual(r["hold"][4], -20, places=6)
        self.assertEqual(r["hold"][5], -3)
        self.assertEqual(r["hold"][6], -3)
        self.assertAlmostEqual(r["hold"][7], -3 - 20 * 0.5, places=6)
        dom = page.evaluate("""() => {
          const bar = document.getElementById('ipMeter');
          return {yellow: bar.style.getPropertyValue('--ip-vu-yellow'), red: bar.style.getPropertyValue('--ip-vu-red'),
                  ticks: Array.from(document.querySelectorAll('#ipMeterScale .ip-tick')).map(t => [t.textContent, t.style.left]),
                  min: bar.getAttribute('aria-valuemin'), label: bar.getAttribute('aria-label'),
                  mask: document.getElementById('ipMeterMask').style.width,
                  peakHidden: document.getElementById('ipMeterPeak').hidden};
        }""")
        self.assertEqual((dom["yellow"], dom["red"], dom["min"]), ("50%", "85%", "-40"))
        self.assertEqual(dom["ticks"], [["-40", "0%"], ["-20", "50%"], ["-6", "85%"], ["0", "100%"]])
        self.assertIn("RMS", dom["label"])
        self.assertEqual((dom["mask"], dom["peakHidden"]), ("100%", True))   # no input yet: nothing lit
        self.assert_clean(page)

    @in_browser
    def test_the_live_meter_reads_rms_and_peak_and_settles_when_released(self):
        page = self.open()
        self.arm(page)
        page.wait_for_function("parseFloat(document.getElementById('ipMeter').dataset.rmsPct) > 0")
        page.wait_for_timeout(400)
        live = page.evaluate("""() => { const bar = document.getElementById('ipMeter');
          return {rmsDb: parseFloat(bar.getAttribute('aria-valuenow')), rmsPct: parseFloat(bar.dataset.rmsPct),
                  peakPct: parseFloat(bar.dataset.peakPct), peakHidden: document.getElementById('ipMeterPeak').hidden}; }""")
        # the fake mic is a 440 Hz sine with a -12 dBFS peak: RMS -15 dBFS, peak -12 dBFS
        self.assertAlmostEqual(live["rmsDb"], TONE_DBFS - 3.01, delta=1.0)
        self.assertAlmostEqual(live["rmsPct"], (TONE_DBFS - 3.01 + 40) / 40 * 100, delta=3)
        self.assertAlmostEqual(live["peakPct"], (TONE_DBFS + 40) / 40 * 100, delta=3)
        self.assertFalse(live["peakHidden"])
        before = page.evaluate("window.IPortalWorkstation.recorder.gainDb")
        page.evaluate("window.IPortalWorkstation.recorder.release()")
        settled = page.evaluate("""() => ({mask: document.getElementById('ipMeterMask').style.width,
          rmsPct: document.getElementById('ipMeter').dataset.rmsPct, peakHidden: document.getElementById('ipMeterPeak').hidden,
          text: document.getElementById('ipMeter').getAttribute('aria-valuetext')})""")
        self.assertEqual(settled, {"mask": "100%", "rmsPct": "0", "peakHidden": True, "text": "no input"})
        self.assertEqual(page.evaluate("window.IPortalWorkstation.recorder.gainDb"), before)   # display never adjusts gain
        self.assert_clean(page)

    @in_browser
    def test_the_meter_minimum_follows_the_station_setting_without_writing_it(self):
        from hardware.models import AudioPipeline
        AudioPipeline.objects.all().delete()
        page = self.open()
        self.assertEqual(page.evaluate("document.getElementById('ipMeter').style.getPropertyValue('--ip-vu-red')"), "85%")
        self.assertFalse(AudioPipeline.objects.exists())                       # opening the page created nothing
        AudioPipeline.objects.create(pk=1, vu_meter_min_db=-60.0)
        page = self.open()
        dom = page.evaluate("""() => { const bar = document.getElementById('ipMeter');
          return [bar.style.getPropertyValue('--ip-vu-yellow'), bar.style.getPropertyValue('--ip-vu-red'),
                  Array.from(document.querySelectorAll('#ipMeterScale .ip-tick')).map(t => t.textContent)]; }""")
        self.assertTrue(dom[0].startswith("66.66"), dom)
        self.assertEqual(dom[1:], ["90%", ["-60", "-20", "-6", "0"]])
        self.assertEqual(AudioPipeline.objects.get().vu_meter_min_db, -60.0)
        AudioPipeline.objects.filter(pk=1).update(vu_meter_min_db=float("nan"))
        page = self.open()
        self.assertEqual(page.evaluate("document.getElementById('ipMeter').style.getPropertyValue('--ip-vu-red')"), "85%")
        self.assert_clean(page)

    @in_browser
    def test_the_shared_editor_refinements_on_the_scratch_consumer(self):
        exercise_shared_editor(self, self.open())

    # -- r0114: waveform navigation ---------------------------------------------------
    @in_browser
    def test_view_math_is_bounded_and_anchored(self):
        page = self.open()
        r = page.evaluate("""() => {
          const A = IPortalAudio;
          return {
            anchored: A.zoomView({start: 0, end: 10}, 10, 0.5, 2.5, 0.25),
            edge: A.zoomView({start: 0, end: 10}, 10, 0.5, 0.2, 0.9),
            floor: A.zoomView({start: 4, end: 6}, 10, 1e-6, 5, 0.5),
            out: A.zoomView({start: 4, end: 6}, 10, 100, 5, 0.5),
            short: A.zoomView({start: 0, end: 0.02}, 0.02, 0.1, 0.01, 0.5),
            shortFit: A.clampView(0, 0.02, 0.02), badTotal: A.clampView(1, 2, 0),
            pan: [A.panView({start: 2, end: 4}, 10, 1.5), A.panView({start: 2, end: 4}, 10, -9),
                  A.panView({start: 2, end: 4}, 10, 99), A.panView({start: 2, end: 4}, 10, NaN)],
            garbage: [A.clampView(NaN, NaN, 10), A.clampView(-5, 3, 10), A.clampView(9, 3, 10)],
          };
        }""")
        self.assertEqual(r["anchored"], {"start": 1.25, "end": 6.25})          # 2.5 s stays at 25 % of the width
        self.assertEqual(r["edge"]["start"], 0)                                # never negative
        self.assertAlmostEqual(r["floor"]["end"] - r["floor"]["start"], 0.05, places=9)
        self.assertEqual(r["out"], {"start": 0, "end": 10})
        self.assertEqual(r["short"], {"start": 0, "end": 0.02})                # a short clip never shows past its end
        self.assertEqual((r["shortFit"], r["badTotal"]), ({"start": 0, "end": 0.02}, {"start": 0, "end": 0}))
        self.assertEqual(r["pan"], [{"start": 3.5, "end": 5.5}, {"start": 0, "end": 2}, {"start": 8, "end": 10},
                                    {"start": 2, "end": 4}])
        self.assertEqual(r["garbage"], [{"start": 0, "end": 10}, {"start": 0, "end": 3}, {"start": 7, "end": 10}])
        self.assert_clean(page)

    def editor_snapshot(self, page):
        return page.evaluate(EDITOR_SNAPSHOT)

    @in_browser
    def test_wheel_and_trackpad_navigation(self):
        page = self.open()
        self.load_browser_pcm(page, 4.0)
        page.evaluate("() => { const S = IPortalWorkstation; S.selection = {start: 1.0, end: 2.0}; S.playhead = 0.5; }")
        before = self.editor_snapshot(page)
        box = page.locator("#ipWave").bounding_box()
        x, y = box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2
        page.mouse.move(x, y)
        page.mouse.wheel(0, -300)                                              # wheel up: zoom in at the cursor
        page.wait_for_timeout(50)
        v = page.evaluate("IPortalWorkstation.view")
        self.assertLess(v["end"] - v["start"], 4.0)
        self.assertAlmostEqual(v["start"] + 0.25 * (v["end"] - v["start"]), 1.0, delta=0.01)   # 1.0 s stays put
        span = v["end"] - v["start"]
        page.mouse.wheel(200, 0)                                               # horizontal swipe: pan right
        page.wait_for_timeout(50)
        v2 = page.evaluate("IPortalWorkstation.view")
        self.assertGreater(v2["start"], v["start"])
        self.assertAlmostEqual(v2["end"] - v2["start"], span, places=6)
        page.mouse.wheel(-5000, 0)                                             # far left: clamps at 0
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("IPortalWorkstation.view.start"), 0)
        page.keyboard.down("Shift")                                            # Shift + wheel: pan with a mouse
        page.mouse.wheel(0, 150)
        page.keyboard.up("Shift")
        page.wait_for_timeout(50)
        v3 = page.evaluate("IPortalWorkstation.view")
        self.assertGreater(v3["start"], 0)
        self.assertAlmostEqual(v3["end"] - v3["start"], span, places=6)
        anchor = v3["start"] + 0.25 * span
        page.keyboard.down("Control")                                          # trackpad pinch (Ctrl + wheel): zoom
        page.mouse.wheel(0, -100)
        page.keyboard.up("Control")
        page.wait_for_timeout(50)
        v4 = page.evaluate("IPortalWorkstation.view")
        self.assertLess(v4["end"] - v4["start"], span)
        self.assertAlmostEqual(v4["start"] + 0.25 * (v4["end"] - v4["start"]), anchor, delta=0.01)
        for _ in range(30):
            page.mouse.wheel(0, -400)                                          # can't zoom past the floor
        page.wait_for_timeout(50)
        v5 = page.evaluate("IPortalWorkstation.view")
        self.assertGreaterEqual(v5["end"] - v5["start"], 0.05 - 1e-9)
        for _ in range(30):
            page.mouse.wheel(0, 400)                                           # nor out past the clip
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("IPortalWorkstation.view"), {"start": 0, "end": 4})
        page.click("#ipZoomIn"); page.click("#ipZoomIn"); page.click("#ipZoomFit")
        self.assertEqual(page.evaluate("IPortalWorkstation.view"), {"start": 0, "end": 4})
        after = self.editor_snapshot(page)
        self.assertEqual({k: after[k] for k in after if k != "view"}, {k: before[k] for k in before if k != "view"})
        # outside the waveform the wheel is the page's: nothing changes
        box = page.locator("#ipWave").bounding_box()                           # the clicks may have scrolled
        outside = page.locator("#ipSelection").bounding_box()
        self.assertGreater(outside["y"], box["y"] + box["height"])
        page.mouse.move(outside["x"] + 5, outside["y"] + outside["height"] / 2)
        page.mouse.wheel(0, -300)
        self.assertEqual(page.evaluate("IPortalWorkstation.view"), {"start": 0, "end": 4})
        self.assert_clean(page)

    @in_browser
    def test_touch_tap_select_pinch_pan_and_cancel(self):
        page = self.open(touch=True)
        self.load_browser_pcm(page, 4.0)
        cdp = page.context.new_cdp_session(page)
        box = page.locator("#ipWave").bounding_box()
        y = box["y"] + box["height"] / 2
        X = lambda frac: box["x"] + box["width"] * frac                        # noqa: E731
        def touch(kind, *points):
            cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [
                {"x": px, "y": py, "id": ident} for ident, px, py in points]})
        before = self.editor_snapshot(page)
        # a tap places the playhead
        touch("touchStart", (1, X(0.5), y)); touch("touchEnd")
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), 2.0, delta=0.03)
        # one finger dragging selects (as the mouse does)
        touch("touchStart", (1, X(0.25), y))
        for frac in (0.3, 0.4, 0.5):
            touch("touchMove", (1, X(frac), y))
        touch("touchEnd")
        sel = page.evaluate("IPortalWorkstation.selection")
        self.assertAlmostEqual(sel["start"], 1.0, delta=0.03)
        self.assertAlmostEqual(sel["end"], 2.0, delta=0.03)
        # pinch out around the midpoint (50 %): zooms in, keeping 2.0 s there
        touch("touchStart", (1, X(0.45), y), (2, X(0.55), y))
        for spread in (0.07, 0.1, 0.15, 0.2):
            touch("touchMove", (1, X(0.5 - spread), y), (2, X(0.5 + spread), y))
        touch("touchEnd")
        v = page.evaluate("IPortalWorkstation.view")
        span = v["end"] - v["start"]
        self.assertLess(span, 2.0)
        self.assertAlmostEqual(v["start"] + 0.5 * span, 2.0, delta=0.02)
        self.assertEqual(page.evaluate("IPortalWorkstation.selection"), sel)  # navigation did not select
        # two fingers moving right pan back in time; moving left pan forward
        touch("touchStart", (1, X(0.4), y), (2, X(0.6), y))
        touch("touchMove", (1, X(0.5), y), (2, X(0.7), y))
        touch("touchEnd")
        v2 = page.evaluate("IPortalWorkstation.view")
        self.assertLess(v2["start"], v["start"])
        self.assertAlmostEqual(v2["end"] - v2["start"], span, delta=1e-6)
        touch("touchStart", (1, X(0.6), y), (2, X(0.8), y))
        touch("touchMove", (1, X(0.3), y), (2, X(0.5), y))
        touch("touchEnd")
        self.assertGreater(page.evaluate("IPortalWorkstation.view.start"), v2["start"])
        # a second finger turns a would-be selection into navigation: the earlier selection survives
        touch("touchStart", (1, X(0.2), y))
        touch("touchMove", (1, X(0.35), y))
        touch("touchStart", (1, X(0.35), y), (2, X(0.6), y))
        touch("touchMove", (1, X(0.3), y), (2, X(0.65), y))
        touch("touchEnd")
        self.assertEqual(page.evaluate("IPortalWorkstation.selection"), sel)
        # changing finger counts mid-gesture re-baselines: no jump, no stuck state
        touch("touchStart", (1, X(0.3), y), (2, X(0.5), y), (3, X(0.7), y))
        touch("touchMove", (1, X(0.32), y), (2, X(0.5), y), (3, X(0.68), y))
        before_lift = page.evaluate("IPortalWorkstation.view")
        touch("touchMove", (1, X(0.32), y), (2, X(0.5), y))                    # finger 3 lifts
        self.assertEqual(page.evaluate("IPortalWorkstation.view"), before_lift)
        touch("touchMove", (1, X(0.30), y), (2, X(0.52), y))
        v3 = page.evaluate("IPortalWorkstation.view")
        self.assertTrue(0 <= v3["start"] < v3["end"] <= 4.0, v3)
        touch("touchEnd")
        self.assertEqual(page.evaluate("IPortalWorkstation.selection"), sel)
        # a cancelled gesture leaves nothing stuck: the next tap is an ordinary tap
        touch("touchStart", (1, X(0.2), y))
        touch("touchMove", (1, X(0.4), y))
        touch("touchCancel")
        self.assertEqual(page.evaluate("IPortalWorkstation.selection"), sel)
        page.click("#ipZoomFit")
        touch("touchStart", (1, X(0.75), y)); touch("touchEnd")
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), 3.0, delta=0.03)
        self.assertIsNone(page.evaluate("IPortalWorkstation.selection"))
        after = self.editor_snapshot(page)
        for key in ("pcmSum", "length", "dirty", "ops", "undo", "baseRevision"):
            self.assertEqual(after[key], before[key], key)                     # no gesture edits audio
        touch_action = page.evaluate("getComputedStyle(document.getElementById('ipWave')).touchAction")
        self.assertEqual(touch_action, "pan-y")                                # vertical page scroll stays native
        self.assert_clean(page)

    @in_browser
    def test_resized_canvas_and_an_off_screen_playhead(self):
        page = self.open()
        self.load_browser_pcm(page, 2.0)
        page.evaluate("() => { document.getElementById('ipWave').style.width = '480px'; window.dispatchEvent(new Event('resize')); }")
        box = page.locator("#ipWave").bounding_box()
        self.assertAlmostEqual(box["width"], 480, delta=1)
        page.mouse.click(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2)
        self.assertAlmostEqual(page.evaluate("IPortalWorkstation.playhead"), 0.5, delta=0.02)
        self.assertEqual(page.evaluate("document.getElementById('ipWave').width"), 480)
        edge = page.evaluate("""() => {
          const S = IPortalWorkstation; S.view = {start: 1.0, end: 1.5}; S.playhead = 0.2;
          document.getElementById('ipZoomIn').disabled = false;
          const canvas = document.getElementById('ipWave'), ctx = canvas.getContext('2d');
          window.dispatchEvent(new Event('resize'));
          let white = 0;
          for (const x of [0, 1, 2, canvas.width - 1]) {
            const d = ctx.getImageData(x, 0, 1, canvas.height).data;
            for (let i = 0; i < d.length; i += 4) if (d[i] > 240 && d[i + 1] > 240 && d[i + 2] > 240) white++;
          }
          return white;
        }""")
        self.assertEqual(edge, 0)                                              # never drawn at an edge
        self.assert_clean(page)

    @in_browser
    def test_punch_in_controls_say_what_each_mode_does(self):
        """r0108: both modes record OVER the take from the playhead; they differ
        only in what follows. The old "Replace the rest (instead of insert)"
        checkbox suggested an insert that never existed."""
        page = self.open()
        modes = page.eval_on_selector_all("input[name=ipPunchTail]", "els => els.map(e => [e.id, e.value, e.checked])")
        self.assertEqual(modes, [["ipPunchKeepTail", "insert", True], ["ipPunchReplace", "replace", False]])
        label = lambda ident: page.eval_on_selector(f"#{ident}", "el => el.closest('label').textContent.trim()")
        self.assertIn("keep the rest", label("ipPunchKeepTail"))
        self.assertIn("discard the rest", label("ipPunchReplace"))
        self.assertNotIn("insert", page.text_content("#ipRecorderPanel").lower())       # no misleading verb
        # The mode choice only applies (and is only enabled) while punch-in is on.
        self.assertTrue(page.is_disabled("#ipPunchKeepTail") and page.is_disabled("#ipPunchReplace"))
        page.check("#ipPunch")
        self.assertFalse(page.is_disabled("#ipPunchKeepTail") or page.is_disabled("#ipPunchReplace"))

        self.arm(page)
        page.uncheck("#ipPunch")
        self.record(page, 3.0)
        total = self.duration(page)
        # "keep the rest": a 1 s punch at 1.0 s leaves the length unchanged.
        page.evaluate("() => { window.IPortalWorkstation.playhead = 1.0; }")
        page.check("#ipPunch")
        self.record(page, 1.0)
        self.assertAlmostEqual(self.duration(page), total, delta=0.05)
        self.assertEqual(page.evaluate("window.IPortalWorkstation.ops")[-1], "punch-insert")   # provenance id unchanged
        # "discard the rest": the take ends where the new recording ends (~2.0 s).
        page.click("#ipUndo")
        page.evaluate("() => { window.IPortalWorkstation.playhead = 1.0; }")
        page.check("#ipPunchReplace")
        self.record(page, 1.0)
        self.assertAlmostEqual(self.duration(page), 2.0, delta=0.35)
        self.assertEqual(page.evaluate("window.IPortalWorkstation.ops")[-1], "punch-replace")
        self.assertFalse(ProductionMedia.objects.exists())
        self.assert_clean(page)

    # -- 4. import and limits --------------------------------------------------------------
    @in_browser
    def test_import_invalid_oversized_and_excessive_duration(self):
        page = self.open()
        page.set_input_files("#ipImport", files=[{"name": "bad.wav", "mimeType": "audio/wav", "buffer": b"\x00" * 2048}])
        page.wait_for_selector("text=could not be read as audio")
        page.set_input_files("#ipImport", files=[{"name": "huge.wav", "mimeType": "audio/wav",
                                                  "buffer": b"\x00" * (5 * 1024 * 1024)}])
        page.wait_for_selector("text=larger than the limit")
        page.set_input_files("#ipImport", str(self.long_file))
        page.wait_for_selector("text=longer than")
        self.assertIsNone(page.evaluate("window.IPortalWorkstation.pcm"))
        # The recorder's own maximum duration stops a take.
        self.arm(page)
        page.evaluate("() => { window.IPortalWorkstation.recorder.maxSeconds = 1; }")
        page.click("#ipRecord")
        page.wait_for_selector("text=Maximum length reached", timeout=10000)
        page.wait_for_function("window.IPortalWorkstation.pcm")
        self.assertLessEqual(self.duration(page), 1.05)
        self.assertFalse(ProductionMedia.objects.exists())
        self.assert_clean(page)

    # -- 5. interruption, cancel and drafts ----------------------------------------------
    @in_browser
    def test_network_interruption_and_cancel_keep_the_draft_and_create_nothing(self):
        page = self.open()
        self.arm(page)
        self.record(page, 1.5)
        recorded = self.duration(page)
        page.route("**/api/scratch/take/**", lambda route: route.abort("failed"))
        page.click("#ipSave")
        page.wait_for_selector("text=The upload was interrupted")
        page.unroute("**/api/scratch/take/**")

        held = []                        # the upload is held in flight (never reaches the server) ...
        page.route("**/api/scratch/take/**", lambda route: held.append(route))
        page.click("#ipSave")
        page.wait_for_selector("#ipCancelUpload:not([hidden])")
        page.click("#ipCancelUpload")
        page.wait_for_selector("text=Upload cancelled")
        for route in held:               # ... and then dropped, as a cancelled request is
            try:
                route.abort("aborted")
            except Exception:  # noqa: BLE001 -- the page already abandoned it
                pass
        self.assertFalse(ProductionMedia.objects.exists())
        self.assertEqual(self.list_files("media"), [])
        # Reload: the browser-local draft is offered back.
        page.reload()
        page.wait_for_selector("#ipDraftBanner:not([hidden])")
        page.click("#ipDraftRestore")
        page.wait_for_function("window.IPortalWorkstation.pcm")
        self.assertAlmostEqual(self.duration(page), recorded, delta=0.01)
        self.assertEqual(ScratchPadAdapter.committed, {})

    # -- 6. retryable validation failure --------------------------------------------------
    @in_browser
    def test_a_retryable_validation_failure_offers_a_retry(self):
        page = self.open()
        self.arm(page)
        self.record(page, 1.0)
        blocked = validation.ValidationOutcome(validation.STATUS_INFRASTRUCTURE, "engine_capability_unavailable", {})
        with mock.patch.object(validation, "_analyze_path", return_value=blocked):
            page.click("#ipSave")
            page.wait_for_selector("#ipRetry:not([hidden])", timeout=60000)
        self.assertEqual(ScratchPadAdapter.committed, {})
        self.assertEqual(ProductionMedia.objects.get().validation_state, "unvalidated")
        page.click("#ipRetry")
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Saved", timeout=60000)
        self.assertEqual(ScratchPadAdapter.committed["a"], ProductionMedia.objects.get().pk)

    # -- 7. stale editor ---------------------------------------------------------------------
    @in_browser
    def test_a_conflicting_save_preserves_the_newer_binding(self):
        page = self.open()
        self.arm(page)
        self.record(page, 1.0)
        import uuid
        newer = uuid.uuid4()
        ScratchPadAdapter.committed["a"] = newer                    # someone else committed meanwhile
        page.click("#ipSave")
        page.wait_for_selector("#ipConflict:not([hidden])", timeout=60000)
        self.assertEqual(ScratchPadAdapter.committed["a"], newer)
        self.assertEqual(ProductionMedia.objects.count(), 1)       # the late take is kept, unbound

    # -- 8. browser capability fallbacks ------------------------------------------------------
    @in_browser
    def test_without_capture_support_import_still_works(self):
        page = self.open(init_script="Object.defineProperty(navigator, 'mediaDevices', {value: undefined});")
        self.assertTrue(page.is_visible("#ipUnsupported"))
        self.assertTrue(page.is_disabled("#ipArm"))
        self.assertTrue(page.is_disabled("#ipRecord"))
        self.assertFalse(page.is_disabled("#ipImportBtn"))

    @in_browser
    def test_media_recorder_fallback_without_audio_worklet(self):
        page = self.open(init_script="delete BaseAudioContext.prototype.audioWorklet;")
        self.arm(page)
        self.assertEqual(page.evaluate("window.IPortalWorkstation.recorder.mode"), "mediarecorder")
        self.assertEqual(page.evaluate("window.IPortalWorkstation.recorder.mimeType"), "audio/webm;codecs=opus")
        self.record(page, 1.5)
        self.assertTrue(1.0 <= self.duration(page) <= 2.4, self.duration(page))
        self.assert_clean(page)

    @in_browser
    def test_device_loss_and_tab_suspension_are_deterministic(self):
        page = self.open()
        self.arm(page)
        self.assertTrue(page.is_disabled("#ipDevice") is False)
        page.click("#ipRecord")
        self.assertTrue(page.is_disabled("#ipDevice"))                    # no device change mid-take
        page.wait_for_timeout(800)
        page.evaluate("""() => {
          Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_selector("text=Recording paused because the page was hidden")
        self.assertEqual(page.evaluate("window.IPortalWorkstation.recorder.state"), "paused")
        page.evaluate("""() => {
          const t = window.IPortalWorkstation.recorder.stream.getAudioTracks()[0];
          t.stop(); t.dispatchEvent(new Event('ended'));
        }""")
        page.wait_for_selector("text=The microphone was disconnected")
        page.wait_for_function("window.IPortalWorkstation.pcm")
        self.assertGreater(self.duration(page), 0.5)                     # captured audio kept
        self.assertEqual(page.evaluate("window.IPortalWorkstation.recorder.state"), "idle")
