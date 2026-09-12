import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Group, User
from django.core.signing import BadSignature, SignatureExpired
from django.test import SimpleTestCase, TestCase, override_settings

from isadoraair.deploy_baseline import REQUIRED_GST_ELEMENTS
from library.models import RemoteDJConfig
from library.services import remote_dj_signaling as signaling
from library.services.remote_dj_connection import (
    FAILURE_DEPENDENCY_SESSION_BUILD,
    FAILURE_SIGNALING_SESSION_BUSY,
    MAX_REASON_LENGTH,
    RemoteDJConnectionAttempt,
    browser_ice_servers,
    mint_remote_dj_token,
    normalize_browser_stun_server,
    verify_remote_dj_token,
)


ATTEMPT_ID = "A1_fixed_attempt_123456"


class RemoteDJTokenContractTests(SimpleTestCase):
    def test_attempt_identity_is_bound_into_signed_token(self):
        token, payload = mint_remote_dj_token(
            42, attempt_id=ATTEMPT_ID, issued_at_ms=1_700_000_000_000
        )

        verified = verify_remote_dj_token(token, max_age=60)

        self.assertEqual(verified["user_id"], 42)
        self.assertEqual(verified["attempt_id"], payload["attempt_id"])
        self.assertEqual(verified["issued_at_ms"], payload["issued_at_ms"])

    def test_tampered_token_remains_invalid(self):
        token, _payload = mint_remote_dj_token(42, attempt_id=ATTEMPT_ID)
        replacement = "A" if token[-1] != "A" else "B"
        with self.assertRaises(BadSignature):
            verify_remote_dj_token(token[:-1] + replacement, max_age=60)

    def test_expired_token_remains_invalid(self):
        token, _payload = mint_remote_dj_token(42, attempt_id=ATTEMPT_ID)
        with self.assertRaises(SignatureExpired):
            verify_remote_dj_token(token, max_age=-1)


class BrowserStunNormalizationTests(SimpleTestCase):
    def test_default_gstreamer_url_becomes_browser_rtc_url(self):
        self.assertEqual(
            normalize_browser_stun_server("stun://stun.l.google.com:19302"),
            "stun:stun.l.google.com:19302",
        )
        self.assertEqual(
            browser_ice_servers("stun://stun.l.google.com:19302"),
            [{"urls": "stun:stun.l.google.com:19302"}],
        )

    def test_valid_hostname_without_port_is_preserved(self):
        self.assertEqual(
            normalize_browser_stun_server("stun://stun.example.test"),
            "stun:stun.example.test",
        )

    def test_malformed_or_unsafe_values_fail_clearly(self):
        for value in (
            "",
            "https://stun.example.test",
            "stun:stun.example.test",
            "stun://user@stun.example.test:3478",
            "stun://stun.example.test:99999",
            "stun://stun.example.test/path",
            "stun://stun.example.test:3478?secret=yes",
            "stun://stun example.test:3478",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_browser_stun_server(value)


@override_settings(SECURE_SSL_REDIRECT=False)
class RemoteDJTokenEndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("remote-observer", password="test")
        group, _created = Group.objects.get_or_create(name="remote_dj")
        self.user.groups.add(group)
        self.client.force_login(self.user)

    def test_response_contains_bound_attempt_and_authoritative_ice_server(self):
        RemoteDJConfig.objects.update_or_create(
            pk=1, defaults={"stun_server": "stun://stun.example.test:3478"}
        )

        response = self.client.post("/api/remote-dj/token/")
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            data["ice_servers"], [{"urls": "stun:stun.example.test:3478"}]
        )
        verified = verify_remote_dj_token(data["token"], max_age=60)
        self.assertEqual(verified["attempt_id"], data["attempt_id"])
        self.assertEqual(verified["user_id"], self.user.id)

    def test_malformed_config_fails_without_fallback_server(self):
        RemoteDJConfig.objects.update_or_create(
            pk=1, defaults={"stun_server": "https://not-stun.example.test"}
        )
        response = self.client.post("/api/remote-dj/token/")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("token", response.json())
        self.assertNotIn("ice_servers", response.json())

    def test_group_authorization_boundary_is_unchanged(self):
        self.user.groups.clear()
        response = self.client.post("/api/remote-dj/token/")
        self.assertEqual(response.status_code, 403)


class _FakeWebSocket:
    def __init__(self, token, messages=()):
        self.request = SimpleNamespace(path=f"/ws/remote-dj/?token={token}")
        self._messages = list(messages)
        self.closed = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def close(self, *, code=None, reason=None):
        self.closed = (code, reason)


