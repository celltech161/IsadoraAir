"""Roadmap 2.17A -- Remote DJ microphone gate: server-side session binding.

A Remote Host must not open or close another DJ's microphone, and a delayed
command issued for one Remote DJ session must never affect a replacement
session. Contract: library.services.remote_dj_gate_identity.

* RemoteDjGateIdentityTests -- the shared credential/schema/proof contract.
* RemoteDjGateEndpointTests -- the Django boundary (before queueing).
* RemoteDjGateEngineTests -- the engine boundary (immediately before
  gate_desired, the physical gate, ducking or Manual ownership changes),
  driven through the real _dispatch_engine_command, _remote_dj_set_gate,
  _apply_talk_ducking, _apply_mic_mode_hold, recovery and session-stop code
  with real GStreamer volume elements; only the duck ramp timer, GLib
  timeouts and SystemEvents are stubbed.
"""
import contextlib
import hashlib
import io
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, SimpleTestCase, TestCase, override_settings

import library.services.engine as eng_module
import library.views as views
from authz.models import GroupRole, Role, ScheduleAccessConfig
from hardware.models import DuckingConfig
from library.models import RemoteDJConfig
from library.services import remote_dj_gate_identity as gi_mod
from library.services.remote_dj_connection import RemoteDJConnectionAttempt, mint_remote_dj_token

Gst.init(None)
User = get_user_model()

ATTEMPT_A = "attemptAAAAAAAAAAAAAAAAAA"
ATTEMPT_A2 = "attemptA2A2A2A2A2A2A2A2A2"
ATTEMPT_B = "attemptBBBBBBBBBBBBBBBBBB"
INSTANCE = "engine-run-1"
URL = "/api/engine/remote-dj-gate/"


def browser_key(attempt_id):
    return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]


def request_body(user_id, attempt_id, *, active=True, sequence=1, generation=1, instance=INSTANCE, key=None):
    return {
        "active": active,
        "attempt_id": attempt_id,
        "control_key": key if key is not None else gi_mod.control_key_for(user_id, attempt_id),
        "sequence": sequence,
        "generation": generation,
        "engine_instance": instance,
    }


def command(user_id, attempt_id, *, active=True, sequence=1, generation=1, instance=INSTANCE, key=None,
            issued_at_ms=None):
    request = request_body(user_id, attempt_id, active=active, sequence=sequence,
                           generation=generation, instance=instance, key=key)
    return gi_mod.build_command(
        user_id=user_id, request=request,
        issued_at_ms=issued_at_ms if issued_at_ms is not None else time.time() * 1000)


def remote_host_group():
    group, _ = Group.objects.get_or_create(name="remote_dj")
    GroupRole.objects.update_or_create(group=group, defaults={"role": Role.objects.get(name="Remote Host")})
    return group


