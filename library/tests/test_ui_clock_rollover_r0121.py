"""r0121 maintenance -- no ':60' / ':60.0' clock displays.

Every edited formatter is executed from its REAL source text (extracted from
the template or static file, not a copy) in headless Chromium, across
minute and hour boundaries, negative countdowns and invalid values; a sweep
proves no finite input renders a 60 in the seconds field. One live-style
Console smoke drives the real dashboard (deck elapsed / remaining / total,
the Coming Up card and the queue table) through the existing 2.23 browser
harness, whose fixtures never reach the engine.
"""
import re
import unittest
from pathlib import Path

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import SimpleTestCase, override_settings

from library.tests.test_console_browser_2_23 import _ConsoleBrowserBase, engine_state, sync_playwright

REPO = Path(__file__).resolve().parents[2]
T = REPO / "library" / "templates" / "library"
WORKSTATION = REPO / "production" / "static" / "production" / "iportal" / "workstation.js"


def block(path, start):
    """The brace-balanced block of `path` that begins at the unique `start`."""
    text = path.read_text()
    assert text.count(start) == 1, (path.name, start)
    i = text.index(start)
    depth, j = 0, text.index("{", i)
    while True:
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1


def function(path, name, params):
    return block(path, f"function {name}({params}) {{")


WHOLE = {           # helper -> (file, function name, parameter)
    "dashboard.fmtDur": (T / "dashboard.html", "fmtDur", "sec"),
    "library.fmtDuration": (T / "library.html", "fmtDuration", "sec"),
    "playlists.fmtDuration": (T / "playlists.html", "fmtDuration", "sec"),
    "logs.fmtDuration": (T / "logs.html", "fmtDuration", "sec"),
    "rotations.fmtDuration": (T / "rotations.html", "fmtDuration", "sec"),
    "import.fmtDuration": (T / "import.html", "fmtDuration", "sec"),
}

ROUNDING = {59.4: "0:59", 59.5: "1:00", 59.9: "1:00", 60: "1:00", 119.6: "2:00", 119.4: "1:59",
            3599.6: "60:00", 3600: "60:00", 7199.5: "120:00", 1.0: "0:01", 9.5: "0:10"}


