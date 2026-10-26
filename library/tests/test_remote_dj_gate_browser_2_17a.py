"""Roadmap 2.17A -- Remote Mic gate session binding in the real Console page
(Chromium and Firefox; desktop and mobile /remote-dj/ layouts).

Reuses the 2.23 Console browser harness: a mocked /api/engine/status/, every
engine-command POST captured. "This browser owns the connection" is simulated
by giving the page what a real connect leaves behind -- a stand-in peer, its
attempt id and the control key from the token response. No WebRTC connection
is made and the real engine command queue is never reached.

The EndToEnd tests let the gate POST through to the REAL Django view (CSRF
middleware, capability check, session binding, fresh engine-state check) with
only the queue write captured, so a forged request from the page gets exactly
the enforcement the normal Console gets.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings

import library.views as views
from library.services import remote_dj_gate_identity as gate_identity
from library.tests.test_console_browser_2_23 import (
    DESKTOP,
    MOBILE_PORTRAIT,
    _ConsoleBrowserBase,
    engine_state,
    sync_playwright,
)

ATTEMPT = "browserAttempt2017A00001"
OTHER_ATTEMPT = "browserAttempt2017A00002"
INSTANCE = "engine-run-1"
KEY = "ab" * 32
GATE = "/api/engine/remote-dj-gate/"
ADMIN = "/api/engine/remote-dj-admin/"
ADMIN_REQUEST_ID = "f" * 32


class _RemoteMicHarness(_ConsoleBrowserBase):
    """The 2.23 Console harness only: its own test methods are not re-run
    here (they run in test_console_browser_2_23)."""

    def setUp(self):
        super().setUp()
        # The TransactionTestCase flush removed authz.0010's rows: recreate them.
        from authz.models import Capability
        for slug in ("remote_dj.emergency_mute", "remote_dj.force_disconnect"):
            Capability.objects.get_or_create(slug=slug, defaults={"label": slug, "requires_schedule": False})
        self.gate_aborts = 0              # gate POSTs to drop with a network error
        self.gate_reply = None            # (status, body) for gate POSTs
        self.passthrough = False          # let gate POSTs reach the real view
        self.admin_reply = None           # (status, body) for administrator POSTs

    def _route(self, route, request):
        path = request.url.split(self.live_server_url, 1)[-1].split("?", 1)[0]
        if request.method == "POST" and path == ADMIN:
            self.posts.append((path, request.post_data))
            if self.passthrough:
                return route.continue_()
            status, body = self.admin_reply or (202, {"ok": True, "accepted": True, "executed": False,
                                                      "request_id": ADMIN_REQUEST_ID})
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        if request.method == "POST" and path == GATE:
            self.posts.append((path, request.post_data))
            if self.passthrough:
                return route.continue_()
            if self.gate_aborts:
                self.gate_aborts -= 1
                return route.abort("connectionreset")
            status, body = self.gate_reply or (202, {"ok": True, "accepted": True, "executed": False})
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        return super()._route(route, request)

    # -- fixtures --------------------------------------------------------------
    def owner_block(self, attempt=ATTEMPT, *, current=True, gate_result=None, emergency_mute=False,
                    generation=1):
        block = {"identity": "known", "display_name": "Ann Air", "is_current_user": current,
                 "browser_key": self.browser_key(attempt), "emergency_mute": emergency_mute,
                 "gate_generation": generation}
        if gate_result is not None:
            block["gate_result"] = gate_result
        return block

    def connected_state(self, attempt=ATTEMPT, *, current=True, gate_result=None, emergency_mute=False,
                        generation=1, **overrides):
        return engine_state(remote_dj_connected=True, remote_dj_quality={"overall": "good"},
                            remote_dj_owner=self.owner_block(attempt, current=current, gate_result=gate_result,
                                                             emergency_mute=emergency_mute, generation=generation),
                            **overrides)

    def become_owner(self, page, *, attempt=ATTEMPT, key=KEY):
        page.evaluate("""async ([attempt, key]) => {
          rdjPc = { close() {}, iceConnectionState: 'connected' };
          rdjAttemptId = attempt;
          rdjBrowserKey = await rdjDeriveBrowserKey(attempt);
          rdjControlKey = key;
        }""", [attempt, key])

    def owner_page(self, *, user=None, path="/remote-dj/", viewport=DESKTOP, key=KEY, attempt=ATTEMPT):
        self.state = self.connected_state(attempt)
        page = self.open_console(user or self.host, path=path, viewport=viewport)
        self.become_owner(page, attempt=attempt, key=key)
        self.poll_now(page)
        return page

    def gate_ui(self, page):
        return page.evaluate("""() => {
          const b = document.getElementById('remoteDjGateBtn');
          return {disabled: b.disabled, aria: b.getAttribute('aria-label'), state: b.dataset.micState,
                  msg: document.getElementById('opsMsg').textContent};
        }""")

    def gate_bodies(self):
        return [json.loads(body) for path, body in self.posts if path == GATE]

    def press(self, page):
        page.evaluate("async () => { await toggleRemoteDjGate(); }")

    def expected_body(self, active, sequence, *, key=KEY, attempt=ATTEMPT, generation=1):
        return {"active": active, "sequence": sequence, "attempt_id": attempt, "control_key": key,
                "generation": generation, "engine_instance": INSTANCE}



for _name in [name for name in dir(_ConsoleBrowserBase) if name.startswith("test")]:
    setattr(_RemoteMicHarness, _name, None)


class _RemoteMicBindingCases(_RemoteMicHarness):
    # -- owner: normal controls with the bound request -------------------------
    def test_owner_opens_and_closes_with_the_bound_request_desktop_and_mobile(self):
        for name, viewport in (("desktop", DESKTOP), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(layout=name):
                self.posts.clear()
                page = self.owner_page(viewport=viewport)
                self.assertFalse(self.gate_ui(page)["disabled"])
                page.locator("#remoteDjGateBtn").click()
                page.wait_for_function("() => !isControlInFlight('remoteDjGateBtn')")
                self.poll_now(page, **self.connected_state(remote_dj_live=True))
                self.assertEqual(self.gate_ui(page)["state"], "on")
                page.locator("#remoteDjGateBtn").click()
                page.wait_for_function("() => !isControlInFlight('remoteDjGateBtn')")
                self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1), self.expected_body(False, 2)])
                self.no_horizontal_overflow(page)
                self.shot(page, f"rdj-gate-2-17a-owner-{name}")

    def test_full_console_owner_uses_the_same_contract(self):
        page = self.owner_page(user=self.superuser, path="/")
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1)])

    def test_lost_response_is_retried_once_with_the_identical_body(self):
        page = self.owner_page()
        self.gate_aborts = 1
        self.press(page)
        bodies = self.gate_bodies()
        self.assertEqual(bodies, [self.expected_body(True, 1)] * 2)
        self.assertEqual(self.gate_ui(page)["msg"], "")          # the retry was accepted
        # Two lost responses: no third send, an unconfirmed warning.
        self.posts.clear()
        self.gate_aborts = 2
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 2)] * 2)
        self.assertIn("outcome unconfirmed", self.gate_ui(page)["msg"])

    def test_no_retry_once_this_page_has_lost_the_session(self):
        page = self.owner_page()
        page.evaluate("""() => {
          const realFetch = window.fetch.bind(window);
          window.fetch = (url, opts) => {
            if (!opts || opts.method !== 'POST') return realFetch(url, opts);
            // The response is lost AND, meanwhile, another browser took over.
            window.__rdjLastEngineState = {...window.__rdjLastEngineState,
              remote_dj_owner: {...window.__rdjLastEngineState.remote_dj_owner, browser_key: 'f'.repeat(32)}};
            return realFetch(url, opts).then(() => { throw new TypeError('network'); });
          };
        }""")
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1)])
        self.assertIn("outcome unconfirmed", self.gate_ui(page)["msg"])

    def test_review_f4_no_owner_retry_once_a_latch_was_seen_since_the_press(self):
        page = self.owner_page()
        page.evaluate("""() => {
          const realFetch = window.fetch.bind(window);
          window.fetch = (url, opts) => {
            if (!opts || opts.method !== 'POST') return realFetch(url, opts);
            // While the press is in flight the station latches, then releases.
            const s = window.__rdjLastEngineState;
            renderRemoteDjGate({...s, remote_dj_owner: {...s.remote_dj_owner, emergency_mute: true}});
            renderRemoteDjGate(s);
            return realFetch(url, opts).then(() => { throw new TypeError('network'); });
          };
        }""")
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1)])       # the original only, no retry
        self.assertIn("outcome unconfirmed", self.gate_ui(page)["msg"])

    def test_lost_response_never_retries_against_a_new_generation(self):
        page = self.owner_page()
        page.evaluate("""() => {
          const realFetch = window.fetch.bind(window);
          window.fetch = (url, opts) => {
            if (!opts || opts.method !== 'POST') return realFetch(url, opts);
            const s = window.__rdjLastEngineState;
            window.__rdjLastEngineState = {...s, remote_dj_owner: {
              ...s.remote_dj_owner, gate_generation: 2}};
            return realFetch(url, opts).then(() => { throw new TypeError('network'); });
          };
        }""")
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1, generation=1)])
        self.assertIn("outcome unconfirmed", self.gate_ui(page)["msg"])

    def test_disable_release_requires_a_fresh_press_with_the_new_generation(self):
        page = self.owner_page()
        self.poll_now(page, **self.connected_state(emergency_mute=True, generation=2))
        self.assertTrue(self.gate_ui(page)["disabled"])
        self.assertEqual(self.gate_bodies(), [])
        self.poll_now(page, **self.connected_state(emergency_mute=False, generation=3))
        self.assertFalse(self.gate_ui(page)["disabled"])
        self.assertEqual(self.gate_bodies(), [])
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1, generation=3)])

    # -- fail closed in the page ---------------------------------------------
    def assert_read_only_and_silent(self, page, why=None):
        ui = self.gate_ui(page)
        self.assertTrue(ui["disabled"], ui)
        if why:
            self.assertIn(why, ui["aria"])
        page.locator("#remoteDjGateBtn").click(force=True)
        self.press(page)
        page.clock.run_for(200)
        self.assertEqual(self.gate_bodies(), [])

    def test_observer_and_wrong_users_browser_are_read_only(self):
        # Another user's connection; this browser holds no peer at all.
        self.state = self.connected_state(OTHER_ATTEMPT, current=False)
        for name, viewport in (("desktop", DESKTOP), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(layout=name):
                page = self.open_console(self.host, path="/remote-dj/", viewport=viewport)
                self.assert_read_only_and_silent(page)
                self.shot(page, f"rdj-gate-2-17a-observer-{name}")

    def test_same_user_in_another_browser_cannot_gate(self):
        # Same account, but this page's own attempt is not the live one.
        page = self.owner_page(attempt=OTHER_ATTEMPT)
        self.poll_now(page, **self.connected_state(ATTEMPT, current=True))
        self.assert_read_only_and_silent(page, "another browser holds the connection")

    def test_owner_page_without_its_control_key_cannot_gate(self):
        page = self.owner_page(key=None)
        self.assert_read_only_and_silent(page, "ownership is unconfirmed")

    def test_owner_status_without_a_generation_fails_closed_truthfully(self):
        page = self.owner_page()
        self.poll_now(page, **self.connected_state(generation=None))
        self.assert_read_only_and_silent(page, "PTT safety state is unavailable")

    def test_older_status_cannot_roll_generation_back(self):
        page = self.owner_page()
        seq = page.evaluate("() => acceptedRevision.seq")
        newer = self.connected_state(generation=3)
        newer["state_revision"] = {"instance": INSTANCE, "seq": seq + 1}
        self.poll_now(page, **newer)
        self.assertEqual(page.evaluate(
            "() => window.__rdjLastEngineState.remote_dj_owner.gate_generation"), 3)
        older = self.connected_state(generation=1)
        older["state_revision"] = {"instance": INSTANCE, "seq": seq}
        self.poll_now(page, **older)
        self.assertEqual(page.evaluate(
            "() => window.__rdjLastEngineState.remote_dj_owner.gate_generation"), 3)
        self.assertTrue(self.gate_ui(page)["disabled"])
        newest = self.connected_state(generation=3)
        newest["state_revision"] = {"instance": INSTANCE, "seq": seq + 2}
        self.poll_now(page, **newest)
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1, generation=3)])

    def test_status_without_an_engine_instance_fails_closed(self):
        page = self.owner_page()
        self.poll_now(page, **{**self.connected_state(), "state_revision": None})
        self.assert_read_only_and_silent(page)

    def test_stale_status_fails_closed(self):
        page = self.owner_page()
        self.status_fails = True
        page.clock.run_for(3_500)
        page.wait_for_function("() => document.getElementById('consoleRoot').classList.contains('connection-stale')")
        self.press(page)
        self.assertEqual(self.gate_bodies(), [])

    def test_disconnected_page_keeps_local_calibration(self):
        self.state = engine_state()
        page = self.open_console(self.host, path="/remote-dj/", viewport=MOBILE_PORTRAIT)
        ui = self.gate_ui(page)
        self.assertFalse(ui["disabled"])
        self.assertEqual(ui["state"], "calibrate")
        self.assertEqual(page.evaluate("() => rdjControlKey"), None)

    def test_disconnect_drops_the_control_key(self):
        page = self.owner_page()
        page.evaluate("() => rdjDisconnect()")
        self.assertIsNone(page.evaluate("() => rdjControlKey"))
        self.poll_now(page)
        self.assert_read_only_and_silent(page)

    # -- truthful refusals ---------------------------------------------------
    def test_server_refusal_reason_is_shown(self):
        page = self.owner_page()
        self.gate_reply = (409, {"error": "x", "reason": "not_current_session"})
        self.press(page)
        self.assertIn("that connection is no longer the live session", self.gate_ui(page)["msg"])
        self.assertEqual(self.gate_ui(page)["state"], "off")       # the engine state is unchanged

    def test_emergency_mute_makes_the_djs_gate_read_only(self):
        page = self.owner_page()
        self.poll_now(page, **self.connected_state(emergency_mute=True))
        # r0119: the owner's button clearly reads PTT DISABLED.
        self.assertEqual(page.evaluate(
            "() => [document.querySelector('#remoteDjGateBtn .ops-btn-label').textContent,"
            " document.getElementById('remoteDjGateBtn').dataset.micState]"), ["PTT DISABLED", "disabled"])
        self.assert_read_only_and_silent(page, "PTT DISABLED by the station")

    def test_engine_refusal_for_this_press_is_reported_once(self):
        page = self.owner_page()
        self.press(page)
        result = {"sequence": 1, "active": True, "outcome": "refused:not_authorized"}
        self.poll_now(page, **self.connected_state(gate_result=result))
        self.assertIn("refused by the engine: permission denied", self.gate_ui(page)["msg"])
        self.assertEqual(self.gate_ui(page)["state"], "off")
        self.assertEqual(page.evaluate("() => rdjGateReportedSeq"), 1)


class _AdminControlCases(_RemoteMicHarness):
    """r0119: the Remote DJ emergency controls operate through the existing
    Remote PTT (OFF-only Disable/Enable) and Connect (Kick) buttons; there is
    no dedicated row. Ownership is this browser's session, never the account."""

    DEDICATED = ("rdjAdminBar", "rdjEmergencyMuteBtn", "rdjReleaseMuteBtn", "rdjKickBtn", "rdjAdminTarget")

    def admin_page(self, viewport=DESKTOP, user=None, **state):
        self.state = self.connected_state(current=False, **state)
        page = self.open_console(user or self.superuser, viewport=viewport)
        self.poll_now(page)
        return page

    def ui(self, page):
        return page.evaluate("""(dedicated) => {
          const g = document.getElementById('remoteDjGateBtn');
          const c = document.getElementById('remoteDjConnectBtn');
          const a = document.getElementById('remoteDjConnectAction');
          const m = document.getElementById('rdjAdminMsg');
          return {
            gate: {disabled: g.disabled, state: g.dataset.micState, word: g.querySelector('.ops-btn-label').textContent,
                   aria: g.getAttribute('aria-label'), admin: g.classList.contains('rdj-admin-mode'),
                   pending: g.classList.contains('pending')},
            connect: {disabled: c.disabled, action: a.textContent,
                      status: document.getElementById('remoteDjConnectStatus').textContent,
                      aria: c.getAttribute('aria-label'), title: c.title, kick: c.classList.contains('rdj-kick-mode'),
                      clipped: a.scrollWidth > a.clientWidth},
            msg: m ? m.textContent : null,
            dedicated: dedicated.filter(id => document.getElementById(id) !== null),
          };
        }""", list(self.DEDICATED))

    def admin_bodies(self):
        return [json.loads(body) for path, body in self.posts if path == ADMIN]

    def other_posts(self):
        return [path for path, _ in self.posts if path != ADMIN]

    def settle(self, page):
        page.wait_for_function("() => rdjAdminUi.pending === null || rdjAdminUi.pending.requestId !== null")

    def press_gate(self, page):
        page.locator("#remoteDjGateBtn").click()
        self.settle(page)

    def engine_result(self, page, action, outcome="executed", **state):
        result = {"request_id": ADMIN_REQUEST_ID, "action": action, "outcome": outcome}
        self.poll_now(page, **self.connected_state(current=False, remote_dj_admin_result=result, **state))

    def expected(self, action):
        return {"action": action, "target_session": self.browser_key(ATTEMPT), "engine_instance": INSTANCE}

    def body_without_id(self, body):
        self.assertRegex(body["client_request_id"], r"^[A-Za-z0-9-]{8,64}$")
        return {k: v for k, v in body.items() if k != "client_request_id"}

    # -- layout and routing ----------------------------------------------------------
    def test_observing_admin_gets_disable_and_kick_in_the_existing_buttons(self):
        for name, viewport in (("desktop", DESKTOP), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(layout=name):
                page = self.admin_page(viewport=viewport)
                ui = self.ui(page)
                self.assertEqual(ui["dedicated"], [])
                self.assertEqual((ui["gate"]["disabled"], ui["gate"]["state"], ui["gate"]["word"], ui["gate"]["admin"]),
                                 (False, "off", "OFF", True))
                self.assertIn("press to disable Remote PTT", ui["gate"]["aria"])
                self.assertEqual((ui["connect"]["disabled"], ui["connect"]["action"], ui["connect"]["status"],
                                  ui["connect"]["kick"]), (False, "Kick Remote DJ", "Other browser · Good", True))
                self.assertFalse(ui["connect"]["clipped"])
                self.assertIn(f"Kick Ann Air (session {self.browser_key(ATTEMPT)[:8]})", ui["connect"]["title"])
                self.no_horizontal_overflow(page)
                self.shot(page, f"r0119-admin-observer-{name}")

    def test_same_account_in_another_browser_gets_the_administrator_controls(self):
        page = self.admin_page(**{})
        self.poll_now(page, **self.connected_state(current=True))      # the owner is this very account
        ui = self.ui(page)
        self.assertTrue(ui["gate"]["admin"] and ui["connect"]["kick"])

    def test_owner_browser_gets_ordinary_ptt_and_disconnect_even_as_superuser(self):
        page = self.owner_page(user=self.superuser, path="/")
        ui = self.ui(page)
        self.assertEqual((ui["gate"]["admin"], ui["connect"]["kick"], ui["connect"]["action"]),
                         (False, False, "Disconnect"))
        self.press(page)
        self.assertEqual(self.gate_bodies(), [self.expected_body(True, 1)])     # the owner's own gate request
        self.assertEqual(self.admin_bodies(), [])
        # Latched by the station: PTT DISABLED, read-only, nothing sent.
        self.posts.clear()
        self.poll_now(page, **self.connected_state(emergency_mute=True))
        ui = self.ui(page)
        self.assertEqual((ui["gate"]["word"], ui["gate"]["state"], ui["gate"]["disabled"], ui["gate"]["admin"]),
                         ("PTT DISABLED", "disabled", True, False))
        self.assertIn("you cannot turn it on until the station enables it", ui["gate"]["aria"])
        self.press(page)
        page.locator("#remoteDjGateBtn").click(force=True)
        self.assertEqual(self.posts, [])
        self.shot(page, "r0119-owner-ptt-disabled")
        # Own Disconnect stays local, never a Kick.
        page.locator("#remoteDjConnectBtn").click()
        self.assertEqual(self.posts, [])
        self.assertIsNone(page.evaluate("() => rdjPc"))

    # -- Disable / Enable --------------------------------------------------------------
    def test_disable_while_live_sends_emergency_mute_and_waits_for_the_engine(self):
        page = self.admin_page(remote_dj_live=True)
        self.assertEqual(self.ui(page)["gate"]["word"], "LIVE")
        self.press_gate(page)
        self.assertEqual([self.body_without_id(b) for b in self.admin_bodies()], [self.expected("emergency_mute")])
        self.assertEqual(self.gate_bodies(), [])
        ui = self.ui(page)
        self.assertIn("queued — waiting for the engine", ui["msg"])              # 202 is not executed
        self.assertTrue(ui["gate"]["disabled"] and ui["gate"]["pending"])       # single flight
        self.assertEqual(ui["gate"]["word"], "LIVE")                             # engine state unchanged yet
        page.evaluate("async () => { await toggleRemoteDjGate(); await rdjAdminCommand('emergency_mute'); }")
        self.assertEqual(len(self.admin_bodies()), 1)                            # repeated clicks: one command
        self.engine_result(page, "emergency_mute", emergency_mute=True, remote_dj_live=False)
        ui = self.ui(page)
        self.assertIn("Remote PTT disabled: the Remote DJ microphone is OFF and latched", ui["msg"])
        self.assertEqual((ui["gate"]["word"], ui["gate"]["state"], ui["gate"]["disabled"]),
                         ("PTT DISABLED", "disabled", False))
        self.assertIn("press to enable Remote PTT (the microphone stays OFF)", ui["gate"]["aria"])
        self.shot(page, "r0119-admin-ptt-disabled")

    def test_disable_while_off_latches_without_any_gate_request(self):
        page = self.admin_page()
        self.press_gate(page)
        self.assertEqual([self.body_without_id(b) for b in self.admin_bodies()], [self.expected("emergency_mute")])
        self.assertEqual(self.other_posts(), [])

    def test_enable_releases_the_latch_and_never_opens_the_mic(self):
        page = self.admin_page(emergency_mute=True)
        self.assertEqual(self.ui(page)["gate"]["word"], "PTT DISABLED")
        self.press_gate(page)
        self.assertEqual([self.body_without_id(b) for b in self.admin_bodies()], [self.expected("release_mute")])
        self.engine_result(page, "release_mute")                                   # latch gone, mic OFF
        ui = self.ui(page)
        self.assertIn("Remote PTT enabled: the microphone stays OFF until the DJ turns it on", ui["msg"])
        self.assertEqual((ui["gate"]["word"], ui["gate"]["admin"]), ("OFF", True))  # Disable available again
        self.assertEqual(self.gate_bodies(), [])                                      # never a mic-ON request
        self.assertNotIn("active", json.dumps(self.admin_bodies()))

    def test_server_refusal_is_reported_on_the_status_line(self):
        page = self.admin_page()
        self.admin_reply = (409, {"error": "x", "reason": "stale_target"})
        self.press_gate(page)
        self.assertIn("Disable Remote PTT refused: that Remote DJ session is no longer the live session",
                      self.ui(page)["msg"])
        self.assertFalse(self.ui(page)["gate"]["disabled"])

    # -- Kick ------------------------------------------------------------------------------
    def test_kick_through_connect_requires_confirmation_and_never_connects(self):
        page = self.admin_page()
        messages = []
        page.once("dialog", lambda d: (messages.append(d.message), d.dismiss()))
        page.locator("#remoteDjConnectBtn").click()
        page.wait_for_timeout(100)
        self.assertEqual(self.posts, [])                                            # cancelled: nothing sent
        self.assertIn(f"Kick Ann Air (session {self.browser_key(ATTEMPT)[:8]})", messages[0])
        self.assertIsNone(page.evaluate("() => rdjWs"))                             # no connection attempt
        page.once("dialog", lambda d: d.accept())
        page.locator("#remoteDjConnectBtn").click()
        self.settle(page)
        self.assertEqual([self.body_without_id(b) for b in self.admin_bodies()], [self.expected("kick")])
        self.assertEqual(self.other_posts(), [])                                    # no token request
        ui = self.ui(page)
        self.assertEqual((ui["connect"]["action"], ui["connect"]["disabled"]), ("Kicking…", True))
        self.poll_now(page, **engine_state(remote_dj_admin_result={"request_id": ADMIN_REQUEST_ID, "action": "kick",
                                                                   "outcome": "executed"}))
        ui = self.ui(page)
        self.assertIn("Kick: the Remote DJ was disconnected.", ui["msg"])
        self.assertEqual((ui["connect"]["action"], ui["connect"]["kick"]), ("Connect", False))
        self.assertEqual(ui["gate"]["state"], "calibrate")                          # no target, no admin mode

    def test_stale_local_link_only_closes_itself(self):
        page = self.admin_page()
        self.become_owner(page, attempt=OTHER_ATTEMPT)                              # leftover local link
        self.poll_now(page)
        ui = self.ui(page)
        self.assertEqual((ui["connect"]["action"], ui["connect"]["kick"]), ("Close stale link", False))
        self.assertEqual((ui["gate"]["admin"], ui["gate"]["disabled"]), (False, True))
        page.locator("#remoteDjConnectBtn").click()
        self.assertEqual(self.posts, [])                                            # local teardown only
        self.assertIsNone(page.evaluate("() => rdjPc"))

    def test_review_f1_closing_its_own_link_never_turns_a_press_into_kick(self):
        # Double press on "Close stale link": the first closes the local link;
        # the second must not reach Kick (no newer trusted snapshot yet).
        page = self.admin_page()
        self.become_owner(page, attempt=OTHER_ATTEMPT)
        self.poll_now(page)
        self.assertEqual(self.ui(page)["connect"]["action"], "Close stale link")
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
        page.locator("#remoteDjConnectBtn").dblclick(force=True)
        page.evaluate("async () => { await toggleRemoteDjConnect(); await toggleRemoteDjConnect(); }")
        ui = self.ui(page)
        self.assertEqual((ui["connect"]["kick"], ui["connect"]["disabled"], ui["gate"]["admin"]), (False, True, False))
        self.assertEqual((self.posts, dialogs), ([], []))
        # The local socket's close then flashes an error: still nothing.
        page.evaluate("() => rdjConnectFail('Closed (1006)')")
        page.evaluate("async () => { await toggleRemoteDjConnect(); }")
        ui = self.ui(page)
        self.assertTrue(ui["connect"]["disabled"])
        self.assertEqual((self.posts, dialogs), ([], []))
        # Only a newer trusted snapshot, seen AFTER the close, offers Kick.
        page.clock.run_for(3_100)                                                   # error text expires
        self.poll_now(page)
        ui = self.ui(page)
        self.assertEqual((ui["connect"]["action"], ui["connect"]["kick"], ui["gate"]["admin"]),
                         ("Kick Remote DJ", True, True))

    def test_review_f2_any_pending_admin_request_blocks_the_other_button(self):
        page = self.admin_page()
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        self.press_gate(page)                                                       # Disable pending
        ui = self.ui(page)
        self.assertTrue(ui["connect"]["disabled"])
        self.assertNotEqual(ui["connect"]["action"], "Kicking…")
        page.evaluate("async () => { await toggleRemoteDjConnect(); await rdjAdminCommand('kick'); }")
        self.assertEqual([b["action"] for b in self.admin_bodies()], ["emergency_mute"])
        self.assertEqual(dialogs, [])

    # -- capability isolation -----------------------------------------------------------
    def grant_user(self, username, slugs):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        from authz.models import Capability, GroupRole, Role, RoleCapability
        from library.models import GroupAccess

        user = get_user_model().objects.create_user(username, password="x", is_staff=True)
        role = Role.objects.create(name=f"{username} role")
        for slug in slugs:
            RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug=slug))
        group = Group.objects.create(name=f"{username} group")
        GroupRole.objects.create(group=group, role=role)
        GroupAccess.objects.create(group=group, allowed_prefixes="/\n", landing_url="/")
        user.groups.add(group)
        return user

    def test_capabilities_route_each_button_independently(self):
        cases = (("r0119_none", (), False, False),
                 ("r0119_mute", ("remote_dj.emergency_mute",), True, False),
                 ("r0119_kick", ("remote_dj.force_disconnect",), False, True))
        for username, slugs, can_mute, can_kick in cases:
            with self.subTest(user=username):
                self.posts.clear()
                page = self.admin_page(user=self.grant_user(username, slugs))
                ui = self.ui(page)
                self.assertEqual(ui["dedicated"], [])
                self.assertEqual((ui["gate"]["admin"], ui["gate"]["disabled"]), (can_mute, not can_mute))
                self.assertEqual((ui["connect"]["kick"], ui["connect"]["disabled"]), (can_kick, not can_kick))
                self.assertEqual(ui["msg"] is not None, can_mute or can_kick)
                if not can_kick:
                    self.assertNotEqual(ui["connect"]["action"], "Kick Remote DJ")
                page.evaluate("""async () => { await toggleRemoteDjGate(); await rdjAdminCommand('emergency_mute');
                                               await rdjAdminCommand('release_mute'); }""")
                if not can_kick:
                    page.evaluate("async () => { await rdjAdminCommand('kick'); }")
                sent = [b["action"] for b in self.admin_bodies()]
                self.assertEqual(sent, ["emergency_mute"] if can_mute else [])
                self.assertEqual(self.gate_bodies(), [])

    def test_staff_controls_follow_independent_role_grants_after_page_refresh(self):
        # r0118: ordinary staff need real Role grants; r0119 routes them
        # into the buttons. A refreshed page reflects grant changes.
        from authz.models import Capability, RoleCapability

        user = self.grant_user("rdj_browser_staff", ())
        page = self.admin_page(user=user)
        self.assertEqual(page.evaluate("() => [CAN_RDJ_MUTE, CAN_RDJ_KICK]"), [False, False])
        self.assertIsNone(self.ui(page)["msg"])
        from authz.models import GroupRole
        role = GroupRole.objects.get(group=user.groups.get()).role
        grant = RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="remote_dj.emergency_mute"))
        page = self.admin_page(user=user)
        self.assertEqual(page.evaluate("() => [CAN_RDJ_MUTE, CAN_RDJ_KICK]"), [True, False])
        self.assertTrue(self.ui(page)["gate"]["admin"])
        grant.delete()
        RoleCapability.objects.create(role=role, capability=Capability.objects.get(slug="remote_dj.force_disconnect"))
        page = self.admin_page(user=user)
        self.assertEqual(page.evaluate("() => [CAN_RDJ_MUTE, CAN_RDJ_KICK]"), [False, True])
        ui = self.ui(page)
        self.assertEqual((ui["gate"]["admin"], ui["connect"]["kick"]), (False, True))

    # -- fail closed ---------------------------------------------------------------------
    def test_unverified_stale_or_absent_session_offers_no_administrator_action(self):
        page = self.admin_page()
        self.poll_now(page, **{**self.connected_state(current=False), "state_revision": None})
        ui = self.ui(page)
        self.assertEqual((ui["gate"]["admin"], ui["gate"]["disabled"], ui["connect"]["kick"], ui["connect"]["disabled"]),
                         (False, True, False, True))
        page.evaluate("""async () => { await toggleRemoteDjGate(); await toggleRemoteDjConnect();
                                       await rdjAdminCommand('emergency_mute'); await rdjAdminCommand('kick'); }""")
        self.poll_now(page, **engine_state())                                       # nobody connected
        ui = self.ui(page)
        self.assertEqual((ui["gate"]["state"], ui["gate"]["admin"], ui["connect"]["action"], ui["connect"]["kick"]),
                         ("calibrate", False, "Connect", False))
        page.evaluate("async () => { await rdjAdminCommand('emergency_mute'); await rdjAdminCommand('kick'); }")
        self.poll_now(page, **self.connected_state(current=False))
        self.status_fails = True
        page.clock.run_for(3_500)
        page.wait_for_function("() => document.getElementById('consoleRoot').classList.contains('connection-stale')")
        page.evaluate("async () => { await toggleRemoteDjGate(); await rdjAdminCommand('kick'); }")
        self.assertEqual(self.posts, [])

    def test_remote_dj_talent_surface_gains_no_administrator_behavior(self):
        self.state = self.connected_state(current=False)
        for user in (self.host, self.superuser):
            with self.subTest(user=user.username):
                page = self.open_console(user, path="/remote-dj/")
                self.poll_now(page)
                ui = self.ui(page)
                self.assertEqual(page.evaluate("() => [CAN_RDJ_MUTE, CAN_RDJ_KICK]"), [False, False])
                self.assertEqual((ui["msg"], ui["dedicated"], ui["gate"]["admin"], ui["connect"]["kick"]),
                                 (None, [], False, False))
                self.assertTrue(ui["gate"]["disabled"] and ui["connect"]["disabled"])
                page.evaluate("async () => { await toggleRemoteDjGate(); await rdjAdminCommand('kick'); }")
        self.assertEqual(self.posts, [])