# ---------------------------------------------------------------------------
class RemoteDjGateIdentityTests(SimpleTestCase):
    def test_control_key_is_bound_to_user_and_attempt(self):
        key = gi_mod.control_key_for(7, ATTEMPT_A)
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertEqual(key, gi_mod.control_key_for(7, ATTEMPT_A))
        self.assertTrue(gi_mod.verify_control_key(7, ATTEMPT_A, key))
        self.assertFalse(gi_mod.verify_control_key(8, ATTEMPT_A, key))          # another user
        self.assertFalse(gi_mod.verify_control_key(7, ATTEMPT_B, key))          # another attempt
        # A freshly minted credential for B never proves ownership of A.
        self.assertFalse(gi_mod.verify_control_key(7, ATTEMPT_A, gi_mod.control_key_for(7, ATTEMPT_B)))

    def test_public_identities_are_not_credentials(self):
        for candidate in (ATTEMPT_A, browser_key(ATTEMPT_A), hashlib.sha256(ATTEMPT_A.encode()).hexdigest(),
                          "", None, 0, key_upper := gi_mod.control_key_for(7, ATTEMPT_A).upper()):
            with self.subTest(candidate=candidate):
                self.assertFalse(gi_mod.verify_control_key(7, ATTEMPT_A, candidate))
        self.assertTrue(key_upper)

    def test_request_schema_is_strict(self):
        good = request_body(7, ATTEMPT_A)
        self.assertEqual(gi_mod.parse_request(good), good)
        bad = []
        for field in gi_mod.REQUEST_FIELDS:
            missing = dict(good)
            del missing[field]
            bad.append(missing)
        bad += [
            {**good, "extra": 1},
            {**good, "user_id": 7},
            {**good, "active": "true"}, {**good, "active": 1}, {**good, "active": None},
            {**good, "sequence": 0}, {**good, "sequence": True}, {**good, "sequence": 1.0},
            {**good, "sequence": "1"}, {**good, "sequence": gi_mod.MAX_SEQUENCE + 1},
            {**good, "generation": 0}, {**good, "generation": True},
            {**good, "generation": -1}, {**good, "generation": 1.5},
            {**good, "generation": gi_mod.MAX_GENERATION + 1},
            {**good, "attempt_id": "short"}, {**good, "attempt_id": None},
            {**good, "control_key": "f" * 63}, {**good, "control_key": "F" * 64},
            {**good, "engine_instance": ""}, {**good, "engine_instance": " x"},
            [], "x", None,
        ]
        for body in bad:
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    gi_mod.parse_request(body)

    def test_command_carries_a_proof_not_the_key(self):
        cmd = command(7, ATTEMPT_A)
        self.assertEqual(set(cmd), gi_mod.COMMAND_FIELDS)
        self.assertNotIn(gi_mod.control_key_for(7, ATTEMPT_A), json.dumps(cmd))
        self.assertEqual(gi_mod.parse_command(cmd), cmd)
        self.assertTrue(gi_mod.verify_command_proof(cmd))

    def test_proof_binds_every_field(self):
        cmd = command(7, ATTEMPT_A, active=True, sequence=3)
        for field, value in (("active", False), ("sequence", 4), ("generation", 2), ("engine_instance", "engine-run-2"),
                             ("issued_at_ms", cmd["issued_at_ms"] + 1), ("user_id", 8),
                             ("attempt_id", ATTEMPT_B)):
            with self.subTest(field=field):
                forged = {**cmd, field: value}
                forged["request_id"] = gi_mod.derive_request_id(
                    forged["user_id"], forged["attempt_id"], forged["sequence"])
                self.assertFalse(gi_mod.verify_command_proof(forged))

    def test_command_schema_is_strict(self):
        cmd = command(7, ATTEMPT_A)
        for bad in ({"command": "remote_dj_gate", "active": True},          # the r0117 shape
                    {**cmd, "control_key": "f" * 64},
                    {**cmd, "request_id": "0" * 32},                      # not derived from its binding
                    {**cmd, "active": 1}, {**cmd, "user_id": True}, {**cmd, "issued_at_ms": 1.5},
                    {**cmd, "generation": 0}, {**cmd, "generation": True},
                    {**cmd, "generation": gi_mod.MAX_GENERATION + 1},
                    {k: v for k, v in cmd.items() if k != "proof"},
                    {k: v for k, v in cmd.items() if k != "generation"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gi_mod.parse_command(bad)

    def test_retry_of_one_press_derives_one_request_id(self):
        first = command(7, ATTEMPT_A, sequence=5, issued_at_ms=1_000)
        retry = command(7, ATTEMPT_A, sequence=5, issued_at_ms=2_000)
        self.assertEqual(first["request_id"], retry["request_id"])
        self.assertNotEqual(first["request_id"], command(7, ATTEMPT_A, sequence=6)["request_id"])
        self.assertNotEqual(first["request_id"], command(7, ATTEMPT_A2, sequence=5)["request_id"])


# ---------------------------------------------------------------------------
@override_settings(SECURE_SSL_REDIRECT=False)
class RemoteDjGateEndpointTests(TestCase):
    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        group = remote_host_group()
        self.dj_a = User.objects.create_user("g217a_dj_a", password="x", first_name="Ann", last_name="Air")
        self.dj_b = User.objects.create_user("g217a_dj_b", password="x")
        for user in (self.dj_a, self.dj_b):
            user.groups.add(group)
        self.contributor = User.objects.create_user("g217a_contrib", password="x")
        self.contributor.groups.add(Group.objects.get_or_create(name="Contributor")[0])
        self.su = User.objects.create_superuser("g217a_su", "su@example.invalid", "x")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "engine_state.json"
        self.set_owner(self.dj_a, ATTEMPT_A)
        patcher = mock.patch.object(views, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        enqueue = mock.patch("library.views.enqueue_engine_command")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)

    def set_owner(self, user, attempt, *, connected=True, timestamp=None, instance=INSTANCE, extra=None):
        state = {
            "timestamp": time.time() if timestamp is None else timestamp,
            "remote_dj_connected": connected,
            "remote_dj_owner": {"user_id": user.pk, "attempt_id": attempt, "gate_generation": 1,
                                **(extra or {})} if user else None,
            "state_revision": {"instance": instance, "seq": 5},
        }
        self.state_path.write_text(json.dumps(state))

    def post(self, user, body, *, csrf=False, client=None):
        client = client or Client(enforce_csrf_checks=csrf)
        if user is not None:
            client.force_login(user)
        return client.post(URL, data=json.dumps(body), content_type="application/json")

    def assert_refused(self, resp, status, reason=None):
        self.assertEqual(resp.status_code, status, resp.content)
        if reason is not None:
            self.assertEqual(resp.json().get("reason"), reason)
        self.enqueue.assert_not_called()

    # -- positive: the owner controls their own mic ------------------------
    def test_owner_opens_and_closes_their_own_mic(self):
        for sequence, active in ((1, True), (2, False)):
            resp = self.post(self.dj_a, request_body(self.dj_a.pk, ATTEMPT_A, active=active, sequence=sequence))
            self.assertEqual(resp.status_code, 202, resp.content)
            body = resp.json()
            self.assertEqual((body["ok"], body["accepted"], body["executed"], body["sequence"]),
                             (True, True, False, sequence))
            queued = self.enqueue.call_args.args[0]
            self.assertEqual(set(queued), gi_mod.COMMAND_FIELDS)
            self.assertEqual((queued["command"], queued["user_id"], queued["attempt_id"], queued["active"],
                              queued["sequence"], queued["generation"], queued["engine_instance"]),
                             ("remote_dj_gate", self.dj_a.pk, ATTEMPT_A, active, sequence, 1, INSTANCE))
            self.assertTrue(gi_mod.verify_command_proof(queued))
            self.assertEqual(body["request_id"], queued["request_id"])
            # The queue never carries the credential itself.
            self.assertNotIn(gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A), json.dumps(queued))
        self.assertEqual(self.enqueue.call_count, 2)

    def test_lost_response_retry_queues_the_same_request_id(self):
        body = request_body(self.dj_a.pk, ATTEMPT_A, sequence=4)
        first = self.post(self.dj_a, body).json()["request_id"]
        second = self.post(self.dj_a, body).json()["request_id"]
        self.assertEqual(first, second)
        self.assertEqual(self.enqueue.call_count, 2)   # the engine applies it once (engine tests)

    def test_superuser_owning_the_session_may_gate_it(self):
        self.set_owner(self.su, ATTEMPT_A)
        resp = self.post(self.su, request_body(self.su.pk, ATTEMPT_A))
        self.assertEqual(resp.status_code, 202, resp.content)

    # -- cross-session negatives -------------------------------------------
    def test_another_remote_host_cannot_open_or_close_the_connected_djs_mic(self):
        own_key = gi_mod.control_key_for(self.dj_b.pk, ATTEMPT_B)
        for active in (True, False):
            with self.subTest(active=active):
                # B knows A's public attempt id; its own attempt's key proves nothing about A.
                resp = self.post(self.dj_b, request_body(self.dj_b.pk, ATTEMPT_A, active=active, key=own_key))
                self.assert_refused(resp, 403, "not_owner")
                # Even A's real key (were it leaked) is bound to A, not to B's session.
                resp = self.post(self.dj_b, request_body(self.dj_a.pk, ATTEMPT_A, active=active))
                self.assert_refused(resp, 403, "not_owner")
                # And a key valid for (B, A's attempt) -- which the server never
                # issues -- still fails the live-owner check.
                resp = self.post(self.dj_b, request_body(self.dj_b.pk, ATTEMPT_A, active=active))
                self.assert_refused(resp, 409, "not_current_session")

    def test_another_remote_host_with_its_own_fresh_attempt_cannot_gate_a(self):
        _token, minted = mint_remote_dj_token(self.dj_b.pk)
        resp = self.post(self.dj_b, request_body(self.dj_b.pk, minted["attempt_id"]))
        self.assert_refused(resp, 409, "not_current_session")

    def test_same_user_in_another_browser_needs_the_owning_browsers_proof(self):
        # The second browser mints its own attempt A2 (busy at signaling);
        # its key proves nothing about the connected A.
        key_a2 = gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A2)
        resp = self.post(self.dj_a, request_body(self.dj_a.pk, ATTEMPT_A, key=key_a2))
        self.assert_refused(resp, 403, "not_owner")
        resp = self.post(self.dj_a, request_body(self.dj_a.pk, ATTEMPT_A2))
        self.assert_refused(resp, 409, "not_current_session")

    def test_copied_attempt_id_or_browser_key_is_not_a_credential(self):
        for key in (browser_key(ATTEMPT_A) * 2, hashlib.sha256(ATTEMPT_A.encode()).hexdigest()):
            with self.subTest(key=key):
                resp = self.post(self.dj_a, request_body(self.dj_a.pk, ATTEMPT_A, key=key))
                self.assert_refused(resp, 403, "not_owner")

    def test_staff_have_no_cross_session_bypass(self):
        # A superuser passes authorize() unconditionally, yet still cannot
        # gate a session it does not own.
        resp = self.post(self.su, request_body(self.su.pk, ATTEMPT_A,
                                               key=gi_mod.control_key_for(self.su.pk, ATTEMPT_B)))
        self.assert_refused(resp, 403, "not_owner")
        resp = self.post(self.su, request_body(self.dj_a.pk, ATTEMPT_A))
        self.assert_refused(resp, 403, "not_owner")
        resp = self.post(self.su, request_body(self.su.pk, ATTEMPT_A))
        self.assert_refused(resp, 409, "not_current_session")

    def test_ended_or_replaced_session_is_refused(self):
        body = request_body(self.dj_a.pk, ATTEMPT_A)
        self.set_owner(self.dj_b, ATTEMPT_B)                     # B replaced A
        self.assert_refused(self.post(self.dj_a, body), 409, "not_current_session")
        self.set_owner(None, None, connected=False)              # nobody connected
        self.assert_refused(self.post(self.dj_a, body), 409, "not_current_session")
        self.set_owner(self.dj_a, ATTEMPT_A, connected=False)    # stale owner block, disconnected
        self.assert_refused(self.post(self.dj_a, body), 409, "not_current_session")

    def test_stale_missing_or_restarted_engine_is_refused(self):
        body = request_body(self.dj_a.pk, ATTEMPT_A)
        self.set_owner(self.dj_a, ATTEMPT_A, timestamp=time.time() - 11)
        self.assert_refused(self.post(self.dj_a, body), 409, "engine_unavailable")
        self.state_path.unlink()
        self.assert_refused(self.post(self.dj_a, body), 409, "engine_unavailable")
        self.state_path.write_text("{not json")
        self.assert_refused(self.post(self.dj_a, body), 409, "engine_unavailable")
        self.set_owner(self.dj_a, ATTEMPT_A, instance="engine-run-2")
        self.assert_refused(self.post(self.dj_a, body), 409, "engine_restarted")

    def test_generation_must_be_available_and_match_current_session(self):
        body = request_body(self.dj_a.pk, ATTEMPT_A, generation=1)
        self.set_owner(self.dj_a, ATTEMPT_A, extra={"gate_generation": 2})
        self.assert_refused(self.post(self.dj_a, body), 409, "stale_generation")

        # Generation gates activation, not a proven owner's request to make
        # this exact current session safer. The queued command retains the
        # observed generation for independent engine validation.
        self.enqueue.reset_mock()
        stale_off = request_body(
            self.dj_a.pk, ATTEMPT_A, active=False, sequence=2, generation=1)
        response = self.post(self.dj_a, stale_off)
        self.assertEqual(response.status_code, 202, response.content)
        queued = self.enqueue.call_args.args[0]
        self.assertEqual((queued["active"], queued["generation"]), (False, 1))
        self.assertTrue(gi_mod.verify_command_proof(queued))

        for unavailable in (None, True, 0, -1, "2", gi_mod.MAX_GENERATION + 1):
            with self.subTest(generation=unavailable):
                self.enqueue.reset_mock()
                self.set_owner(self.dj_a, ATTEMPT_A, extra={"gate_generation": unavailable})
                self.assert_refused(self.post(self.dj_a, body), 409, "generation_unavailable")

    def test_malformed_requests_are_refused_before_any_check(self):
        good = request_body(self.dj_a.pk, ATTEMPT_A)
        for body in ({"active": True}, {**good, "active": "false"}, {**good, "active": 0},
                     {**good, "extra": True}, {k: v for k, v in good.items() if k != "control_key"},
                     {k: v for k, v in good.items() if k != "sequence"},
                     {k: v for k, v in good.items() if k != "generation"}, [good]):
            with self.subTest(body=body):
                self.assert_refused(self.post(self.dj_a, body), 400)
        client = Client()
        client.force_login(self.dj_a)
        resp = client.post(URL, data="{bad", content_type="application/json")
        self.assert_refused(resp, 400)

    def test_capability_and_schedule_are_still_required(self):
        self.assert_refused(self.post(self.contributor, request_body(self.contributor.pk, ATTEMPT_A)), 403)
        resp = self.post(None, request_body(self.dj_a.pk, ATTEMPT_A))
        self.assertNotIn(resp.status_code, (200, 202))
        self.enqueue.assert_not_called()
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True     # A has no TalentAssignment window
        cfg.save()
        self.assert_refused(self.post(self.dj_a, request_body(self.dj_a.pk, ATTEMPT_A)), 403)

    # -- CSRF -----------------------------------------------------------------
    def test_gate_requires_the_csrf_token(self):
        body = request_body(self.dj_a.pk, ATTEMPT_A)
        self.assert_refused(self.post(self.dj_a, body, csrf=True), 403)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.dj_a)
        client.get("/remote-dj/")                    # ensure_csrf_cookie
        token = client.cookies["csrftoken"].value
        resp = client.post(URL, data=json.dumps(body), content_type="application/json",
                           HTTP_X_CSRFTOKEN=token)
        self.assertEqual(resp.status_code, 202, resp.content)

    def test_token_endpoint_requires_csrf_and_returns_the_attempt_key_only_there(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.dj_a)
        self.assertEqual(client.post("/api/remote-dj/token/").status_code, 403)
        client.get("/remote-dj/")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resp = client.post("/api/remote-dj/token/", HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(gi_mod.verify_control_key(self.dj_a.pk, body["attempt_id"], body["control_key"]))
        self.assertNotIn(body["control_key"], out.getvalue())       # never logged
        self.assertNotIn(body["control_key"], body["token"])        # not inside the signaling token

    # -- status --------------------------------------------------------------
    def test_status_never_carries_the_key_and_shows_gate_result_to_the_owner_only(self):
        result = {"sequence": 3, "active": True, "outcome": "refused:out_of_order"}
        self.set_owner(self.dj_a, ATTEMPT_A, extra={"gate_result": result})
        key = gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A)
        for user, expected in ((self.dj_a, result), (self.dj_b, None), (self.su, None)):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                resp = client.get("/api/engine/status/")
                self.assertNotIn(key, resp.content.decode())
                self.assertNotIn(ATTEMPT_A, resp.content.decode())
                self.assertEqual(resp.json()["remote_dj_owner"].get("gate_result"), expected)


