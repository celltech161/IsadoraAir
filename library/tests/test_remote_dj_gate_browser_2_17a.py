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
    def owner_block(self, attempt=ATTEMPT, *, current=True, gate_result=None, emergency_mute=False):
        block = {"identity": "known", "display_name": "Ann Air", "is_current_user": current,
                 "browser_key": self.browser_key(attempt), "emergency_mute": emergency_mute}
        if gate_result is not None:
            block["gate_result"] = gate_result
        return block

    def connected_state(self, attempt=ATTEMPT, *, current=True, gate_result=None, emergency_mute=False,
                        **overrides):
        return engine_state(remote_dj_connected=True, remote_dj_quality={"overall": "good"},
                            remote_dj_owner=self.owner_block(attempt, current=current, gate_result=gate_result,
                                                             emergency_mute=emergency_mute),
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

    def expected_body(self, active, sequence, *, key=KEY, attempt=ATTEMPT):
        return {"active": active, "sequence": sequence, "attempt_id": attempt, "control_key": key,
                "engine_instance": INSTANCE}



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
        self.assert_read_only_and_silent(page, "muted by the station")

    def test_engine_refusal_for_this_press_is_reported_once(self):
        page = self.owner_page()
        self.press(page)
        result = {"sequence": 1, "active": True, "outcome": "refused:not_authorized"}
        self.poll_now(page, **self.connected_state(gate_result=result))
        self.assertIn("refused by the engine: permission denied", self.gate_ui(page)["msg"])
        self.assertEqual(self.gate_ui(page)["state"], "off")
        self.assertEqual(page.evaluate("() => rdjGateReportedSeq"), 1)


class _AdminControlCases(_RemoteMicHarness):
    """Roadmap 2.17A Station Administrator controls on the full Console."""

    def admin_page(self, viewport=DESKTOP, **state):
        self.state = self.connected_state(current=False, **state)
        page = self.open_console(self.superuser, viewport=viewport)
        self.poll_now(page)
        return page

    def admin_ui(self, page):
        return page.evaluate("""() => {
          const q = id => document.getElementById(id);
          const b = id => q(id) ? {hidden: q(id).hidden, disabled: q(id).disabled} : null;
          return {bar: q('rdjAdminBar') ? q('rdjAdminBar').hidden : null,
                  target: q('rdjAdminTarget') ? q('rdjAdminTarget').textContent : null,
                  msg: q('rdjAdminMsg') ? q('rdjAdminMsg').textContent : null,
                  mute: b('rdjEmergencyMuteBtn'), release: b('rdjReleaseMuteBtn'), kick: b('rdjKickBtn')};
        }""")

    def admin_bodies(self):
        return [json.loads(body) for path, body in self.posts if path == ADMIN]

    def click(self, page, selector):
        page.locator(selector).click()
        page.wait_for_function("() => rdjAdminUi.pending === null || rdjAdminUi.pending.requestId !== null")

    def test_controls_target_the_observed_session_desktop_and_mobile(self):
        for name, viewport in (("desktop", DESKTOP), ("mobile", MOBILE_PORTRAIT)):
            with self.subTest(layout=name):
                page = self.admin_page(viewport=viewport)
                ui = self.admin_ui(page)
                self.assertFalse(ui["bar"])
                self.assertEqual(ui["target"], f"Remote DJ: Ann Air · session {self.browser_key(ATTEMPT)[:8]}")
                self.assertEqual((ui["mute"], ui["release"], ui["kick"]),
                                 ({"hidden": False, "disabled": False}, {"hidden": True, "disabled": False},
                                  {"hidden": False, "disabled": False}))
                self.no_horizontal_overflow(page)
                self.shot(page, f"rdj-admin-controls-{name}")

    def test_emergency_mute_sends_the_exact_target_and_waits_for_the_engine(self):
        page = self.admin_page()
        self.click(page, "#rdjEmergencyMuteBtn")
        bodies = self.admin_bodies()
        self.assertEqual(len(bodies), 1)
        self.assertEqual({k: v for k, v in bodies[0].items() if k != "client_request_id"},
                         {"action": "emergency_mute", "target_session": self.browser_key(ATTEMPT),
                          "engine_instance": INSTANCE})
        self.assertRegex(bodies[0]["client_request_id"], r"^[A-Za-z0-9-]{8,64}$")
        ui = self.admin_ui(page)
        self.assertIn("queued — waiting for the engine", ui["msg"])        # accepted is not executed
        self.assertTrue(ui["mute"]["disabled"])                             # single flight
        result = {"request_id": ADMIN_REQUEST_ID, "action": "emergency_mute", "outcome": "executed"}
        self.poll_now(page, **self.connected_state(current=False, emergency_mute=True,
                                                   remote_dj_admin_result=result))
        ui = self.admin_ui(page)
        self.assertIn("microphone is OFF and latched", ui["msg"])
        self.assertIn("EMERGENCY MUTED", ui["target"])
        self.assertEqual((ui["mute"]["hidden"], ui["release"]["hidden"], ui["release"]["disabled"]),
                         (True, False, False))
        self.shot(page, "rdj-admin-emergency-muted")

    def test_release_and_engine_refusals_are_reported_from_engine_results(self):
        page = self.admin_page(emergency_mute=True)
        self.click(page, "#rdjReleaseMuteBtn")
        self.assertEqual(self.admin_bodies()[0]["action"], "release_mute")
        result = {"request_id": ADMIN_REQUEST_ID, "action": "release_mute", "outcome": "stale_target"}
        self.poll_now(page, **self.connected_state(current=False, emergency_mute=True,
                                                   remote_dj_admin_result=result))
        self.assertIn("Release Emergency Mute refused: that Remote DJ session is no longer the live session",
                      self.admin_ui(page)["msg"])

    def test_kick_requires_a_confirmation_naming_the_target(self):
        page = self.admin_page()
        messages = []
        page.once("dialog", lambda d: (messages.append(d.message), d.dismiss()))
        page.locator("#rdjKickBtn").click()
        page.wait_for_timeout(100)
        self.assertEqual(self.admin_bodies(), [])
        self.assertIn(f"Kick Ann Air (session {self.browser_key(ATTEMPT)[:8]})", messages[0])
        page.once("dialog", lambda d: d.accept())
        self.click(page, "#rdjKickBtn")
        self.assertEqual(self.admin_bodies()[0]["action"], "kick")

    def test_server_refusal_is_shown(self):
        page = self.admin_page()
        self.admin_reply = (409, {"error": "x", "reason": "stale_target"})
        self.click(page, "#rdjEmergencyMuteBtn")
        self.assertIn("Emergency Mute refused: that Remote DJ session is no longer the live session",
                      self.admin_ui(page)["msg"])

    def test_unverified_stale_or_absent_session_disables_the_controls(self):
        page = self.admin_page()
        self.poll_now(page, **{**self.connected_state(current=False), "state_revision": None})
        ui = self.admin_ui(page)
        self.assertTrue(ui["mute"]["disabled"] and ui["kick"]["disabled"])
        self.assertIn("status unconfirmed", ui["target"])
        page.evaluate("async () => { await rdjAdminCommand('emergency_mute'); await rdjAdminCommand('kick'); }")
        self.poll_now(page, **engine_state())                               # nobody connected
        self.assertTrue(self.admin_ui(page)["bar"])
        self.status_fails = True
        page.clock.run_for(3_500)
        page.evaluate("async () => { await rdjAdminCommand('emergency_mute'); }")
        self.assertEqual(self.admin_bodies(), [])

    def test_remote_dj_talent_surface_has_no_admin_controls(self):
        self.state = self.connected_state()
        for user in (self.host, self.superuser):
            with self.subTest(user=user.username):
                page = self.open_console(user, path="/remote-dj/")
                self.assertIsNone(self.admin_ui(page)["bar"])
                self.assertEqual(page.evaluate("() => [CAN_RDJ_MUTE, CAN_RDJ_KICK]"), [False, False])


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
            "remote_dj_owner": {"user_id": self.host.pk, "attempt_id": ATTEMPT},
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
        page.locator("#rdjEmergencyMuteBtn").click()
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

