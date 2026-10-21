"""Roadmap 2.23 Phase 1 -- Console presentation in real browsers.

Chromium and Firefox (Playwright) against the real rendered dashboard.html
on both console modes, at desktop / tablet / mobile portrait / mobile
landscape sizes, device-pixel ratios 1 and 2, with a window resize.

Isolation -- nothing here can touch playout:
* every /api/ and /monitoring/api/ request the page makes is answered in
  the BROWSER from fixtures (``page.route``); only the page itself and
  /static/ reach the test server, and any non-GET is recorded and answered
  locally, never forwarded;
* ``library.views.enqueue_engine_command`` is patched to fail the test if
  anything ever does reach it (backstop);
* the browser runs with an empty ALSA config, no PulseAudio, muted audio,
  and the page is never asked to connect a Remote DJ or calibrate a mic;
* Date/timers are Playwright's fake clock, so playhead positions are exact.

Screenshots go to $CONSOLE_2_23_SCREENSHOT_DIR when set (evidence runs),
otherwise to a throwaway temp directory.
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.db import connections
from django.test import Client, override_settings

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment without Playwright
    sync_playwright = None

User = get_user_model()

T0_MS = 1_800_000_000_000          # fixed fake wall clock (ms)
SETTLE_MS = 2_000                  # paused here: two polls have run
DURATION = 240.0                   # every fixture track: duration == analysis_duration
POSITION = 60.0                    # live-deck position the engine reports
CUE_IN = 4.0
NEXT_START = 228.0
ART = (
    "data:image/svg+xml;utf8,"
    "<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'>"
    "<rect width='64' height='64' fill='%23a855f7'/><circle cx='32' cy='32' r='14' fill='%23111827'/></svg>"
)

# Deck-A card height for this fixture at DESKTOP (Chromium): 312.9px on the
# r0116 template (4179ca3, same fixtures and harness; see the Phase 1
# evidence), 249.7px after Phase 1. The ceiling leaves headroom for font
# metrics differing between engines.
COMPACT_BASELINE_PX = 312.9
COMPACT_LIMIT_PX = 270

MOBILE_PORTRAIT = {"width": 375, "height": 812}
MOBILE_LANDSCAPE = {"width": 812, "height": 375}
TABLET = {"width": 768, "height": 1024}
DESKTOP = {"width": 1440, "height": 900}


def _track(track_id, title, artist):
    return {"track_id": track_id, "title": title, "artist": artist}


TRACKS = {
    "A": _track(101, "Deck A Song", "Artist Alpha"),
    "B": _track(202, "Deck B Song", "Artist Bravo"),
}


def engine_state(live="A", *, position=POSITION, paused=False, **overrides):
    """A minimal-but-complete engine_state.json as the Console reads it."""
    preview = "B" if live == "A" else "A"
    decks = {preview: None, live: {
        **TRACKS[live], "slot": live, "album": "Fixture Album", "position": position,
        "duration": DURATION, "next_start": NEXT_START, "cue_in": CUE_IN,
        "category": "Current", "format": "FLAC", "paused": paused,
    }}
    up_next = {**TRACKS[preview], "item_id": 9001, "category": "Gold", "format": "MP3",
               "duration": 200.0, "airtime_seconds": 190.0, "eta_seconds": 168.0}
    state = {
        "transport": "PLAYING", "decks": decks, "queue": [up_next], "queue_cursor": 3,
        "total_items": 12, "log_id": 77, "hour": 14, "date": "2027-01-15",
        "timestamp": (T0_MS + SETTLE_MS) / 1000,
        "mic_configured": True, "mic_ok": True, "mic_live": False, "mic_recovery": None,
        "manual_mode": False, "manual_from_mic": False, "ptt_auto_manual_enabled": True,
        "remote_dj_configured": True, "remote_dj_connected": False, "remote_dj_live": False,
        "remote_dj_reconnecting": False, "remote_dj_connection": None, "remote_dj_quality": None,
        "fx_fires": [],
    }
    state.update(overrides)
    return state


def waveform(points=400, analysis_duration=DURATION):
    """``analysis_duration`` may differ slightly from the engine duration (it
    is the final envelope timestamp), or be None (older analysis), in which
    case the Console falls back to the engine-reported duration."""
    left = [int(40 + 180 * abs(((i * 7) % 100) - 50) / 50) for i in range(points)]
    right = [int(30 + 170 * abs(((i * 11) % 100) - 50) / 50) for i in range(points)]
    return {"samples_left": left, "samples_right": right, "cue_in_seconds": CUE_IN,
            "next_start": NEXT_START, "analysis_duration": analysis_duration}


# Read the canvas the way an operator sees it: the playhead column, rows
# near the top triangle / middle of the line, and every pure-white column
# on the top row (one cluster == one playhead).
PLAYHEAD_PROBE = """
([slot, seconds, duration]) => {
  const c = document.getElementById('npWaveform-' + slot);
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  const ctx = c.getContext('2d');
  const at = (xCss, yCss) => Array.from(ctx.getImageData(
      Math.min(c.width - 1, Math.max(0, Math.floor(xCss * dpr))),
      Math.min(c.height - 1, Math.max(0, Math.floor(yCss * dpr))), 1, 1).data);
  const cssW = c.width / dpr;
  const expectedX = (seconds / duration) * cssW;
  const row = ctx.getImageData(0, Math.floor(1 * dpr), c.width, 1).data;
  const whiteCols = [];
  for (let i = 0; i < c.width; i++) {
    const o = i * 4;
    if (row[o] > 245 && row[o + 1] > 245 && row[o + 2] > 245 && row[o + 3] > 245) whiteCols.push(i / dpr);
  }
  return {
    dpr, backingW: c.width, backingH: c.height, cssW: rect.width, cssH: rect.height,
    expectedX, top: at(expectedX, 1), middle: at(expectedX, rect.height / 2 + 9),
    bottom: at(expectedX, rect.height - 1.5), clear: at(expectedX + 14, 1), whiteCols,
  };
}
"""

WHITE_PIXELS = """
(slot) => {
  const c = document.getElementById('npWaveform-' + slot);
  if (!c) return -1;
  const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let n = 0;
  for (let o = 0; o < d.length; o += 4) {
    if (d[o] > 245 && d[o + 1] > 245 && d[o + 2] > 245 && d[o + 3] > 245) n++;
  }
  return n;
}
"""

BOX = "(sel) => { const r = document.querySelector(sel).getBoundingClientRect(); " \
      "return {left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height}; }"


def _is_white(rgba):
    return all(v > 245 for v in rgba)


class _ConsoleBrowserBase:
    BROWSER = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._assets = tempfile.TemporaryDirectory()
        assets = Path(cls._assets.name)
        (assets / "empty-alsa.conf").write_text("")
        cls.browser_env = {**os.environ, "ALSA_CONFIG_PATH": str(assets / "empty-alsa.conf"),
                           "PULSE_SERVER": "unix:/nonexistent/pulse", "PULSE_RUNTIME_PATH": str(assets)}
        shots = os.environ.get("CONSOLE_2_23_SCREENSHOT_DIR")
        cls._shots_tmp = None if shots else tempfile.TemporaryDirectory()
        cls.screenshot_dir = Path(shots or cls._shots_tmp.name)
        cls.screenshot_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls._assets.cleanup()
        if cls._shots_tmp is not None:
            cls._shots_tmp.cleanup()
        super().tearDownClass()

    def _start_browser(self):
        """Per test, like production.tests.test_recorder_browser.in_browser:
        Playwright's sync API runs an event loop on this thread, and it must
        be gone again before TransactionTestCase teardown, whose database
        safety guard (isadoraair.test_safety) queries the connection."""
        pw = sync_playwright().start()
        self.addCleanup(pw.stop)
        # Once Playwright's sync API is running, Django's per-context
        # connection handler hands this test's ORM calls (session login,
        # theme save) a SEPARATE database connection. Close it while that
        # context is still current -- before pw.stop() (cleanups are LIFO)
        # -- or it outlives the test and blocks the test-database drop.
        self.addCleanup(connections.close_all)
        launcher = getattr(pw, self.BROWSER)
        if self.BROWSER == "chromium":
            browser = launcher.launch(env=self.browser_env, args=["--mute-audio", "--disable-audio-output"])
        else:
            browser = launcher.launch(env=self.browser_env, firefox_user_prefs={"media.volume_scale": "0.0"})
        self.addCleanup(browser.close)
        return browser

    def setUp(self):
        super().setUp()
        env_patch = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        enqueue_patch = mock.patch(
            "library.views.enqueue_engine_command",
            side_effect=AssertionError("a browser test reached the real engine command queue"),
        )
        self.enqueue = enqueue_patch.start()
        self.addCleanup(enqueue_patch.stop)

        # A TransactionTestCase flush removes migration-seeded rows: recreate
        # exactly the capability split this page depends on.
        from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig
        from library.models import GroupAccess
        caps = {}
        for slug in ("playout.control", "playout.queue_manage", "playout.manual_mode",
                     "remote_dj.connect", "remote_dj.mic_gate", "studio.mic_ptt"):
            caps[slug], _ = Capability.objects.get_or_create(
                slug=slug, defaults={"label": slug, "requires_schedule": True})
        remote_host = Role.objects.create(name="Remote Host (test)")
        for slug in ("remote_dj.connect", "playout.queue_manage", "playout.manual_mode", "remote_dj.mic_gate"):
            RoleCapability.objects.create(role=remote_host, capability=caps[slug])
        group, _ = Group.objects.get_or_create(name="remote_dj")
        GroupRole.objects.update_or_create(group=group, defaults={"role": remote_host})
        GroupAccess.objects.update_or_create(group=group, defaults={
            "allowed_prefixes": "/remote-dj/\n/static/", "landing_url": "/remote-dj/"})
        ScheduleAccessConfig.load()

        self.superuser = User.objects.create_superuser("c223_browser_su", "b@example.invalid", "x")
        self.host = User.objects.create_user("c223_browser_host", password="x")
        self.host.groups.add(group)

        self.browser = self._start_browser()
        self.state = engine_state()
        self.status_fails = False
        self.post_status = {}
        self.posts = []
        self.analysis_duration = DURATION

    # -- harness -----------------------------------------------------------
    def _route(self, route, request):
        url = urlparse(request.url)
        if not request.url.startswith(self.live_server_url):
            return route.abort()
        path = url.path
        if request.method != "GET":
            self.posts.append((path, request.post_data))
            status = self.post_status.get(path, 200)
            if status == 302:  # the login gate turning an expired session away
                return route.fulfill(status=302, headers={"Location": "/login-fixture/"})
            body = {"ok": True} if status == 200 else {"error": "refused by test fixture"}
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if path == "/login-fixture/":
            return route.fulfill(status=200, content_type="text/html", body="<html><body>login</body></html>")
        if path == "/api/engine/status/":
            if self.status_fails:
                return route.fulfill(status=500, content_type="application/json", body="{}")
            return route.fulfill(content_type="application/json", body=json.dumps(self.state))
        if path.startswith("/api/waveform/"):
            return route.fulfill(content_type="application/json",
                                 body=json.dumps(waveform(analysis_duration=self.analysis_duration)))
        if path.startswith("/api/albumart/"):
            return route.fulfill(content_type="application/json", body=json.dumps({"art": ART}))
        if path.startswith("/api/") or path.startswith("/monitoring/api/") or path.startswith("/ws/"):
            return route.fulfill(content_type="application/json", body="{}")
        return route.continue_()

    def open_console(self, user, path="/", viewport=DESKTOP, dpr=1):
        client = Client()
        client.force_login(user)
        context = self.browser.new_context(viewport=viewport, device_scale_factor=dpr)
        self.addCleanup(context.close)
        context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value,
                              "url": self.live_server_url}])
        page = context.new_page()
        self.console_errors = []
        page.on("pageerror", lambda exc: self.console_errors.append(str(exc)))
        page.route("**/*", self._route)
        page.clock.install(time=T0_MS / 1000)  # Python API: seconds
        page.goto(self.live_server_url + path)
        page.clock.pause_at((T0_MS + SETTLE_MS) / 1000)
        self.settle(page)
        return page

    def settle(self, page):
        live = next(slot for slot, deck in self.state["decks"].items() if deck)
        try:
            page.wait_for_function(
                "(slot) => typeof deckState !== 'undefined' && deckState[slot].mode === 'live'"
                " && waveformState[slot].samplesLeft !== null", arg=live, timeout=15_000)
        except Exception:
            detail = page.evaluate(
                "() => ({url: location.href, title: document.title,"
                " modes: typeof deckState === 'undefined' ? null : SLOTS.map(s => deckState[s].mode),"
                " wf: typeof waveformState === 'undefined' ? null"
                " : SLOTS.map(s => waveformState[s].samplesLeft === null)})")
            self.fail(f"console never rendered a live deck: {detail}; errors={self.console_errors}")
        page.evaluate("() => { poll(); }")
        try:
            page.wait_for_function(
                "(t) => SLOTS.every(s => deckState[s].mode !== 'live' || deckState[s].lastPollTime === t)",
                arg=T0_MS + SETTLE_MS, timeout=10_000)
        except Exception:
            detail = page.evaluate("() => ({now: Date.now(), polls: SLOTS.map(s => deckState[s].lastPollTime)})")
            self.fail(f"poll never settled at the paused clock: {detail}, expected {T0_MS + SETTLE_MS}")
        page.evaluate("() => { updatePositionIndicators(); SLOTS.forEach(drawWaveform); }")
        self.assertEqual(self.console_errors, [])

    def shot(self, page, name):
        page.screenshot(path=str(self.screenshot_dir / f"{self.BROWSER}-{name}.png"), full_page=True)

    def box(self, page, selector):
        return page.evaluate(BOX, selector)

    def time_scale(self):
        return self.analysis_duration or DURATION

    def assert_playhead(self, page, slot, seconds):
        probe = page.evaluate(PLAYHEAD_PROBE, [slot, seconds, self.time_scale()])
        self.assertAlmostEqual(probe["cssH"], 62, delta=0.6)
        self.assertEqual(probe["backingH"], round(probe["cssH"] * probe["dpr"]))
        self.assertEqual(probe["backingW"], round(probe["cssW"] * probe["dpr"]))
        self.assertTrue(_is_white(probe["top"]), probe)       # top triangle
        self.assertTrue(_is_white(probe["middle"]), probe)    # line
        self.assertTrue(_is_white(probe["bottom"]), probe)    # bottom triangle
        self.assertFalse(_is_white(probe["clear"]), probe)    # nothing 14px to the right
        cols = probe["whiteCols"]
        self.assertTrue(cols, probe)
        # ONE playhead: every white column on the top row is the triangle
        # (8px wide) around the expected x -- no second marker anywhere.
        self.assertLessEqual(max(cols) - min(cols), 9, probe)
        self.assertLessEqual(abs((max(cols) + min(cols)) / 2 - probe["expectedX"]), 1.5, probe)
        return probe

    def ops_ids(self, page):
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('.playnow-bar > button'))"
            ".filter(b => b.offsetParent !== null).map(b => b.id)")

    def no_horizontal_overflow(self, page):
        width = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        self.assertLessEqual(width[0], width[1], width)

    # -- full console ------------------------------------------------------
    def test_full_console_desktop_deck_a_live(self):
        page = self.open_console(self.superuser)
        self.assertEqual(self.ops_ids(page),
                         ["lineInBtn", "micPttBtn", "manualModeBtn", "remoteDjGateBtn", "remoteDjConnectBtn"])
        tops = {b["top"] for b in (self.box(page, f"#{i}") for i in self.ops_ids(page))}
        self.assertEqual(len(tops), 1, "ops row must be a single row on desktop")
        self.assertEqual(page.locator(".position-line").count(), 0)
        self.assertEqual(page.locator("#posLine-A").count(), 0)
        self.assert_playhead(page, "A", POSITION)
        self.assertEqual(page.evaluate(WHITE_PIXELS, "B"), 0, "preview deck must not draw a playhead")
        # Live deck: Eject + Pause enabled with honest labels; preview deck: present but disabled.
        eject = page.locator("#ejectBtn-A")
        self.assertTrue(eject.is_enabled())
        self.assertIn("Eject Deck A", eject.get_attribute("aria-label"))
        self.assertIn("no fade", eject.get_attribute("title"))
        self.assertNotIn("Start Next", eject.get_attribute("title"))
        self.assertEqual(page.locator("#playPauseBtn-A").get_attribute("aria-label"), "Pause Deck A")
        self.assertTrue(page.locator("#ejectBtn-B").is_disabled())
        self.assertTrue(page.locator("#npWaveform-A").evaluate("c => c.classList.contains('seekable')"))
        a, b = self.box(page, "#deck-A"), self.box(page, "#deck-B")
        self.assertLess(a["right"], b["left"])
        self.assertAlmostEqual(a["top"], b["top"], delta=1)
        self.assertFalse(page.locator("#startNextSlot").is_visible())
        self.no_horizontal_overflow(page)
        self.shot(page, "full-desktop-deckA-live")
        self.assertEqual(self.posts, [])

    def test_full_console_desktop_deck_b_live(self):
        self.state = engine_state(live="B", position=150.0)
        page = self.open_console(self.superuser)
        self.assert_playhead(page, "B", 150.0)
        self.assertEqual(page.evaluate(WHITE_PIXELS, "A"), 0)
        self.assertTrue(page.locator("#ejectBtn-B").is_enabled())
        self.assertTrue(page.locator("#ejectBtn-A").is_disabled())
        self.shot(page, "full-desktop-deckB-live")

    def test_waveform_click_seek_maps_with_the_drawn_time_scale(self):
        page = self.open_console(self.superuser)
        canvas = self.box(page, "#npWaveform-A")
        with page.expect_request("**/api/engine/seek/"):
            page.mouse.click(canvas["left"] + canvas["width"] * 0.75, canvas["top"] + 20)
        page.clock.run_for(100)
        self.assertEqual(len(self.posts), 1, self.posts)
        path, body = self.posts[0]
        self.assertEqual(path, "/api/engine/seek/")
        sent = json.loads(body)
        self.assertEqual(sent["slot"], "A")
        self.assertAlmostEqual(sent["position"], 0.75 * DURATION, delta=DURATION / canvas["width"] + 0.01)
        # Clicking the PREVIEW deck's waveform sends nothing.
        preview = self.box(page, "#npWaveform-B")
        page.mouse.click(preview["left"] + 30, preview["top"] + 20)
        page.clock.run_for(300)
        self.assertEqual(len(self.posts), 1)

    def test_paused_deck_holds_its_playhead(self):
        self.state = engine_state(position=90.0, paused=True)
        page = self.open_console(self.superuser)
        page.clock.run_for(3_000)
        page.evaluate("() => { poll(); }")
        page.wait_for_function("() => deckState.A.paused === true")
        page.evaluate("() => drawWaveform('A')")
        self.assert_playhead(page, "A", 90.0)
        self.assertEqual(page.locator("#playPauseBtn-A").get_attribute("aria-label"), "Resume Deck A")
        self.assertEqual(page.locator("#transportBadge-A").inner_text().strip(), "PAUSED")

    def test_high_dpi_rendering(self):
        page = self.open_console(self.superuser, viewport={"width": 1280, "height": 800}, dpr=2)
        reported = page.evaluate("() => window.devicePixelRatio")
        if reported != 2:
            # Playwright's headless Firefox ignores device_scale_factor (it
            # reports devicePixelRatio 1); the backing-store maths is still
            # asserted at the ratio the engine actually renders with in every
            # other test. High-DPI is proven in Chromium.
            self.skipTest(f"{self.BROWSER} headless reports devicePixelRatio {reported} for device_scale_factor=2")
        probe = self.assert_playhead(page, "A", POSITION)
        self.assertEqual(probe["dpr"], 2)
        self.assertEqual(probe["backingH"], 124)
        self.shot(page, "full-desktop-dpr2")

    def test_window_resize_redraws_at_the_new_size(self):
        page = self.open_console(self.superuser, viewport={"width": 1280, "height": 800})
        before = self.assert_playhead(page, "A", POSITION)
        resized = ("() => { const c = document.getElementById('npWaveform-A');"
                   " return c.width === Math.round(c.getBoundingClientRect().width * (window.devicePixelRatio || 1)); }")
        page.set_viewport_size({"width": 900, "height": 800})
        page.wait_for_function(resized)
        after = self.assert_playhead(page, "A", POSITION)
        self.assertLess(after["cssW"], before["cssW"])
        self.shot(page, "full-900px-after-resize")
        # Down through the single-column breakpoint and back up again.
        page.set_viewport_size({"width": 640, "height": 900})
        page.wait_for_function(resized)
        narrow = self.assert_playhead(page, "A", POSITION)
        page.set_viewport_size({"width": 1280, "height": 800})
        page.wait_for_function(resized)
        wide = self.assert_playhead(page, "A", POSITION)
        self.assertGreater(narrow["cssW"], after["cssW"])   # single column: full-width deck
        self.assertAlmostEqual(wide["cssW"], before["cssW"], delta=1)

    def test_deck_rebuilds_do_not_accumulate_observed_canvases(self):
        page = self.open_console(self.superuser)
        page.evaluate("""() => {
          window.__observed = new Set();
          const ro = waveformResizeObserver;
          const observe = ro.observe.bind(ro), unobserve = ro.unobserve.bind(ro);
          ro.observe = (el) => { window.__observed.add(el); observe(el); };
          ro.unobserve = (el) => { window.__observed.delete(el); unobserve(el); };
          document.querySelectorAll('canvas[id^="npWaveform-"]').forEach(c => window.__observed.add(c));
        }""")
        for live, transport in (("B", "PLAYING"), ("A", "PLAYING"), ("B", "PLAYING"), ("A", "STOPPED"), ("A", "PLAYING")):
            self.state = engine_state(live=live, transport=transport)
            page.evaluate("() => poll()")
            page.wait_for_function(
                "([slot, t]) => t === 'STOPPED' ? deckState.A.mode === 'offline'"
                " : deckState[slot].mode === 'live'", arg=[live, transport])
        still_observed = page.evaluate(
            "() => Array.from(window.__observed).map(c => [c.id, c.isConnected])")
        # Only canvases still in the page are observed: at most one per deck.
        self.assertTrue(all(connected for _, connected in still_observed), still_observed)
        self.assertLessEqual(len(still_observed), 2, still_observed)

    def test_full_console_mobile_portrait(self):
        page = self.open_console(self.superuser, viewport=MOBILE_PORTRAIT, dpr=2)
        ids = self.ops_ids(page)
        self.assertEqual(ids, ["lineInBtn", "micPttBtn", "manualModeBtn", "remoteDjGateBtn", "remoteDjConnectBtn"])
        boxes = {i: self.box(page, f"#{i}") for i in ids}
        self.assertEqual(len({round(boxes[i]["top"]) for i in ids[:3]}), 1, boxes)
        self.assertEqual(len({round(boxes[i]["top"]) for i in ids[3:]}), 1, boxes)
        self.assertGreater(boxes["remoteDjGateBtn"]["top"], boxes["lineInBtn"]["top"])
        for i in ids:
            self.assertGreaterEqual(boxes[i]["height"], 31, i)
        for selector in ("#ejectBtn-A", "#playPauseBtn-A"):
            target = self.box(page, selector)
            self.assertGreaterEqual(target["width"] * target["height"], 31 * 23, selector)
        a, b = self.box(page, "#deck-A"), self.box(page, "#deck-B")
        self.assertLessEqual(a["bottom"], b["top"])
        self.assert_playhead(page, "A", POSITION)
        self.no_horizontal_overflow(page)
        self.shot(page, "full-mobile-portrait")

    def test_full_console_mobile_landscape_and_tablet(self):
        for name, viewport in (("full-mobile-landscape", MOBILE_LANDSCAPE), ("full-tablet", TABLET)):
            with self.subTest(viewport=name):
                page = self.open_console(self.superuser, viewport=viewport, dpr=2)
                self.assert_playhead(page, "A", POSITION)
                self.no_horizontal_overflow(page)
                a, b = self.box(page, "#deck-A"), self.box(page, "#deck-B")
                self.assertLess(a["right"], b["left"])  # both > 700px: side by side
                self.shot(page, name)

    def test_compact_deck_cards(self):
        page = self.open_console(self.superuser)
        deck = self.box(page, "#deck-A")
        # The compact card keeps every element but must be materially
        # shorter than the r0116 card for the same content.
        self.assertLess(deck["height"], COMPACT_LIMIT_PX, deck)
        self.assertLess(deck["height"], COMPACT_BASELINE_PX * 0.87, deck)
        for selector in ("#npTitle-A", "#npArtist-A", "#npCategory-A", "#npFormat-A",
                         "#npCountdown-A", "#npElapsed-A", "#transportBadge-A", "#deckArtThumb-A"):
            with self.subTest(selector=selector):
                self.assertTrue(page.locator(selector).is_visible(), selector)

    # -- Line In / mic indicators -------------------------------------------
    def test_line_in_is_disabled_and_sends_nothing(self):
        page = self.open_console(self.superuser)
        line_in = page.locator("#lineInBtn")
        self.assertTrue(line_in.is_disabled())
        self.assertEqual(line_in.get_attribute("aria-disabled"), "true")
        self.assertIn("unavailable", line_in.get_attribute("aria-label"))
        line_in.click(force=True)
        page.clock.run_for(1_500)
        self.assertEqual(self.posts, [])
        self.assertEqual(line_in.evaluate("b => getComputedStyle(b).filter"), "grayscale(1)")

    def test_mic_indicator_states(self):
        chip = "(sel) => getComputedStyle(document.querySelector(sel + ' .ops-icon-chip')).backgroundColor"
        cases = (
            ("off", {}, "off", "Studio Mic: OFF", "rgb(75, 85, 99)"),
            ("live", {"mic_live": True}, "on", "Studio Mic: LIVE", "rgb(22, 163, 74)"),
            ("checking", {"mic_recovery": {"media_health": {"status": "UNVERIFIED"}}},
             "checking", "Studio Mic: CHECKING", "rgb(180, 83, 9)"),
            ("error", {"mic_ok": False}, "error", "Studio Mic: ERROR", "rgb(185, 28, 28)"),
            ("unconfigured", {"mic_configured": False}, "unavailable", "Studio Mic: not configured", None),
        )
        for name, overrides, state, label, colour in cases:
            with self.subTest(case=name):
                self.state = engine_state(**overrides)
                page = self.open_console(self.superuser)
                btn = page.locator("#micPttBtn")
                self.assertEqual(btn.get_attribute("data-mic-state"), state)
                self.assertEqual(btn.get_attribute("aria-label"), label)
                self.assertNotIn("Studio Mic:", btn.inner_text())
                if colour:
                    self.assertEqual(page.evaluate(chip, "#micPttBtn"), colour)
                self.shot(page, f"ops-row-studio-mic-{name}")
        remote_cases = (
            ("unavailable", {"remote_dj_configured": False}, "unavailable", "N/A"),
            ("calibrate", {}, "calibrate", "Calibrate"),
            ("live-other-browser", {"remote_dj_connected": True, "remote_dj_live": True}, "on", "LIVE"),
            ("off-other-browser", {"remote_dj_connected": True}, "off", "OFF"),
        )
        for name, overrides, state, word in remote_cases:
            with self.subTest(remote=name):
                self.state = engine_state(**overrides)
                page = self.open_console(self.superuser)
                btn = page.locator("#remoteDjGateBtn")
                self.assertEqual(btn.get_attribute("data-mic-state"), state)
                self.assertEqual(btn.locator(".ops-btn-label").inner_text(), word)
                self.assertTrue(btn.get_attribute("aria-label").startswith("Remote Mic:"))
                if name == "live-other-browser":
                    self.assertTrue(btn.is_disabled())  # only the connected browser may gate it
                    self.assertIn("read-only", btn.get_attribute("aria-label"))
                self.shot(page, f"ops-row-remote-mic-{name}")

    def test_refusals_are_reported_not_swallowed(self):
        page = self.open_console(self.superuser)
        self.post_status = {"/api/engine/mic-ptt/": 403, "/api/engine/deck/A/": 403}
        page.locator("#micPttBtn").click()
        page.wait_for_function("() => document.getElementById('opsMsg').textContent.length > 0")
        self.assertIn("Studio Mic request refused: permission denied (role or schedule).",
                      page.locator("#opsMsg").inner_text())
        btn = page.locator("#micPttBtn")
        self.assertIn("refused", btn.get_attribute("class"))
        # The button itself still states the engine's truth.
        self.assertEqual(btn.get_attribute("data-mic-state"), "off")
        self.assertEqual(btn.get_attribute("aria-label"), "Studio Mic: OFF")
        page.locator("#ejectBtn-A").click()
        page.wait_for_function("() => document.getElementById('deckMsg-A').textContent.length > 0")
        self.assertEqual(page.locator("#deckMsg-A").inner_text(),
                         "Eject refused: permission denied (role or schedule)")
        self.shot(page, "refusal-feedback")
        # Outlives the next status render, then expires.
        page.clock.run_for(1_100)
        self.assertIn("refused", btn.get_attribute("class"))
        page.clock.run_for(5_200)
        self.assertNotIn("refused", btn.get_attribute("class"))
        self.assertEqual(page.locator("#opsMsg").inner_text(), "")
        self.assertEqual(page.locator("#deckMsg-A").inner_text(), "")
        # An accepted Eject is exactly one request with the existing body.
        self.post_status = {}
        self.posts.clear()
        with page.expect_request("**/api/engine/deck/A/"):
            page.locator("#ejectBtn-A").click()
        page.clock.run_for(100)
        self.assertEqual(self.posts, [("/api/engine/deck/A/", json.dumps({"action": "eject"}, separators=(",", ":")))])
        self.assertEqual(page.locator("#deckMsg-A").inner_text(), "")

    def test_a_refused_request_never_hides_a_live_mic(self):
        self.state = engine_state(mic_live=True)
        page = self.open_console(self.superuser)
        self.post_status = {"/api/engine/mic-ptt/": 403}
        btn = page.locator("#micPttBtn")
        btn.click()
        page.wait_for_function("() => document.getElementById('opsMsg').textContent.length > 0")
        page.clock.run_for(1_100)
        self.assertEqual(btn.get_attribute("data-mic-state"), "on")
        self.assertEqual(btn.get_attribute("aria-label"), "Studio Mic: LIVE")
        self.assertEqual(btn.locator(".ops-btn-label").inner_text(), "LIVE")
        self.assertIn("active", btn.get_attribute("class"))
        self.shot(page, "refusal-while-live")

    # -- Codex Phase 1 P3: playhead endpoints, duration mismatch/fallback ----
    ENDPOINT_PROBE = """
    (slot) => {
      const c = document.getElementById('npWaveform-' + slot);
      const dpr = window.devicePixelRatio || 1;
      const ctx = c.getContext('2d');
      const painted = (yCss) => {
        const y = Math.min(c.height - 1, Math.floor(yCss * dpr));
        const d = ctx.getImageData(0, y, c.width, 1).data;
        const cols = [];
        for (let i = 0; i < c.width; i++) if (d[i * 4 + 3] > 0) cols.push(i / dpr);
        return cols;
      };
      const white = (yCss) => {
        const y = Math.min(c.height - 1, Math.floor(yCss * dpr));
        const d = ctx.getImageData(0, y, c.width, 1).data;
        const cols = [];
        for (let i = 0; i < c.width; i++) {
          const o = i * 4;
          if (d[o] > 245 && d[o + 1] > 245 && d[o + 2] > 245 && d[o + 3] > 245) cols.push(i / dpr);
        }
        return cols;
      };
      const h = c.height / dpr;
      return {dpr, cssW: c.width / dpr, top: painted(0.25), bottom: painted(h - 0.25), line: white(h / 2 + 9)};
    }
    """

    def _assert_endpoint(self, page, at_end):
        probe = page.evaluate(self.ENDPOINT_PROBE, "A")
        w = probe["cssW"]
        for row in ("top", "bottom"):
            cols = probe[row]
            self.assertTrue(cols, probe)
            if at_end:
                # The whole 8px triangle base is inside the right edge.
                self.assertLessEqual(min(cols), w - 8 + 0.01, (row, probe))
                self.assertGreaterEqual(max(cols), w - 1, (row, probe))
            else:
                self.assertEqual(min(cols), 0, (row, probe))
                self.assertGreaterEqual(max(cols), 7.5, (row, probe))
        # The line still marks the true (edge) position.
        line = probe["line"]
        self.assertTrue(line, probe)
        if at_end:
            self.assertGreaterEqual(min(line), w - 2.01, probe)
        else:
            self.assertLessEqual(max(line), 2, probe)
        return probe

    def test_playhead_triangles_stay_whole_at_both_ends(self):
        for dpr in (1, 2):
            for name, position in (("start", 0.0), ("end", DURATION)):
                with self.subTest(dpr=dpr, at=name):
                    self.state = engine_state(position=position)
                    page = self.open_console(self.superuser, viewport={"width": 1280, "height": 800}, dpr=dpr)
                    probe = self._assert_endpoint(page, at_end=(name == "end"))
                    if probe["dpr"] == dpr:
                        self.shot(page, f"playhead-{name}-dpr{dpr}")

    def test_playhead_and_seek_use_the_analysis_scale_when_it_differs(self):
        self.analysis_duration = DURATION - 4.0       # 236.0 vs engine 240.0
        page = self.open_console(self.superuser)
        self.assert_playhead(page, "A", POSITION)     # x = 60 / 236 * width
        canvas = self.box(page, "#npWaveform-A")
        with page.expect_request("**/api/engine/seek/"):
            page.mouse.click(canvas["left"] + canvas["width"] * 0.75, canvas["top"] + 20)
        page.clock.run_for(100)
        sent = json.loads(self.posts[-1][1])
        self.assertAlmostEqual(sent["position"], 0.75 * 236.0, delta=236.0 / canvas["width"] + 0.01)

    def test_engine_position_past_the_analysis_span_pins_to_the_end(self):
        self.analysis_duration = DURATION - 4.0
        self.state = engine_state(position=DURATION - 1.0)    # beyond 236.0
        page = self.open_console(self.superuser)
        self._assert_endpoint(page, at_end=True)

    def test_missing_analysis_duration_falls_back_to_engine_duration(self):
        self.analysis_duration = None
        page = self.open_console(self.superuser)
        self.assertIsNone(page.evaluate("() => waveformState.A.analysisDuration"))
        self.assert_playhead(page, "A", POSITION)     # x = 60 / 240 * width
        canvas = self.box(page, "#npWaveform-A")
        with page.expect_request("**/api/engine/seek/"):
            page.mouse.click(canvas["left"] + canvas["width"] * 0.5, canvas["top"] + 20)
        page.clock.run_for(100)
        sent = json.loads(self.posts[-1][1])
        self.assertAlmostEqual(sent["position"], 0.5 * DURATION, delta=DURATION / canvas["width"] + 0.01)

    # -- Codex Phase 1 P2: exception-safe, latest-request-wins feedback -------
    # Engine-command POSTs are replaced IN THE PAGE by deferred promises the
    # test resolves/rejects explicitly, in any order -- no real-network timing.
    DEFERRED_FETCH = """() => {
      const realFetch = window.fetch.bind(window);
      window.__deferred = [];
      window.__p = {};
      window.fetch = (url, opts) => {
        if (!opts || opts.method !== 'POST') return realFetch(url, opts);
        return new Promise((resolve, reject) =>
          window.__deferred.push({url: String(url), body: opts.body, resolve, reject}));
      };
      window.__respond = (i, status, body) => window.__deferred[i].resolve(new Response(
        body !== undefined ? body
          : JSON.stringify(status === 200 ? {ok: true} : {error: 'refused by test fixture'}),
        {status, headers: {'Content-Type': 'application/json'}}));
      window.__reject = (i) => window.__deferred[i].reject(new TypeError('simulated network loss'));
    }"""

    def deferred_console(self, **state):
        self.state = engine_state(**state)
        page = self.open_console(self.superuser)
        page.evaluate(self.DEFERRED_FETCH)
        return page

    def start(self, page, *calls):
        """Start commands (JS call expressions), each of which issues exactly
        one engine-command POST; return their request indexes. Each command's
        promise is kept under its request index."""
        indexes = page.evaluate("""(calls) => calls.map(c => {
          const before = window.__deferred.length;
          const promise = (0, eval)(c);
          if (window.__deferred.length !== before + 1) throw new Error('expected one POST from ' + c);
          window.__p[before] = promise;
          return before;
        })""", list(calls))
        return indexes

    def settle_all(self, page, action):
        """Run ONE resolve/reject action ("__respond(i, ...)"/"__reject(i)") and
        wait for exactly that command to finish -- other outstanding commands
        stay outstanding, so arrival order is fully controlled."""
        index = int(action[action.index("(") + 1:].split(",")[0].rstrip(")"))
        page.evaluate("async ([a, i]) => { (0, eval)(a); await window.__p[i]; }", [action, index])

    def ui(self, page, button="micPttBtn"):
        return page.evaluate("""(id) => {
          const b = document.getElementById(id);
          return {pending: b.classList.contains('pending'), refused: b.classList.contains('refused'),
                  state: b.dataset.micState, aria: b.getAttribute('aria-label'),
                  active: b.classList.contains('active'),
                  msg: document.getElementById('opsMsg').textContent};
        }""", button)

    def poll_now(self, page, **state):
        """Run one status poll; ``state`` is either engine_state() overrides
        or (when it has "decks") a complete fixture state."""
        if state:
            self.state = state if "decks" in state else engine_state(**state)
        page.evaluate("async () => { await poll(); }")

    def test_network_rejection_clears_pending_and_reports_an_unconfirmed_outcome(self):
        page = self.deferred_console()
        i, = self.start(page, "toggleMicPtt()")
        self.assertTrue(self.ui(page)["pending"])
        self.settle_all(page, f"__reject({i})")
        ui = self.ui(page)
        self.assertFalse(ui["pending"])
        self.assertTrue(ui["refused"])
        self.assertEqual(ui["msg"], "Studio Mic: Connection error — command outcome unconfirmed.")
        self.assertNotIn("not sent", ui["msg"].lower())
        self.assertEqual((ui["state"], ui["aria"]), ("off", "Studio Mic: OFF"))
        self.shot(page, "network-error-unconfirmed")
        page.clock.run_for(6_100)
        self.assertEqual(self.ui(page)["msg"], "")
        self.assertFalse(self.ui(page)["refused"])

    def test_network_rejection_while_live_keeps_live_until_the_engine_says_otherwise(self):
        page = self.deferred_console(mic_live=True)
        i, = self.start(page, "toggleMicPtt()")              # asks for OFF
        self.settle_all(page, f"__reject({i})")
        ui = self.ui(page)
        self.assertEqual((ui["state"], ui["aria"], ui["active"]), ("on", "Studio Mic: LIVE", True))
        self.assertIn("command outcome unconfirmed", ui["msg"])
        self.poll_now(page, mic_live=True)                    # engine: still LIVE
        self.assertIn("command outcome unconfirmed", self.ui(page)["msg"])
        self.assertEqual(self.ui(page)["aria"], "Studio Mic: LIVE")
        self.shot(page, "network-error-while-live")
        self.poll_now(page, mic_live=False)                   # engine now reports the requested state
        ui = self.ui(page)
        self.assertEqual((ui["state"], ui["msg"], ui["refused"]), ("off", "", False))

    def test_older_failure_newer_success_in_either_arrival_order(self):
        for order in ("newer-first", "older-first"):
            with self.subTest(order=order):
                page = self.deferred_console()
                old, new = self.start(page, "toggleMicPtt()", "toggleMicPtt()")
                steps = ([f"__respond({new}, 200)", f"__respond({old}, 403)"] if order == "newer-first"
                         else [f"__respond({old}, 403)", f"__respond({new}, 200)"])
                for step in steps:
                    self.settle_all(page, step)
                ui = self.ui(page)
                self.assertEqual((ui["refused"], ui["msg"]), (False, ""), ui)
                self.assertTrue(ui["pending"])        # accepted: busy until the next poll
                self.poll_now(page, mic_live=True)
                ui = self.ui(page)
                self.assertEqual((ui["pending"], ui["state"]), (False, "on"))

    def test_older_success_newer_failure_in_either_arrival_order(self):
        for order in ("newer-first", "older-first"):
            with self.subTest(order=order):
                page = self.deferred_console()
                old, new = self.start(page, "toggleMicPtt()", "toggleMicPtt()")
                steps = ([f"__respond({new}, 403)", f"__respond({old}, 200)"] if order == "newer-first"
                         else [f"__respond({old}, 200)", f"__respond({new}, 403)"])
                for step in steps:
                    self.settle_all(page, step)
                ui = self.ui(page)
                self.assertTrue(ui["refused"], ui)
                self.assertEqual(ui["msg"], "Studio Mic request refused: permission denied (role or schedule).")
                self.assertFalse(ui["pending"])

    def test_repeated_success_clears_an_older_warning(self):
        page = self.deferred_console()
        i, = self.start(page, "toggleMicPtt()")
        self.settle_all(page, f"__respond({i}, 403)")
        self.assertTrue(self.ui(page)["refused"])
        for _ in range(2):
            i, = self.start(page, "toggleMicPtt()")
            self.settle_all(page, f"__respond({i}, 200)")
            ui = self.ui(page)
            self.assertEqual((ui["refused"], ui["msg"]), (False, ""))

    def test_repeated_failure_keeps_the_newest_warning_for_its_full_period(self):
        page = self.deferred_console()
        i, = self.start(page, "toggleMicPtt()")
        self.settle_all(page, f"__respond({i}, 403)")
        page.clock.run_for(3_000)
        i, = self.start(page, "toggleMicPtt()")
        self.settle_all(page, f"__reject({i})")
        self.assertIn("command outcome unconfirmed", self.ui(page)["msg"])
        page.clock.run_for(3_100)        # the FIRST warning's 6s would end here
        self.assertIn("command outcome unconfirmed", self.ui(page)["msg"])
        page.clock.run_for(3_000)        # the second warning's own 6s
        self.assertEqual(self.ui(page)["msg"], "")

    def test_overlapping_studio_and_remote_mic_requests_are_independent(self):
        page = self.deferred_console(remote_dj_connected=True)
        studio, remote = self.start(page, "toggleMicPtt()", "toggleRemoteDjGate()")
        self.settle_all(page, f"__reject({studio})")
        page.clock.run_for(3_000)
        self.settle_all(page, f"__respond({remote}, 403)")
        msg = self.ui(page)["msg"]
        self.assertIn("Studio Mic: Connection error — command outcome unconfirmed.", msg)
        self.assertIn("Remote Mic request refused: permission denied (role or schedule).", msg)
        self.assertTrue(self.ui(page, "remoteDjGateBtn")["refused"])
        self.shot(page, "overlapping-mic-warnings")
        page.clock.run_for(3_100)        # Studio's timer ends; Remote's must survive it
        msg = self.ui(page)["msg"]
        self.assertNotIn("Studio Mic", msg)
        self.assertIn("Remote Mic request refused", msg)
        self.assertFalse(self.ui(page)["refused"])
        self.assertTrue(self.ui(page, "remoteDjGateBtn")["refused"])
        page.clock.run_for(3_000)
        self.assertEqual(self.ui(page)["msg"], "")

    def test_status_poll_during_a_request_keeps_it_pending(self):
        page = self.deferred_console()
        i, = self.start(page, "toggleMicPtt()")            # asks for LIVE
        self.poll_now(page)                                 # engine still OFF, request in flight
        ui = self.ui(page)
        self.assertEqual((ui["pending"], ui["state"]), (True, "off"))
        self.settle_all(page, f"__respond({i}, 200)")
        ui = self.ui(page)
        self.assertTrue(ui["pending"])                      # queued, not yet confirmed
        self.assertEqual(ui["state"], "off")                # HTTP success is not execution
        self.poll_now(page, mic_live=True)
        ui = self.ui(page)
        self.assertEqual((ui["pending"], ui["state"], ui["msg"]), (False, "on", ""))

    def test_late_deck_responses_cannot_overwrite_newer_feedback(self):
        page = self.deferred_console()
        msg = "() => document.getElementById('deckMsg-A').textContent"
        old, new = self.start(page, "deckCommand('A', 'pause')", "deckCommand('A', 'eject')")
        self.settle_all(page, f"__respond({new}, 200)")
        self.settle_all(page, f"__respond({old}, 403)")
        self.assertEqual(page.evaluate(msg), "")
        old, new = self.start(page, "deckCommand('A', 'pause')", "deckCommand('A', 'eject')")
        self.settle_all(page, f"__respond({new}, 503, JSON.stringify({{error: 'engine command dispatch failed: full'}}))")
        self.settle_all(page, f"__respond({old}, 200)")
        self.assertEqual(page.evaluate(msg), "Eject not queued: engine command queue unavailable")
        self.assertEqual(page.evaluate("() => document.getElementById('deckMsg-B').textContent"), "")
        i, = self.start(page, "deckCommand('A', 'pause')")   # a newer ACCEPTED request clears it
        self.settle_all(page, f"__respond({i}, 200)")
        self.assertEqual(page.evaluate(msg), "")

    def test_a_superseded_seek_never_moves_the_playhead(self):
        page = self.deferred_console()
        old, new = self.start(page, "seekTo('A', 30)", "seekTo('A', 90)")
        self.settle_all(page, f"__respond({new}, 403)")
        self.settle_all(page, f"__respond({old}, 200)")
        self.assertEqual(page.evaluate("() => deckState.A.localPosition"), POSITION)
        self.assertEqual(page.evaluate("() => document.getElementById('deckMsg-A').textContent"),
                         "Seek refused: permission denied (role or schedule)")
        i, = self.start(page, "seekTo('A', 120)")
        self.settle_all(page, f"__respond({i}, 200)")
        self.assertEqual(page.evaluate("() => deckState.A.localPosition"), 120)

    def test_command_outcome_classification(self):
        page = self.deferred_console()
        cases = (
            ("__respond(I, 200)", "accepted", None),
            ("__respond(I, 200, 'not json')", "unconfirmed",
             "Unexpected server response (HTTP 200) — command outcome unconfirmed"),
            ("__respond(I, 401)", "refused", "refused: permission denied (role or schedule)"),
            ("__respond(I, 403)", "refused", "refused: permission denied (role or schedule)"),
            ("__respond(I, 400)", "rejected", "rejected by the server (HTTP 400)"),
            ("__respond(I, 503, JSON.stringify({error: 'engine command dispatch failed: x'}))", "unqueued",
             "not queued: engine command queue unavailable"),
            ("__respond(I, 503, '<html>proxy</html>')", "unconfirmed",
             "Server error (HTTP 503) — command outcome unconfirmed"),
            ("__respond(I, 502)", "unconfirmed", "Server error (HTTP 502) — command outcome unconfirmed"),
            ("__reject(I)", "unconfirmed", "Connection error — command outcome unconfirmed"),
        )
        for action, kind, text in cases:
            with self.subTest(action=action):
                page.evaluate("() => { window.__out = postEngineCommand('/api/engine/manual-mode/', {active: true}); }")
                i = page.evaluate("() => window.__deferred.length - 1")
                result = page.evaluate("async (a) => { (0, eval)(a); return await window.__out; }",
                                       action.replace("(I", f"({i}"))
                self.assertEqual(result.get("kind"), kind, result)
                self.assertEqual(result.get("text"), text, result)

    def test_signed_out_redirect_is_a_definite_refusal(self):
        page = self.open_console(self.superuser)
        self.post_status = {"/api/engine/mic-ptt/": 302}
        result = page.evaluate("async () => await postEngineCommand('/api/engine/mic-ptt/', {active: true})")
        self.assertEqual(result, {"kind": "refused", "text": "refused: signed out — reload and sign in"})

    def test_stale_connection_presentation(self):
        page = self.open_console(self.superuser)
        self.status_fails = True
        page.clock.run_for(3_500)
        page.wait_for_function("() => document.getElementById('consoleRoot').classList.contains('connection-stale')")
        self.assertTrue(page.locator("#connectionStaleBanner").is_visible())
        outline = page.evaluate(
            "() => getComputedStyle(document.querySelector('#micPttBtn .ops-icon-chip')).outlineStyle")
        self.assertEqual(outline, "dashed")
        self.shot(page, "stale-connection")

    def test_light_theme_palette(self):
        from library.models import UITheme
        theme = UITheme.load()
        theme.bg_dark, theme.bg_darker, theme.panel_bg = "#f8fafc", "#e2e8f0", "#ffffff"
        theme.text_main, theme.text_muted = "#0f172a", "#475569"
        theme.border_subtle, theme.accent = "rgba(15, 23, 42, 0.25)", "#15803d"
        theme.deck_text_shadow_color = "rgba(255, 255, 255, 0.0)"
        theme.save()
        self.state = engine_state(mic_live=False, remote_dj_connected=True, remote_dj_live=True)
        page = self.open_console(self.superuser)
        self.assertEqual(page.evaluate("() => getComputedStyle(document.body).backgroundColor"),
                         "rgb(226, 232, 240)")
        self.assert_playhead(page, "A", POSITION)
        self.shot(page, "full-desktop-light-theme")
        page.set_viewport_size(MOBILE_PORTRAIT)
        page.wait_for_function(
            "() => { const c = document.getElementById('npWaveform-A');"
            " return c.width === Math.round(c.getBoundingClientRect().width); }")
        self.shot(page, "full-mobile-light-theme")

    # -- Roadmap 2.23 Phase 1B: Remote DJ connection identity ---------------
    # The browser receives /api/engine/status/ from fixtures here, shaped as
    # library.views.api_engine_status returns it (test_remote_dj_identity_2_23
    # covers the view itself). No WebRTC connection is ever made: "this
    # browser owns it" is simulated by giving the page a stand-in peer
    # connection and its own attempt id, exactly the two things it holds.
    OWNER_ATTEMPT = "owner-attempt-0123456789ab"

    @staticmethod
    def browser_key(attempt_id):
        return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]

    def owner(self, name="Jane Smith", *, current=False, attempt=None, identity="known"):
        return {"identity": identity, "display_name": name if identity == "known" else None,
                "is_current_user": current,
                "browser_key": self.browser_key(attempt or "someone-elses-attempt-xyz")}

    def connected(self, owner, **overrides):
        state = dict(remote_dj_connected=True, remote_dj_owner=owner,
                     remote_dj_quality={"overall": "good"})
        state.update(overrides)
        return engine_state(**state)

    def connect_ui(self, page):
        return page.evaluate("""() => {
          const b = document.getElementById('remoteDjConnectBtn');
          const action = document.getElementById('remoteDjConnectAction');
          const r = b.getBoundingClientRect();
          return {action: action.textContent, status: document.getElementById('remoteDjConnectStatus').textContent,
                  aria: b.getAttribute('aria-label'), title: b.title, disabled: b.disabled,
                  width: r.width, height: r.height, top: r.top,
                  actionClipped: action.scrollWidth > action.clientWidth};
        }""")

    def become_owner_browser(self, page, attempt):
        page.evaluate("""async (attempt) => {
          rdjPc = { close() {}, iceConnectionState: 'connected' };
          rdjAttemptId = attempt;
          rdjBrowserKey = await rdjDeriveBrowserKey(attempt);
        }""", attempt)

    def test_observer_sees_who_is_connected(self):
        self.state = self.connected(self.owner("Jane Smith"))
        page = self.open_console(self.superuser)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"], ui["disabled"]), ("Jane Smith", "Other browser · Good", True))
        self.assertEqual(ui["aria"], "Remote DJ connected — Jane Smith (in another browser) — link quality Good")
        self.assertIn("Only that browser can disconnect it", ui["title"])
        # .playnow-btn animates opacity (0.12s, real time -- not the fake
        # clock): wait for the settled value rather than sampling mid-fade.
        page.wait_for_function(
            "() => getComputedStyle(document.getElementById('remoteDjConnectBtn')).opacity === '0.75'",
            timeout=5_000)
        self.shot(page, "rdj-identity-observer")

    def test_same_account_in_another_browser_is_an_observer(self):
        self.state = self.connected(self.owner("Jane Smith", current=True))
        page = self.open_console(self.superuser)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"], ui["disabled"]),
                         ("Jane Smith (you)", "Other browser · Good", True))
        self.assertIn("your account, in another browser", ui["aria"])
        self.shot(page, "rdj-identity-same-account-other-browser")

    def test_owning_browser_is_recognised_by_its_own_attempt_key(self):
        self.state = self.connected(self.owner("Jane Smith", current=True, attempt=self.OWNER_ATTEMPT))
        page = self.open_console(self.superuser)
        self.become_owner_browser(page, self.OWNER_ATTEMPT)
        self.poll_now(page)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"], ui["disabled"]), ("Disconnect", "You · Good", False))
        self.assertIn("you are connected in this browser as Jane Smith", ui["aria"])
        self.shot(page, "rdj-identity-owner-browser")
        # A page holding a peer connection whose key does NOT match is not
        # told "You": matching user identity is never proof of ownership.
        self.become_owner_browser(page, "a-different-attempt-000000")
        self.poll_now(page)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"]), ("Disconnect", "Good"))
        self.assertNotIn("you are connected", ui["aria"])

    def test_owner_disconnect_is_local_and_unchanged(self):
        self.state = self.connected(self.owner(attempt=self.OWNER_ATTEMPT))
        page = self.open_console(self.superuser)
        self.become_owner_browser(page, self.OWNER_ATTEMPT)
        self.poll_now(page)
        page.locator("#remoteDjConnectBtn").click()
        # rdjAttemptId is kept for diagnostics, as in r0116; ownership is gone.
        self.assertEqual(page.evaluate("() => [rdjPc, rdjBrowserKey]"), [None, None])
        ui = self.connect_ui(page)
        self.assertNotIn("You", ui["status"])
        self.assertEqual(self.posts, [])   # Disconnect is local WebRTC teardown, no engine command

    def test_unknown_or_withheld_identity_stays_generic(self):
        for name, owner, status in (("unavailable", self.owner(identity="unavailable"), "Other browser · Good"),
                                    ("withheld", None, "Good")):
            with self.subTest(owner=name):
                self.state = self.connected(owner)
                page = self.open_console(self.superuser)
                ui = self.connect_ui(page)
                self.assertEqual((ui["action"], ui["status"]), ("Remote in use", status))
                self.assertNotIn("Jane", ui["aria"] + ui["title"])

    def test_disconnected_shows_no_identity(self):
        self.state = engine_state(remote_dj_connected=False, remote_dj_owner=None)
        page = self.open_console(self.superuser)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"], ui["title"]), ("Connect", "Offline", ""))

    def test_owner_changes_and_expiry_leave_no_stale_name(self):
        self.state = self.connected(self.owner("Jane Smith"))
        page = self.open_console(self.superuser)
        self.assertEqual(self.connect_ui(page)["action"], "Jane Smith")
        # A's connection degrades: engine reconnect grace.
        self.poll_now(page, **{**self.connected(self.owner("Jane Smith"), remote_dj_reconnecting=True)})
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"]), ("Jane Smith", "Reconnecting…"))
        self.shot(page, "rdj-identity-reconnecting")
        # The server expires the abandoned session.
        self.poll_now(page, remote_dj_connected=False, remote_dj_owner=None)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"]), ("Connect", "Offline"))
        self.assertNotIn("Jane", ui["aria"] + ui["title"])
        # User B connects.
        self.poll_now(page, **{**self.connected(self.owner("Bob Jones"))})
        ui = self.connect_ui(page)
        self.assertEqual(ui["action"], "Bob Jones")
        self.assertNotIn("Jane", ui["aria"] + ui["title"])

    def test_a_late_older_status_response_cannot_resurrect_an_owner(self):
        self.state = self.connected(self.owner("Jane Smith"))
        page = self.open_console(self.superuser)
        page.evaluate("""() => {
          const realFetch = window.fetch.bind(window);
          window.__statusQueue = [];
          window.fetch = (url, opts) => String(url).endsWith('/api/engine/status/')
            ? new Promise(resolve => window.__statusQueue.push(resolve))
            : realFetch(url, opts);
          window.__answerStatus = (i, body) => window.__statusQueue[i](
            new Response(JSON.stringify(body), {status: 200, headers: {'Content-Type': 'application/json'}}));
          window.__polls = [poll(), poll()];      // older request 0, newer request 1
        }""")
        jane = self.connected(self.owner("Jane Smith"))
        gone = engine_state(remote_dj_connected=False, remote_dj_owner=None)
        page.evaluate("async ([b]) => { __answerStatus(1, b); await __polls[1]; }", [gone])
        page.evaluate("async ([b]) => { __answerStatus(0, b); await __polls[0]; }", [jane])
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"]), ("Connect", "Offline"))
        self.assertNotIn("Jane", ui["aria"] + ui["title"])

    def test_status_outage_withholds_identity_until_fresh_status(self):
        self.state = self.connected(self.owner("Jane Smith"))
        page = self.open_console(self.superuser)
        self.status_fails = True
        page.clock.run_for(3_500)
        page.wait_for_function("() => document.getElementById('consoleRoot').classList.contains('connection-stale')")
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"]), ("Remote in use", "Status unavailable"))
        self.assertNotIn("Jane", ui["aria"] + ui["title"])
        self.shot(page, "rdj-identity-status-stale")
        self.status_fails = False
        page.clock.run_for(1_100)
        page.wait_for_function("() => document.getElementById('remoteDjConnectAction').textContent === 'Jane Smith'")

    def test_reload_while_another_user_is_connected(self):
        self.state = self.connected(self.owner("Jane Smith"))
        page = self.open_console(self.superuser)
        page.reload()
        page.clock.run_for(1_100)
        page.wait_for_function("() => document.getElementById('remoteDjConnectAction').textContent === 'Jane Smith'")

    def test_long_names_never_resize_the_control(self):
        long_name = "Maximiliana Alexandra Featherstonehaugh-Cholmondeley Jr."
        for viewport in (DESKTOP, MOBILE_PORTRAIT):
            with self.subTest(viewport=viewport["width"]):
                self.state = self.connected(self.owner("Jo"))
                page = self.open_console(self.superuser, viewport=viewport)
                short = self.connect_ui(page)
                self.poll_now(page, **{**self.connected(self.owner(long_name))})
                long = self.connect_ui(page)
                self.assertEqual((long["width"], long["height"], long["top"]),
                                 (short["width"], short["height"], short["top"]))
                self.assertTrue(long["actionClipped"])
                self.assertIn(long_name, long["aria"])
                self.assertIn(long_name, long["title"])
                self.no_horizontal_overflow(page)
                self.shot(page, f"rdj-identity-long-name-{viewport['width']}")

    def test_observer_quality_is_the_engine_published_measurement(self):
        self.state = self.connected(self.owner("Jane Smith"), remote_dj_quality={"overall": "fair"})
        page = self.open_console(self.superuser)
        self.assertEqual(self.connect_ui(page)["status"], "Other browser · Fair")
        self.poll_now(page, **{**self.connected(self.owner("Jane Smith"), remote_dj_quality=None)})
        self.assertEqual(self.connect_ui(page)["status"], "Other browser · Initializing…")

    def test_remote_host_page_shows_the_connected_dj(self):
        self.state = self.connected(self.owner("Jane Smith"), live="B")
        page = self.open_console(self.host, path="/remote-dj/", viewport=MOBILE_PORTRAIT)
        ui = self.connect_ui(page)
        self.assertEqual((ui["action"], ui["status"], ui["disabled"]), ("Jane Smith", "Other browser · Good", True))
        self.no_horizontal_overflow(page)
        self.shot(page, "rdj-identity-remote-dj-mobile-observer")

    # -- Remote DJ ----------------------------------------------------------
    def test_remote_dj_mobile_live_deck_first_in_both_slot_arrangements(self):
        for live, preview in (("A", "B"), ("B", "A")):
            with self.subTest(live=live):
                self.state = engine_state(live=live)
                page = self.open_console(self.host, path="/remote-dj/", viewport=MOBILE_PORTRAIT, dpr=2)
                live_box, preview_box = self.box(page, f"#deck-{live}"), self.box(page, f"#deck-{preview}")
                self.assertLess(live_box["top"], preview_box["top"])
                self.assertLess(preview_box["height"], live_box["height"] * 0.6, "preview card stays compact")
                self.assertFalse(page.locator(f"#npWaveform-{preview}").is_visible())
                self.assert_playhead(page, live, POSITION)
                self.assertEqual(self.ops_ids(page), ["manualModeBtn", "remoteDjGateBtn", "remoteDjConnectBtn"])
                row = self.box(page, ".playnow-bar")
                connect = self.box(page, "#remoteDjConnectBtn")
                self.assertAlmostEqual(connect["width"], row["width"], delta=2)  # full second row
                self.no_horizontal_overflow(page)
                self.shot(page, f"remote-dj-mobile-deck{live}-live")

    def test_remote_host_gets_no_deck_transport_or_seek(self):
        for name, viewport in (("desktop", DESKTOP), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(viewport=name):
                page = self.open_console(self.host, path="/remote-dj/", viewport=viewport)
                self.assertEqual(page.locator(".deck-transport-btn").count(), 0)
                self.assertEqual(page.locator("#lineInBtn").count(), 0)
                self.assertEqual(page.locator("#micPttBtn").count(), 0)
                canvas = page.locator("#npWaveform-A")
                self.assertFalse(canvas.evaluate("c => c.classList.contains('seekable')"))
                box = self.box(page, "#npWaveform-A")
                page.mouse.click(box["left"] + box["width"] / 2, box["top"] + 20)
                page.clock.run_for(200)
                self.assertEqual(self.posts, [])
                self.shot(page, f"remote-dj-{name}-remote-host-no-transport")

    def test_transport_holder_on_remote_dj_sees_live_deck_controls_only(self):
        page = self.open_console(self.superuser, path="/remote-dj/")
        self.assertTrue(page.locator("#ejectBtn-A").is_visible())
        self.assertFalse(page.locator("#ejectBtn-B").is_visible())   # preview deck: CSS-hidden
        self.assertFalse(page.locator("#npWaveform-A").evaluate("c => c.classList.contains('seekable')"))
        self.shot(page, "remote-dj-desktop-transport-holder")

    # -- Start Next layout preparation (structure only) ----------------------
    def _inject_dev_placeholder(self, page):
        """TEST-ONLY: Phase 1 ships the slot hidden and unwired. To prove the
        reserved geometry, the test un-hides it with an inert placeholder."""
        page.evaluate("""() => {
          const slot = document.getElementById('startNextSlot');
          slot.hidden = false;
          slot.innerHTML = '<div style="padding:.5rem .8rem;border:1px dashed currentColor;'
            + 'border-radius:8px;font-size:.75rem;opacity:.7">layout placeholder (test-injected)</div>';
          document.getElementById('decksRow').classList.add('has-start-next');
        }""")

    def test_start_next_slot_geometry(self):
        page = self.open_console(self.superuser)
        self._inject_dev_placeholder(page)
        a, slot, b = (self.box(page, s) for s in ("#deck-A", "#startNextSlot", "#deck-B"))
        self.assertLess(a["right"], slot["left"])
        self.assertLess(slot["right"], b["left"])
        self.shot(page, "layout-prep-start-next-desktop")

        page.set_viewport_size(MOBILE_PORTRAIT)
        page.wait_for_function("() => document.getElementById('deck-B').getBoundingClientRect().top >"
                               " document.getElementById('startNextSlot').getBoundingClientRect().bottom - 1")
        a, slot, b = (self.box(page, s) for s in ("#deck-A", "#startNextSlot", "#deck-B"))
        self.assertLessEqual(a["bottom"], slot["top"])
        self.assertLessEqual(slot["bottom"], b["top"])
        self.shot(page, "layout-prep-start-next-mobile")

        for live, preview in (("A", "B"), ("B", "A")):
            with self.subTest(remote_dj_live=live):
                self.state = engine_state(live=live)
                page = self.open_console(self.host, path="/remote-dj/", viewport=MOBILE_PORTRAIT)
                self._inject_dev_placeholder(page)
                top, slot, bottom = (self.box(page, s) for s in (f"#deck-{live}", "#startNextSlot", f"#deck-{preview}"))
                self.assertLessEqual(top["bottom"], slot["top"])
                self.assertLessEqual(slot["bottom"], bottom["top"])
                self.shot(page, f"layout-prep-start-next-remote-dj-mobile-deck{live}-live")
        self.assertEqual(self.posts, [])


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ConsoleChromiumTests(_ConsoleBrowserBase, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ConsoleFirefoxTests(_ConsoleBrowserBase, StaticLiveServerTestCase):
    BROWSER = "firefox"
