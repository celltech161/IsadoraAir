"""iPortal D3D in headless Chromium: the Operator Board's compact, mobile-first
layout (360/390 px phones, a tablet, a desktop), no horizontal page overflow
with long names and many rows, predictable expansion with keyboard access,
confirmations that fit, filter state kept through actions and Back, a double
click that publishes once, and a browser-local audio preview that never
reaches the station output. Hardware-free: no audio output device."""
import io
import os
import uuid
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings
from django.utils import timezone

from active_messages import services
from active_messages.models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from library.models import GroupAccess
from produced_content.models import ProducedContentType
from production.models import ProductionMedia
from production.tests.support import ffmpeg_available
from production.tests.test_recorder_browser import in_browser, sync_playwright

from .support import Domain, assign, content_type, media, raw, user_with_role

M = ActiveBroadcastMessage
X = ActiveBroadcastExecution
BOARD = "/iportal/messages/board/"
PHONES = ((360, 740), (390, 844))
NO_OVERFLOW = """() => {
  const main = document.querySelector('main');
  return {page: document.documentElement.scrollWidth - window.innerWidth,
          main: main.scrollWidth - main.clientWidth};
}"""


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class OperatorBoardBrowserTests(Domain, StaticLiveServerTestCase):
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.assets = tempfile.TemporaryDirectory()
        (Path(cls.assets.name) / "empty-alsa.conf").write_text("")
        cls.browser_env = {**os.environ, "ALSA_CONFIG_PATH": str(Path(cls.assets.name) / "empty-alsa.conf"),
                           "PULSE_SERVER": "unix:/nonexistent/pulse", "PULSE_RUNTIME_PATH": cls.assets.name}
        cls.browser_args = ["--disable-audio-output", "--mute-audio", "--autoplay-policy=no-user-gesture-required"]
        cls.shots = os.environ.get("AMB_SCREENSHOTS")          # optional: where to save layout screenshots

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for group in Group.objects.filter(name__startswith="test-"):
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})
        self.notice = content_type("town_notice", "public_address")
        self.reviewed = content_type("board_notice", "public_address", producer_self_publish=False)
        self.urgent = content_type("school_urgent", "urgent")
        self.long_type = ProducedContentType.objects.create(
            key="county_emergency_management_public_information", delivery_mode="public_address",
            name="County Emergency Management Public Information Announcements (Extended)")
        self.long_producer = user_with_role("emergency-management-coordinator-for-the-county-office",
                                            "Test Producer")
        for producer in (self.producer, self.producer2, self.long_producer):
            assign(producer, self.notice, self.reviewed, self.urgent, self.long_type)
        client = Client()
        client.force_login(self.controller)                     # control only, not staff
        self.session_cookie = client.cookies["sessionid"].value

    # -- fixtures ------------------------------------------------------------------------------------
    def ready(self, producer, content_type_, title="", audio=None):
        message = services.prepare_message(actor=producer, content_type=content_type_, title=title)
        return services.bind_message_media(message, audio or media(producer), actor=producer)

    def live(self, producer, content_type_, title=""):
        return services.activate_message(self.ready(producer, content_type_, title), actor=self.admin)

    def real_audio(self, owner):
        from production.services import intake
        from spoken.tests import support as audio
        return intake.ingest_stream(io.BytesIO(audio.bed(2.0)), kind=ProductionMedia.KIND_RECORDING, owner=owner,
                                    validate=True).media

    def populate(self):
        """A realistic, crowded board: long names everywhere, several of each."""
        long_title = ("Countywide boil-water advisory for the north service area until further notice "
                      "from the utilities department")
        self.live(self.long_producer, self.urgent, long_title)
        self.live(self.producer, self.long_type, "Shelter information")
        library = self.live(self.producer2, self.notice, "Library closed Monday")
        services.set_message_expiry(library, timezone.now() + timedelta(minutes=45), actor=self.admin)
        for index in range(6):
            self.live(self.producer, self.notice, f"Town notice {index}")        # each replaces the last
        self.review = self.ready(self.long_producer, self.reviewed, long_title)
        self.ready(self.producer2, self.reviewed, "Road closure on Main Street")
        services.prepare_message(actor=self.producer, content_type=self.reviewed, title="Still recording")
        for index in range(30):
            gone = services.prepare_message(actor=self.producer2, content_type=self.notice, title=f"Old {index}")
            services.clear_message(gone, actor=self.producer2)

    # -- browser -------------------------------------------------------------------------------------
    def open(self, path=BOARD, *, width=390, height=844, touch=True):
        context = self.browser.new_context(viewport={"width": width, "height": height}, has_touch=touch,
                                           is_mobile=touch and width < 700, device_scale_factor=1)
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        page = context.new_page()
        page.errors, page.requests = [], []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.on("request", lambda req: page.requests.append(req.url))
        page.goto(self.live_server_url + path)
        return page

    def shot(self, page, name):
        if self.shots:
            page.screenshot(path=str(Path(self.shots) / f"{name}.png"), full_page=True)

    def assert_no_overflow(self, page, label):
        overflow = page.evaluate(NO_OVERFLOW)
        self.assertLessEqual(overflow["page"], 0, label)
        self.assertLessEqual(overflow["main"], 0, label)

    # -- tests ---------------------------------------------------------------------------------------
    @in_browser
    def test_phones_show_the_status_and_the_list_first_without_horizontal_overflow(self):
        self.populate()
        for width, height in PHONES:
            for view in ("", "?filter=review", "?filter=history", "?filter=active"):
                page = self.open(BOARD + view, width=width, height=height)
                self.assert_no_overflow(page, (width, view))
                if not view:
                    self.shot(page, f"board-{width}")
                    strip = page.locator(".amb-strip").bounding_box()
                    first = page.locator(".amb-item").first.bounding_box()
                    self.assertLess(strip["y"] + strip["height"], height * 0.3, width)   # a compact strip
                    self.assertLess(first["y"], height * 0.4, width)                      # the list follows at once
                    self.assertGreaterEqual(page.locator(".amb-item").count(), 4)
                    visible = sum(1 for box in (page.locator(".amb-item").nth(i).bounding_box()
                                                for i in range(page.locator(".amb-item").count()))
                                  if box["y"] + box["height"] <= height)
                    self.assertGreaterEqual(visible, 6, f"rows above the fold at {width}px")
                for index in range(page.locator("details.amb-row > summary").count()):
                    summary = page.locator("details.amb-row > summary").nth(index)
                    box = summary.bounding_box()
                    lines = 3 if summary.locator(".m-note").text_content().strip() else 2
                    self.assertLessEqual(box["height"], 22 * lines + 20, (width, view, index, lines))
                    self.assertGreaterEqual(box["height"], 40, (width, view, index))    # a comfortable target
                    self.assertLessEqual(box["x"] + box["width"], width + 0.5)
                filters = page.locator(".amb-filters").bounding_box()
                self.assertLessEqual(filters["x"] + filters["width"], width)
                self.assertEqual(page.errors, [])
                page.context.close()

    @in_browser
    def test_long_names_stay_distinguishable_on_a_phone(self):
        self.populate()
        page = self.open(width=360, height=740)
        row = page.locator(f"#m-{self.review.pk} details.amb-row > summary")
        title = row.locator(".amb-title")
        self.assertTrue(title.evaluate("el => el.scrollWidth > el.clientWidth"))        # ellipsis, not overflow
        self.assertTrue(title.get_attribute("title").startswith("Countywide boil-water advisory"))
        for part in (".amb-title", ".amb-state:not([hidden])", ".m-type", ".m-producer"):
            box = row.locator(part).bounding_box()
            self.assertGreater(box["width"], 20, part)
            self.assertLessEqual(box["x"] + box["width"], 360, part)
        self.assertEqual(row.locator(".amb-state:not([hidden])").text_content().strip(), "REVIEW")
        page.context.close()

    @in_browser
    def test_expansion_is_predictable_and_keyboard_accessible(self):
        self.populate()
        page = self.open(width=390, height=844)
        summary = page.locator(f"#m-{self.review.pk} details.amb-row > summary")
        page.evaluate("window.scrollTo(0, 0)")
        summary.focus()
        before = page.evaluate("window.scrollY")
        page.keyboard.press("Enter")
        details = page.locator(f"#m-{self.review.pk} details.amb-row")
        self.assertTrue(details.evaluate("el => el.open"))
        self.assertEqual(page.evaluate("window.scrollY"), before)                        # no page jump
        self.assertEqual(page.evaluate("document.activeElement.tagName"), "SUMMARY")    # focus stays put
        outline = summary.evaluate("el => getComputedStyle(el).outlineStyle")
        self.assertNotEqual(outline, "none")                                              # visible focus
        publish = page.locator(f"#m-{self.review.pk} [data-action='publish']")
        self.assertTrue(publish.is_visible())
        self.assertGreaterEqual(publish.bounding_box()["height"], 36)                    # touch target
        self.assertEqual(page.locator(f"#m-{self.review.pk} [data-action='publish']").count(), 1)
        self.assertEqual(page.locator("details.amb-row[open]").count(), 1)               # one at a time is natural
        self.assert_no_overflow(page, "expanded")
        self.shot(page, "board-390-expanded")
        page.keyboard.press("Space")
        self.assertFalse(details.evaluate("el => el.open"))
        page.context.close()

    @in_browser
    def test_a_confirmation_fits_and_cancel_back_and_actions_keep_the_view(self):
        self.populate()
        target = M.objects.filter(state="active", title="Library closed Monday").get()
        page = self.open(f"{BOARD}?filter=active&q=Library", width=360, height=740)
        page.click(f"#m-{target.pk} details.amb-row > summary")
        page.click(f"#m-{target.pk} [data-action='clear']")
        page.wait_for_selector("#amConfirm")
        self.assert_no_overflow(page, "confirm")
        self.shot(page, "confirm-360")
        button = page.locator("#ambConfirm").bounding_box()
        self.assertLessEqual(button["y"] + button["height"], 740)                         # fits without scrolling
        self.assertIn("producer2", page.text_content("#amConfirm"))
        page.click("text=Cancel")
        page.wait_for_selector("#amBoard")
        self.assertIn("filter=active", page.url)
        self.assertIn("q=Library", page.url)
        self.assertTrue(page.locator(f"#m-{target.pk} details.amb-row").evaluate("el => el.open"))
        page.click(f"#m-{target.pk} [data-action='clear']")                              # still expanded
        page.wait_for_selector("#amConfirm")
        page.fill("#ambReason", "reopened")
        page.click("#ambConfirm")
        page.wait_for_selector(".amb-flash")
        self.assertIn("Cleared", page.text_content(".amb-flash"))
        self.assertIn("filter=active", page.url)
        self.assertEqual(M.objects.get(pk=target.pk).state, "cleared")
        page.go_back()                                                                    # the stale confirmation
        page.wait_for_selector("#amBoard, #amConfirm")
        if page.locator("#amConfirm").count():                                            # restored from cache
            page.click("#ambConfirm")
            page.wait_for_selector("#amBoard")
            self.assertIn("Already cleared", page.text_content(".amb-flash"))
        else:
            self.assertIn("nothing to clear", page.text_content(".amb-flash"))
        self.assertEqual(M.objects.get(pk=target.pk).terminal_reason, "reopened")         # cleared exactly once
        self.assertEqual(page.errors, [])
        page.context.close()

    @in_browser
    def test_a_double_click_publishes_once_and_a_stale_review_is_explained(self):
        first = self.ready(self.producer2, self.reviewed, "Publish me")
        second = self.ready(self.producer2, self.reviewed, "Changed after review")
        page = self.open(width=390, height=844)
        page.click(f"#m-{first.pk} details.amb-row > summary")
        page.dblclick(f"#m-{first.pk} [data-action='publish']")
        page.wait_for_selector(".amb-flash")
        self.assertIn("Published", page.text_content(".amb-flash"))
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(message=first, event="activated").count(), 1)
        page.click(f"#m-{second.pk} details.amb-row > summary")
        services.bind_message_media(second, media(self.producer2), actor=self.producer2)    # saved after review
        page.click(f"#m-{second.pk} [data-action='publish']")
        page.wait_for_selector(".amb-flash")
        self.assertIn("changed since you reviewed it", page.text_content(".amb-flash"))
        self.assertEqual(M.objects.get(pk=second.pk).state, "prepared")
        self.assertTrue(page.locator(f"#m-{second.pk} details.amb-row").evaluate("el => el.open"))  # back where it was
        page.context.close()

    @in_browser
    def test_desktop_gains_columns_and_a_tablet_stays_within_its_width(self):
        self.populate()
        page = self.open(width=1280, height=800, touch=False)
        self.shot(page, "board-1280")
        row = page.locator(f"#m-{self.review.pk} details.amb-row > summary")
        tops = {part: row.locator(part).bounding_box()["y"]
                for part in (".amb-title", ".m-type", ".m-producer", ".m-when", ".m-audio", ".amb-state:not([hidden])")}
        self.assertLess(max(tops.values()) - min(tops.values()), 4, tops)                 # one line per message
        self.assertLessEqual(row.bounding_box()["height"], 44)
        self.assertTrue(page.locator(".amb-colhead").is_visible())
        self.assert_no_overflow(page, "desktop")
        tablet = self.open(width=820, height=1180)
        self.assert_no_overflow(tablet, "tablet")
        self.shot(tablet, "board-820")
        page.context.close()
        tablet.context.close()

    @in_browser
    def test_the_audio_preview_is_browser_local(self):
        draft = self.ready(self.producer2, self.reviewed, "Listen first", audio=self.real_audio(self.producer2))
        page = self.open(width=390, height=844)
        page.click(f"#m-{draft.pk} details.amb-row > summary")
        audio = page.locator(f"#m-{draft.pk} audio")
        self.assertEqual(audio.get_attribute("preload"), "none")
        page.requests.clear()
        audio.evaluate("el => el.play()")
        page.wait_for_function(f"document.querySelector('#m-{draft.pk} audio').currentTime > 0.2")
        audio.evaluate("el => el.pause()")
        media_url = f"/iportal/messages/api/recorder/media/{draft.message_media_id}/?message={draft.pk}"
        self.assertTrue(any(url.endswith(media_url) for url in page.requests), page.requests)
        self.assertFalse([url for url in page.requests if not url.startswith(self.live_server_url)
                          or ("/media/" not in url and "/status/" not in url)])
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")                   # listening changes nothing
        self.assertEqual(page.errors, [])
        page.context.close()

    @in_browser
    def test_the_status_refresh_never_disturbs_an_expanded_row_or_typed_input(self):
        draft = self.ready(self.producer2, self.reviewed, "Keep me open")
        page = self.open(width=390, height=844)
        page.click(f"#m-{draft.pk} details.amb-row > summary")
        page.click(f"#m-{draft.pk} .amb-sub > summary")
        page.fill(f"#exp-{draft.pk}", "2031-01-02T03:04")
        page.focus(f"#exp-{draft.pk}")
        self.live(self.producer, self.urgent, "Arrives meanwhile")
        page.wait_for_function("document.querySelector('[data-count=\"urgent\"]').textContent === '1'",
                               timeout=30000)
        self.assertFalse(page.locator("#ambChanged").is_hidden())
        self.assertTrue(page.locator(f"#m-{draft.pk} details.amb-row").evaluate("el => el.open"))
        self.assertEqual(page.input_value(f"#exp-{draft.pk}"), "2031-01-02T03:04")
        self.assertEqual(page.evaluate("document.activeElement.id"), f"exp-{draft.pk}")
        page.context.close()

    @in_browser
    def test_a_light_station_theme_keeps_the_board_legible(self):
        from library.models import UITheme
        theme = UITheme.load()
        UITheme.objects.filter(pk=theme.pk).update(bg_dark="#f8fafc", bg_darker="#ffffff", panel_bg="#ffffff",
                                                   text_main="#0f172a", text_muted="#475569",
                                                   border_subtle="rgba(15, 23, 42, 0.2)", accent="#15803d")
        self.populate()
        page = self.open(width=390, height=844)
        self.shot(page, "board-390-light")
        colors = page.evaluate("""() => {
          const rgb = s => s.match(/\\d+(\\.\\d+)?/g).slice(0, 3).map(Number);
          const lum = c => { const [r, g, b] = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
                             return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
          const ratio = (a, b) => { const [x, y] = [lum(rgb(a)), lum(rgb(b))].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
          const bg = getComputedStyle(document.body).backgroundColor;
          return ['.amb-title', '.amb-state:not([hidden])', '.amb-chip', '.m-type', '.amb-filters a'].map(sel =>
            [sel, ratio(getComputedStyle(document.querySelector(sel)).color, bg)]);
        }""")
        for selector, ratio in colors:
            self.assertGreaterEqual(ratio, 4.5, selector)                                # WCAG AA text contrast
        page.context.close()


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class OnAirRefreshBrowserTests(OperatorBoardBrowserTests):
    """Finding 1: ON AIR follows the server through every status refresh."""

    def on_air(self, page) -> set:
        """Rows marked on air, cross-checked against the visible badges."""
        marked = set(page.eval_on_selector_all("li.amb-item[data-on-air='true']", "els => els.map(e => e.dataset.message)"))
        shown = set(page.eval_on_selector_all("span.amb-state.onair:not([hidden])",
                                              "els => els.map(e => e.dataset.onairMessage)"))
        self.assertEqual(marked, shown)
        hidden_normal = set(page.eval_on_selector_all("span.amb-state[data-onair-when='off'][hidden]",
                                                      "els => els.map(e => e.dataset.onairMessage)"))
        self.assertEqual(hidden_normal, shown)                    # each on-air row hides its normal state badge
        return shown

    def chip(self, page):
        return (page.text_content("#ambAiring").strip(),
                page.eval_on_selector("#ambAiring", "el => [...el.classList].filter(c => c !== 'amb-chip')"))

    def refresh(self, page):
        page.evaluate("window.ambBoardStatus.poll()")

    def start(self):
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        return self.put_on_air(execution)

    # Inherited tests are not re-run here.
    test_phones_show_the_status_and_the_list_first_without_horizontal_overflow = None
    test_long_names_stay_distinguishable_on_a_phone = None
    test_expansion_is_predictable_and_keyboard_accessible = None
    test_a_confirmation_fits_and_cancel_back_and_actions_keep_the_view = None
    test_a_double_click_publishes_once_and_a_stale_review_is_explained = None
    test_desktop_gains_columns_and_a_tablet_stays_within_its_width = None
    test_the_audio_preview_is_browser_local = None
    test_the_status_refresh_never_disturbs_an_expanded_row_or_typed_input = None
    test_a_light_station_theme_keeps_the_board_legible = None

    @in_browser
    def test_on_air_follows_every_transition_without_a_reload(self):
        a = self.live(self.producer, self.notice, "A")
        b = self.live(self.producer2, self.notice, "B")
        idle = self.live(self.long_producer, self.urgent, "Not in the airing")
        services.clear_message(idle, actor=self.admin)
        first, token = self.start()
        idle = self.live(self.long_producer, self.urgent, "Active, not in the airing")
        page = self.open(width=390, height=844)
        self.assertEqual(self.on_air(page), {str(a.pk), str(b.pk)})
        self.assertEqual(self.chip(page), ("● ON AIR", ["onair"]))
        # Unsaved work and focus that every refresh must leave alone.
        page.click(f"#m-{a.pk} details.amb-row > summary")
        page.click(f"#m-{a.pk} .amb-sub > summary")
        page.fill(f"#exp-{a.pk}", "2031-01-02T03:04")
        page.focus(f"#exp-{a.pk}")

        # STARTED -> COMPLETED
        services.record_air_completed(first, attempt_token=token, ended_at=timezone.now(),
                                      confirmed_duration_seconds=Decimal("30.000"))
        self.refresh(page)
        self.assertEqual(self.on_air(page), set())
        self.assertEqual(self.chip(page), ("Last: Completed", []))
        self.assertEqual(page.text_content(f"[data-execution-label='{first.pk}']"), "Completed")
        self.assertIn("not airing", page.text_content(f"#m-{a.pk} .amb-facts"))
        self.assertNotIn("on air now", page.inner_text(f"#m-{a.pk} .amb-facts"))

        # A later STARTED airing with a different frozen composition (B cleared, the urgent one added)
        services.clear_message(b, actor=self.admin)
        second, token = self.start()
        self.refresh(page)
        self.assertEqual(self.on_air(page), {str(a.pk), str(idle.pk)})
        self.assertEqual(self.chip(page), ("● ON AIR", ["onair"]))
        stale_body = page.evaluate("fetch(document.getElementById('amBoard').dataset.statusUrl)"
                                   ".then(r => r.text())")                          # a genuine ON AIR response

        # STARTED -> INTERRUPTED, replaced by another STARTED airing before the next refresh
        services.record_air_interrupted(second, attempt_token=token, last_evidence_at=timezone.now())
        raw(M).filter(pk=idle.pk).update(next_due_at=timezone.now())
        services.clear_message(idle, actor=self.admin)
        third, token = self.start()
        self.refresh(page)
        self.assertEqual(self.on_air(page), {str(a.pk)})

        # STARTED -> FAILED (no domain service does this; the database allows it, so the board must cope)
        raw(X).filter(pk=third.pk).update(state="failed", open_slot=None, terminal_at=timezone.now(),
                                          failure_code="publication_unavailable")
        self.refresh(page)
        self.assertEqual(self.on_air(page), set())
        self.assertEqual(self.chip(page), ("Last: Failed", ["alert"]))

        # An older response arriving after a newer one is never applied.
        held = []
        page.route("**/iportal/messages/board/status/", lambda route: held.append(route) if not held
                   else route.continue_())
        page.evaluate("void window.ambBoardStatus.poll()")           # request 1: held
        page.wait_for_function("true")
        self.refresh(page)                                             # request 2: applied (nothing on air)
        self.assertEqual(len(held), 1)
        held[0].fulfill(status=200, content_type="application/json", body=stale_body)
        page.wait_for_timeout(300)
        self.assertEqual(self.on_air(page), set())
        self.assertEqual(self.chip(page), ("Last: Failed", ["alert"]))
        self.assertTrue(page.locator("#ambStale").is_hidden())

        # A failed refresh restores nothing and says the page may be out of date.
        page.unroute("**/iportal/messages/board/status/")
        page.route("**/iportal/messages/board/status/", lambda route: route.abort())
        self.refresh(page)
        self.assertFalse(page.locator("#ambStale").is_hidden())
        self.assertEqual(self.on_air(page), set())
        page.unroute("**/iportal/messages/board/status/")
        self.refresh(page)                                             # a later success clears the warning
        self.assertTrue(page.locator("#ambStale").is_hidden())

        # Focus, the expanded row and the typed expiry survived every refresh.
        self.assertTrue(page.locator(f"#m-{a.pk} details.amb-row").evaluate("el => el.open"))
        self.assertEqual(page.input_value(f"#exp-{a.pk}"), "2031-01-02T03:04")
        self.assertEqual(page.evaluate("document.activeElement.id"), f"exp-{a.pk}")
        self.assertEqual(page.errors, [])
        page.context.close()

    @in_browser
    def test_detail_pages_follow_the_same_status(self):
        a = self.live(self.producer, self.notice, "A")
        execution, token = self.start()
        message_page = self.open(f"{BOARD}{a.pk}/", width=390, height=844)
        airing_page = self.open(f"{BOARD}airings/{execution.pk}/", width=390, height=844)
        self.assertFalse(message_page.locator(f"[data-onair-message='{a.pk}'][data-onair-when='on']").is_hidden())
        self.assertEqual(airing_page.text_content(f"[data-execution-label='{execution.pk}']"), "● On air")
        services.record_air_completed(execution, attempt_token=token, ended_at=timezone.now(),
                                      confirmed_duration_seconds=Decimal("30.000"))
        for page in (message_page, airing_page):
            self.refresh(page)
        self.assertTrue(message_page.locator(f"[data-onair-message='{a.pk}'][data-onair-when='on']").is_hidden())
        self.assertEqual(message_page.text_content(f"#amMessage [data-execution-label='{execution.pk}']"), "Completed")
        self.assertEqual(airing_page.text_content(f"[data-execution-label='{execution.pk}']"), "Completed")
        message_page.context.close()
        airing_page.context.close()
