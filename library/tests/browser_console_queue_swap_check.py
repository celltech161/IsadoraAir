"""Real-browser check of the Console "Coming Up" queue: the Drag | NEXT | Swap | # column layout, row-state
eligibility, drag-to-reorder and NEXT, on the full Console and the dedicated Remote DJ page. Not a Django
test module (no ``test_`` prefix, no Django import): it runs on a host with Playwright's Chromium, OUTSIDE
the validation sandbox (where Playwright cannot launch a browser), against pages the sandbox rendered with
library.tests.render_schedule_console_artifact:

    run_with_validation_service.sh <python> manage.py test library.tests.render_schedule_console_artifact
    # extract CONSOLE_PAGE / REMOTE_DJ_PAGE into <dir>/console_page.html, remote_dj_page.html
    <python with playwright> -I library/tests/browser_console_queue_swap_check.py <dir> [scenario]

Every request is intercepted: a scripted engine answers /api/engine/status/ (and the played-history
endpoint) and records queue mutations. Exit status 0 only if every check passes.
"""
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

BASE = "https://station.test"
results, failures = [], []
HEADER = ["Reorder", "Next", "Swap", "#", "Artist", "Title", "Cat", "Dur", "Intro", "Time On"]


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    if not ok:
        failures.append(f"{name}: {detail}")
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""))


def item(item_id, title, eta, *, track=None, duration=240.0, color="#283c78", category="Rock"):
    return {"item_id": item_id, "track_id": track or item_id + 1000, "artist": f"Artist {item_id}", "title": title,
            "category": category, "fill_color": color, "duration": duration, "intro_until_seconds": 12.0,
            "eta_seconds": eta}


class Engine:
    def __init__(self):
        self.seq = 0
        self.log_id = 77
        self.queue = [item(904, "Upcoming One", 60), item(905, "Upcoming Two", 300, color="#5a2a78"),
                      item(906, "Upcoming Three", 540)]
        self.history = []          # played-history rows (Show played items)
        self.posts = []            # (path, json body)
        self.searches = []         # query strings of /api/tracks/ requests
        self.status_ok = True
        self.replace_answer = lambda body: (200, {"ok": True, "changed": True, "item_id": body["item_id"],
                                                  "track_id": body["track_id"]})
        self.hold_replace = False
        self.hold_search = False
        self.held = []

    def status(self):
        self.seq += 1
        return {"transport": "PLAYING", "decks": {"A": None, "B": None}, "queue": self.queue, "fx_fires": [],
                "log_id": self.log_id, "timestamp": 1_800_000_000, "engine_session_id": "eng-1",
                "state_revision": {"instance": "eng-1", "seq": self.seq},
                "remote_dj_configured": True, "remote_dj_connected": False, "remote_dj_live": False,
                "remote_dj_owner_supported": True, "manual_mode": False, "mic_live": False}