@unittest.skipIf(sync_playwright is None, "Playwright not available")
class FormatterSourceTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.page = cls.browser.new_page()
        cls.page.set_content('<span id="displayDuration"></span>')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        super().tearDownClass()

    def run_fn(self, source, name, values):
        """Results of the real source's `name` for every value (JS `undefined`
        is sent as the string marker and converted in the page)."""
        return self.page.evaluate(
            "([src, name, values]) => { const fn = new Function(src + '; return ' + name + ';')();"
            " return values.map(v => fn(v === '__undefined__' ? undefined : v === '__nan__' ? NaN : v)); }",
            [source, name, values])

    def expect(self, label, source, name, cases):
        values, wanted = list(cases), list(cases.values())
        self.assertEqual(dict(zip(values, self.run_fn(source, name, values))), dict(zip(values, wanted)), label)

    def test_whole_second_helpers_round_first_then_split(self):
        for label, (path, name, param) in WHOLE.items():
            with self.subTest(label):
                self.expect(label, function(path, name, param), name, ROUNDING)

    def test_each_helper_keeps_its_placeholders_and_sign(self):
        dashboard = function(*WHOLE["dashboard.fmtDur"])
        self.expect("dashboard", dashboard, "fmtDur", {
            0: "0:00", None: "0:00", "__undefined__": "0:00", "__nan__": "0:00",
            -119.6: "-2:00", -59.5: "-1:00", -59.4: "-0:59", -3599.6: "-60:00",
        })
        for label in ("library.fmtDuration", "playlists.fmtDuration", "logs.fmtDuration", "rotations.fmtDuration"):
            with self.subTest(label):
                self.expect(label, function(*WHOLE[label]), "fmtDuration",
                            {0: "0:00", None: "", "__undefined__": "", "__nan__": ""})
        self.expect("import", function(*WHOLE["import.fmtDuration"]), "fmtDuration",
                    {0: "-", None: "-", "__undefined__": "-", "__nan__": "-"})

    def test_reports_keeps_its_under_a_minute_convention_and_fixes_the_minute_split(self):
        source = function(T / "reports.html", "fmtTime", "seconds")
        self.expect("reports", source, "fmtTime", {
            None: "—", "__undefined__": "—", "__nan__": "—", 0: "0.0 sec", 59.4: "59.4 sec",
            60: "01:00", 119.6: "02:00", 119.4: "01:59", 3599.6: "60:00", 3600: "60:00",
        })

    def test_track_detail_duration_displays(self):
        initial = block(T / "track_detail.html", "if (dur !== null) {")
        reanalysis = block(T / "track_detail.html", "if (data.duration_seconds != null) {")
        for label, body in (("initial", f"const dur = arguments[0]; {initial}"),
                            ("reanalysis", "const data = {duration_seconds: arguments[0]};"
                                           " const durEl = document.getElementById('displayDuration');"
                                           f" {reanalysis}")):
            with self.subTest(label):
                source = (f"function show() {{ document.getElementById('displayDuration').textContent = '';"
                          f" {body} return document.getElementById('displayDuration').textContent; }}")
                self.expect(label, source, "show", {59.5: "1:00", 119.6: "2:00", 3599.6: "60:00", 59.4: "0:59"})

    def test_iportal_tenths_round_first_then_split(self):
        source = function(WORKSTATION, "fmt", "seconds")
        self.expect("workstation.fmt", source, "fmt", {
            59.94: "0:59.9", 59.95: "1:00.0", 59.96: "1:00.0", 119.96: "2:00.0", 9.96: "0:10.0",
            9.94: "0:09.9", 0: "0:00.0", 0.04: "0:00.0", 0.05: "0:00.1", 3599.96: "60:00.0",
            -1: "0:00.0", None: "0:00.0", "__undefined__": "0:00.0", "__nan__": "0:00.0",
            "12.34": "0:12.3",
        })

    def test_no_finite_input_ever_shows_a_sixty(self):
        whole_values = [round(i * 0.05, 2) for i in range(0, 7300 * 20 + 1, 7)]
        for label, (path, name, param) in WHOLE.items():
            with self.subTest(label):
                out = self.run_fn(function(path, name, param), name, whole_values)
                bad = [(v, o) for v, o in zip(whole_values, out) if re.search(r":(6\d|[7-9]\d)\b", o)]
                self.assertEqual(bad[:5], [])
        reports = self.run_fn(function(T / "reports.html", "fmtTime", "seconds"), "fmtTime", whole_values)
        self.assertEqual([o for o in reports if re.search(r":(6\d|[7-9]\d)$", o)], [])
        tenths = [round(i * 0.01, 2) for i in range(0, 40000, 3)]
        out = self.run_fn(function(WORKSTATION, "fmt", "seconds"), "fmt", tenths)
        bad = [(v, o) for v, o in zip(tenths, out) if not re.fullmatch(r"\d+:[0-5]\d\.\d", o)]
        self.assertEqual(bad[:5], [])


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class DashboardClockRolloverChromiumTests(_ConsoleBrowserBase, StaticLiveServerTestCase):
    """Live-style Console: every value sits half a second before a minute."""
    BROWSER = "chromium"

    # Only this smoke: the harness mixin also carries the Console's own tests,
    # which their own module already runs.
    locals().update({name: None for name in dir(_ConsoleBrowserBase) if name.startswith("test_")})

    def test_deck_coming_up_and_queue_never_show_sixty(self):
        state = engine_state(position=119.6)
        live = state["decks"]["A"]
        live.update(duration=239.6, next_start=239.6)               # remaining 120.0 -> 2:00
        up_next = state["queue"][0]
        up_next.update(duration=179.6, airtime_seconds=119.6)        # Coming Up card
        state["queue"].append({**up_next, "item_id": 9002, "duration": 59.5, "airtime_seconds": 59.5})
        self.state = state
        page = self.open_console(self.superuser)
        texts = page.evaluate(
            "() => ({elapsed: document.getElementById('npElapsed-A').textContent,"
            " remaining: document.getElementById('npCountdown-A').textContent,"
            " total: document.getElementById('npTotal-A').textContent,"
            " upNextAirtime: document.getElementById('npCountdown-B').textContent,"
            " upNextTotal: document.getElementById('npTotal-B').textContent,"
            " queue: Array.from(document.querySelectorAll('td.qt-dur')).map(td => td.textContent),"
            " body: document.body.innerText})")
        self.assertEqual((texts["elapsed"], texts["remaining"], texts["total"]), ("2:00", "2:00", "4:00"))
        self.assertEqual((texts["upNextAirtime"], texts["upNextTotal"]), ("2:00", "3:00"))
        self.assertIn("1:00", texts["queue"])
        self.assertNotRegex(texts["body"], r"\d:60\b")
        self.assertEqual(self.console_errors, [])
        self.assertEqual(self.posts, [])
