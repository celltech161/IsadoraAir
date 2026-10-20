"""D3E-D: the Operator Board's live status over a whole operator shift, in
headless Chromium with Playwright's fake clock -- no real-time sleeps decide
anything. The status endpoint is answered by the test (route interception), so
each refresh's outcome is exact; the page's own counters
(``window.ambBoardStatus.state()``) say when a refresh has settled.

Covered: more than 90 refreshes without the old silent cutoff, timeouts and
back-off, a persistent outage that is not hammered, session expiry, a hidden
tab and its return, a back-forward-cache restore, a version change, a stale
ON AIR that is never asserted as current, the worker chip, and user work
(expanded row, typed search, focus) preserved throughout."""
import json
import unittest

from active_messages.tests import test_board_browser       # (not the class itself: it would be re-run here)
from production.tests.support import ffmpeg_available
from production.tests.test_recorder_browser import in_browser, sync_playwright

BOARD = test_board_browser.BOARD
STATUS = "**/iportal/messages/board/status/"
EVERY, TIMEOUT, STALE_AFTER, MAX_BACKOFF = 20000, 10000, 60000, 300000


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
class LongSessionBrowserTests(test_board_browser.OperatorBoardBrowserTests):
    # Inherited layout tests are not re-run here.
    test_phones_show_the_status_and_the_list_first_without_horizontal_overflow = None
    test_long_names_stay_distinguishable_on_a_phone = None
    test_expansion_is_predictable_and_keyboard_accessible = None
    test_a_confirmation_fits_and_cancel_back_and_actions_keep_the_view = None
    test_a_double_click_publishes_once_and_a_stale_review_is_explained = None
    test_desktop_gains_columns_and_a_tablet_stays_within_its_width = None
    test_the_audio_preview_is_browser_local = None
    test_the_status_refresh_never_disturbs_an_expanded_row_or_typed_input = None
    test_a_light_station_theme_keeps_the_board_legible = None

    # -- harness ---------------------------------------------------------------------------------------
    def clocked(self, path=BOARD):
        """The board with a fake clock installed BEFORE it loads, and every
        status request answered by ``self.answer``."""
        context = self.browser.new_context(viewport={"width": 1100, "height": 900}, device_scale_factor=1)
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        page = context.new_page()
        page.errors, self.hits, self.held = [], [], []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.clock.install()
        self.mode, self.overrides = "ok", {}
        page.route(STATUS, self.handle)
        page.goto(self.live_server_url + path)
        self.version = page.get_attribute(".amb[data-status-url]", "data-version")
        return page

    def body(self):
        status = {"urgent": 0, "public_address": 1, "review": 0, "gate_open": False,
                  "chip": {"text": "Last: Completed", "class": ""}, "on_air": {"execution": None, "messages": []},
                  "latest": None, "over": [], "version": self.version,
                  "worker": {"state": "ok", "label": "Worker OK", "class": "ok"}}
        status.update(self.overrides)
        return json.dumps(status)

    def handle(self, route):
        self.hits.append(self.mode)
        if self.mode == "ok":
            route.fulfill(status=200, content_type="application/json", body=self.body())
        elif self.mode == "fail":
            route.abort()
        elif self.mode == "error":
            route.fulfill(status=503, content_type="application/json", body="{}")
        elif self.mode == "forbidden":
            route.fulfill(status=403, content_type="text/html", body="<h1>403 Forbidden</h1>")
        elif self.mode == "login_page":
            route.fulfill(status=200, content_type="text/html", body="<form>Sign in</form>")
        elif self.mode == "live":                                      # the real status view answers
            route.continue_()
        else:                                                          # "hold": answered later (or never)
            self.held.append(route)

    def close(self, page):
        """Release any deliberately unanswered request, then close."""
        page.unroute_all(behavior="ignoreErrors")
        while self.held:
            try:
                self.held.pop().abort()
            except Exception:  # noqa: BLE001 -- the page already gave up on it
                pass
        page.context.close()

    def state(self, page):
        return page.evaluate("window.ambBoardStatus.state()")

    def settle(self, page):
        """Until no request is outstanding (each evaluate lets the page run its tasks)."""
        for _ in range(2000):
            state = self.state(page)
            if not state["inflight"]:
                return state
        self.fail(f"a status request never settled: {state}")

    def until_requests(self, page, count):
        """Until the test has seen ``count`` status requests (route callbacks run
        while Playwright processes events; each evaluate lets it do so)."""
        for _ in range(2000):
            if len(self.hits) >= count:
                return
            page.evaluate("0")
        self.fail(f"expected {count} status requests, saw {len(self.hits)}")

    def advance(self, page, ms):
        page.clock.run_for(ms)
        return self.settle(page)

    def stale(self, page) -> bool:
        return page.get_attribute(".amb[data-status-url]", "data-status-stale") == "1"

    # -- tests -----------------------------------------------------------------------------------------
    @in_browser
    def test_refresh_continues_well_beyond_ninety_cycles_and_keeps_user_work(self):
        message = self.live(self.producer, self.notice, "Library closed Monday")
        page = self.clocked()
        page.click(f"#m-{message.pk} details.amb-row > summary")              # expanded row
        page.fill("input[name=q]", "typed but not submitted")
        page.focus("input[name=q]")
        for cycle in range(1, 121):                                           # the old helper stopped at 90
            self.advance(page, EVERY)
            self.assertEqual(len(self.hits), cycle, f"refresh {cycle} was not sent")
        state = self.state(page)
        self.assertEqual((state["applied"], state["failures"], state["stopped"], state["scheduled"]),
                         (120, 0, False, True))
        self.assertFalse(self.stale(page))
        self.assertTrue(page.locator(f"#m-{message.pk} details.amb-row").evaluate("el => el.open"))
        self.assertEqual(page.input_value("input[name=q]"), "typed but not submitted")
        self.assertEqual(page.evaluate("document.activeElement.name"), "q")
        self.assertEqual(page.errors, [])
        self.close(page)

    @in_browser
    def test_a_timeout_marks_stale_backs_off_and_recovers(self):
        page = self.clocked()
        self.mode = "hold"
        page.clock.run_for(EVERY)                                             # request 1 sent, never answered
        self.assertEqual(len(self.hits), 1)
        self.assertTrue(self.state(page)["inflight"])
        state = self.advance(page, TIMEOUT)                                   # aborted by its own timeout
        self.assertTrue(self.stale(page))
        self.assertEqual((state["failures"], state["delay"]), (1, 2 * EVERY))
        self.mode = "ok"
        self.advance(page, 2 * EVERY - 1000)
        self.assertEqual(len(self.hits), 1)                                   # backing off: nothing yet
        state = self.advance(page, 1000)
        self.assertEqual(len(self.hits), 2)
        self.assertFalse(self.stale(page))                                    # recovered
        self.assertEqual((state["failures"], state["delay"]), (0, EVERY))
        self.assertEqual(page.errors, [])
        self.close(page)

    @in_browser
    def test_a_persistent_outage_is_not_hammered(self):
        page = self.clocked()
        self.mode = "fail"
        for _ in range(90):                                                   # 30 simulated minutes
            self.advance(page, EVERY)
        # 20 s, then +40, +80, +160, +300 (capped) ... -- not one request every 20 s
        self.assertLessEqual(len(self.hits), 10)
        self.assertGreaterEqual(len(self.hits), 6)
        state = self.state(page)
        self.assertEqual((state["delay"], state["stopped"]), (MAX_BACKOFF, False))
        self.assertTrue(self.stale(page))
        self.assertIn("retrying less often", page.text_content("#ambStale"))
        self.mode = "ok"
        before = len(self.hits)
        self.advance(page, MAX_BACKOFF)
        self.assertEqual(len(self.hits), before + 1)
        self.assertFalse(self.stale(page))
        self.assertEqual(self.state(page)["delay"], EVERY)
        self.close(page)

    @in_browser
    def test_an_expired_session_stops_refreshing_and_says_so(self):
        for mode in ("forbidden", "login_page"):
            with self.subTest(mode=mode):
                page = self.clocked()
                self.mode = mode
                self.advance(page, EVERY)
                self.assertEqual(len(self.hits), 1)
                state = self.state(page)
                self.assertEqual((state["stopped"], state["scheduled"]), (True, False))
                self.assertTrue(page.locator("#ambExpired").is_visible())
                self.assertTrue(self.stale(page))
                self.advance(page, 30 * 60 * 1000)                            # never retried in a loop
                self.assertEqual(len(self.hits), 1)
                self.assertEqual(page.errors, [])
                self.close(page)

    @in_browser
    def test_a_hidden_tab_sends_nothing_and_refreshes_on_return(self):
        page = self.clocked()
        page.evaluate("""() => {
            window.__hidden = false;
            Object.defineProperty(document, 'hidden', {configurable: true, get: () => window.__hidden});
            Object.defineProperty(document, 'visibilityState', {configurable: true,
                get: () => window.__hidden ? 'hidden' : 'visible'});
        }""")
        self.advance(page, EVERY)
        self.assertEqual(len(self.hits), 1)
        page.evaluate("window.__hidden = true; document.dispatchEvent(new Event('visibilitychange'))")
        self.advance(page, 10 * 60 * 1000)                                    # ten hidden minutes
        self.assertEqual(len(self.hits), 1)
        self.mode = "hold"
        page.evaluate("window.__hidden = false; document.dispatchEvent(new Event('visibilitychange'))")
        self.until_requests(page, 2)                                          # refreshed at once on return
        self.assertEqual(len(self.hits), 2)
        self.assertTrue(self.stale(page))                                     # ...and marked old until answered
        self.held.pop().fulfill(status=200, content_type="application/json", body=self.body())
        self.settle(page)
        self.assertFalse(self.stale(page))
        self.close(page)

    @in_browser
    def test_a_back_forward_restore_rechecks_at_once(self):
        page = self.clocked()
        self.advance(page, EVERY)
        page.evaluate("document.querySelector('.amb form[method=post]').dataset.sent = '1'")
        self.mode = "hold"
        page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        self.until_requests(page, 2)
        self.assertEqual(len(self.hits), 2)
        self.assertTrue(self.stale(page))
        self.assertIsNone(page.get_attribute(".amb form[method=post]", "data-sent"))   # forms usable again
        self.held.pop().fulfill(status=200, content_type="application/json", body=self.body())
        self.settle(page)
        self.assertFalse(self.stale(page))
        self.close(page)

    @in_browser
    def test_a_version_change_offers_a_reload_and_keeps_user_work(self):
        message = self.live(self.producer, self.notice, "Library closed Monday")
        page = self.clocked(BOARD + "?filter=active&q=Library")
        page.click(f"#m-{message.pk} details.amb-row > summary")
        page.fill("input[name=q]", "unsent words")
        self.overrides = {"version": "999.999"}
        self.advance(page, EVERY)
        self.assertTrue(page.locator("#ambChanged").is_visible())
        self.assertEqual(page.get_attribute("#ambChanged [data-reload]", "href"), "/iportal/messages/board/"
                         "?filter=active&q=Library")                        # the same view, not a reset
        self.assertTrue(page.locator(f"#m-{message.pk} details.amb-row").evaluate("el => el.open"))
        self.assertEqual(page.input_value("input[name=q]"), "unsent words")
        self.close(page)

    @in_browser
    def test_a_stale_view_never_asserts_on_air_as_current(self):
        message = self.live(self.producer, self.notice, "Library closed Monday")
        page = self.clocked()
        self.overrides = {"on_air": {"execution": None, "messages": [str(message.pk)]},
                          "chip": {"text": "● ON AIR", "class": "onair"}}
        self.advance(page, EVERY)
        badge = f"#m-{message.pk} span.amb-state.onair"
        self.assertTrue(page.locator(badge).is_visible())
        self.assertEqual(page.eval_on_selector("#ambAiring", "el => getComputedStyle(el, '::after').content"),
                         "none")
        self.mode = "fail"
        self.advance(page, EVERY)
        self.assertTrue(self.stale(page))
        for selector in ("#ambAiring", badge):
            self.assertEqual(page.eval_on_selector(selector, "el => getComputedStyle(el).opacity"), "0.55")
            self.assertEqual(page.eval_on_selector(selector, "el => getComputedStyle(el, '::after').content"),
                             '" (last known)"')
        self.close(page)

    @in_browser
    def test_the_worker_chip_follows_monitoring(self):
        page = self.clocked()
        self.overrides = {"worker": {"state": "critical", "label": "Worker critical — stale", "class": "alert"}}
        self.advance(page, EVERY)
        self.assertEqual(page.text_content("#ambWorker").strip(), "Worker critical — stale")
        self.assertIn("alert", page.get_attribute("#ambWorker", "class"))
        self.assertEqual(page.get_attribute("#ambWorker", "data-worker-state"), "critical")
        self.close(page)

    @in_browser
    def test_an_untrusted_monitoring_state_is_shown_as_unavailable_and_recovers(self):
        """Corrective pass: an untrusted Monitoring document now arrives as a
        normal status answer (worker "monitoring_unavailable"), never a 500 --
        so the rest of the status keeps updating and the page is not stale."""
        page = self.clocked()
        self.advance(page, EVERY)
        self.assertIn("ok", page.get_attribute("#ambWorker", "class"))
        self.overrides = {"worker": {"state": "monitoring_unavailable", "label": "Monitoring status unavailable",
                                     "class": "off"}, "urgent": 3}
        self.advance(page, EVERY)
        self.assertEqual(page.get_attribute("#ambWorker", "data-worker-state"), "monitoring_unavailable")
        self.assertEqual(page.text_content("#ambWorker").strip(), "Monitoring status unavailable")
        self.assertNotIn("ok", page.get_attribute("#ambWorker", "class").split())
        self.assertEqual(page.text_content('[data-count="urgent"]'), "3")    # the rest still updates
        self.assertFalse(self.stale(page))
        self.overrides = {}
        self.advance(page, EVERY)
        self.assertEqual(page.get_attribute("#ambWorker", "data-worker-state"), "ok")
        state = self.state(page)
        self.assertEqual((state["sent"], state["failures"], state["stopped"]), (3, 0, False))
        self.assertEqual(len(self.hits), 3)                                  # one request per refresh
        self.close(page)

    @in_browser
    def test_an_incomplete_worker_entry_from_the_real_status_view(self):
        """Nested-schema correction, end to end: the real status view reads a
        disposable Monitoring document; an incomplete worker entry turns the
        chip unavailable (never Worker OK) as an ordinary 200 answer, a
        malformed document likewise, and a valid observation restores it."""
        import tempfile
        import time
        from pathlib import Path
        from unittest import mock

        from active_messages import diagnostics
        from monitoring.models import MonitorCheck
        holder = tempfile.TemporaryDirectory(prefix="am-monitoring-")
        self.addCleanup(holder.cleanup)
        state_file = Path(holder.name) / "monitoring_state.json"
        patcher = mock.patch.object(diagnostics, "MONITORING_STATE_PATH", state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        check = MonitorCheck.objects.filter(kind="systemd", systemd_unit=diagnostics.WORKER_UNIT).first() \
            or MonitorCheck.objects.create(kind="systemd", systemd_unit=diagnostics.WORKER_UNIT, name="Worker")
        MonitorCheck.objects.filter(pk=check.pk).update(enabled=True)

        def observe(*, drop=None, checks=None, status="ok", **changes):
            detail = {"active_state": "active", "sub_state": "running", "uptime_seconds": 597.0,
                      "restart_via_dashboard": False, "n_restarts": 0, "native_airing": "closed",
                      "phase": "polling", "outcome": "idle", "code": "", "execution": "",
                      "heartbeat_age_seconds": 3.1, "reason": "idle", **changes}
            if drop:
                del detail[drop]
            entry = {"id": check.pk, "name": check.name, "kind": "systemd", "status": status, "detail": detail}
            state_file.write_text(json.dumps({"timestamp": time.time() - 2,
                                              "checks": [entry] if checks is None else checks}))

        observe()
        page = self.clocked()
        self.mode = "live"
        self.advance(page, EVERY)
        self.assertEqual((page.get_attribute("#ambWorker", "data-worker-state"),
                          page.text_content("#ambWorker").strip()), ("ok", "Worker OK — idle"))
        observe(reason="rendering", phase="rendering", outcome="",           # the producer's first-cycle render
                execution="0f8fad5b-d9cb-469f-a165-70867728950e")
        self.advance(page, EVERY)
        self.assertEqual((page.get_attribute("#ambWorker", "data-worker-state"),
                          page.text_content("#ambWorker").strip()), ("ok", "Worker OK — rendering"))
        observe(status="critical", reason="crash_looping", heartbeat_age_seconds=899.9)   # within the crash window
        self.advance(page, EVERY)
        self.assertEqual((page.get_attribute("#ambWorker", "data-worker-state"),
                          page.text_content("#ambWorker").strip()), ("critical", "Worker critical — crash_looping"))
        self.assertIn("alert", page.get_attribute("#ambWorker", "class").split())
        for label, kwargs in (("crash loop beyond its window", {"status": "critical", "reason": "crash_looping",
                                                                "heartbeat_age_seconds": 901.0}),
                              ("missing reason", {"drop": "reason"}), ("missing active_state",
                                                                         {"drop": "active_state"}),
                              ("impossible evidence", {"reason": "busy", "outcome": "idle"}),
                              ("checks not a list", {"checks": 1})):
            with self.subTest(label):
                observe(**kwargs)
                self.advance(page, EVERY)
                self.assertEqual(page.get_attribute("#ambWorker", "data-worker-state"), "monitoring_unavailable")
                text = page.text_content("#ambWorker").strip()
                self.assertTrue(text.startswith("Monitoring status unavailable"), text)
                self.assertNotIn("Worker OK", text)
                self.assertNotIn("Worker critical", text)
                self.assertNotIn("ok", page.get_attribute("#ambWorker", "class").split())
                if "checks" not in kwargs:
                    self.assertEqual(text, "Monitoring status unavailable — worker check data is incomplete")
                self.assertFalse(self.stale(page))                           # unavailable worker != stale page
                self.assertEqual(self.state(page)["failures"], 0)            # an ordinary 200 answer
        observe()
        self.advance(page, EVERY)
        self.assertEqual(page.get_attribute("#ambWorker", "data-worker-state"), "ok")
        state = self.state(page)
        self.assertEqual((state["sent"], state["failures"], state["stopped"], state["scheduled"]), (9, 0, False, True))
        self.assertEqual(len(self.hits), 9)                                  # one request per refresh
        self.assertEqual(page.errors, [])
        self.close(page)
