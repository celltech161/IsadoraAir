"""Console / Remote DJ log display in real browsers: the Intro column and
"Show played items" (UITheme.show_played_items).

Same harness and isolation as test_console_browser_2_23 (Playwright against
the real rendered dashboard.html; every /api/ request answered in the browser
from fixtures; engine commands patched to fail the test if reached; fake
clock). Additionally the fixture answers /api/engine/status/log/<id>/ -- the
read-only active-log history the display merges with the live queue -- and
records every history request, so "Off makes no history request" is checked
rather than assumed.
"""
import io
import json
import re
import time
import unittest
from urllib.parse import urlparse

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from library.models import UITheme
from library.tests.test_console_browser_2_23 import (
    DESKTOP, MOBILE_PORTRAIT, T0_MS, TABLET, TRACKS, _ConsoleBrowserBase, engine_state, sync_playwright,
)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

RED = "rgb(200, 60, 60)"
BLUE = "rgb(40, 90, 200)"
AIRED = T0_MS / 1000 - 900        # epoch seconds: an item that went to air 15 minutes ago
ROWS = """() => Array.from(document.querySelectorAll('#queueList tr')).map(tr => ({
  id: tr.dataset.itemId || null, state: tr.dataset.rowState || null, queueRow: tr.dataset.queueRow === '1',
  handle: !!tr.querySelector('.qt-drag-handle'), next: !!tr.querySelector('.qt-next-btn'),
  current: tr.getAttribute('aria-current') === 'true',
  cells: Array.from(tr.children).map(td => td.textContent.trim()),
}))"""


def hist(item_id, position, title, *, fill=RED, intro=None, played=None, category="Gold"):
    return {"item_id": item_id, "position": position, "track_id": 5000 + item_id, "title": title,
            "artist": f"Artist {item_id}", "duration": 200.0, "category": category, "fill_color": fill,
            "intro_until_seconds": intro, "played_at": played}


def queued(item_id, title, eta, *, fill=RED, intro=None, category="Gold"):
    return {"item_id": item_id, "track_id": 5000 + item_id, "title": title, "artist": f"Artist {item_id}",
            "duration": 200.0, "airtime_seconds": 190.0, "category": category, "format": "MP3",
            "fill_color": fill, "eta_seconds": eta, "intro_until_seconds": intro}


def console_state(*, current=903, queue=(), log_id=77, claimed=None, **overrides):
    """Deck A airing LogItem ``current`` of ``log_id``; optionally deck B holding
    ``claimed`` (a preloaded, not yet airing LogItem)."""
    state = engine_state(live="A", log_id=log_id, queue=list(queue), **overrides)
    state["decks"]["A"].update(
        log_item_id=current, playlist_log_id=log_id, log_position=3, generation=1,
        actually_playing=True, air_started_at=AIRED)
    if claimed is not None:
        state["decks"]["B"] = {
            **state["decks"]["A"], **TRACKS["B"], "slot": "B", "position": 0.0,
            "log_item_id": claimed, "log_position": 4, "generation": 2,
            "actually_playing": False, "air_started_at": None}
    return state


def standard_history():
    return [
        hist(901, 1, "Played One", played=AIRED - 600),
        hist(902, 2, "Played Two", played=AIRED - 300),
        hist(903, 3, "On Air Now", fill=BLUE, played=AIRED),
        hist(904, 4, "Upcoming Four", intro=12.4),
        hist(905, 5, "Upcoming Five", intro=None),
    ]


def standard_queue():
    return [queued(904, "Upcoming Four", 60.0, intro=12.4), queued(905, "Upcoming Five", 250.0)]


