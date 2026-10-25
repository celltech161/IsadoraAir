"""Roadmap 2.23 Phase 3 -- Start Next in real browsers (Chromium + Firefox).

Built on the Phase 1 harness (test_console_browser_2_23): every /api/
request is answered inside the browser from fixtures, the engine command
queue is a test-failing backstop, and the clock is Playwright's fake clock.
The Start Next endpoint is answered like the real view (202 + request_id),
and the engine-published ``start_next`` block is driven by the test, so the
page is proven to show completion only from engine status.
"""
import json
import unittest

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

from library.tests.test_console_browser_2_23 import (
    DESKTOP,
    MOBILE_LANDSCAPE,
    MOBILE_PORTRAIT,
    SETTLE_MS,
    T0_MS,
    TABLET,
    _ConsoleBrowserBase,
    engine_state,
    sync_playwright,
)

START_NEXT = "/api/engine/start-next/"
LOG_ITEM = {"A": 5101, "B": 5202}


def sn_state(live="A", *, active=None, recent=(), block=True, **overrides):
    state = engine_state(live=live, **overrides)
    for slot, deck in state["decks"].items():
        if deck:
            deck.update({"log_item_id": LOG_ITEM[slot], "generation": 7, "air_started_at": 1_799_999_000.0})
    if block:
        state["start_next"] = {"version": 1, "active": active, "recent": list(recent)}
    return state