class RemoteDJSignalingAttemptTests(SimpleTestCase):
    def _server(self):
        engine = SimpleNamespace(
            _remote_dj_session_start=MagicMock(),
            _remote_dj_session_stop=MagicMock(),
            _remote_dj_handle_answer=MagicMock(),
            _remote_dj_handle_ice=MagicMock(),
            _remote_dj_record_browser_milestone=MagicMock(),
            _remote_dj_record_signaling_failure=MagicMock(),
        )
        return signaling.RemoteDJSignalingServer(engine), engine

    def test_valid_socket_admits_only_signed_attempt_identity(self):
        token, payload = mint_remote_dj_token(
            42, attempt_id=ATTEMPT_ID, issued_at_ms=1_700_000_000_000
        )
        ws = _FakeWebSocket(token)
        server, engine = self._server()

        with patch.object(signaling.GLib, "idle_add") as idle_add:
            asyncio.run(server._handler(ws))

        self.assertEqual(idle_add.call_args_list[0].args, (
            engine._remote_dj_session_start,
            payload["attempt_id"],
            payload["issued_at_ms"],
        ))
        self.assertEqual(
            idle_add.call_args_list[-1].args, (engine._remote_dj_session_stop,)
        )

    def test_invalid_token_is_rejected_before_engine_crossing(self):
        ws = _FakeWebSocket("client-selected-not-signed")
        server, _engine = self._server()
        with patch.object(signaling.GLib, "idle_add") as idle_add:
            asyncio.run(server._handler(ws))
        self.assertEqual(ws.closed[0], signaling.CLOSE_CODE_INVALID_TOKEN)
        idle_add.assert_not_called()

    def test_session_busy_has_typed_attempt_correlated_failure(self):
        token, payload = mint_remote_dj_token(42, attempt_id=ATTEMPT_ID)
        ws = _FakeWebSocket(token)
        server, engine = self._server()
        server._ws = object()

        with patch.object(signaling.GLib, "idle_add") as idle_add:
            asyncio.run(server._handler(ws))

        self.assertEqual(ws.closed[0], signaling.CLOSE_CODE_SESSION_BUSY)
        self.assertEqual(idle_add.call_args.args, (
            engine._remote_dj_record_signaling_failure,
            payload["attempt_id"],
            payload["issued_at_ms"],
            FAILURE_SIGNALING_SESSION_BUSY,
            "a session is already active",
        ))

    def test_browser_milestone_cannot_select_engine_attempt(self):
        token, _payload = mint_remote_dj_token(42, attempt_id=ATTEMPT_ID)
        raw = json.dumps({
            "type": "milestone",
            "milestone": "browser_token_received",
            "elapsed_ms": 12.3,
            "attempt_id": "malicious-other-attempt",
        })
        ws = _FakeWebSocket(token, [raw])
        server, engine = self._server()

        with patch.object(signaling.GLib, "idle_add") as idle_add:
            asyncio.run(server._handler(ws))

        milestone_call = next(
            call for call in idle_add.call_args_list
            if call.args and call.args[0] is engine._remote_dj_record_browser_milestone
        )
        self.assertEqual(
            milestone_call.args,
            (engine._remote_dj_record_browser_milestone, "browser_token_received", 12.3),
        )


class RemoteDJAttemptTelemetryTests(SimpleTestCase):
    def _attempt(self):
        monotonic_values = iter((10.0, 10.1, 10.2, 10.3, 10.4))
        return RemoteDJConnectionAttempt(
            ATTEMPT_ID,
            1_700_000_000_000,
            monotonic=lambda: next(monotonic_values),
            wall_time=lambda: 1_700_000_000.050,
        )

    def test_stage_does_not_regress_when_browser_timing_arrives_late(self):
        attempt = self._attempt()
        attempt.record("websocket_admitted")
        attempt.record("glib_session_start")
        attempt.record("browser_token_received", browser_elapsed_ms=20)
        self.assertEqual(attempt.stage, "glib_session_start")
        self.assertEqual(attempt.milestones["browser_token_received"], 20.0)
        snapshot = attempt.snapshot()
        self.assertEqual(
            snapshot["milestones_ms"]["browser"]["browser_token_received"],
            20.0,
        )
        self.assertIn("glib_session_start", snapshot["milestones_ms"]["server"])

    def test_duplicate_unknown_and_unbounded_milestones_are_rejected(self):
        attempt = self._attempt()
        self.assertTrue(
            attempt.record("browser_token_received", browser_elapsed_ms=20)
        )
        self.assertFalse(
            attempt.record("browser_token_received", browser_elapsed_ms=30)
        )
        self.assertFalse(attempt.record("client_chose_this"))
        self.assertFalse(
            attempt.record("ice_connected", browser_elapsed_ms=99_000_000)
        )
        self.assertEqual(len(attempt.milestones), 2)

    def test_failure_vocabulary_and_reason_are_bounded_in_snapshot(self):
        attempt = self._attempt()
        attempt.fail(FAILURE_DEPENDENCY_SESSION_BUILD, "x" * 1000)
        snapshot = attempt.snapshot()
        self.assertEqual(
            snapshot["failure"]["class"], FAILURE_DEPENDENCY_SESSION_BUILD
        )
        self.assertLessEqual(
            len(snapshot["failure"]["reason"]), MAX_REASON_LENGTH
        )
        self.assertEqual(snapshot["status"], "failed")
        self.assertNotIn("candidate", snapshot)


class RemoteDJBrowserAndDeploymentContractTests(SimpleTestCase):
    def test_dashboard_uses_server_ice_config_and_relative_timing(self):
        template = (
            Path(__file__).parents[1] / "templates/library/dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "iceServers: [{urls: 'stun:stun.l.google.com:19302'}]", template
        )
        self.assertIn("iceServers: rdjIceServers", template)
        self.assertIn("rdjConnectStartedAt = performance.now()", template)
        self.assertIn("type: 'milestone'", template)
        milestone_helper = template[
            template.index("function rdjSendMilestone"):
            template.index("function rdjConnect")
        ]
        self.assertNotIn("Date.now()", milestone_helper)

    def test_deploy_baseline_explicitly_requires_libnice_elements(self):
        self.assertIn("webrtcbin", REQUIRED_GST_ELEMENTS)
        self.assertIn("nicesrc", REQUIRED_GST_ELEMENTS)
        self.assertIn("nicesink", REQUIRED_GST_ELEMENTS)
