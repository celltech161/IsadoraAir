"""Roadmap 3.1D corrective pass -- async context hardening, in a real browser.

These tests hold individual network responses and release them OUT OF ORDER to
prove that a stale response can neither repaint the schedule UI nor let a click
write into the wrong context.  They need Playwright + Chromium, which are not
part of the standard test requirements, so the whole module skips cleanly when
they are unavailable.  To run it on a machine that has Playwright installed in
a different virtualenv, point ``PLAYWRIGHT_SITE_PACKAGES`` at that venv's
site-packages directory (it is appended to ``sys.path``, never prepended).

Everything runs through ``manage.py test`` against the throwaway test database
(LiveServerTestCase); nothing here can reach any other database.
"""
import contextlib
import os
import re
import sys
import unittest
from datetime import date, time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, LiveServerTestCase, override_settings

from authz.models import Capability
from library.models import Rotation, ScheduleBlock, ScheduleProfile
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state

try:  # pragma: no cover - environment dependent
    extra = os.environ.get("PLAYWRIGHT_SITE_PACKAGES")
    if extra and extra not in sys.path:
        sys.path.append(extra)
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

MONDAY = date(2027, 3, 1)
TUESDAY = date(2027, 3, 2)
WIDTHS = (1366, 768, 390)


@unittest.skipIf(sync_playwright is None, "Playwright is not installed")
@override_settings(SECURE_SSL_REDIRECT=False)
class AsyncContextBrowserTests(LiveServerTestCase):
    def setUp(self):
        # Playwright's sync API runs an event loop; Django's ORM guard objects
        # to the test thread touching the DB beside it.
        patcher = patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A TransactionTestCase flush removes migration-seeded rows.
        Capability.objects.get_or_create(slug="schedule.edit", defaults={"label": "Edit schedule"})
        self.profile = ensure_schedule_profile_state().active_profile
        self.second = ScheduleProfile.objects.create(name="Second Profile")
        self.archived = ScheduleProfile.objects.create(name="Archived Profile", is_archived=True)
        self.alpha = Rotation.objects.create(name="Alpha Rot")
        self.bravo = Rotation.objects.create(name="Bravo Rot")
        self.charlie = Rotation.objects.create(name="Charlie Rot")
        # Default profile: Monday 10:00 Alpha, 11:00 Alpha; Tuesday 10:00 Bravo.
        for dow, hour, rotation in ((0, 10, self.alpha), (0, 11, self.alpha), (1, 10, self.bravo)):
            self.weekly(self.profile, dow, hour, rotation)
        self.weekly(self.second, 0, 10, self.charlie)
        self.user = get_user_model().objects.create_superuser("race", "r@example.invalid", "pw")
        self.held = []
        self.errors = []

    def weekly(self, profile, dow, hour, rotation, minute=0):
        return ScheduleBlock.objects.create(
            profile=profile, day_of_week=dow, start_time=time(hour, minute),
            end_time=time((hour + 1) % 24, 0), rotation=rotation,
        )

    # ---- browser plumbing -------------------------------------------------
    @contextlib.contextmanager
    def session(self):
        """Playwright, with every browser closed BEFORE the driver stops, and
        every Django DB connection opened inside the session closed with it.

        r0104 / 1.18: Django keeps connections in context-local storage.
        Inside ``sync_playwright()`` ORM calls (force_login, DB assertions)
        resolve to a different contextvars context, so the connection they
        open is invisible to the test's ordinary teardown, which runs after
        this block exits. Left open, it made Django's final DROP of the test
        database fail with "being accessed by other users" (exit 1) after every
        test had passed. Close it here, deterministically, while its context is
        still current and after the browsers (and their in-flight requests to
        the live server) are gone.
        """
        self._browsers = []
        with sync_playwright() as pw:
            try:
                yield pw
            finally:
                for browser in self._browsers:
                    try:
                        browser.close()
                    except Exception:
                        pass
                connections.close_all()

    def open_page(self, pw, width, height=900):
        browser = pw.chromium.launch()
        self._browsers.append(browser)
        context = browser.new_context(viewport={"width": width, "height": height})
        client = Client()
        client.force_login(self.user)
        host = self.live_server_url.split("//")[1].split(":")[0]
        context.add_cookies([{
            "name": "sessionid", "value": client.cookies["sessionid"].value,
            "domain": host, "path": "/",
        }])
        page = context.new_page()
        page.set_default_timeout(8000)
        page.on("pageerror", lambda exc: self.errors.append(f"pageerror: {exc}"))
        page.goto(self.live_server_url + "/schedule/")
        page.wait_for_function("document.getElementById('profileSelect').options.length >= 2")
        return page

    def hold(self, page, pattern):
        """Intercept matching requests and keep them in flight until released."""
        page.unroute_all()  # routes stack; start from a clean slate
        self.held = []
        page.route(re.compile(pattern), lambda route: self.held.append(route))

    def wait_held(self, page, count, timeout_ms=6000):
        waited = 0
        while len(self.held) < count and waited < timeout_ms:
            page.wait_for_timeout(50)  # route handlers run while the driver is serviced
            waited += 50
        self.assertGreaterEqual(len(self.held), count, f"expected {count} held request(s)")

    def wait_settled(self, page, minimum=1):
        """Wait until the number of held requests stops changing.  (A date
        input's fill() already fires 'change', so exact counts are brittle.)"""
        stable, last, waited = 0, -1, 0
        while waited < 6000 and not (stable >= 4 and len(self.held) >= minimum):
            page.wait_for_timeout(75)
            waited += 75
            stable = stable + 1 if len(self.held) == last else 0
            last = len(self.held)
        self.assertGreaterEqual(len(self.held), minimum)

    def release_all(self, page, *, newest_first=False):
        """Answer every held request, then stop holding so later requests
        (for example the reload after a write) flow normally."""
        routes = list(reversed(self.held)) if newest_first else list(self.held)
        for route in routes:
            route.continue_()
        page.unroute_all()

    def release(self, route, *, mutate=None):
        """Let the real server answer; optionally fabricate a stale payload."""
        if mutate is None:
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        mutate(payload)
        route.fulfill(response=response, json=payload)

    def pick(self, page, name):
        page.locator(".content-picker .rotation-btn", has_text=name).first.click()

    def to_date_mode(self, page, iso=MONDAY.isoformat()):
        page.click("#dateModeButton")
        page.fill("#overrideDate", iso)
        page.dispatch_event("#overrideDate", "change")
        page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")

    def row_text(self, page, hour):
        return page.locator("#dateHourList .date-hour-row").nth(hour).inner_text()

    def assert_no_overflow(self, page, width):
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width + 2)

    def assert_clean(self):
        self.assertEqual([e for e in self.errors if "Failed to load resource" not in e], [])

    @staticmethod
    def make_date_stale(payload):
        for cell in payload["cells"]:
            if cell["effective_block"]:
                cell["effective_block"]["content_name"] = "STALE-DATA"

    @staticmethod
    def make_weekly_stale(payload):
        for block in payload["blocks"]:
            block["content_name"] = "STALE-WEEKLY"

    # ---- harness: a browser session leaves no DB session behind -----------
    def test_a_browser_session_leaves_no_database_connection_behind(self):
        """r0104 / 1.18 regression: ORM use inside sync_playwright() (here the
        session-cookie login plus a DB read) must not leave a connection open
        on the test database once the session ends -- that orphan used to make
        the final DROP DATABASE fail after every test passed."""
        with self.session() as pw:
            page = self.open_page(pw, 1366)            # force_login inside Playwright
            self.assertTrue(ScheduleProfile.objects.filter(pk=self.second.pk).exists())
            self.assertTrue(page.locator("#profileSelect").count())
        with connection.cursor() as cur:
            # Client backends only: DROP DATABASE itself terminates any
            # autovacuum worker, so only these can block it.
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND backend_type = 'client backend'"
            )
            others = cur.fetchone()[0]
        self.assertEqual(others, 0, "a database session outlived the browser session")

    # ---- 1. non-interactive while the new context loads --------------------
    def test_old_rows_are_removed_and_busy_while_a_new_date_loads(self):
        with self.session() as pw:
            for width in WIDTHS:
                with self.subTest(width=width):
                    page = self.open_page(pw, width)
                    self.to_date_mode(page)
                    self.assertIn("Alpha Rot", self.row_text(page, 10))
                    self.hold(page, r".*/api/schedule/\?.*date=2027-03-02.*")
                    page.fill("#overrideDate", TUESDAY.isoformat())
                    page.dispatch_event("#overrideDate", "change")
                    self.wait_settled(page)

                    # Before the new response: the old A rows are gone, not
                    # merely hidden, and the surface says it is busy.
                    self.assertEqual(page.locator("#dateHourList .date-hour-row").count(), 0)
                    self.assertEqual(page.locator("#dateHourList .date-hour-main").count(), 0)
                    self.assertEqual(page.get_attribute("#dateHourList", "aria-busy"), "true")
                    self.assertIn("Loading schedule", page.inner_text("#dateHourList"))
                    # The heading already names the newest request's date.
                    self.assertIn("Tuesday", page.inner_text("#selectedDateTitle"))
                    self.assert_no_overflow(page, width)

                    self.release_all(page)
                    page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
                    self.assertEqual(page.get_attribute("#dateHourList", "aria-busy"), "false")
                    self.assertIn("Bravo Rot", self.row_text(page, 10))
                    self.assertIn("Tuesday", page.inner_text("#selectedDateTitle"))
                    self.assert_no_overflow(page, width)
                    page.close()
        self.assert_clean()

    # ---- 2. same profile/date, overlapping requests ------------------------
    def test_an_older_request_for_the_same_context_cannot_repaint_over_a_newer_one(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.to_date_mode(page)
            self.hold(page, r".*/api/schedule/\?.*date=2027-03-01.*")
            page.evaluate("() => { loadDateSchedule(); }")   # A1
            page.evaluate("() => { loadDateSchedule(); }")   # A2: same profile, same date
            self.wait_held(page, 2)
            first, second = self.held[0], self.held[1]

            self.release(second)                   # A2 (newest) returns first
            page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
            self.assertIn("Alpha Rot", self.row_text(page, 10))
            self.release(first, mutate=self.make_date_stale)   # A1 returns late, stale
            page.wait_for_timeout(400)

            self.assertNotIn("STALE-DATA", page.inner_text("#dateHourList"))
            self.assertIn("Alpha Rot", self.row_text(page, 10))
            self.assertEqual(page.get_attribute("#dateHourList", "aria-busy"), "false")
        self.assert_clean()

    def test_a_stale_failure_cannot_replace_the_newer_contexts_ui(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.to_date_mode(page)
            self.hold(page, r".*/api/schedule/\?.*date=2027-03-01.*")
            page.evaluate("() => { loadDateSchedule(); }")   # A1
            page.evaluate("() => { loadDateSchedule(); }")   # A2
            self.wait_held(page, 2)
            self.release(self.held[1])
            page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
            self.held[0].fulfill(status=500, json={"error": "late boom"})
            page.wait_for_timeout(400)

            self.assertEqual(page.locator("#dateHourList .date-hour-row").count(), 24)
            self.assertNotIn("late boom", page.inner_text("#apiError"))
        self.assert_clean()

    def test_rapid_date_and_profile_switching_settles_on_the_visible_context(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.to_date_mode(page)
            self.hold(page, r".*/api/schedule/\?.*date=2027-03-0[12].*")
            for iso in (TUESDAY, MONDAY, TUESDAY, MONDAY):
                page.fill("#overrideDate", iso.isoformat())
                page.dispatch_event("#overrideDate", "change")
            self.wait_settled(page, minimum=4)
            self.release_all(page, newest_first=True)    # worst case: newest first, then the rest
            page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
            page.wait_for_timeout(300)
            self.assertIn("Monday", page.inner_text("#selectedDateTitle"))
            self.assertIn("Alpha Rot", self.row_text(page, 10))

            # Profile switch with the old rows on screen, response held.
            self.hold(page, r".*/api/schedule/\?profile=.*date=2027-03-01.*")
            page.select_option("#profileSelect", label="Second Profile")
            self.wait_settled(page)
            self.assertEqual(page.locator("#dateHourList .date-hour-row").count(), 0)
            self.release_all(page)
            page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
            self.assertIn("Charlie Rot", self.row_text(page, 10))
        self.assert_clean()

    # ---- 2b. Weekly/profile list request ordering -------------------------
    def test_weekly_profile_switch_ignores_late_success_and_late_failure(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.hold(page, r".*/api/schedule/\?profile=.*")
            page.evaluate("() => { loadSchedule(); }")  # A: active profile
            page.select_option("#profileSelect", label="Second Profile")  # B
            self.wait_held(page, 2)
            request_a, request_b = self.held[0], self.held[1]

            self.release(request_b)
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Charlie Rot'"
            )
            self.release(request_a, mutate=self.make_weekly_stale)
            page.wait_for_timeout(400)
            self.assertNotIn("STALE-WEEKLY", page.inner_text("#weeklyDesktop"))
            self.assertIn("Charlie Rot", page.inner_text(
                '.grid-cell[data-day="0"][data-hour="10"]'
            ))

            # Repeat with the older request failing after the newer context
            # has painted; the stale error must remain silent.
            self.hold(page, r".*/api/schedule/\?profile=.*")
            page.evaluate("() => { loadSchedule(); }")  # A1: Second
            page.select_option("#profileSelect", label=self.profile.name)  # B1: active
            self.wait_held(page, 2)
            old, newest = self.held[0], self.held[1]
            self.release(newest)
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Alpha Rot'"
            )
            old.fulfill(status=500, json={"error": "stale weekly boom"})
            page.wait_for_timeout(400)
            self.assertNotIn("stale weekly boom", page.inner_text("#apiError"))
            self.assertEqual(page.get_attribute("#weeklyDesktop", "aria-busy"), "false")
        self.assert_clean()

    def test_profile_list_reordering_preserves_the_newest_preferred_selection(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.hold(page, r".*/api/schedule/profiles/$")
            page.evaluate("uuid => { loadProfiles(uuid); }", str(self.profile.uuid))
            page.evaluate("uuid => { loadProfiles(uuid); }", str(self.second.uuid))
            self.wait_held(page, 2)
            request_a, request_b = self.held[0], self.held[1]

            self.release(request_b)
            page.wait_for_function(
                "uuid => document.getElementById('profileSelect').value === uuid",
                arg=str(self.second.uuid),
            )
            self.release(request_a)
            page.wait_for_timeout(400)
            self.assertEqual(page.input_value("#profileSelect"), str(self.second.uuid))
            self.assertIn("Charlie Rot", page.inner_text(
                '.grid-cell[data-day="0"][data-hour="10"]'
            ))

            self.hold(page, r".*/api/schedule/profiles/$")
            page.evaluate("uuid => { loadProfiles(uuid); }", str(self.profile.uuid))
            page.evaluate("uuid => { loadProfiles(uuid); }", str(self.second.uuid))
            self.wait_held(page, 2)
            old, newest = self.held[0], self.held[1]
            self.release(newest)
            page.wait_for_function(
                "uuid => document.getElementById('profileSelect').value === uuid",
                arg=str(self.second.uuid),
            )
            old.fulfill(status=500, json={"error": "stale profile boom"})
            page.wait_for_timeout(400)
            self.assertNotIn("stale profile boom", page.inner_text("#apiError"))
        self.assert_clean()

    # ---- writes land only in the visibly selected context ------------------
    def test_a_write_after_rapid_navigation_lands_in_the_visible_context_only(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.to_date_mode(page)
            self.hold(page, r".*/api/schedule/\?profile=.*date=2027-03-02.*")
            page.fill("#overrideDate", TUESDAY.isoformat())
            page.dispatch_event("#overrideDate", "change")
            self.wait_settled(page)
            # The stale Monday rows cannot be clicked: they no longer exist.
            self.assertEqual(page.locator("#dateHourList .date-hour-main").count(), 0)
            self.release_all(page)
            page.wait_for_function("document.querySelectorAll('#dateHourList .date-hour-row').length === 24")
            self.pick(page, "Charlie Rot")
            page.locator("#dateHourList .date-hour-main").nth(15).click()
            page.wait_for_function(
                "document.querySelectorAll('#dateHourList .date-hour-row')[15]?.innerText.includes('Charlie Rot') === true")
            written = list(ScheduleBlock.objects.filter(specific_date__isnull=False).values_list(
                "specific_date", "profile_id", "start_time"))
            self.assertEqual(written, [(TUESDAY, self.profile.id, time(15, 0))])
        self.assert_clean()

    # ---- 3. Hour Detail: a stale response cannot repaint a newer hour ------
    def open_weekly_detail(self, page, hour):
        page.locator(f'.grid-cell[data-day="0"][data-hour="{hour}"] .cell-detail-btn').click()

    def test_a_late_response_for_hour_a_cannot_repaint_hour_b(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Alpha Rot'")
            self.hold(page, r".*/api/schedule/hour-detail/.*")
            self.open_weekly_detail(page, 10)      # request A
            self.wait_held(page, 1)
            self.open_weekly_detail(page, 11)      # request B supersedes A
            self.wait_held(page, 2)
            request_a, request_b = self.held[0], self.held[1]
            # While loading, no stale A minutes are on screen to click.
            self.assertEqual(page.locator("#minuteGrid .minute-cell").count(), 0)

            self.release(request_b)
            page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")
            self.assertIn("11:00a", page.inner_text("#hourDetailTitle"))

            def stale(payload):
                for minute in payload["minutes"]:
                    if minute["effective_block"]:
                        minute["effective_block"]["content_name"] = "STALE-HOUR-A"
            self.release(request_a, mutate=stale)  # A answers late
            page.wait_for_timeout(400)

            self.assertIn("11:00a", page.inner_text("#hourDetailTitle"))
            self.assertEqual(page.locator("#minuteGrid .minute-cell").count(), 60)
            titles = page.eval_on_selector_all("#minuteGrid .minute-cell", "els => els.map(e => e.title)")
            self.assertFalse([t for t in titles if "STALE-HOUR-A" in t])

            # A write from the visible panel lands on hour 11, never hour 10.
            page.unroute_all()  # nothing further is held
            self.pick(page, "Bravo Rot")
            page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
            page.wait_for_function(
                "document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 2")
            starts = list(ScheduleBlock.objects.filter(
                profile=self.profile, day_of_week=0, start_time__minute=30).values_list("start_time", flat=True))
            self.assertEqual(starts, [time(11, 30)])
        self.assert_clean()

    # ---- 4. a closed Hour Detail stays closed ------------------------------
    def test_a_response_arriving_after_close_neither_reopens_nor_repaints(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Alpha Rot'")
            self.hold(page, r".*/api/schedule/hour-detail/.*")
            self.open_weekly_detail(page, 10)
            self.wait_held(page, 1)
            page.click("#hourDetailClose")
            self.assertTrue(page.locator("#hourDetail").is_hidden())
            title_before = page.inner_text("#hourDetailTitle")

            self.release(self.held[0])
            page.wait_for_timeout(500)
            self.assertTrue(page.locator("#hourDetail").is_hidden())
            self.assertEqual(page.locator("#minuteGrid .minute-cell").count(), 0)
            self.assertEqual(page.inner_text("#hourDetailTitle"), title_before)

            # A late FAILURE after close must not surface either.
            self.hold(page, r".*/api/schedule/hour-detail/.*")
            self.open_weekly_detail(page, 10)
            self.wait_held(page, 1)
            page.click("#hourDetailClose")
            self.held[0].fulfill(status=500, json={"error": "stale hour boom"})
            page.wait_for_timeout(400)
            self.assertNotIn("stale hour boom", page.inner_text("#apiError"))
            self.assertTrue(page.locator("#hourDetail").is_hidden())
        self.assert_clean()

    def test_right_click_delete_response_cannot_repaint_a_new_hour_context(self):
        transition = self.weekly(self.profile, 0, 11, self.bravo, minute=30)
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            self.open_weekly_detail(page, 11)
            page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")
            self.hold(page, r".*/api/schedule/\d+/.*")
            page.locator('#minuteGrid .minute-cell[data-minute="30"]').dispatch_event("contextmenu")
            self.wait_held(page, 1)

            page.select_option("#profileSelect", label="Second Profile")
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Charlie Rot'"
            )
            self.open_weekly_detail(page, 10)
            page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")
            self.assertIn("10:00a", page.inner_text("#hourDetailTitle"))

            self.release_all(page)
            page.wait_for_timeout(500)
            self.assertIn("10:00a", page.inner_text("#hourDetailTitle"))
            self.assertEqual(page.locator("#minuteGrid .minute-cell").count(), 60)
            self.assertFalse(ScheduleBlock.objects.filter(pk=transition.pk).exists())
            self.assertFalse(ScheduleBlock.objects.filter(
                profile=self.second, day_of_week=0, start_time=time(11, 30),
            ).exists())
        self.assert_clean()

    def test_changing_profile_mode_or_date_invalidates_an_open_hour_detail(self):
        with self.session() as pw:
            page = self.open_page(pw, 1366)
            page.wait_for_function(
                "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"10\"] .cell-label')?.textContent === 'Alpha Rot'")
            for change in (
                lambda: page.select_option("#profileSelect", label="Second Profile"),
                lambda: page.click("#dateModeButton"),
            ):
                self.hold(page, r".*/api/schedule/hour-detail/.*")
                if page.locator("#weeklyDesktop").is_visible():
                    self.open_weekly_detail(page, 10)
                    self.wait_held(page, 1)
                    change()
                    self.release(self.held[0])
                    page.wait_for_timeout(400)
                    self.assertTrue(page.locator("#hourDetail").is_hidden())
                    self.assertEqual(page.locator("#minuteGrid .minute-cell").count(), 0)
        self.assert_clean()

    # ---- 5. 3.1E partial-hour workflow at supported widths ---------------
    def test_partial_hour_workflow_is_truthful_and_responsive(self):
        with self.session() as pw:
            for width in WIDTHS:
                with self.subTest(width=width):
                    page = self.open_page(pw, width)
                    self.pick(page, "Bravo Rot")

                    # Weekly blank hour -> :30 partial transition.
                    page.evaluate("openHourDetail('weekly', 0, 12)")
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell').length === 60"
                    )
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1"
                    )
                    page.locator("#hourDetailClose").click()
                    page.evaluate("openHourDetail('weekly', 0, 12)")
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell').length === 60"
                        " && document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1"
                    )
                    self.assertIn("continues until 12:30", page.inner_text("#hourDetailSegments"))
                    self.assertIn("Continues", page.inner_text('#minuteGrid .minute-cell[data-minute="0"]'))
                    self.assertIn("Bravo Rot", page.get_attribute(
                        '#minuteGrid .minute-cell[data-minute="30"]', "title"
                    ))
                    page.wait_for_function(
                        "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"12\"]')?.classList.contains('has-detail') === true"
                    )
                    weekly_cell = page.locator('.grid-cell[data-day="0"][data-hour="12"]')
                    self.assertTrue(weekly_cell.evaluate("e => e.classList.contains('has-detail')"))
                    self.assertEqual(weekly_cell.locator(".detail-badge").inner_text(), "+1")

                    # Add then remove only the base; :30 remains a valid
                    # partial transition and the overview stays detailed.
                    page.locator('#minuteGrid .minute-cell[data-minute="0"]').click()
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 2"
                    )
                    page.locator("#contentPicker .clear-btn").click()
                    page.locator('#minuteGrid .minute-cell[data-minute="0"]').click()
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1"
                    )
                    page.wait_for_function(
                        "document.querySelector('.grid-cell[data-day=\"0\"][data-hour=\"12\"]')?.classList.contains('has-detail') === true"
                    )
                    self.assertIn("continues until 12:30", page.inner_text("#hourDetailSegments"))

                    # Date Override has the same server-derived behavior and
                    # its overview remains truthfully EMPTY +1.
                    self.pick(page, "Charlie Rot")
                    self.to_date_mode(page)
                    page.locator("#dateHourList .date-detail-button").nth(13).click()
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell').length === 60"
                    )
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function(
                        "document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1"
                    )
                    page.wait_for_function(
                        "document.querySelectorAll('#dateHourList .date-hour-row').length === 24"
                        " && document.getElementById('dateHourList').getAttribute('aria-busy') === 'false'"
                        " && document.querySelectorAll('#dateHourList .date-hour-row')[13]?.classList.contains('has-detail') === true"
                    )
                    self.assertIn("EMPTY", self.row_text(page, 13))
                    self.assertIn("+1", self.row_text(page, 13))
                    self.assertIn("continues until 13:30", page.inner_text("#hourDetailSegments"))
                    self.assert_no_overflow(page, width)
                    page.close()
        self.assertEqual(
            list(ScheduleBlock.objects.filter(
                profile=self.profile, day_of_week=0, start_time=time(12, 30),
            ).values_list("rotation__name", flat=True)),
            ["Bravo Rot"],
        )
        self.assertEqual(
            list(ScheduleBlock.objects.filter(
                profile=self.profile, specific_date=MONDAY, start_time=time(13, 30),
            ).values_list("rotation__name", flat=True)),
            ["Charlie Rot"],
        )
        self.assert_clean()

    def test_right_click_exact_delete_and_layer_protection_at_supported_widths(self):
        with self.session() as pw:
            for index, width in enumerate(WIDTHS):
                with self.subTest(width=width):
                    hour = 12 + index
                    page = self.open_page(pw, width)
                    self.pick(page, "Bravo Rot")
                    page.evaluate("h => openHourDetail('weekly', 0, h)", hour)
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")

                    # Create :00 and :30. A carried :31 cell must not infer
                    # or remove the explicit :30 transition.
                    page.locator('#minuteGrid .minute-cell[data-minute="0"]').click()
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1")
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 2")
                    page.locator('#minuteGrid .minute-cell[data-minute="31"]').dispatch_event("contextmenu")
                    self.assertIn("No explicit transition begins at :31", page.inner_text("#apiError"))
                    self.assertEqual(page.locator("#minuteGrid .minute-cell.explicit").count(), 2)

                    # Clear + left-click remains the touch/keyboard route.
                    page.locator("#contentPicker .clear-btn").click()
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1")
                    self.pick(page, "Bravo Rot")
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 2")

                    # Right-clicking :00 removes only that row and exposes the
                    # 3.1E partial-hour continuation presentation.
                    page.locator('#minuteGrid .minute-cell[data-minute="0"]').dispatch_event("contextmenu")
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell.explicit').length === 1")
                    self.assertIn(f"continues until {hour:02d}:30", page.inner_text("#hourDetailSegments"))

                    # In Date Override, the Weekly :30 row is protected. A
                    # dated row at the same minute can be created and removed,
                    # revealing the inherited Weekly transition again.
                    self.to_date_mode(page)
                    page.locator("#dateHourList .date-detail-button").nth(hour).click()
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').dispatch_event("contextmenu")
                    self.assertIn("inherited from Weekly", page.inner_text("#apiError"))
                    self.pick(page, "Charlie Rot")
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').click()
                    page.wait_for_function(
                        "document.querySelector('#minuteGrid .minute-cell[data-minute=\"30\"]')?.classList.contains('explicit') === true"
                    )
                    page.locator('#minuteGrid .minute-cell[data-minute="30"]').dispatch_event("contextmenu")
                    page.wait_for_function(
                        "document.querySelector('#minuteGrid .minute-cell[data-minute=\"30\"]')?.classList.contains('inherited-transition') === true"
                    )

                    # Archived profiles remain inspectable but right-click is
                    # mutation-free. Clear remains visible at mobile width.
                    page.select_option("#profileSelect", label="Archived Profile (Archived)")
                    page.wait_for_function("document.body.classList.contains('schedule-readonly')")
                    page.evaluate("h => openHourDetail('date', null, h)", hour)
                    page.wait_for_function("document.querySelectorAll('#minuteGrid .minute-cell').length === 60")
                    page.locator('#minuteGrid .minute-cell[data-minute="0"]').dispatch_event("contextmenu")
                    self.assertIn("Archived profiles are read-only", page.inner_text("#apiError"))
                    self.assertTrue(page.locator("#contentPicker .clear-btn").is_visible())
                    self.assert_no_overflow(page, width)
                    page.close()

        for index, _width in enumerate(WIDTHS):
            hour = 12 + index
            self.assertFalse(ScheduleBlock.objects.filter(
                profile=self.profile, day_of_week=0, start_time=time(hour, 0),
            ).exists())
            self.assertTrue(ScheduleBlock.objects.filter(
                profile=self.profile, day_of_week=0, start_time=time(hour, 30),
                rotation=self.bravo,
            ).exists())
            self.assertFalse(ScheduleBlock.objects.filter(
                profile=self.profile, specific_date=MONDAY, start_time=time(hour, 30),
            ).exists())
        self.assert_clean()