# ---------------------------------------------------------------------------
class GateEngineHarness:
    """A bare PlaybackEngine with one real persistent slot gate (synthetic
    GStreamer volume element), real ducking/Manual/recovery/session-stop
    code, and only the duck ramp timer, GLib timeouts, SystemEvents and the
    connection recycler stubbed. Shared by the 2.17A engine test modules."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        DuckingConfig.objects.update_or_create(
            pk=1, defaults={"enabled": True, "duck_level_db": -12.0, "ptt_auto_manual_enabled": True})
        rcfg = RemoteDJConfig.load()
        rcfg.reconnect_grace_seconds = 10
        rcfg.save()
        group = remote_host_group()
        self.dj_a = User.objects.create_user("g217a_eng_a", password="x")
        self.dj_b = User.objects.create_user("g217a_eng_b", password="x")
        for user in (self.dj_a, self.dj_b):
            user.groups.add(group)

        self.ramps = []
        self.events = []
        self.timers = []
        for patcher in (
            mock.patch.object(eng_module.PlaybackEngine, "_start_duck_ramp",
                              lambda engine, target: self.ramps.append(round(target, 4))),
            mock.patch.object(eng_module, "emit_event", lambda *a, **k: self.events.append(k)),
            mock.patch.object(eng_module.GLib, "timeout_add", side_effect=self._timeout_add),
            # The engine recycles its connection before each authorization
            # read; inside a TestCase transaction that would close it.
            mock.patch.object(eng_module, "_close_old_connections", new=lambda: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        engine = object.__new__(eng_module.PlaybackEngine)
        engine.mic_ptt_volume = Gst.ElementFactory.make("volume", None)
        engine.mic_ptt_volume.set_property("volume", 0.0)
        engine.mic_live = False
        engine.manual_mode = False
        engine._manual_from_mic = False
        engine._manual_hold_pending = False
        engine._ptt_auto_manual_enabled = True
        engine._engine_session_id = INSTANCE
        engine._remote_dj_server = mock.MagicMock()
        engine._remote_dj_last_attempt = None
        engine.remote_dj_tee = mock.MagicMock()
        engine.local_mic_tee = None
        engine.main_pipeline = mock.MagicMock()
        self.gate = Gst.ElementFactory.make("volume", None)
        self.gate.set_property("volume", 0.0)
        self.slot = eng_module.RemoteDJSlot(
            slot_id=0, selector=mock.MagicMock(), silence_pad=mock.MagicMock(),
            webrtc_pad=mock.MagicMock(), remote_gain=mock.MagicMock(), remote_gate=self.gate,
            master_mixer_pad=mock.MagicMock(), silence_src=mock.MagicMock(),
        )
        self.slot.webrtc_pad.get_peer.return_value = None
        engine.dj_slots = [self.slot]
        engine.remote_dj_session = None
        self.engine = engine

    def _timeout_add(self, interval_ms, callback, *args):
        self.timers.append((interval_ms, callback, args))
        return 5000 + len(self.timers)

    # -- harness ---------------------------------------------------------------
    def connect(self, user, attempt_id):
        """What _remote_dj_session_start leaves behind for an admitted,
        connected attempt: a fresh session, physical gate closed."""
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(attempt_id, int(time.time() * 1000))
        session.connection_attempt.record("peer_connected")
        session.webrtc = mock.MagicMock()
        session.slot_id = 0
        session.user_id = user.pk
        self.slot.session = session
        self.gate.set_property("volume", 0.0)
        self.engine.remote_dj_session = session
        return session

    def send(self, user, attempt_id, **kwargs):
        session = self.engine.remote_dj_session
        if "generation" not in kwargs and session is not None:
            kwargs["generation"] = session.gate_generation
        cmd = command(user.pk, attempt_id, **kwargs)
        before = self.snapshot()
        return self.engine._remote_dj_gate_command(cmd), before

    def dispatch(self, data):
        return self.engine._dispatch_engine_command(data)

    def snapshot(self):
        session = self.engine.remote_dj_session
        return {
            "physical": self.gate.get_property("volume"),
            "desired": session.gate_desired if session else None,
            "manual": self.engine.manual_mode,
            "manual_from_mic": self.engine._manual_from_mic,
            "mic_live": self.engine.mic_live,
            "studio_gate": self.engine.mic_ptt_volume.get_property("volume"),
            "session": session,
            "attempt": session.connection_attempt.attempt_id if session else None,
            "last_sequence": session.gate_last_sequence if session else None,
            "ramps": list(self.ramps),
        }

    def assert_no_effect(self, outcome, before, reason):
        self.assertEqual(outcome, f"refused:{reason}")
        self.assertEqual(self.snapshot(), before)

    DUCKED = round(10 ** (-12.0 / 20.0), 4)


class RemoteDjGateEngineTests(GateEngineHarness, TestCase):
    # -- positive ---------------------------------------------------------------
    def test_owner_opens_and_closes_their_own_mic(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        outcome, _ = self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        self.assertEqual(outcome, "applied")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (1.0, True))
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, True))
        self.assertEqual(self.ramps, [self.DUCKED])
        self.assertEqual(session.gate_result, {"sequence": 1, "active": True, "outcome": "applied"})
        outcome, _ = self.send(self.dj_a, ATTEMPT_A, active=False, sequence=2)
        self.assertEqual(outcome, "applied")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertFalse(self.engine.manual_mode)               # mic-owned Manual released to Auto
        self.assertEqual(self.ramps, [self.DUCKED, 1.0])

    def test_wrong_generation_is_refused_without_any_session_or_audio_mutation(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        stale = command(self.dj_a.pk, ATTEMPT_A, active=True, generation=2)
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(stale), before, "stale_generation")
        self.assertEqual((session.gate_generation, session.gate_last_sequence), (1, 0))
        self.assertIsNone(session.emergency_latch)

    def test_stale_generation_off_during_recovery_is_safe_ordered_and_does_not_block_fresh_on(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)[0],
                         "applied")
        stale_off = command(
            self.dj_a.pk, ATTEMPT_A, active=False, sequence=2, generation=1)
        self.engine._remote_dj_begin_recovery(
            session, reason="transport_disconnected")
        self.assertEqual(session.gate_generation, 2)

        self.assertEqual(self.engine._remote_dj_gate_command(stale_off), "applied")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired,
                          self.engine.manual_mode, session.gate_last_sequence),
                         (0.0, False, False, 2))
        before = self.snapshot()
        self.assertEqual(self.engine._remote_dj_gate_command(dict(stale_off)), "duplicate")
        self.assertEqual(self.snapshot(), before)

        fresh_on = command(
            self.dj_a.pk, ATTEMPT_A, active=True, sequence=3, generation=2)
        self.assertEqual(self.engine._remote_dj_gate_command(fresh_on),
                         "applied_held_muted")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired),
                         (0.0, True))

        # Once the later ON is accepted, neither an older OFF nor an old-
        # generation ON can override it.
        before = self.snapshot()
        self.assert_no_effect(
            self.engine._remote_dj_gate_command(dict(stale_off)), before,
            "out_of_order")
        old_on = command(
            self.dj_a.pk, ATTEMPT_A, active=True, sequence=4, generation=1)
        before = self.snapshot()
        self.assert_no_effect(
            self.engine._remote_dj_gate_command(old_on), before,
            "stale_generation")

        self.engine._remote_dj_complete_recovery(
            session, trigger="transport_connected")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired),
                         (1.0, True))

    def test_the_queued_command_goes_through_the_real_dispatcher(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.dispatch(command(self.dj_a.pk, ATTEMPT_A, active=True))
        self.assertEqual(self.gate.get_property("volume"), 1.0)

    def test_real_queue_round_trip_preserves_the_bound_command(self):
        # Enqueue with the real producer API, consume with the real poller.
        from isadoraair import engine_commands

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with mock.patch.object(engine_commands, "ENGINE_COMMAND_QUEUE_DIR", root / "engine_cmd.d"), \
                mock.patch.object(engine_commands, "ENGINE_COMMAND_LOCK_PATH", root / "engine_cmd.lock"), \
                mock.patch.object(eng_module, "CMD_PATH", root / "engine_cmd.json"):
            self.connect(self.dj_a, ATTEMPT_A)
            engine_commands.enqueue_engine_command(command(self.dj_a.pk, ATTEMPT_A, active=True))
            self.engine._check_commands()
        self.assertEqual(self.gate.get_property("volume"), 1.0)
        self.assertEqual(self.engine.remote_dj_session.gate_result["outcome"], "applied")

    # -- cross-session negatives --------------------------------------------------
    def test_unbound_r0117_command_has_no_effect(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        for data in ({"command": "remote_dj_gate", "active": True},
                     {"command": "remote_dj_gate", "active": False}):
            before = self.snapshot()
            self.dispatch(data)
            self.assertEqual(self.snapshot(), before)
        self.assertIsNone(session.gate_result)

    def test_a_delayed_on_after_b_replaces_a_has_no_effect(self):
        self.connect(self.dj_a, ATTEMPT_A)
        late_on = command(self.dj_a.pk, ATTEMPT_A, active=True)   # issued while A was live
        self.engine._remote_dj_session_stop()
        b = self.connect(self.dj_b, ATTEMPT_B)
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(late_on), before, "not_current_session")
        self.assertIsNone(b.gate_result)                            # B's owner sees nothing of A's

    def test_a_delayed_off_after_b_replaces_a_does_not_mute_b(self):
        self.connect(self.dj_a, ATTEMPT_A)
        late_off = command(self.dj_a.pk, ATTEMPT_A, active=False)
        self.engine._remote_dj_session_stop()
        b = self.connect(self.dj_b, ATTEMPT_B)
        self.assertEqual(self.send(self.dj_b, ATTEMPT_B, active=True)[0], "applied")
        before = self.snapshot()
        self.assertEqual((before["physical"], before["desired"], before["manual_from_mic"]), (1.0, True, True))
        self.assert_no_effect(self.engine._remote_dj_gate_command(late_off), before, "not_current_session")
        self.assertEqual(b.gate_result["outcome"], "applied")

    def test_remote_host_a_cannot_gate_bs_session(self):
        self.connect(self.dj_b, ATTEMPT_B)
        self.send(self.dj_b, ATTEMPT_B, active=True)
        for active in (False, True):
            with self.subTest(active=active):
                # A names B's attempt: A cannot produce B's proof.
                outcome, before = self.send(self.dj_a, ATTEMPT_B, active=active, sequence=9,
                                            key=gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A))
                self.assert_no_effect(outcome, before, "wrong_user")
                # Even claiming B's user id, a proof under any key but B's fails.
                forged = command(self.dj_b.pk, ATTEMPT_B, active=active, sequence=9,
                                 key=gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A))
                before = self.snapshot()
                self.assert_no_effect(self.engine._remote_dj_gate_command(forged), before, "bad_proof")

    def test_same_user_new_attempt_replaces_the_old_one(self):
        self.connect(self.dj_a, ATTEMPT_A)
        old = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=7)
        self.engine._remote_dj_session_stop()
        session = self.connect(self.dj_a, ATTEMPT_A2)
        self.assertEqual((session.gate_last_sequence, session.gate_result, session.gate_generation), (0, None, 1))   # nothing inherited
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(old), before, "not_current_session")
        # The new attempt starts its own sequence at 1.
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A2, active=True, sequence=1)[0], "applied")

    def test_same_user_other_browser_or_copied_identity_has_no_proof(self):
        self.connect(self.dj_a, ATTEMPT_A)
        for key in (gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A2), browser_key(ATTEMPT_A) * 2,
                    hashlib.sha256(ATTEMPT_A.encode()).hexdigest()):
            with self.subTest(key=key):
                outcome, before = self.send(self.dj_a, ATTEMPT_A, key=key)
                self.assert_no_effect(outcome, before, "bad_proof")

    # -- replay, duplicates, ordering, freshness ----------------------------------
    def test_lost_response_retry_and_duplicate_delivery_apply_once(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        first = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=1)
        retry = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=1, issued_at_ms=first["issued_at_ms"] + 300)
        self.assertEqual(self.engine._remote_dj_gate_command(first), "applied")
        self.assertEqual(self.ramps, [self.DUCKED])
        for again in (retry, dict(first)):
            before = self.snapshot()
            self.assertEqual(self.engine._remote_dj_gate_command(again), "duplicate")
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(session.gate_result["outcome"], "applied")

    def test_an_older_press_never_undoes_a_newer_one(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        press1_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=1)
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=False, sequence=2)[0], "applied")
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(press1_on), before, "out_of_order")
        self.assertEqual(session.gate_result, {"sequence": 1, "active": True, "outcome": "refused:out_of_order"})

    def test_engine_restart_between_request_and_execution(self):
        self.connect(self.dj_a, ATTEMPT_A)
        outcome, before = self.send(self.dj_a, ATTEMPT_A, instance="engine-run-0")
        self.assert_no_effect(outcome, before, "engine_restarted")

    def test_delayed_or_future_dated_command_is_stale(self):
        self.connect(self.dj_a, ATTEMPT_A)
        now = time.time() * 1000
        for issued in (now - gi_mod.MAX_COMMAND_AGE_MS - 50, now + gi_mod.MAX_FUTURE_SKEW_MS + 500):
            outcome, before = self.send(self.dj_a, ATTEMPT_A, issued_at_ms=issued)
            self.assert_no_effect(outcome, before, "stale_command")

    def test_scheduled_access_expiry_refuses_both_directions(self):
        """r0119: deterministic. The refused OFF makes the engine force the
        mic off and set gate_mute_floor_ms = now. A later ON is then refused
        by ONE of two safe checks, depending only on its issue time relative
        to that floor (the floor check runs before re-authorization): issued
        in the floor's millisecond or earlier -> superseded_by_mute; after it
        -> not_authorized. r0118's version stamped the ON with the wall clock,
        so which refusal it saw depended on whether both stamps fell in the
        same millisecond. Both orderings are pinned here, each with zero
        effect; the safety invariants are asserted directly."""
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, True))
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True          # no TalentAssignment window
        cfg.save()
        # The OFF is refused (no authority), but the engine -- having just
        # learned mic permission is gone -- forces the live mic off itself
        # (2.17A correction; test_remote_dj_mic_permission_expiry_2_17a).
        outcome, _ = self.send(self.dj_a, ATTEMPT_A, active=False, sequence=2)
        self.assertEqual(outcome, "refused:not_authorized")
        floor = session.gate_mute_floor_ms
        self.assertGreater(floor, 0)
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertFalse(self.engine.manual_mode)              # mic-owned Manual released
        # An ON is refused with no effect at all, whichever check sees it first.
        for issued_at_ms, reason in ((floor, "superseded_by_mute"), (floor + 1, "not_authorized")):
            with self.subTest(issued_at_ms=issued_at_ms - floor):
                outcome, before = self.send(self.dj_a, ATTEMPT_A, active=True, sequence=3,
                                            issued_at_ms=issued_at_ms)
                self.assert_no_effect(outcome, before, reason)
                self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
                self.assertFalse(self.engine.manual_mode)
                self.assertEqual(session.gate_last_sequence, 1)
        # The revocation lifecycle itself (the existing authorization tick)
        # retires the session and mutes through the internal path.
        self.engine._remote_dj_authorization_tick()
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual(self.gate.get_property("volume"), 0.0)
        self.assertFalse(self.engine.manual_mode)
        # A stale action for the retired session cannot touch its replacement.
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        replacement = self.connect(self.dj_b, ATTEMPT_B)
        stale_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=4)
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(stale_on), before, "not_current_session")
        self.assertIs(self.engine.remote_dj_session, replacement)

    def test_no_session_or_ended_session(self):
        outcome, before = self.send(self.dj_a, ATTEMPT_A)
        self.assert_no_effect(outcome, before, "no_session")
        session = self.connect(self.dj_a, ATTEMPT_A)
        session.connection_attempt.end()
        outcome, before = self.send(self.dj_a, ATTEMPT_A)
        self.assert_no_effect(outcome, before, "session_ended")

    def test_disconnect_while_a_command_is_queued(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        queued_off = command(self.dj_a.pk, ATTEMPT_A, active=False, sequence=2)
        queued_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=3)
        self.engine._remote_dj_session_stop(ATTEMPT_A)       # signaling disconnect
        self.assertEqual((self.gate.get_property("volume"), self.engine.manual_mode), (0.0, False))
        for cmd in (queued_off, queued_on):
            before = self.snapshot()
            self.assert_no_effect(self.engine._remote_dj_gate_command(cmd), before, "no_session")

    # -- recovery -----------------------------------------------------------------
    def test_on_during_recovery_holds_intent_and_stays_muted_until_recovered(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_begin_recovery(session, reason="transport_disconnected")
        self.assertEqual(session.connection_attempt.status, "reconnecting")
        outcome, _ = self.send(self.dj_a, ATTEMPT_A, active=True)
        self.assertEqual(outcome, "applied_held_muted")
        self.assertEqual((session.gate_desired, self.gate.get_property("volume")), (True, 0.0))
        self.assertFalse(self.engine.manual_mode)
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.gate.get_property("volume"), 1.0)
        self.assertTrue(self.engine.manual_mode)

    def test_recovery_advances_once_rejects_queued_pre_recovery_on_and_allows_fresh_intent(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        queued_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=1, generation=1)
        self.assertTrue(self.engine._remote_dj_begin_recovery(
            session, reason="transport_disconnected"))
        self.assertEqual(session.gate_generation, 2)
        self.assertFalse(self.engine._remote_dj_begin_recovery(
            session, reason="transport_disconnected"))
        self.assertEqual(session.gate_generation, 2)
        before = self.snapshot()
        self.assert_no_effect(
            self.engine._remote_dj_gate_command(queued_on), before, "stale_generation")
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True, sequence=2)[0],
                         "applied_held_muted")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired),
                         (0.0, True))
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.gate.get_property("volume"), 1.0)

    def test_recovery_mutes_without_any_credential(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True)
        self.engine._remote_dj_begin_recovery(session, reason="media_liveness")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, True))

    def test_recovery_grace_expiry_while_a_command_is_in_flight(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        self.engine._remote_dj_begin_recovery(session, reason="transport_disconnected")
        in_flight = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=2)
        deadline = next(t for t in self.timers if t[0] == 10000)
        deadline[1](*deadline[2])                             # grace expires
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual((self.gate.get_property("volume"), self.engine.manual_mode), (0.0, False))
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(in_flight), before, "no_session")

    def test_an_earlier_sessions_callback_cannot_touch_the_replacement(self):
        a = self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_begin_recovery(a, reason="transport_disconnected")
        stale_deadline = next(t for t in self.timers if t[0] == 10000)
        self.engine._remote_dj_session_stop()
        self.connect(self.dj_b, ATTEMPT_B)
        self.send(self.dj_b, ATTEMPT_B, active=True)
        before = self.snapshot()
        stale_deadline[1](*stale_deadline[2])                # A's late timer
        self.assertEqual(self.snapshot(), before)

    # -- Manual ownership and Studio Mic --------------------------------------------
    def test_mic_owned_manual_is_released_only_by_the_authoritative_lifecycle(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, True))
        for forged in (command(self.dj_b.pk, ATTEMPT_A, active=False, sequence=2),
                       command(self.dj_a.pk, ATTEMPT_A, active=False, sequence=2,
                               key=gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A2)),
                       {"command": "remote_dj_gate", "active": False}):
            before = self.snapshot()
            self.dispatch(forged)
            self.assertEqual(self.snapshot(), before)
        self.engine._remote_dj_session_stop()
        self.assertFalse(self.engine.manual_mode)

    def test_studio_mic_and_operator_manual_are_unaffected(self):
        self.engine._set_manual_mode(True)                    # operator-held Manual
        self.engine._set_mic_ptt(True)                        # Studio Mic live
        self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)
        self.send(self.dj_a, ATTEMPT_A, active=False, sequence=2)
        outcome, before = self.send(self.dj_b, ATTEMPT_A, active=False, sequence=3)
        self.assert_no_effect(outcome, before, "wrong_user")
        self.assertEqual((self.engine.mic_live, self.engine.mic_ptt_volume.get_property("volume")), (True, 1.0))
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, False))
        self.engine._remote_dj_session_stop()
        self.assertEqual((self.engine.mic_live, self.engine.manual_mode), (True, True))

    # -- publication and logs ------------------------------------------------------
    def test_refusals_never_publish_into_another_session_and_never_log_secrets(self):
        b = self.connect(self.dj_b, ATTEMPT_B)
        key_a = gi_mod.control_key_for(self.dj_a.pk, ATTEMPT_A)
        cmd = command(self.dj_a.pk, ATTEMPT_A, active=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.engine._remote_dj_gate_command(cmd)
            self.send(self.dj_b, ATTEMPT_B, active=True)
        self.assertNotIn(key_a, out.getvalue())
        self.assertNotIn(cmd["proof"], out.getvalue())
        owner = self.engine._remote_dj_owner_state()
        self.assertEqual(owner["gate_result"], {"sequence": 1, "active": True, "outcome": "applied"})
        self.assertEqual(b.gate_result, owner["gate_result"])
        self.assertNotIn(gi_mod.control_key_for(self.dj_b.pk, ATTEMPT_B), json.dumps(owner))

    def test_shutdown_mutes_through_the_internal_path(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.send(self.dj_a, ATTEMPT_A, active=True)
        self.engine._remote_dj_session_stop()
        self.assertEqual((self.gate.get_property("volume"), self.engine.manual_mode), (0.0, False))