class _LogDisplayBase(_ConsoleBrowserBase):
    def setUp(self):
        super().setUp()
        self.history = standard_history()
        self.history_fails = False
        self.history_requests = []
        self.state = console_state(queue=standard_queue())

    def set_flag(self, on, **appearance):
        theme = UITheme.load()
        theme.show_played_items = on
        for field, value in appearance.items():        # r0139: played_item_darkening, current_item_outline_color
            setattr(theme, field, value)
        theme.save()

    def _route(self, route, request):
        path = urlparse(request.url).path
        if request.method == "GET" and path.startswith("/api/engine/status/log/"):
            self.history_requests.append(path)
            if self.history_fails:
                return route.fulfill(status=500, content_type="application/json", body="{}")
            log_id = int(path.rstrip("/").rsplit("/", 1)[1])
            return route.fulfill(content_type="application/json",
                                 body=json.dumps({"log_id": log_id, "items": self.history}))
        return super()._route(route, request)

    def console(self, *, flag=True, user=None, path="/", appearance=None, **kw):
        self.set_flag(flag, **(appearance or {}))
        page = self.open_console(user or self.superuser, path=path, **kw)
        if flag:
            self.wait_rows(page)
        return page

    def wait_rows(self, page, count=None):
        page.wait_for_function(
            "(n) => { const r = document.querySelectorAll('#queueList tr[data-row-state]');"
            " return n === null ? r.length > 0 : r.length === n; }", arg=count, timeout=10_000)

    def rows(self, page):
        return page.evaluate(ROWS)

    def states(self, page):
        return [(r["id"], r["state"]) for r in self.rows(page)]

    def refresh(self, page, state=None, *, history=None):
        """Apply a new engine snapshot (and optionally new history) as the next poll.
        The history fetch is rate-limited on the page's own (fake) clock."""
        if state is not None:
            self.state = state
        if history is not None:
            self.history = history
        page.clock.run_for(2_500)
        page.wait_for_function("() => typeof statusInFlightSeq === 'undefined' || statusInFlightSeq === 0")
        page.evaluate("async () => { await poll(); }")

    def wait_states(self, page, expected):
        try:
            page.wait_for_function(
                "(want) => JSON.stringify(Array.from(document.querySelectorAll('#queueList tr[data-row-state]'))"
                ".map(r => [r.dataset.itemId, r.dataset.rowState])) === JSON.stringify(want)",
                arg=[[str(i), s] for i, s in expected], timeout=10_000)
        except Exception:
            self.fail(f"rows never became {expected}; saw {self.states(page)}; errors={self.console_errors}")

    def pixel(self, page, x, y):
        shot = page.screenshot()
        return Image.open(io.BytesIO(shot)).convert("RGB").getpixel((int(x), int(y)))

    def reveal_queue(self, page):
        page.evaluate("() => document.getElementById('comingUpCard').scrollIntoView({block: 'center'})")

    def wait_post(self, page, path, timeout=10):
        """Playwright's sync API only dispatches the route handler (which records the POST)
        while the test is calling into it, so wait with page.wait_for_timeout, never time.sleep."""
        end = time.time() + timeout
        while time.time() < end:
            hits = [body for p, body in self.posts if p == path]
            if hits:
                return hits
            page.wait_for_timeout(50)
        self.fail(f"no POST to {path}; posts={self.posts}")

    # ---------------------------------------------------------------- Intro
    def test_intro_column_formats_populated_null_and_zero(self):
        self.state = console_state(queue=[
            queued(904, "Populated", 10.0, intro=12.4), queued(905, "Missing", 200.0, intro=None),
            queued(906, "Zero", 400.0, intro=0.0), queued(907, "Minute", 600.0, intro=65.0),
            queued(908, "Rounds Up", 800.0, intro=59.6)])
        for flag in (False, True):
            with self.subTest(show_played=flag):
                page = self.console(flag=flag)
                upcoming = [r for r in self.rows(page) if r["state"] in (None, "upcoming")]
                header = page.evaluate("() => Array.from(document.querySelectorAll('.queue-table th')).map(t => t.textContent.trim())")
                self.assertEqual(header[6], "Intro")
                self.assertEqual(len(header), 9)
                self.assertEqual([r["cells"][6] for r in upcoming], ["0:12", "", "0:00", "1:05", "1:00"])
                self.assertEqual([r["cells"][5] for r in upcoming][0], "3:20")     # Dur still its own column
                self.assertEqual(self.console_errors, [])

    def test_intro_appears_in_the_remote_dj_page_too(self):
        page = self.console(flag=False, user=self.host, path="/remote-dj/")
        self.assertEqual([r["cells"][6] for r in self.rows(page)], ["0:12", ""])
        self.assertEqual(self.console_errors, [])
        self.assertEqual(self.posts, [])

    # ------------------------------------------------------------- Off mode
    def test_off_is_the_r0137_queue_only_and_never_asks_for_history(self):
        page = self.console(flag=False)
        page.wait_for_function("() => document.querySelectorAll('#queueList tr[data-item-id]').length === 2")
        self.refresh(page)
        self.assertEqual(self.history_requests, [])
        rows = self.rows(page)
        self.assertEqual([(r["id"], r["handle"], r["next"], r["queueRow"]) for r in rows],
                         [("904", True, True, True), ("905", True, True, True)])
        self.assertEqual([r["cells"][1] for r in rows], ["1", "2"])          # numbered from the queue, as before
        self.assertFalse(page.evaluate("() => document.querySelector('.queue-table-wrap').classList.contains('qt-history')"))
        self.assertFalse(any(r["current"] for r in rows))
        self.assertEqual(self.console_errors, [])

    # -------------------------------------------------------------- On mode
    def test_on_shows_played_current_and_upcoming_in_order(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        rows = self.rows(page)
        self.assertEqual([r["cells"][1] for r in rows], ["1", "2", "3", "4", "5"])
        self.assertEqual([r["cells"][3] for r in rows],
                         ["Played One", "Played Two", "On Air Now", "Upcoming Four", "Upcoming Five"])
        # Aired rows show when they aired; upcoming rows keep the live ETA clock.
        self.assertTrue(all(r["cells"][7] for r in rows))
        self.assertEqual(self.history_requests, ["/api/engine/status/log/77/"])
        self.assertEqual(self.console_errors, [])
        self.reveal_queue(page)
        self.shot(page, "show-played-desktop")

    def test_only_upcoming_rows_have_controls(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        for r in self.rows(page):
            with self.subTest(item=r["id"]):
                upcoming = r["state"] == "upcoming"
                self.assertEqual((r["handle"], r["next"], r["queueRow"]), (upcoming,) * 3)

    def test_played_rows_are_darker_with_the_same_hue_and_upcoming_is_untouched(self):
        self.history[1] = hist(902, 2, "Played Two", fill=RED, played=AIRED - 300)
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        boxes = {r["id"]: self.box(page, f'#queueList tr[data-item-id="{r["id"]}"] td') for r in self.rows(page)}
        upcoming = self.pixel(page, boxes["905"]["left"] + 3, boxes["905"]["top"] + 4)
        played = self.pixel(page, boxes["901"]["left"] + 3, boxes["901"]["top"] + 4)
        self.assertEqual(upcoming, (200, 60, 60))                       # exactly its category colour, as ever
        for got, base in zip(played, upcoming):
            self.assertAlmostEqual(got, base * 0.45, delta=6)           # darker by the overlay ...
        self.assertGreater(played[0], 2 * played[1])                    # ... and still recognisably red
        self.assertLess(sum(played), 0.6 * sum(upcoming))
        text = page.evaluate("() => getComputedStyle(document.querySelector('tr.qt-played td:nth-child(6)')).color")
        self.assertEqual(text, "rgb(229, 231, 235)")                    # explicit light text on the dark overlay
        self.shot(page, "show-played-darkening")

    def test_current_row_has_a_white_outline_and_keeps_its_colour(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        self.assertEqual(page.evaluate(
            "() => getComputedStyle(document.querySelector('tr.qt-current')).backgroundColor"), "rgb(40, 90, 200)")
        cell = self.box(page, 'tr.qt-current td:nth-child(5)')
        other = self.box(page, 'tr[data-item-id="905"] td:nth-child(5)')
        x = cell["left"] + cell["width"] / 2

        def edge(box):
            return [self.pixel(page, x, box["top"] + dy) for dy in (0, 1)] + \
                   [self.pixel(page, x, box["bottom"] - dy) for dy in (1, 2)]
        self.assertTrue(any(min(px) >= 240 for px in edge(cell)[:2]), edge(cell))      # top line
        self.assertTrue(any(min(px) >= 240 for px in edge(cell)[2:]), edge(cell))      # bottom line
        self.assertFalse(any(min(px) >= 240 for px in edge(other)), edge(other))       # others: none
        # The row body is still its own blue.
        body = self.pixel(page, cell["left"] + 6, cell["top"] + cell["height"] / 2 + 8)
        self.assertGreater(body[2], body[0])
        self.shot(page, "show-played-current-outline")

    def test_claimed_or_preloaded_deck_is_not_shown_as_airing(self):
        self.state = console_state(current=903, claimed=904, queue=[queued(905, "Upcoming Five", 250.0)])
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "ondeck"), (905, "upcoming")])
        row = {r["id"]: r for r in self.rows(page)}["904"]
        self.assertEqual((row["current"], row["handle"], row["next"], row["queueRow"]), (False, False, False, False))
        self.assertEqual(self.rows(page)[3]["cells"][7], "")           # no invented air time
        # Nothing on air yet (deck A not actually playing): nothing is outlined.
        state = console_state(current=903, claimed=904, queue=[queued(905, "Upcoming Five", 250.0)])
        state["decks"]["A"]["actually_playing"] = False
        self.refresh(page, state)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "ondeck"),
                                (904, "ondeck"), (905, "upcoming")])
        self.assertFalse(any(r["current"] for r in self.rows(page)))

    def test_untrusted_or_mismatched_state_never_invents_rows_or_a_current_row(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        result = page.evaluate("""() => {
          const base = lastQueueData;
          const stale = buildQueueRows({...base, transport: 'STALE'}).map(r => r.state);
          const offline = buildQueueRows({...base, transport: 'OFFLINE'}).map(r => r.state);
          const otherHour = buildQueueRows({...base, log_id: 999}).map(r => r.state);
          const noLog = buildQueueRows({...base, log_id: null}).map(r => r.state);
          const bare = buildQueueRows({queue: [], decks: {A: null, B: null}, log_id: 77}).map(r => r.state);
          return {stale, offline, otherHour, noLog, bare};
        }""")
        self.assertNotIn("current", result["stale"])
        self.assertNotIn("current", result["offline"])
        self.assertEqual(result["otherHour"], ["upcoming", "upcoming"])
        self.assertEqual(result["noLog"], ["upcoming", "upcoming"])
        self.assertNotIn("current", result["bare"])
        self.assertEqual(self.console_errors, [])

    def test_history_failure_degrades_to_the_plain_queue(self):
        self.history_fails = True
        page = self.console(flag=True)
        page.wait_for_function("() => document.querySelectorAll('#queueList tr[data-item-id]').length === 2")
        self.wait_for_history_request(page, 1)
        self.assertEqual(self.states(page), [("904", "upcoming"), ("905", "upcoming")])
        # The next successful fetch fills the history in.
        self.history_fails = False
        self.refresh(page, history=standard_history())
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.assertEqual(self.console_errors, [])

    def wait_for_history_request(self, page, count):
        end = time.time() + 10
        while len(self.history_requests) < count and time.time() < end:
            page.wait_for_timeout(50)
        self.assertGreaterEqual(len(self.history_requests), count)

    def test_steady_polling_does_not_refetch_history_every_second(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        before = len(self.history_requests)
        for _ in range(10):                     # ten more 1 s polls, nothing changing
            page.clock.run_for(1_000)
            page.wait_for_function("() => statusInFlightSeq === 0")
        self.assertEqual(len(self.history_requests), before)
        self.assertEqual(self.console_errors, [])

    def test_live_insertion_reorder_and_advance_are_reflected(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        # Insertion: a new item Play-Next'd ahead of 904, history now lists it.
        history = standard_history()
        history.insert(3, hist(906, 4, "Inserted Six", played=None))
        for h in history[4:]:
            h["position"] += 1
        self.refresh(page, console_state(queue=[queued(906, "Inserted Six", 60.0),
                                                queued(904, "Upcoming Four", 250.0, intro=12.4),
                                                queued(905, "Upcoming Five", 450.0)]), history=history)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (906, "upcoming"), (904, "upcoming"), (905, "upcoming")])
        # Reorder: 905 moved ahead of 904 -- the live queue's order is the display's order.
        self.refresh(page, console_state(queue=[queued(906, "Inserted Six", 60.0),
                                                queued(905, "Upcoming Five", 250.0),
                                                queued(904, "Upcoming Four", 450.0, intro=12.4)]))
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (906, "upcoming"), (905, "upcoming"), (904, "upcoming")])
        # Advance: 903 finishes, 906 goes to air (played_at now set) and leaves the queue.
        history = [dict(h) for h in history]
        for h in history:
            if h["item_id"] == 906:
                h["played_at"] = AIRED + 200
        state = console_state(current=906, queue=[queued(905, "Upcoming Five", 250.0),
                                                  queued(904, "Upcoming Four", 450.0, intro=12.4)])
        self.refresh(page, state, history=history)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "played"), (906, "current"),
                                (905, "upcoming"), (904, "upcoming")])
        self.assertEqual(self.console_errors, [])

    def test_a_cancelled_upcoming_item_disappears(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        # 904 cancelled: gone from the queue, never aired: not shown as played.
        self.refresh(page, console_state(queue=[queued(905, "Upcoming Five", 60.0)]))
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"), (905, "upcoming")])

    def test_hour_adoption_shows_the_new_hour_without_carryover(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        # The next hour (log 78) is adopted while 903 -- still on deck A, from log 77 -- plays on.
        new_history = [hist(1001, 1, "Next Hour One"), hist(1002, 2, "Next Hour Two")]
        state = console_state(current=903, log_id=78,
                              queue=[queued(1001, "Next Hour One", 30.0), queued(1002, "Next Hour Two", 230.0)])
        state["decks"]["A"]["playlist_log_id"] = 77
        self.refresh(page, state, history=new_history)
        self.wait_states(page, [(1001, "upcoming"), (1002, "upcoming")])
        self.assertIn("/api/engine/status/log/78/", self.history_requests)
        self.assertEqual(self.console_errors, [])

    # ------------------------------------------------ history fetch: bounded, never stale
    HOLD_HISTORY = """() => {
      window.__hist = {mode: 'pass', held: []};
      const realFetch = window.fetch.bind(window);
      // A transport that, while 'hold'ing, never settles AND ignores abort.
      window.fetch = (url, opts) => {
        if (String(url).includes('/api/engine/status/log/') && window.__hist.mode === 'hold') {
          return new Promise((resolve, reject) => window.__hist.held.push({url: String(url), resolve, reject}));
        }
        return realFetch(url, opts);
      };
      window.__histRespond = (i, logId, items) => window.__hist.held[i].resolve(new Response(
        JSON.stringify({log_id: logId, items}), {status: 200, headers: {'Content-Type': 'application/json'}}));
      window.__histReject = (i) => window.__hist.held[i].reject(new TypeError('simulated network loss'));
    }"""
    HIST_INFO = """() => ({inFlight: logHistory.inFlight, logId: logHistory.logId,
      ids: logHistory.items.map(i => i.item_id), held: window.__hist ? window.__hist.held.length : 0})"""

    def hold(self, page, on=True):
        if page.evaluate("() => typeof window.__hist === 'undefined'"):
            page.evaluate(self.HOLD_HISTORY)
        page.evaluate("(m) => { window.__hist.mode = m; }", "hold" if on else "pass")

    def info(self, page):
        return page.evaluate(self.HIST_INFO)

    def wait_info(self, page, predicate):
        page.wait_for_function(f"() => {{ const i = ({self.HIST_INFO})(); return ({predicate})(i); }}", timeout=10_000)

    def advance(self, page, ms):
        page.clock.run_for(ms)
        page.wait_for_function("() => statusInFlightSeq === 0")

    GHOST = hist(999, 1, "Stale Ghost", played=AIRED - 5000)

    def test_a_stalled_history_request_times_out_and_history_recovers(self):
        for label, user, path in (("console", self.superuser, "/"), ("remote_dj", self.host, "/remote-dj/")):
            with self.subTest(page=label):
                self.history_requests.clear()
                self.state = console_state(queue=standard_queue())
                self.history = standard_history()
                page = self.console(flag=True, user=user, path=path)
                self.wait_states(page, GOLD)
                self.hold(page)
                # The queue moves on (904 airs): a refresh begins and never settles.
                h2 = [dict(h) for h in standard_history()]
                h2[3]["played_at"] = AIRED + 200
                self.refresh(page, console_state(current=904, queue=[queued(905, "Upcoming Five", 60.0)]), history=h2)
                self.wait_info(page, "i => i.held === 1 && i.inFlight")
                # Still legitimately pending: more changes must not start a second request.
                s3 = console_state(current=904, queue=[queued(905, "Upcoming Five", 60.0), queued(906, "Upcoming Six", 260.0)])
                h3 = h2 + [hist(906, 6, "Upcoming Six")]
                self.state, self.history = s3, h3
                self.advance(page, 3_000)
                info = self.info(page)
                self.assertEqual((info["held"], info["inFlight"]), (1, True))
                # Past the deadline the stalled request frees the slot and the next
                # change is fetched for real, completing and updating the display.
                self.hold(page, on=False)
                self.advance(page, 4_000)
                self.wait_states(page, [(901, "played"), (902, "played"), (903, "played"), (904, "current"),
                                        (905, "upcoming"), (906, "upcoming")])
                info = self.info(page)
                self.assertEqual((info["held"], info["inFlight"]), (1, False))   # the stalled one never retried itself
                self.assertEqual(len(self.history_requests), 2)                   # initial + the recovery
                self.assertEqual(self.console_errors, [])

    def test_a_late_response_from_a_timed_out_request_changes_nothing(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.hold(page)
        self.refresh(page, console_state(current=904, queue=[queued(905, "Upcoming Five", 60.0)]),
                     history=standard_history())
        self.wait_info(page, "i => i.held === 1 && i.inFlight")
        self.hold(page, on=False)
        self.advance(page, 7_000)                                       # times out; recovery fetch completes
        expected = [(901, "played"), (902, "played"), (903, "played"), (904, "current"), (905, "upcoming")]
        self.wait_states(page, expected)
        before = self.info(page)
        # The original request finally answers -- with history that was never true.
        # (log_id 77, the active log: only the attempt guard can reject it.)
        page.evaluate("([ids]) => __histRespond(0, 77, ids)", [[self.GHOST]])
        self.advance(page, 1_000)
        self.assertEqual(self.states(page), [(str(i), st) for i, st in expected])
        self.assertEqual(self.info(page)["ids"], before["ids"])
        self.assertNotIn("999", [r["id"] for r in self.rows(page)])
        self.assertEqual(self.info(page)["logId"], 77)
        self.assertEqual(self.console_errors, [])

    def _rollover(self, outcome):
        self.state = console_state(queue=standard_queue())
        self.history = standard_history()
        self.history_requests.clear()
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.hold(page)
        self.refresh(page, console_state(current=903, queue=[queued(905, "Upcoming Five", 60.0)]))
        self.wait_info(page, "i => i.held === 1 && i.inFlight")          # a request for log 77 is outstanding
        # The next hour (log 78) is adopted while it is still outstanding.
        self.hold(page, on=False)
        new_history = [hist(1000, 1, "Aired Early", played=AIRED + 400), hist(1001, 2, "Next Hour One"),
                       hist(1002, 3, "Next Hour Two")]
        state = console_state(current=903, log_id=78,
                              queue=[queued(1001, "Next Hour One", 30.0), queued(1002, "Next Hour Two", 230.0)])
        state["decks"]["A"]["playlist_log_id"] = 77
        self.refresh(page, state, history=new_history)
        want = [(1000, "played"), (1001, "upcoming"), (1002, "upcoming")]
        self.wait_states(page, want)
        self.wait_info(page, "i => i.logId === 78 && !i.inFlight")
        self.assertIn("/api/engine/status/log/78/", self.history_requests)
        if outcome == "completes":
            page.evaluate("([ids]) => __histRespond(0, 77, ids)", [[self.GHOST]])
        elif outcome == "fails":
            page.evaluate("() => __histReject(0)")
        else:                                                            # "times out"
            self.advance(page, 7_000)
        self.advance(page, 1_000)
        self.assertEqual(self.states(page), [(str(i), st) for i, st in want])
        info = self.info(page)
        self.assertEqual((info["logId"], info["inFlight"], sorted(info["ids"])), (78, False, [1000, 1001, 1002]))
        self.assertEqual(self.console_errors, [])

    def test_an_outstanding_request_from_the_previous_hour_cannot_affect_the_new_hour(self):
        for outcome in ("completes", "fails", "times out"):
            with self.subTest(old_request=outcome):
                self._rollover(outcome)

    def test_controls_stay_correct_after_a_timeout_and_recovery(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.hold(page)
        self.refresh(page, console_state(queue=[queued(905, "Upcoming Five", 60.0), queued(904, "Upcoming Four", 260.0)]))
        self.wait_info(page, "i => i.held === 1 && i.inFlight")
        self.hold(page, on=False)
        self.advance(page, 7_000)
        # Upcoming order comes from the LATEST engine queue (905 now ahead of 904).
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"), (905, "upcoming"), (904, "upcoming")])
        for r in self.rows(page):
            self.assertEqual((r["handle"], r["next"], r["queueRow"]), ((r["state"] == "upcoming"),) * 3, r["id"])
        self.reveal_queue(page)
        self.drag(page, 904, 905)
        self.assertEqual(json.loads(self.wait_post(page, "/api/log/77/reorder/")[0]), {"order": [904, 905]})
        page.locator('#queueList tr[data-item-id="904"] .qt-next-btn').click()
        self.assertEqual(json.loads(self.wait_post(page, "/api/engine/queue/set-next/")[0]), {"item_id": 904})
        self.assertEqual(page.locator("#queueList .qt-next-btn").count(), 2)

    def test_request_spacing_safety_refresh_and_no_needless_retries(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.assertEqual(len(self.history_requests), 1)
        # A change right away is held back by the 2 s minimum spacing ...
        self.state = console_state(queue=[queued(905, "Upcoming Five", 60.0), queued(904, "Upcoming Four", 260.0)])
        page.evaluate("async () => { await poll(); }")
        self.assertEqual(len(self.history_requests), 1)
        # ... and goes out once the spacing has passed, exactly once.
        self.advance(page, 2_100)
        page.wait_for_function("() => !logHistory.inFlight")
        self.assertEqual(len(self.history_requests), 2)
        self.advance(page, 20_000)                                       # nothing changes: no retries
        self.assertEqual(len(self.history_requests), 2)
        # The 30 s safety refresh fires on its own once the data is that old.
        self.advance(page, 11_000)
        page.wait_for_function("() => !logHistory.inFlight")
        self.assertEqual(len(self.history_requests), 3)

    def test_off_never_fetches_history_however_long_it_runs(self):
        page = self.console(flag=False)
        for _ in range(4):
            self.advance(page, 10_000)
        self.assertEqual(self.history_requests, [])
        self.assertEqual(self.console_errors, [])

    # ------------------------------------------------------------ r0139 refinements
    def edge_pixels(self, page, selector):
        cell = self.box(page, selector)
        x = cell["left"] + cell["width"] / 2
        return [self.pixel(page, x, cell["top"] + d) for d in (0, 1)]

    def test_default_appearance_is_r0138s(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.assertEqual(page.evaluate(
            "() => { const c = getComputedStyle(document.documentElement);"
            " return [c.getPropertyValue('--played-item-darkening').trim(),"
            " c.getPropertyValue('--current-item-outline').trim()]; }"), ["0.550000", "rgba(255, 255, 255, 1.00)"])

    def test_played_item_darkening_setting_scales_the_overlay_and_keeps_the_hue(self):
        self.history[1] = hist(902, 2, "Played Two", fill=RED, played=AIRED - 300)
        for darkening, factor in ((0.0, 1.0), (0.8, 0.2), (1.0, 0.0), (0.55, 0.45)):
            with self.subTest(darkening=darkening):
                page = self.console(flag=True, appearance={"played_item_darkening": darkening})
                self.wait_states(page, GOLD)
                self.reveal_queue(page)
                played = self.box(page, '#queueList tr[data-item-id="901"] td')
                upcoming = self.box(page, '#queueList tr[data-item-id="905"] td')
                px = self.pixel(page, played["left"] + 3, played["top"] + 4)
                for got, base in zip(px, (200, 60, 60)):
                    self.assertAlmostEqual(got, base * factor, delta=8)
                self.assertEqual(self.pixel(page, upcoming["left"] + 3, upcoming["top"] + 4), (200, 60, 60))

    def test_current_outline_colour_setting(self):
        page = self.console(flag=True, appearance={"current_item_outline_color": "rgba(255, 0, 0, 1.00)"})
        self.wait_states(page, GOLD)
        self.reveal_queue(page)
        self.assertIn("rgb(255, 0, 0)", page.evaluate(
            "() => getComputedStyle(document.querySelector('tr.qt-current td:nth-child(5)')).boxShadow"))
        self.assertTrue(any(p[0] >= 240 and p[1] <= 30 and p[2] <= 30
                            for p in self.edge_pixels(page, "tr.qt-current td:nth-child(5)")))
        self.assertFalse(any(p[0] >= 240 and p[1] <= 30
                             for p in self.edge_pixels(page, 'tr[data-item-id="905"] td:nth-child(5)')))

    def test_outline_colour_formats_and_alpha_render(self):
        blue = (40, 90, 200)           # the current row's own fill
        for value, colour, alpha in (("rgb(0, 255, 0)", (0, 255, 0), 1.0), ("rgba(255, 0, 0, 0.50)", (255, 0, 0), 0.5),
                                     ("#FFFFFF80", (255, 255, 255), 128 / 255), ("#fff", (255, 255, 255), 1.0),
                                     ("#0f08", (0, 255, 0), 0x88 / 255)):
            with self.subTest(outline=value):
                page = self.console(flag=True, appearance={"current_item_outline_color": value})
                self.wait_states(page, GOLD)
                self.reveal_queue(page)
                want = tuple(round(alpha * c + (1 - alpha) * b) for c, b in zip(colour, blue))
                edges = self.edge_pixels(page, "tr.qt-current td:nth-child(5)")
                self.assertTrue(any(all(abs(a - b) <= 28 for a, b in zip(px, want)) for px in edges), (value, want, edges))

    def playing_ids(self, page):
        return page.evaluate("() => Array.from(document.querySelectorAll('#queueList tr'))"
                             ".filter(tr => tr.querySelector('.qt-playing-icon')).map(tr => tr.dataset.itemId)")

    def test_play_icon_only_on_rows_the_engine_says_are_playing(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.assertEqual(self.playing_ids(page), ["903"])
        icon = page.evaluate("""() => { const i = document.querySelector('.qt-playing-icon');
          return {label: i.getAttribute('aria-label'), title: i.title, svg: !!i.querySelector('svg'),
                  interactive: !!i.closest('td').querySelector('button, a, input'), handle: !!i.closest('.qt-drag-handle'),
                  ref: ICON_PLAY.includes(i.querySelector('path').getAttribute('d'))}; }""")
        self.assertEqual(icon, {"label": "Currently playing", "title": "Currently playing", "svg": True,
                                "interactive": False, "handle": False, "ref": True})
        # A genuine crossfade: both decks actually playing -> both rows.
        state = console_state(current=903, claimed=904, queue=[queued(905, "Upcoming Five", 250.0)])
        state["decks"]["B"].update(actually_playing=True, air_started_at=AIRED + 100)
        history = standard_history()
        history[3]["played_at"] = AIRED + 100
        self.refresh(page, state, history=history)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"), (904, "current"), (905, "upcoming")])
        self.assertEqual(self.playing_ids(page), ["903", "904"])
        # A claimed / preloaded deck is not playing: no icon on it.
        self.refresh(page, console_state(current=903, claimed=904, queue=[queued(905, "Upcoming Five", 250.0)]),
                     history=standard_history())
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"), (904, "ondeck"), (905, "upcoming")])
        self.assertEqual(self.playing_ids(page), ["903"])
        # A paused / not-actually-playing deck: no icon at all.
        paused = console_state(current=903, queue=standard_queue())
        paused["decks"]["A"]["actually_playing"] = False
        self.refresh(page, paused)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "ondeck"), (904, "upcoming"), (905, "upcoming")])
        self.assertEqual(self.playing_ids(page), [])

    def test_play_icon_never_appears_with_show_played_items_off(self):
        page = self.console(flag=False)
        self.assertEqual(page.locator(".qt-playing-icon").count(), 0)

    def test_sort_icon_is_an_svg_only_on_draggable_rows(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        rows = page.evaluate("""() => Array.from(document.querySelectorAll('#queueList tr')).map(tr => {
          const h = tr.querySelector('.qt-drag-handle'); const i = h && h.querySelector('.qt-sort-icon svg');
          return {id: tr.dataset.itemId, handle: !!h, svg: !!i, paths: i ? i.querySelectorAll('path').length : 0,
                  text: h ? h.textContent.trim() : null, h: i ? i.getBoundingClientRect().height : 0}; })""")
        for row in rows:
            draggable = row["id"] in ("904", "905")
            self.assertEqual((row["handle"], row["svg"]), (draggable, draggable), row["id"])
            if draggable:
                self.assertEqual((row["paths"], row["text"]), (2, ""))
                self.assertGreaterEqual(row["h"], 12)

    def test_headers_align_with_their_cells(self):
        want = ["center", "right", "left", "left", "left", "right", "right", "right", "left"]
        probe = """() => { const norm = v => (v === 'start' ? 'left' : v);
          const ths = Array.from(document.querySelectorAll('.queue-table thead th'));
          const rows = {};
          document.querySelectorAll('#queueList tr[data-row-state]').forEach(tr => {
            rows[tr.dataset.rowState] = Array.from(tr.children).map(td => norm(getComputedStyle(td).textAlign)); });
          return {headers: ths.map(th => norm(getComputedStyle(th).textAlign)), rows,
                  sticky: getComputedStyle(ths[0]).position, texts: ths.map(th => th.textContent.trim().toLowerCase())}; }"""
        for name, viewport in (("desktop", DESKTOP), ("tablet", TABLET), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(viewport=name):
                page = self.console(flag=True, viewport=viewport)
                self.wait_states(page, GOLD)
                got = page.evaluate(probe)
                self.assertEqual(got["headers"], want)
                self.assertEqual(set(got["rows"]), {"played", "current", "upcoming"})
                for state, cells in got["rows"].items():
                    self.assertEqual(cells, want, state)
                self.assertEqual(got["sticky"], "sticky")
                self.assertEqual(got["texts"][1:8], ["#", "artist", "title", "cat", "dur", "intro", "time on"])
        page = self.console(flag=False)
        self.assertEqual(page.evaluate(probe)["headers"], want)

    def test_touch_drag_with_the_new_sort_handle(self):
        if self.BROWSER != "chromium":
            self.skipTest("touch input is driven through Chromium's DevTools protocol")
        self.set_flag(True)
        client = Client()
        client.force_login(self.superuser)
        context = self.browser.new_context(viewport=DESKTOP, has_touch=True)
        self.addCleanup(context.close)
        context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value,
                              "url": self.live_server_url}])
        page = context.new_page()
        page.route("**/*", self._route)
        page.clock.install(time=T0_MS / 1000)
        page.goto(self.live_server_url + "/")
        page.clock.pause_at((T0_MS + 2_000) / 1000)
        self.wait_rows(page)
        self.wait_states(page, GOLD)
        self.reveal_queue(page)
        cdp = context.new_cdp_session(page)
        src = self.box(page, '#queueList tr[data-item-id="905"] .qt-drag-handle')
        dst = self.box(page, '#queueList tr[data-item-id="904"] td:nth-child(5)')
        sx, sy = src["left"] + src["width"] / 2, src["top"] + src["height"] / 2
        dx, dy = dst["left"] + 10, dst["top"] + dst["height"] / 2
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": sx, "y": sy}]})
        for k in range(1, 7):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [
                {"x": sx + (dx - sx) * k / 6, "y": sy + (dy - sy) * k / 6}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        self.assertEqual(json.loads(self.wait_post(page, "/api/log/77/reorder/")[0]), {"order": [905, 904]})

    # ------------------------------------------------------------ admin colour widget
    def open_admin(self, path):
        client = Client()
        client.force_login(self.superuser)
        context = self.browser.new_context(viewport=DESKTOP)
        self.addCleanup(context.close)
        context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value,
                              "url": self.live_server_url}])
        page = context.new_page()
        self.console_errors = []
        page.on("pageerror", lambda exc: self.console_errors.append(str(exc)))
        page.goto(self.live_server_url + path)
        return page

    def save_admin(self, page):
        page.click("input[name=_save]")
        page.wait_for_selector(".messagelist .success", timeout=15_000)

    def test_admin_round_trip_keeps_untouched_outline_colours(self):
        """The real RGBAColorWidget JavaScript, in the real UI Theme admin form: opening it and
        saving an unrelated setting must not change a stored colour."""
        for stored in ("#fff", "#FFFFFF80", "#fff8", "rgba(10, 20, 30, 0.37)"):
            with self.subTest(stored=stored):
                UITheme.objects.filter(pk=1).update(current_item_outline_color=stored, show_played_items=False)
                page = self.open_admin("/admin/library/uitheme/1/change/")
                self.assertEqual(page.input_value("#id_current_item_outline_color"), stored)
                page.check("#id_show_played_items")                    # the unrelated change
                self.save_admin(page)
                theme = UITheme.objects.get(pk=1)
                self.assertEqual(theme.current_item_outline_color, stored)
                self.assertTrue(theme.show_played_items)
                self.assertEqual(self.console_errors, [])

    def test_admin_intentional_colour_and_opacity_changes_are_saved(self):
        UITheme.objects.filter(pk=1).update(current_item_outline_color="#FFFFFF80")
        page = self.open_admin("/admin/library/uitheme/1/change/")
        page.evaluate("""() => { const set = (id, v) => { const e = document.getElementById(id); e.value = v;
          e.dispatchEvent(new Event('input', {bubbles: true})); };
          set('id_current_item_outline_color_alpha', '25'); set('id_current_item_outline_color_color', '#ff0000'); }""")
        self.save_admin(page)
        self.assertEqual(UITheme.objects.get(pk=1).current_item_outline_color, "rgba(255, 0, 0, 0.25)")

    def test_other_colour_widget_consumers_survive_opening_and_saving(self):
        from library.models import CategoryKind
        CategoryKind.objects.all().delete()
        CategoryKind.objects.create(code="wk1", name="Widget Kind One", fill_color="#fff", sort_order=1)
        CategoryKind.objects.create(code="wk2", name="Widget Kind Two", fill_color="rgba(1, 2, 3, 0.4)", sort_order=2)
        page = self.open_admin("/admin/library/categorykind/")
        self.save_admin(page)                                       # nothing touched
        self.assertEqual(dict(CategoryKind.objects.values_list("code", "fill_color")),
                         {"wk1": "#fff", "wk2": "rgba(1, 2, 3, 0.4)"})
        self.assertEqual(self.console_errors, [])
        # And the UI Theme palette (a dozen widgets on one form) is untouched by an ordinary save.
        before = {f: getattr(UITheme.load(), f) for f in ("bg_dark", "panel_bg", "accent", "border_subtle",
                                                          "deck_text_shadow_color", "nav_clock_color")}
        page = self.open_admin("/admin/library/uitheme/1/change/")
        self.save_admin(page)
        self.assertEqual({f: getattr(UITheme.objects.get(pk=1), f) for f in before}, before)

    # ------------------------------------------------------------ shared navigation
    NAV = """() => ({open: document.getElementById('navLinks').classList.contains('open'),
      clock: document.getElementById('navClock').getAttribute('aria-expanded'),
      toggle: document.getElementById('navToggle').getAttribute('aria-expanded')})"""

    def check_nav(self, page, label):
        def state(open_):
            return {"open": open_, "clock": str(open_).lower(), "toggle": str(open_).lower()}
        self.assertEqual(page.evaluate(self.NAV), state(False), f"{label} initial")
        for selector, expected in (("#navToggle", True), ("#navClock", False), ("#navClock", True),
                                   ("#navToggle", False), ("#navToggle", True), ("#navClock", False)):
            page.locator(selector).click()
            self.assertEqual(page.evaluate(self.NAV), state(expected), f"{label} after {selector}")
        for selector in ("#navClock", "#navToggle"):          # keyboard: Enter opens, Space closes
            page.focus(selector)
            page.keyboard.press("Enter")
            self.assertEqual(page.evaluate(self.NAV), state(True), f"{label} {selector} Enter")
            page.keyboard.press("Space")
            self.assertEqual(page.evaluate(self.NAV), state(False), f"{label} {selector} Space")
        page.locator("#navClock").click()                      # one click is exactly one toggle
        self.assertEqual(page.evaluate(self.NAV), state(True))
        page.locator("#navClock").click()
        self.assertEqual(page.locator("#navLinks .nav-logout-btn").count(), 1)
        self.assertEqual(page.locator("#navClock button, #navToggle button, button #navClock, button #navToggle").count(), 0)

    def test_clock_and_hamburger_toggle_one_menu_on_both_console_pages(self):
        for label, user, path in (("console", self.superuser, "/"), ("remote_dj", self.host, "/remote-dj/")):
            with self.subTest(page=label):
                page = self.console(flag=True, user=user, path=path)
                self.check_nav(page, label)
                before = page.evaluate("() => document.getElementById('navClock').textContent")
                page.clock.run_for(2_000)
                after = page.evaluate("() => document.getElementById('navClock').textContent")
                self.assertNotEqual(before, after)
                self.assertRegex(after, r"^\d{1,2}:\d\d:\d\d (AM|PM)$")
                self.assertEqual(after, page.evaluate("() => window.stationFormatTime(new Date())"))

    def test_clock_and_hamburger_work_on_a_non_console_page(self):
        client = Client()
        client.force_login(self.superuser)
        context = self.browser.new_context(viewport=DESKTOP)
        self.addCleanup(context.close)
        context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value,
                              "url": self.live_server_url}])
        page = context.new_page()
        page.route("**/*", self._route)
        page.goto(self.live_server_url + "/library/")
        page.wait_for_selector("#navClock")
        self.check_nav(page, "library")
        first = page.evaluate("() => document.getElementById('navClock').textContent")
        page.wait_for_function("(t) => document.getElementById('navClock').textContent !== t", arg=first, timeout=5_000)

    def test_clock_and_hamburger_stay_on_screen_on_narrow_phones(self):
        for width in (375, 320):
            with self.subTest(width=width):
                page = self.console(flag=True, viewport={"width": width, "height": 700})
                rect = page.evaluate("""() => { const b = id => document.getElementById(id).getBoundingClientRect();
                  return {w: window.innerWidth, clock: b('navClock').right, toggle: b('navToggle').right,
                          left: b('navToggle').left, scroll: document.documentElement.scrollWidth}; }""")
                self.assertLessEqual(rect["clock"], rect["w"], rect)
                self.assertLessEqual(rect["toggle"], rect["w"], rect)
                self.assertGreaterEqual(rect["left"], 0, rect)
                self.assertLessEqual(rect["scroll"], rect["w"], rect)
                page.locator("#navClock").click()
                self.assertTrue(page.evaluate(self.NAV)["open"])

    # ------------------------------------------------------ queue-control safety
    def drag(self, page, source_id, target_id):
        src = self.box(page, f'#queueList tr[data-item-id="{source_id}"] .qt-drag-handle')
        dst = self.box(page, f'#queueList tr[data-item-id="{target_id}"] td:nth-child(5)')
        page.mouse.move(src["left"] + src["width"] / 2, src["top"] + src["height"] / 2)
        page.mouse.down()
        page.mouse.move(dst["left"] + 10, dst["top"] + dst["height"] / 2, steps=6)
        page.mouse.up()

    def test_reorder_posts_only_eligible_upcoming_ids(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        self.drag(page, 905, 904)
        bodies = self.wait_post(page, "/api/log/77/reorder/")
        self.assertEqual(json.loads(bodies[0]), {"order": [905, 904]})
        self.assertEqual(len(bodies), 1)
        self.assertEqual(self.console_errors, [])

    def test_dropping_on_a_played_or_current_row_sends_nothing(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        for target in (901, 903):
            self.drag(page, 905, target)
        page.wait_for_timeout(500)
        self.assertEqual([p for p, _ in self.posts if "reorder" in p], [])
        self.assertEqual(self.states(page)[-2:], [("904", "upcoming"), ("905", "upcoming")])

    def test_played_and_current_rows_cannot_start_a_drag(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        for source in (901, 903):
            cell = self.box(page, f'#queueList tr[data-item-id="{source}"] td:nth-child(1)')
            dst = self.box(page, '#queueList tr[data-item-id="905"] td:nth-child(5)')
            page.mouse.move(cell["left"] + cell["width"] / 2, cell["top"] + cell["height"] / 2)
            page.mouse.down()
            page.mouse.move(dst["left"] + 10, dst["top"] + dst["height"] / 2, steps=6)
            self.assertFalse(page.evaluate("() => queueDragActive"))
            page.mouse.up()
        page.wait_for_timeout(300)
        self.assertEqual([p for p, _ in self.posts if "reorder" in p], [])

    def test_next_button_is_the_themed_pill_and_still_the_same_control(self):
        page = self.console(flag=True)
        self.wait_states(page, GOLD)
        self.reveal_queue(page)
        buttons = page.evaluate("""() => Array.from(document.querySelectorAll('#queueList .qt-next-btn')).map(b => {
          const cs = getComputedStyle(b); const badge = b.querySelector('.qt-next-badge');
          return {row: b.closest('tr').dataset.itemId, tag: b.tagName, text: b.textContent.trim(), label: b.getAttribute('aria-label'),
                  badge: !!badge && badge.querySelectorAll('svg path').length, radius: cs.borderTopLeftRadius,
                  color: cs.color, bg: cs.backgroundColor, h: b.getBoundingClientRect().height}; })""")
        self.assertEqual([b["row"] for b in buttons], ["904", "905"])           # upcoming rows only
        for b in buttons:
            self.assertEqual((b["tag"], b["text"], b["label"], b["badge"]), ("BUTTON", "NEXT", "Play this next", 2))
            self.assertEqual(b["color"], "rgb(34, 197, 94)")                     # green lettering
            self.assertRegex(b["bg"], r"^rgba\(15, 23, 42, 0\.55\)$")           # dark translucent
            self.assertGreaterEqual(float(b["radius"][:-2]), 10)                 # pill
            self.assertLess(b["h"], 30)                                          # compact: row height is unchanged
        self.shot(page, "next-button")

    def test_next_posts_the_upcoming_item_only(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        self.assertEqual(page.locator("#queueList .qt-next-btn").count(), 2)
        page.locator('#queueList tr[data-item-id="905"] .qt-next-btn').click()
        bodies = self.wait_post(page, "/api/engine/queue/set-next/")
        self.assertEqual(json.loads(bodies[0]), {"item_id": 905})

    def test_claimed_item_is_not_draggable_or_in_a_reorder(self):
        self.state = console_state(current=903, claimed=904, queue=[queued(905, "Upcoming Five", 250.0),
                                                                    queued(906, "Upcoming Six", 450.0)])
        self.history.append(hist(906, 6, "Upcoming Six"))
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "ondeck"), (905, "upcoming"), (906, "upcoming")])
        self.reveal_queue(page)
        self.drag(page, 906, 905)
        bodies = self.wait_post(page, "/api/log/77/reorder/")
        self.assertEqual(json.loads(bodies[0]), {"order": [906, 905]})

    def test_stale_connection_still_gates_queue_mutation(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        page.evaluate("() => { connectionStale = true; }")
        page.evaluate("() => setNext(905)")
        self.assertEqual(self.posts, [])

    # ------------------------------------------------------------ consistency / layout
    def test_remote_dj_and_console_show_the_same_rows(self):
        console_rows = None
        for label, user, path in (("console", self.superuser, "/"), ("remote_dj", self.host, "/remote-dj/")):
            page = self.console(flag=True, user=user, path=path)
            self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                    (904, "upcoming"), (905, "upcoming")])
            rows = [{k: v for k, v in r.items() if k != "cells"} | {"cells": r["cells"][1:]} for r in self.rows(page)]
            if label == "console":
                console_rows = rows
            else:
                self.assertEqual(rows, console_rows)
                self.assertEqual(page.locator("#queueList a.qt-track-link").count(), 0)   # no navigation for a remote DJ
                self.reveal_queue(page)
                self.drag(page, 905, 904)
                self.assertEqual(json.loads(self.wait_post(page, "/api/log/77/reorder/")[0]), {"order": [905, 904]})
            self.assertEqual(self.console_errors, [])

    def test_long_log_scrolls_and_the_operators_scroll_position_survives_polls(self):
        history = [hist(i, i, f"Played {i}", played=AIRED - 3000 + i) for i in range(1, 31)]
        history.append(hist(931, 31, "On Air Long", fill=BLUE, played=AIRED))
        history += [hist(940 + i, 32 + i, f"Next {i}") for i in range(3)]
        self.history = history
        self.state = console_state(current=931, queue=[queued(940 + i, f"Next {i}", 60.0 + 200 * i) for i in range(3)])
        page = self.console(flag=True)
        self.wait_states(page, [(i, "played") for i in range(1, 31)] + [(931, "current")]
                         + [(940 + i, "upcoming") for i in range(3)])
        metrics = page.evaluate("""() => { const w = document.querySelector('.queue-table-wrap');
          return {client: w.clientHeight, scroll: w.scrollHeight, top: w.scrollTop}; }""")
        self.assertGreater(metrics["scroll"], metrics["client"])            # the card scrolls, the page doesn't grow
        self.assertGreater(metrics["top"], 0)                               # opened on the item on air
        wrap_box = self.box(page, ".queue-table-wrap")
        cur_box = self.box(page, "tr.qt-current")
        self.assertGreaterEqual(cur_box["top"], wrap_box["top"])
        self.assertLessEqual(cur_box["bottom"], wrap_box["bottom"] + 1)
        # The operator scrolls back to the top; later polls must not yank it.
        page.evaluate("() => { document.querySelector('.queue-table-wrap').scrollTop = 0; }")
        for _ in range(4):
            page.clock.run_for(1_000)
            page.wait_for_function("() => statusInFlightSeq === 0")
        self.assertEqual(page.evaluate("() => document.querySelector('.queue-table-wrap').scrollTop"), 0)

    def test_layouts_have_no_page_overflow_with_the_extra_column(self):
        for name, viewport in (("tablet", TABLET), ("mobile", MOBILE_PORTRAIT), ("desktop", DESKTOP)):
            for flag in (False, True):
                with self.subTest(viewport=name, show_played=flag):
                    page = self.console(flag=flag, viewport=viewport)
                    self.no_horizontal_overflow(page)
                    self.reveal_queue(page)
                    self.shot(page, f"layout-{name}-{'on' if flag else 'off'}")
                    self.assertEqual(self.console_errors, [])


# The shared harness base also carries the 2.23 Console tests; they already run
# in test_console_browser_2_23 and must not run a second time here.
for _name in [n for n in dir(_ConsoleBrowserBase) if n.startswith("test_")]:
    if _name not in vars(_LogDisplayBase):
        setattr(_LogDisplayBase, _name, None)


@unittest.skipIf(sync_playwright is None or Image is None, "Playwright/Pillow not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class LogDisplayChromiumTests(_LogDisplayBase, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None or Image is None, "Playwright/Pillow not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class LogDisplayFirefoxTests(_LogDisplayBase, StaticLiveServerTestCase):
    BROWSER = "firefox"