class _StartNextBrowserBase(_ConsoleBrowserBase):
    def setUp(self):
        super().setUp()
        from authz.models import Capability, Role, RoleCapability
        cap, _ = Capability.objects.get_or_create(
            slug="playout.start_next", defaults={"label": "Start Next", "requires_schedule": True})
        # The Phase 1 harness's Remote Host test role, granted Start Next
        # exactly as authz.0009 grants the seeded Remote Host.
        RoleCapability.objects.create(role=Role.objects.get(name="Remote Host (test)"), capability=cap)
        self.state = sn_state()
        self.request_serial = 0
        self.abort_next_start_next = 0

    def _route(self, route, request):
        from urllib.parse import urlparse
        if request.method != "GET" and urlparse(request.url).path == START_NEXT:
            self.posts.append((START_NEXT, request.post_data))
            if self.abort_next_start_next:
                self.abort_next_start_next -= 1
                return route.abort("connectionreset")   # the response is lost in transit
            status = self.post_status.get(START_NEXT, 202)
            if status == 202:
                self.request_serial += 1
                body = {"ok": True, "accepted": True, "executed": False,
                        "request_id": f"req-{self.request_serial}"}
            elif status == 503:   # the view's own truthful queue failure
                body = {"error": "engine command dispatch failed: engine command queue is full"}
            else:
                body = {"error": "refused by test fixture"}
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        return super()._route(route, request)

    # -- helpers ---------------------------------------------------------
    def sn_posts(self):
        return [json.loads(body) for path, body in self.posts if path == START_NEXT]

    def status_text(self, page):
        return page.locator("#startNextStatus").inner_text()

    def repoll(self, page, predicate_js, arg=None):
        page.evaluate("() => { statusInFlightSeq = 0; poll(); }")
        page.wait_for_function(predicate_js, arg=arg, timeout=10_000)

    def wait_status(self, page, text):
        self.repoll(page, "(t) => document.getElementById('startNextStatus').textContent.includes(t)", text)

    def click(self, page):
        page.locator("#startNextBtn").click()
        page.wait_for_function(
            "() => startNextUi.pending === null || startNextUi.pending.requestId !== null", timeout=10_000)

    def contrast(self, page, selector):
        """Return the rendered WCAG contrast ratio for an element."""
        return page.locator(selector).evaluate("""element => {
          const rgb = value => value.match(/[\\d.]+/g).slice(0, 3).map(Number);
          const luminance = value => {
            const channels = rgb(value).map(channel => {
              channel /= 255;
              return channel <= 0.04045
                ? channel / 12.92
                : Math.pow((channel + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
          };
          const style = getComputedStyle(element);
          const values = [luminance(style.color), luminance(style.backgroundColor)].sort((a, b) => b - a);
          return (values[0] + 0.05) / (values[1] + 0.05);
        }""")

    def luminance(self, page, selector, property_name):
        return page.locator(selector).evaluate("""(element, propertyName) => {
          const rgb = getComputedStyle(element)[propertyName].match(/[\\d.]+/g).slice(0, 3).map(Number);
          const channels = rgb.map(channel => {
            channel /= 255;
            return channel <= 0.04045
              ? channel / 12.92
              : Math.pow((channel + 0.055) / 1.055, 2.4);
          });
          return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
        }""", property_name)

    # -- layout and identity --------------------------------------------
    def test_desktop_button_sits_between_the_decks_and_names_the_route(self):
        for live, route in (("A", "Deck A → Deck B"), ("B", "Deck B → Deck A")):
            with self.subTest(live=live):
                self.state = sn_state(live=live)
                page = self.open_console(self.superuser, viewport=DESKTOP)
                a, slot, b = (self.box(page, s) for s in ("#deck-A", "#startNextSlot", "#deck-B"))
                self.assertLess(a["right"], slot["left"])
                self.assertLess(slot["right"], b["left"])
                button = page.locator("#startNextBtn")
                self.assertTrue(button.is_enabled())
                self.assertEqual(page.locator("#startNextRoute").inner_text(), route)
                self.assertIn("Start Next", button.inner_text())
                # Distinct from Eject: different element, label and size.
                eject = self.box(page, f"#ejectBtn-{live}")
                self.assertGreater(self.box(page, "#startNextBtn")["height"], eject["height"] * 1.5)
                self.no_horizontal_overflow(page)
                self.shot(page, f"start-next-desktop-deck{live}-live")
        self.assertEqual(self.posts, [])

    def test_desktop_visual_contract_is_compact_card_height_and_accessible(self):
        page = self.open_console(self.superuser, viewport=DESKTOP)
        deck = self.box(page, "#deck-A")
        slot = self.box(page, "#startNextSlot")
        button = self.box(page, "#startNextBtn")

        # 4.875rem is 65% of the former 7.5rem slot.
        self.assertAlmostEqual(slot["width"], 78.0, delta=1.0)
        self.assertAlmostEqual(slot["top"], deck["top"], delta=1.0)
        self.assertAlmostEqual(slot["bottom"], deck["bottom"], delta=1.0)
        self.assertAlmostEqual(button["top"], deck["top"], delta=1.0)
        self.assertGreater(button["height"], deck["height"] * 0.85)

        normal_background = self.luminance(page, "#startNextBtn", "backgroundColor")
        normal_foreground = self.luminance(page, "#startNextBtn", "color")
        self.assertGreaterEqual(self.contrast(page, "#startNextBtn"), 4.5)
        self.assertLess(float(page.locator("#startNextBtn").evaluate(
            "e => parseFloat(getComputedStyle(e).borderTopLeftRadius)")), 16.0)

        page.locator("#startNextBtn").hover()
        self.assertGreaterEqual(self.contrast(page, "#startNextBtn"), 4.5)
        self.assertGreater(self.luminance(page, "#startNextBtn", "backgroundColor"), normal_background)
        self.assertGreater(self.luminance(page, "#startNextBtn", "color"), normal_foreground)

        page.locator("#startNextBtn").focus()
        focus = page.locator("#startNextBtn").evaluate(
            "e => ({style: getComputedStyle(e).outlineStyle, width: parseFloat(getComputedStyle(e).outlineWidth)})")
        self.assertEqual(focus["style"], "solid")
        self.assertGreaterEqual(focus["width"], 3.0)

        page.locator("#startNextBtn").evaluate("e => { e.disabled = true; e.blur(); }")
        self.assertGreaterEqual(self.contrast(page, "#startNextBtn"), 4.5)
        self.assertEqual(page.locator("#startNextBtn").evaluate("e => getComputedStyle(e).cursor"), "not-allowed")

    def test_stacked_layouts_give_start_next_its_own_row_between_the_decks(self):
        for name, viewport in (("tablet", TABLET), ("mobile-portrait", MOBILE_PORTRAIT),
                               ("mobile-landscape", MOBILE_LANDSCAPE)):
            with self.subTest(viewport=name):
                page = self.open_console(self.superuser, viewport=viewport)
                a, slot, b = (self.box(page, s) for s in ("#deck-A", "#startNextSlot", "#deck-B"))
                if viewport["width"] <= 700:
                    self.assertLessEqual(a["bottom"], slot["top"])
                    self.assertLessEqual(slot["bottom"], b["top"])
                else:
                    self.assertLess(a["right"], slot["left"])
                    self.assertLess(slot["right"], b["left"])
                self.assertTrue(page.locator("#startNextBtn").is_visible())
                if viewport["width"] <= 700:
                    button = self.box(page, "#startNextBtn")
                    self.assertGreaterEqual(button["height"], 44.0)
                    self.assertLessEqual(button["height"], 56.0)
                    self.assertGreaterEqual(self.contrast(page, "#startNextBtn"), 4.5)
                self.no_horizontal_overflow(page)
                self.shot(page, f"start-next-full-{name}")

    def test_remote_dj_mobile_keeps_live_first_compact_preview_with_start_next_between(self):
        for live, preview in (("A", "B"), ("B", "A")):
            with self.subTest(live=live):
                self.state = sn_state(live=live)
                page = self.open_console(self.host, path="/remote-dj/", viewport=MOBILE_PORTRAIT)
                top, slot, bottom = (self.box(page, s) for s in (f"#deck-{live}", "#startNextSlot",
                                                                 f"#deck-{preview}"))
                self.assertLessEqual(top["bottom"], slot["top"])
                self.assertLessEqual(slot["bottom"], bottom["top"])
                self.assertFalse(page.locator(f"#npWaveform-{preview}").is_visible())  # compact preview
                self.assertTrue(page.locator("#startNextBtn").is_enabled())
                # Start Next does not bring deck transport with it.
                self.assertEqual(page.locator(".deck-transport-btn").count(), 0)
                self.no_horizontal_overflow(page)
                self.shot(page, f"start-next-remote-dj-mobile-deck{live}-live")

    def test_a_user_without_the_capability_sees_no_start_next(self):
        from authz.models import RoleCapability
        RoleCapability.objects.filter(capability__slug="playout.start_next").delete()
        page = self.open_console(self.host, path="/remote-dj/", viewport=DESKTOP)
        self.assertFalse(page.locator("#startNextSlot").is_visible())
        self.assertEqual(page.locator("#startNextBtn").count(), 0)
        self.assertFalse(page.evaluate("() => document.getElementById('decksRow')"
                                       ".classList.contains('has-start-next')"))

    def test_overlap_names_the_older_deck_and_engine_identity_wins_during_a_transition(self):
        state = sn_state(live="A")
        state["decks"]["B"] = {**state["decks"]["A"], "slot": "B", "track_id": 202, "title": "Deck B Song",
                               "log_item_id": LOG_ITEM["B"], "air_started_at": 1_799_999_500.0,
                               "position": 3.0}
        self.state = state
        page = self.open_console(self.superuser)
        self.assertEqual(page.locator("#startNextRoute").inner_text(), "fade Deck A")
        self.click(page)
        body = self.sn_posts()[0]
        self.assertEqual(body["expected"], {"live_decks": [
            {"slot": "A", "log_item_id": LOG_ITEM["A"], "generation": 7},
            {"slot": "B", "log_item_id": LOG_ITEM["B"], "generation": 7}]})
        # The engine, not the browser, says which deck is outgoing.
        self.state = {**state, "start_next": {"version": 1, "recent": [], "active": {
            "request_id": "req-1", "phase": "advancing", "mode": "overlap",
            "outgoing": {"slot": "B", "log_item_id": LOG_ITEM["B"], "generation": 9},
            "incoming": {"slot": "A", "log_item_id": LOG_ITEM["A"]}, "fade_ms": 2000,
            "accepted_at": 1_800_000_001.0, "fade_ends_at": 1_800_000_003.0}}}
        self.wait_status(page, "Deck B → Deck A")
        self.assertEqual(page.locator("#startNextRoute").inner_text(), "Deck B → Deck A")

    # -- request / engine acknowledgement --------------------------------
    def test_202_means_queued_and_completion_comes_only_from_the_engine(self):
        page = self.open_console(self.superuser)
        self.click(page)
        posts = self.sn_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["engine_instance"], self.engine_instance)
        self.assertEqual(posts[0]["expected"],
                         {"live_decks": [{"slot": "A", "log_item_id": LOG_ITEM["A"], "generation": 7}]})
        self.assertRegex(posts[0]["client_request_id"], r"^[A-Za-z0-9-]{8,64}$")
        self.assertIn("Queued", self.status_text(page))
        self.assertTrue(page.locator("#startNextBtn").is_disabled())
        self.assertEqual(page.locator("#startNextBtn").get_attribute("aria-busy"), "true")
        self.shot(page, "start-next-queued")

        active = {"request_id": "req-1", "phase": "fading", "mode": "fade_and_start",
                  "outgoing": {"slot": "A", "log_item_id": LOG_ITEM["A"], "generation": 7},
                  "incoming": None, "fade_ms": 2000,
                  "accepted_at": (T0_MS + SETTLE_MS) / 1000 - 1.0,
                  "fade_ends_at": (T0_MS + SETTLE_MS) / 1000 + 1.0}
        self.state = sn_state(active=active)
        self.wait_status(page, "Fading: Deck A")
        self.assertTrue(page.locator("#startNextProgressWrap").is_visible())
        self.assertEqual(page.locator("#startNextProgress").evaluate("e => e.style.width"), "50%")
        self.shot(page, "start-next-fading")

        record = {"request_id": "req-1", "outcome": "completed", "reason": None, "mode": "fade_and_start",
                  "outgoing": active["outgoing"], "incoming": {"slot": "B", "log_item_id": 9001},
                  "fade_ms": 2000, "manual_released": False}
        self.state = sn_state(live="B", recent=[record])
        self.wait_status(page, "Next item started")
        self.assertTrue(page.locator("#startNextBtn").is_enabled())
        self.assertEqual(len(self.sn_posts()), 1)
        self.shot(page, "start-next-completed")

    def test_fade_to_silence_and_engine_refusals_are_reported_truthfully(self):
        cases = (
            ({"outcome": "completed_to_silence", "reason": None}, "Faded to silence"),
            ({"outcome": "rejected", "reason": "busy_voice_track"},
             "Not started: a Voice Track transition is in progress"),
            ({"outcome": "rejected", "reason": "target_changed"}, "the decks changed"),
            ({"outcome": "failed", "reason": "incoming_open_failed"},
             "Failed: the next item could not be opened"),
            ({"outcome": "cancelled", "reason": "superseded_by_eject"}, "Stopped: superseded by Eject"),
        )
        page = self.open_console(self.superuser)
        for index, (record, text) in enumerate(cases, start=1):
            with self.subTest(outcome=record):
                self.state = sn_state()
                self.repoll(page, "() => !document.getElementById('startNextBtn').disabled")
                self.click(page)
                self.state = sn_state(recent=[{"request_id": f"req-{index}", **record}])
                self.wait_status(page, text)
                tone = page.locator("#startNextStatus").get_attribute("data-sn-tone")
                self.assertEqual(tone, "ok" if record["outcome"].startswith("completed") else "error")
        self.shot(page, "start-next-refused-engine")

    def test_a_double_click_sends_exactly_one_request(self):
        page = self.open_console(self.superuser)
        page.locator("#startNextBtn").dblclick()
        page.evaluate("() => startNext()")   # a scripted repeat is refused too
        page.wait_for_function("() => startNextUi.pending && startNextUi.pending.requestId", timeout=10_000)
        self.assertEqual(len(self.sn_posts()), 1)

    def test_an_unacknowledged_request_is_reported_unconfirmed(self):
        page = self.open_console(self.superuser)
        self.click(page)
        page.clock.run_for(7_000)
        self.wait_status(page, "Not confirmed by the engine")
        self.assertTrue(page.locator("#startNextBtn").is_enabled())

    def test_an_engine_restart_while_queued_is_not_reported_as_success(self):
        page = self.open_console(self.superuser)
        self.click(page)
        self.engine_instance = "engine-run-2"
        self.revision_seq = 1
        page.evaluate("() => { statusInFlightSeq = 0; poll(); }")
        self.wait_status(page, "the engine restarted")

    def test_http_refusals_are_definitive_and_send_nothing_more(self):
        for status, text in ((403, "refused: permission denied"), (503, "not queued")):
            with self.subTest(status=status):
                self.post_status[START_NEXT] = status
                page = self.open_console(self.superuser)
                self.click(page)
                self.assertIn(text, self.status_text(page))
                self.assertEqual(page.locator("#startNextStatus").get_attribute("data-sn-tone"), "error")
                self.assertTrue(page.locator("#startNextBtn").is_enabled())
        self.shot(page, "start-next-refused-http")

    def test_a_stale_connection_disables_start_next(self):
        page = self.open_console(self.superuser)
        self.status_fails = True
        page.clock.run_for(12_000)
        page.wait_for_function("() => connectionStale === true", timeout=10_000)
        page.wait_for_function("() => document.getElementById('startNextBtn').disabled", timeout=10_000)
        self.assertIn("connection lost", self.status_text(page))
        page.evaluate("() => startNext()")
        self.assertEqual(self.sn_posts(), [])
        self.shot(page, "start-next-stale")

    def test_an_engine_without_start_next_and_another_operators_transition_disable_it(self):
        self.state = sn_state(block=False)
        page = self.open_console(self.superuser)
        self.assertTrue(page.locator("#startNextBtn").is_disabled())
        self.assertIn("does not support Start Next", self.status_text(page))
        self.state = sn_state(active={
            "request_id": "someone-else", "phase": "fading", "mode": "fade_and_start",
            "outgoing": {"slot": "A", "log_item_id": LOG_ITEM["A"], "generation": 7},
            "incoming": None, "fade_ms": 2000, "accepted_at": 1.0, "fade_ends_at": 3.0})
        self.wait_status(page, "another operator")
        self.assertTrue(page.locator("#startNextBtn").is_disabled())
        self.assertEqual(self.sn_posts(), [])

    def test_incomplete_engine_identity_disables_start_next_and_sends_nothing(self):
        cases = {
            "no generation": lambda state: state["decks"]["A"].pop("generation"),
            "no log item": lambda state: state["decks"]["A"].pop("log_item_id"),
            "no engine instance": lambda state: state.update(state_revision=None),
        }
        for label, damage in cases.items():
            with self.subTest(label):
                state = sn_state()
                damage(state)
                self.state = state
                page = self.open_console(self.superuser)
                self.assertTrue(page.locator("#startNextBtn").is_disabled())
                self.assertIn("engine status is incomplete", self.status_text(page))
                page.evaluate("() => startNext()")
                page.clock.run_for(200)
                self.assertEqual(self.sn_posts(), [])
        self.shot(page, "start-next-incomplete-identity")

    def test_an_explicit_empty_live_set_is_sent_for_a_stopped_console(self):
        state = sn_state()
        state["decks"]["A"]["paused"] = True     # nothing live, a paused deck to retire
        self.state = state
        page = self.open_console(self.superuser)
        self.click(page)
        self.assertEqual(self.sn_posts()[0]["expected"], {"live_decks": []})
        self.assertEqual(self.sn_posts()[0]["engine_instance"], self.engine_instance)

    def test_a_lost_response_is_retried_once_with_the_same_press_identity(self):
        self.abort_next_start_next = 1
        page = self.open_console(self.superuser)
        self.click(page)
        posts = self.sn_posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])                 # same client_request_id, same binding
        self.assertIn("Queued", self.status_text(page))
        page.wait_for_function("() => startNextUi.pending && startNextUi.pending.requestId === 'req-1'")

    def test_every_request_carries_all_three_bindings(self):
        page = self.open_console(self.superuser)
        self.click(page)
        body = self.sn_posts()[0]
        self.assertEqual(set(body), {"client_request_id", "engine_instance", "expected"})
        self.assertTrue(body["engine_instance"])
        self.assertRegex(body["client_request_id"], r"^[A-Za-z0-9-]{8,64}$")

    def test_eject_is_bound_to_the_occurrence_on_screen(self):
        page = self.open_console(self.superuser)
        page.locator("#ejectBtn-A").click()
        page.wait_for_function("() => true")
        page.clock.run_for(100)
        ejects = [json.loads(body) for path, body in self.posts if path == "/api/engine/deck/A/"]
        self.assertEqual(ejects, [{"action": "eject", "expected_log_item_id": LOG_ITEM["A"]}])
        self.assertEqual(self.sn_posts(), [])


# Only this module's tests: the inherited Phase 1 matrix runs in its own module.
for _name in dir(_ConsoleBrowserBase):
    if _name.startswith("test_"):
        setattr(_StartNextBrowserBase, _name, None)


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class StartNextChromiumTests(_StartNextBrowserBase, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class StartNextFirefoxTests(_StartNextBrowserBase, StaticLiveServerTestCase):
    BROWSER = "firefox"
