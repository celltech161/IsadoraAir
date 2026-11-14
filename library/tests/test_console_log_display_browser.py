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

    def set_flag(self, on):
        theme = UITheme.load()
        theme.show_played_items = on
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

    def console(self, *, flag=True, user=None, path="/", **kw):
        self.set_flag(flag)
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
        text = page.evaluate("() => getComputedStyle(document.querySelector('tr.qt-played td:nth-child(4)')).color")
        self.assertEqual(text, "rgb(229, 231, 235)")                    # explicit light text on the dark overlay
        self.shot(page, "show-played-darkening")

    def test_current_row_has_a_white_outline_and_keeps_its_colour(self):
        page = self.console(flag=True)
        self.wait_states(page, [(901, "played"), (902, "played"), (903, "current"),
                                (904, "upcoming"), (905, "upcoming")])
        self.reveal_queue(page)
        self.assertEqual(page.evaluate(
            "() => getComputedStyle(document.querySelector('tr.qt-current')).backgroundColor"), "rgb(40, 90, 200)")
        cell = self.box(page, 'tr.qt-current td:nth-child(3)')
        other = self.box(page, 'tr[data-item-id="905"] td:nth-child(3)')
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

    # ------------------------------------------------------ queue-control safety
    def drag(self, page, source_id, target_id):
        src = self.box(page, f'#queueList tr[data-item-id="{source_id}"] .qt-drag-handle')
        dst = self.box(page, f'#queueList tr[data-item-id="{target_id}"] td:nth-child(3)')
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
            dst = self.box(page, '#queueList tr[data-item-id="905"] td:nth-child(3)')
            page.mouse.move(cell["left"] + cell["width"] / 2, cell["top"] + cell["height"] / 2)
            page.mouse.down()
            page.mouse.move(dst["left"] + 10, dst["top"] + dst["height"] / 2, steps=6)
            self.assertFalse(page.evaluate("() => queueDragActive"))
            page.mouse.up()
        page.wait_for_timeout(300)
        self.assertEqual([p for p, _ in self.posts if "reorder" in p], [])

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