class Page:
    def __init__(self, browser, html, engine, *, path="/", width=1366):
        self.engine = engine
        self.context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": 900})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.route("**/*", lambda route, request: self._route(route, request, html, path))
        self.page.goto(f"{BASE}{path}")
        self.wait_rows()

    def _json(self, route, body, status=200):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _route(self, route, request, html, page_path):
        parsed = urlparse(request.url)
        path = parsed.path
        if path == page_path and request.method == "GET" and request.resource_type == "document":
            return route.fulfill(status=200, content_type="text/html", body=html)
        if path == "/api/engine/status/":
            if not self.engine.status_ok:
                return route.fulfill(status=503, body="")
            return self._json(route, self.engine.status())
        if path == "/api/tracks/":
            self.engine.searches.append(parsed.query)
            query = parse_qs(parsed.query).get("q", [""])[0]
            items = [{"id": 7000 + n, "title": f"{query} Result {n}", "artist": "Swap Artist", "album": "",
                      "category_code": "ROCK", "category_name": "Rock", "duration_seconds": 125.0 + n,
                      "next_start_seconds": 120.0, "format": "mp3"} for n in range(3)]
            if self.engine.hold_search:
                self.engine.held.append((route, {"items": items}))
                return None
            return self._json(route, {"items": items, "total": 3, "page": 1, "pages": 1, "per_page": 200})
        if path == "/api/engine/queue/replace/":
            body = json.loads(request.post_data or "{}")
            self.engine.posts.append((path, body))
            if self.engine.hold_replace:
                self.engine.held.append((route, None))
                return None
            status, answer = self.engine.replace_answer(body)
            return self._json(route, answer, status)
        if path.startswith("/api/engine/status/log/"):
            return self._json(route, {"log_id": self.engine.log_id, "items": self.engine.history})
        if request.method == "POST":
            try:
                body = json.loads(request.post_data or "{}")
            except ValueError:
                body = request.post_data
            self.engine.posts.append((path, body))
            return self._json(route, {"ok": True})
        return self._json(route, {})

    def release(self, index=0, body=None):
        route, payload = self.engine.held.pop(index)
        self._json(route, payload if body is None else body)

    def wait_rows(self):
        self.page.wait_for_function("document.querySelectorAll('#queueList tr[data-item-id]').length > 0",
                                    timeout=8000)

    def js(self, expr):
        return self.page.evaluate(expr)

    def close(self):
        self.context.close()


def scenario_layout(browser, html, page_name="console"):
    path = "/" if page_name == "console" else "/remote-dj/"
    engine = Engine()
    p = Page(browser, html, engine, path=path)
    header = p.js("[...document.querySelectorAll('table.queue-table thead th')].map(th => th.textContent.trim())")
    check(f"{page_name}: header order Drag | NEXT | Swap | # | Artist | Title | Cat | Dur | Intro | Time On",
          header == HEADER, header)
    cells = p.js("[...document.querySelectorAll('#queueList tr[data-item-id=\"905\"] td')].map(td => td.className)")
    check(f"{page_name}: body cells follow the header",
          len(cells) == 10 and "qt-drag-handle" in cells[0] and "qt-next-cell" in cells[1]
          and "qt-swap-cell" in cells[2] and cells[3] == "qt-num" and cells[9] == "qt-time", cells)
    widths = p.js("""(() => { const h = [...document.querySelectorAll('table.queue-table thead th')];
        const r = [...document.querySelectorAll('#queueList tr[data-item-id="905"] td')];
        return h.map((th, i) => Math.abs(th.getBoundingClientRect().left - r[i].getBoundingClientRect().left)); })()""")
    check(f"{page_name}: header and body columns are aligned", max(widths) < 1, widths)
    check(f"{page_name}: NEXT keeps its pill and icon",
          p.js("(() => { const b = document.querySelector('#queueList tr[data-item-id=\"905\"] .qt-next-btn');"
               " return b.textContent.trim() === 'NEXT' && !!b.querySelector('.qt-next-badge svg'); })()"))
    swap = p.js("(() => { const b = document.querySelector('#queueList tr[data-item-id=\"905\"] .qt-swap-btn');"
                " return {label: b.getAttribute('aria-label'), svg: !!b.querySelector('svg'),"
                " stroke: b.querySelector('svg').getAttribute('stroke'), type: b.type}; })()")
    check(f"{page_name}: Swap is an accessible currentColor icon button",
          swap == {"label": "Swap this track", "svg": True, "stroke": "currentColor", "type": "button"}, swap)
    check(f"{page_name}: row numbers and category colours unchanged",
          p.js("[...document.querySelectorAll('#queueList tr[data-item-id] td.qt-num')].map(td => td.textContent)") == ["1", "2", "3"]
          and p.js("getComputedStyle(document.querySelector('#queueList tr[data-item-id=\"905\"]')).backgroundColor")
          == "rgb(90, 42, 120)")
    p.page.locator('#queueList tr[data-item-id="906"] .qt-next-btn').click()
    p.page.wait_for_timeout(300)
    check(f"{page_name}: NEXT still posts the existing set-next request",
          ("/api/engine/queue/set-next/", {"item_id": 906}) in engine.posts, engine.posts)
    # Drag from the handle of 906 onto 904: the reorder request names only upcoming rows.
    engine.posts.clear()
    src = p.page.locator('#queueList tr[data-item-id="906"] td.qt-drag-handle').bounding_box()
    dst = p.page.locator('#queueList tr[data-item-id="904"] td:nth-child(5)').bounding_box()
    p.page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    p.page.mouse.down()
    p.page.mouse.move(dst["x"] + 10, dst["y"] + dst["height"] / 2, steps=8)
    p.page.mouse.up()
    p.page.wait_for_timeout(300)
    reorder = [body for path_, body in engine.posts if path_.endswith("/reorder/")]
    check(f"{page_name}: drag-to-reorder unchanged", reorder == [{"order": [906, 904, 905]}], engine.posts)
    # A press on NEXT or Swap never starts a drag.
    engine.posts.clear()
    for selector in ('.qt-next-btn', '.qt-swap-btn'):
        box = p.page.locator(f'#queueList tr[data-item-id="905"] {selector}').bounding_box()
        p.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        p.page.mouse.down()
        p.page.mouse.move(dst["x"] + 10, dst["y"] + dst["height"] / 2, steps=5)
        p.page.mouse.up()
    p.page.wait_for_timeout(300)
    check(f"{page_name}: pressing NEXT or Swap never starts a drag",
          not [b for path_, b in engine.posts if path_.endswith("/reorder/")]
          and not p.js("queueDragActive"), engine.posts)
    check(f"{page_name}: no page errors", not p.errors, p.errors)
    p.close()

    p = Page(browser, html, Engine(), path=path, width=390)
    wrap = p.js("(() => { const w = document.querySelector('.queue-table-wrap');"
                " return {scrolls: w.scrollWidth > w.clientWidth, page: document.documentElement.scrollWidth"
                " - document.documentElement.clientWidth}; })()")
    check(f"{page_name}: phone width scrolls the table, not the page", wrap["page"] <= 1, wrap)
    check(f"{page_name}: no page errors (phone)", not p.errors, p.errors)
    p.close()


def scenario_layout_remote_dj(browser, html_rdj):
    scenario_layout(browser, html_rdj, "remote-dj")


def scenario_row_states(browser, html_history):
    """Show played items: played, on-air and on-deck rows carry neither NEXT nor Swap."""
    engine = Engine()
    engine.queue = [item(904, "Upcoming One", 60), item(905, "Upcoming Two", 300)]
    engine.history = [
        {"item_id": n, "position": n - 900, "track_id": n + 1000, "artist": f"Artist {n}", "title": f"Row {n}",
         "category": "Rock", "fill_color": "#283c78", "duration": 200.0, "intro_until_seconds": None,
         "played_at": 1_799_999_000 if n == 901 else None}
        for n in (901, 902, 903)
    ] + [{"item_id": n, "position": n - 900, "track_id": n + 1000, "artist": "", "title": "", "category": "",
          "fill_color": "", "duration": 0, "intro_until_seconds": None, "played_at": None} for n in (904, 905)]
    status = engine.status

    def with_decks():
        data = status()
        data["decks"] = {
            "A": {"log_item_id": 903, "playlist_log_id": engine.log_id, "actually_playing": True,
                  "air_started_at": 1_799_999_800},
            "B": {"log_item_id": 902, "playlist_log_id": engine.log_id, "actually_playing": False},
        }
        return data
    engine.status = with_decks
    p = Page(browser, html_history, engine)
    p.page.wait_for_function("document.querySelectorAll('#queueList tr[data-row-state]').length === 5", timeout=8000)
    states = p.js("""[...document.querySelectorAll('#queueList tr[data-item-id]')].map(tr => [tr.dataset.itemId,
        tr.dataset.rowState, !!tr.querySelector('.qt-next-btn'), !!tr.querySelector('.qt-swap-btn'),
        !!tr.querySelector('.qt-drag-handle'), tr.children.length])""")
    expected = [["901", "played", False, False, False, 10], ["903", "current", False, False, False, 10],
                ["902", "ondeck", False, False, False, 10], ["904", "upcoming", True, True, True, 10],
                ["905", "upcoming", True, True, True, 10]]
    check("played / on-air / on-deck rows have no NEXT, Swap or drag handle; every row has ten cells",
          sorted(states) == sorted(expected), states)
    check("the on-air row keeps its play indicator in the first column",
          p.js("!!document.querySelector('#queueList tr.qt-current td:first-child .qt-playing-icon')"))
    check("no page errors", not p.errors, p.errors)
    p.close()


def open_swap(p, item_id=905):
    p.page.locator(f'#queueList tr[data-item-id="{item_id}"] .qt-swap-btn').click()
    p.page.wait_for_function("document.getElementById('swapDialog').open", timeout=3000)


def search_and_choose(p, text="Rock", index=0):
    p.page.fill("#swapSearch", text)
    p.page.wait_for_function("document.querySelectorAll('#swapResults .qir-item').length === 3", timeout=3000)
    p.page.locator("#swapResults .qir-btn").nth(index).click()


def scenario_swap_dialog(browser, html):
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    check("swap: the dialog shows the target row",
          "Upcoming Two" in p.js("document.getElementById('swapTarget').textContent"))
    check("swap: focus moves to the search field", p.js("document.activeElement.id") == "swapSearch")
    search_and_choose(p, "Rock", 1)
    check("swap: uses the same search request as Search to add track",
          engine.searches and engine.searches[-1] == "q=Rock&per_page=200&ready2air=true", engine.searches)
    review = p.js("({choice: document.getElementById('swapChoice').textContent,"
                  " length: document.getElementById('swapLength').textContent,"
                  " confirm: !document.getElementById('swapConfirm').hidden && !document.getElementById('swapConfirm').disabled})")
    check("swap: review shows the chosen replacement and the length change before committing",
          "Rock Result 1" in review["choice"] and "4:00" in review["length"] and "shorter" in review["length"]
          and review["confirm"], review)
    check("swap: nothing is sent before Confirm", not [b for path, b in engine.posts if path.endswith("/replace/")])
    p.page.click("#swapConfirm")
    p.page.wait_for_function("!document.getElementById('swapDialog').open", timeout=3000)
    sent = [b for path, b in engine.posts if path.endswith("/replace/")]
    check("swap: Confirm sends exactly the row, its track, the choice and the engine instance",
          sent == [{"item_id": 905, "expected_track_id": 1905, "track_id": 7001, "engine_instance": "eng-1"}], sent)
    check("swap: no insert request was made", not [b for path, b in engine.posts if path.endswith("/insert/")])
    check("swap: focus returns to the row's Swap button",
          p.js("document.activeElement && document.activeElement.classList.contains('qt-swap-btn')"))
    check("swap: no page errors", not p.errors, p.errors)
    p.close()

    # Cancel (Escape) changes nothing.
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    search_and_choose(p)
    p.page.keyboard.press("Escape")
    p.page.wait_for_timeout(300)
    check("swap: Escape cancels without a request", not p.js("document.getElementById('swapDialog').open")
          and not [b for path, b in engine.posts if path.endswith("/replace/")])
    # Keyboard: Enter on the focused Swap button opens it.
    p.page.locator('#queueList tr[data-item-id="906"] .qt-swap-btn').focus()
    p.page.keyboard.press("Enter")
    p.page.wait_for_function("document.getElementById('swapDialog').open", timeout=3000)
    check("swap: keyboard-operable", "Upcoming Three" in p.js("document.getElementById('swapTarget').textContent"))
    check("swap: no page errors (cancel/keyboard)", not p.errors, p.errors)
    p.close()


def scenario_swap_guards(browser, html):
    # Double submission: one request while the first is in flight.
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    search_and_choose(p)
    engine.hold_replace = True
    p.page.click("#swapConfirm")
    p.page.evaluate("document.getElementById('swapConfirm').click(); confirmSwap(); confirmSwap();")
    p.page.wait_for_timeout(300)
    check("swap: double clicks send one request", len([b for path, b in engine.posts if path.endswith("/replace/")]) == 1)
    check("swap: Confirm is disabled while in flight", p.js("document.getElementById('swapConfirm').disabled"))
    p.release(0, {"ok": True, "changed": True})
    p.page.wait_for_function("!document.getElementById('swapDialog').open", timeout=3000)
    check("swap: the dialog closes after the server confirms", True)
    p.close()

    # A row that stops being upcoming (claimed by the engine) while the dialog is open.
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    search_and_choose(p)
    engine.queue = [q for q in engine.queue if q["item_id"] != 905]
    p.page.wait_for_function("document.getElementById('swapConfirm').disabled", timeout=4000)
    check("swap: a row that left the queue cannot be committed",
          "no longer upcoming" in p.js("document.getElementById('swapStatus').textContent"))
    p.page.evaluate("confirmSwap()")
    p.page.wait_for_timeout(200)
    check("swap: nothing is sent for it", not [b for path, b in engine.posts if path.endswith("/replace/")])
    p.close()

    # The row's track changed (someone else swapped it) or the engine restarted.
    for change, expect in (("track", "changed since"), ("engine", "engine restarted")):
        engine = Engine()
        p = Page(browser, html, engine)
        open_swap(p)
        if change == "track":
            engine.queue[1] = {**engine.queue[1], "track_id": 4242}
        else:
            status = engine.status
            engine.status = lambda: {**status(), "state_revision": {"instance": "eng-2", "seq": 1},
                                     "engine_session_id": "eng-2"}
        p.page.wait_for_function("document.getElementById('swapStatus').textContent.length > 0", timeout=4000)
        check(f"swap: {change} change is detected", expect in p.js("document.getElementById('swapStatus').textContent"),
              p.js("document.getElementById('swapStatus').textContent"))
        p.close()

    # A refusal from the server is shown and leaves the dialog open, disabled.
    engine = Engine()
    engine.replace_answer = lambda body: (409, {"ok": False, "reason": "item_changed",
                                                "error": "That queue row changed since it was shown."})
    p = Page(browser, html, engine)
    open_swap(p)
    search_and_choose(p)
    p.page.click("#swapConfirm")
    p.page.wait_for_function("document.getElementById('swapStatus').classList.contains('error')", timeout=3000)
    check("swap: a refusal is explained and the dialog stays open",
          p.js("document.getElementById('swapDialog').open")
          and "changed since it was shown" in p.js("document.getElementById('swapStatus').textContent")
          and p.js("document.getElementById('swapConfirm').disabled"))
    p.close()

    # Late search answers never replace newer results.
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    engine.hold_search = True
    p.page.fill("#swapSearch", "Old"); p.page.wait_for_timeout(450)
    p.page.fill("#swapSearch", "New"); p.page.wait_for_timeout(450)
    engine.hold_search = False
    p.release(1)
    p.page.wait_for_timeout(200)
    p.release(0)
    p.page.wait_for_timeout(300)
    titles = p.js("[...document.querySelectorAll('#swapResults .qir-item span:first-child')].map(s => s.textContent)")
    check("swap: a late search answer never replaces the newer results", titles and all(t.startswith("New") for t in titles), titles)
    p.close()

    # A stale console (no status for > 3 s) cannot commit.
    engine = Engine()
    p = Page(browser, html, engine)
    open_swap(p)
    search_and_choose(p)
    engine.status_ok = False
    p.page.wait_for_function("document.getElementById('consoleRoot').classList.contains('connection-stale')", timeout=8000)
    check("swap: a stale console disables Confirm", p.js("document.getElementById('swapConfirm').disabled"))
    p.page.evaluate("confirmSwap()")
    p.page.wait_for_timeout(200)
    check("swap: and sends nothing", not [b for path, b in engine.posts if path.endswith("/replace/")])
    check("swap: no page errors (guards)", not p.errors, p.errors)
    p.close()


def scenario_swap_unavailable_on_remote_dj(browser, html_rdj):
    p = Page(browser, html_rdj, Engine(), path="/remote-dj/")
    state = p.js("(() => { const b = document.querySelector('#queueList tr[data-item-id=\"905\"] .qt-swap-btn');"
                 " return {disabled: b.disabled, title: b.title}; })()")
    check("remote-dj: Swap is shown but unavailable for an account that cannot reach it",
          state == {"disabled": True, "title": "Swap is not available for this account"}, state)
    p.close()


def scenario_empty_queue(browser, html):
    engine = Engine()
    engine.queue = [item(904, "Only", 60)]
    p = Page(browser, html, engine)
    engine.queue = []
    p.page.wait_for_function("document.querySelector('#queueList .queue-empty') !== null", timeout=8000)
    check("empty state spans all ten columns", p.js("document.querySelector('#queueList .queue-empty').colSpan") == 10)
    check("no page errors", not p.errors, p.errors)
    p.close()


def main():
    render = Path(sys.argv[1])
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    console = (render / "console_page.html").read_text()
    rdj = (render / "remote_dj_page.html").read_text()
    history = (render / "console_history_page.html").read_text()
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scenario, html in ((scenario_layout, console), (scenario_layout_remote_dj, rdj),
                               (scenario_row_states, history), (scenario_empty_queue, console),
                               (scenario_swap_dialog, console), (scenario_swap_guards, console),
                               (scenario_swap_unavailable_on_remote_dj, rdj)):
            if only and only not in scenario.__name__:
                continue
            try:
                scenario(browser, html)
            except Exception as exc:
                check(f"{scenario.__name__} completed", False, repr(exc))
        print(f"browser {browser.version}")
        browser.close()
    print(f"{sum(ok for _n, ok in results)}/{len(results)} checks passed in {time.time() - started:.1f}s")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