class _EndToEndCases(_RemoteMicHarness):
    """The page's gate POST reaches the real view; only the queue is captured."""

    def setUp(self):
        super().setUp()
        self.passthrough = True
        self.queued = []
        self.enqueue.side_effect = self.queued.append
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "engine_state.json"
        # Dated ahead so it stays fresh however long the browser takes to start.
        self.state_path.write_text(json.dumps({
            "timestamp": time.time() + 3600, "remote_dj_connected": True,
            "remote_dj_owner": {"user_id": self.host.pk, "attempt_id": ATTEMPT, "gate_generation": 1},
            "state_revision": {"instance": INSTANCE, "seq": 9},
        }))
        patcher = mock.patch.object(views, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # As seeded in production (library.0054): remote_dj reaches the gate.
        from library.models import GroupAccess
        access = GroupAccess.objects.get(group__name="remote_dj")
        access.allowed_prefixes += "\n/api/engine/remote-dj-gate/"
        access.save()

    def real_key(self, attempt=ATTEMPT):
        return gate_identity.control_key_for(self.host.pk, attempt)

    def test_owner_press_is_accepted_by_the_real_view(self):
        page = self.owner_page(key=self.real_key())
        self.press(page)
        self.assertEqual(self.gate_ui(page)["msg"], "")
        self.assertEqual(len(self.queued), 1)
        queued = self.queued[0]
        self.assertEqual((queued["attempt_id"], queued["user_id"], queued["active"], queued["sequence"]),
                         (ATTEMPT, self.host.pk, True, 1))
        self.assertTrue(gate_identity.verify_command_proof(queued))
        self.assertNotIn(self.real_key(), json.dumps(queued))

    def test_a_key_minted_for_another_attempt_is_refused_by_the_real_view(self):
        page = self.owner_page(key=self.real_key(OTHER_ATTEMPT))
        self.press(page)
        self.assertIn("this browser does not hold the Remote DJ connection", self.gate_ui(page)["msg"])
        self.assertEqual(self.queued, [])

    def test_admin_emergency_mute_is_accepted_and_signed_by_the_real_view(self):
        self.state = self.connected_state(current=False)
        page = self.open_console(self.superuser)
        self.poll_now(page)
        page.locator("#remoteDjGateBtn").click()                 # r0119: the Remote PTT button
        page.wait_for_function("() => rdjAdminUi.pending && rdjAdminUi.pending.requestId")
        self.assertEqual(len(self.queued), 1)
        queued = self.queued[0]
        from library.services import remote_dj_admin_identity as admin_identity
        self.assertEqual((queued["command"], queued["action"], queued["attempt_id"], queued["target_user_id"]),
                         ("remote_dj_admin", "emergency_mute", ATTEMPT, self.host.pk))
        self.assertTrue(admin_identity.verify_signature(queued))

    def test_a_remote_host_page_cannot_call_the_admin_api(self):
        self.state = self.connected_state()
        page = self.open_console(self.host, path="/remote-dj/")
        statuses = page.evaluate("""async (url) => {
          const out = [];
          for (const action of ['emergency_mute', 'release_mute', 'kick']) {
            const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json',
              'X-CSRFToken': getCookie('csrftoken')}, body: JSON.stringify({action,
              target_session: 'a'.repeat(32), engine_instance: 'engine-run-1', client_request_id: 'forged-press-1'})});
            out.push(r.status);
          }
          return out;
        }""", ADMIN)
        self.assertEqual(statuses, [403, 403, 403])
        self.assertEqual(self.queued, [])

    def test_forged_direct_posts_get_the_same_enforcement(self):
        self.state = self.connected_state()
        page = self.open_console(self.host, path="/remote-dj/")
        forged = page.evaluate("""async ([url, bodies]) => {
          const out = [];
          for (const [csrf, body] of bodies) {
            const headers = {'Content-Type': 'application/json'};
            if (csrf) headers['X-CSRFToken'] = getCookie('csrftoken');
            const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
            out.push(r.status);
          }
          return out;
        }""", [GATE, [
            [False, self.expected_body(True, 1, key=self.real_key())],        # no CSRF token
            [True, {"active": True}],                                            # the r0117 body
            [True, self.expected_body(True, 1, key=KEY)],                        # copied ids, no proof
            [True, self.expected_body(False, 1, key=self.real_key(OTHER_ATTEMPT))],
        ]])
        self.assertEqual(forged, [403, 400, 403, 403])
        self.assertEqual(self.queued, [])


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteMicBindingChromiumTests(_RemoteMicBindingCases, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteMicBindingFirefoxTests(_RemoteMicBindingCases, StaticLiveServerTestCase):
    BROWSER = "firefox"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteMicEndToEndChromiumTests(_EndToEndCases, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteMicEndToEndFirefoxTests(_EndToEndCases, StaticLiveServerTestCase):
    BROWSER = "firefox"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteDjAdminChromiumTests(_AdminControlCases, StaticLiveServerTestCase):
    BROWSER = "chromium"


@unittest.skipIf(sync_playwright is None, "Playwright not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class RemoteDjAdminFirefoxTests(_AdminControlCases, StaticLiveServerTestCase):
    BROWSER = "firefox"

