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
    def open(self, init_script=None):
        context = self.browser.new_context(accept_downloads=True)
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
        page.wait_for_function("parseFloat(document.getElementById('ipMeter').style.width) > 0")
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
