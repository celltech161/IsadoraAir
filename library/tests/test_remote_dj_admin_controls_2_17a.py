"""Roadmap 2.17A -- Station Administrator emergency controls for a connected
Remote DJ: Emergency Mute (session latch), Release Emergency Mute, and Kick.

Contract: library.services.remote_dj_admin_identity. Capabilities
remote_dj.emergency_mute / remote_dj.force_disconnect come from the real
authz.0010 seed (Station Administrator only). Engine tests use the shared
GateEngineHarness (real gate/ducking/Manual/recovery/teardown code, synthetic
GStreamer gate).
"""
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, SimpleTestCase, TestCase, override_settings

import library.views as views
from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig
from library.services import remote_dj_admin_identity as admin_mod
from library.services import remote_dj_gate_identity as gate_mod
from library.tests.test_remote_dj_gate_binding_2_17a import (
    ATTEMPT_A,
    ATTEMPT_A2,
    ATTEMPT_B,
    INSTANCE,
    GateEngineHarness,
    command,
    remote_host_group,
)

User = get_user_model()
URL = "/api/engine/remote-dj-admin/"


def station_admin(username):
    group, _ = Group.objects.get_or_create(name="Station Admins (2.17A test)")
    GroupRole.objects.update_or_create(group=group, defaults={"role": Role.objects.get(name="Station Administrator")})
    # Station Administrator is not auto-bound to a group; an operator who binds
    # it to a non-staff group also gives that group Console access.
    from library.models import GroupAccess
    GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/\n", "landing_url": "/"})
    user = User.objects.create_user(username, password="x")     # not staff: the grant is the authority
    user.groups.add(group)
    return user


def admin_request(action, attempt_id=ATTEMPT_A, *, instance=INSTANCE, client_id="admin-press-0001", **extra):
    return {"action": action, "target_session": admin_mod.session_ref(attempt_id),
            "engine_instance": instance, "client_request_id": client_id, **extra}


def admin_command(admin, action, attempt_id, target_user, *, issued_at_ms=None, client_id="admin-press-0001",
                  instance=INSTANCE, reason=""):
    request = admin_mod.parse_request(admin_request(action, attempt_id, instance=instance,
                                                    client_id=client_id, reason=reason))
    return admin_mod.build_command(
        admin_user_id=admin.pk, request=request, attempt_id=attempt_id, target_user_id=target_user.pk,
        issued_at_ms=issued_at_ms if issued_at_ms is not None else time.time() * 1000)


# ---------------------------------------------------------------------------
class AdminIdentityTests(SimpleTestCase):
    def _cmd(self, **over):
        request = admin_mod.parse_request(admin_request("emergency_mute"))
        cmd = admin_mod.build_command(admin_user_id=5, request=request, attempt_id=ATTEMPT_A,
                                      target_user_id=7, issued_at_ms=1_000_000)
        return {**cmd, **over}

    def test_signature_covers_the_literal_command_type_action_and_every_field(self):
        cmd = self._cmd()
        self.assertTrue(admin_mod.verify_signature(cmd))
        for field, value in (("command", "remote_dj_gate"), ("action", "kick"), ("action", "release_mute"),
                             ("admin_user_id", 6), ("attempt_id", ATTEMPT_B), ("target_user_id", 8),
                             ("engine_instance", "engine-run-2"), ("issued_at_ms", 1_000_001),
                             ("reason", "x"), ("request_id", "0" * 32)):
            with self.subTest(field=field, value=value):
                self.assertFalse(admin_mod.verify_signature({**cmd, field: value}))

    def test_request_schema_is_strict(self):
        good = admin_request("kick")
        self.assertEqual(admin_mod.parse_request(good)["reason"], "")
        for bad in ({**good, "action": "unmute"}, {**good, "action": "open"}, {**good, "extra": 1},
                    {**good, "control_key": "a" * 64}, {**good, "attempt_id": ATTEMPT_A},
                    {**good, "target_session": ATTEMPT_A}, {**good, "target_session": "A" * 32},
                    {**good, "engine_instance": ""}, {**good, "client_request_id": "x"},
                    {**good, "reason": 5}, {**good, "reason": "r" * 201},
                    {k: v for k, v in good.items() if k != "target_session"}, [good]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    admin_mod.parse_request(bad)

    def test_a_gate_command_never_parses_as_an_admin_command(self):
        gate = command(7, ATTEMPT_A)
        for relabelled in (gate, {**gate, "command": "remote_dj_admin"}):
            with self.assertRaises(ValueError):
                admin_mod.parse_command(relabelled)

    def test_gate_proof_now_covers_the_literal_command_type(self):
        gate = command(7, ATTEMPT_A)
        self.assertTrue(gate_mod.verify_command_proof(gate))
        self.assertFalse(gate_mod.verify_command_proof({**gate, "command": "remote_dj_disconnect"}))

    def test_session_reference_is_the_status_browser_key(self):
        import hashlib
        self.assertEqual(admin_mod.session_ref(ATTEMPT_A), hashlib.sha256(ATTEMPT_A.encode()).hexdigest()[:32])
        self.assertNotEqual(admin_mod.session_ref(ATTEMPT_A), admin_mod.session_ref(ATTEMPT_A2))


# ---------------------------------------------------------------------------
@override_settings(SECURE_SSL_REDIRECT=False)
class AdminEndpointTests(TestCase):
    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.admin = station_admin("adm217a_admin")
        self.dj = User.objects.create_user("adm217a_dj", password="x")
        self.dj.groups.add(remote_host_group())
        # Holds mic_gate, playout.control, start_next... but not the new grants.
        operator_role = Role.objects.create(name="Operator (2.17A test)")
        for slug in ("remote_dj.mic_gate", "remote_dj.connect", "playout.control", "playout.start_next",
                     "playout.manual_mode", "studio.mic_ptt"):
            RoleCapability.objects.create(role=operator_role, capability=Capability.objects.get(slug=slug))
        group = Group.objects.create(name="Operators (2.17A test)")
        GroupRole.objects.create(group=group, role=operator_role)
        from library.models import GroupAccess
        GroupAccess.objects.create(group=group, allowed_prefixes="/\n", landing_url="/")
        self.operator = User.objects.create_user("adm217a_operator", password="x")
        self.operator.groups.add(group)
        self.su = User.objects.create_superuser("adm217a_su", "su@example.invalid", "x")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_path = Path(tmp.name) / "engine_state.json"
        self.set_live(self.dj, ATTEMPT_A)
        for patcher in (mock.patch.object(views, "ENGINE_STATE_PATH", self.state_path),):
            patcher.start()
            self.addCleanup(patcher.stop)
        enqueue = mock.patch("library.views.enqueue_engine_command")
        self.enqueue = enqueue.start()
        self.addCleanup(enqueue.stop)
        self.events = []
        emit = mock.patch("monitoring.models.emit_event", side_effect=lambda **k: self.events.append(k))
        emit.start()
        self.addCleanup(emit.stop)

    def set_live(self, user, attempt, *, connected=True, timestamp=None, instance=INSTANCE, extra=None):
        self.state_path.write_text(json.dumps({
            "timestamp": time.time() if timestamp is None else timestamp,
            "remote_dj_connected": connected,
            "remote_dj_owner": {"user_id": user.pk, "attempt_id": attempt, **(extra or {})} if user else None,
            "state_revision": {"instance": instance, "seq": 4},
            "remote_dj_admin_result": {"request_id": "r" * 32, "action": "kick", "outcome": "executed",
                                       "at": 1.0},
        }))

    def post(self, user, body, *, csrf=False):
        client = Client(enforce_csrf_checks=csrf)
        if user is not None:
            client.force_login(user)
        return client.post(URL, data=json.dumps(body), content_type="application/json")

    def refused(self, resp, status, reason=None):
        self.assertEqual(resp.status_code, status, resp.content)
        if reason:
            self.assertEqual(resp.json()["reason"], reason)
        self.enqueue.assert_not_called()

    def test_station_admin_actions_queue_exact_signed_commands(self):
        for action in ("emergency_mute", "release_mute", "kick"):
            with self.subTest(action=action):
                self.enqueue.reset_mock()
                resp = self.post(self.admin, admin_request(action, client_id=f"press-{action.replace('_', '-')}",
                                                            reason=" too  hot "))
                self.assertEqual(resp.status_code, 202, resp.content)
                body = resp.json()
                self.assertEqual((body["accepted"], body["executed"], body["action"]), (True, False, action))
                queued = self.enqueue.call_args.args[0]
                self.assertEqual(set(queued), admin_mod.COMMAND_FIELDS)
                self.assertEqual((queued["command"], queued["action"], queued["admin_user_id"],
                                  queued["attempt_id"], queued["target_user_id"], queued["reason"]),
                                 ("remote_dj_admin", action, self.admin.pk, ATTEMPT_A, self.dj.pk, "too hot"))
                self.assertTrue(admin_mod.verify_signature(queued))
                self.assertEqual(body["request_id"], queued["request_id"])
        accepted = [e for e in self.events if e["detail"]["outcome"] == "accepted"]
        self.assertEqual([e["detail"]["action"] for e in accepted], ["emergency_mute", "release_mute", "kick"])

    def test_retry_of_one_press_has_one_request_id(self):
        body = admin_request("kick", client_id="same-press-01")
        self.assertEqual(self.post(self.admin, body).json()["request_id"],
                         self.post(self.admin, body).json()["request_id"])

    def test_remote_host_and_capability_bearing_operator_are_refused(self):
        for user in (self.dj, self.operator):
            for action in ("emergency_mute", "release_mute", "kick"):
                with self.subTest(user=user.username, action=action):
                    self.refused(self.post(user, admin_request(action)), 403)

    def test_remote_host_is_refused_by_capability_even_if_its_paths_were_widened(self):
        from library.models import GroupAccess
        access = GroupAccess.objects.get(group__name="remote_dj")
        access.allowed_prefixes += "\n/api/engine/remote-dj-admin/"
        access.save()
        for action in ("emergency_mute", "release_mute", "kick"):
            with self.subTest(action=action):
                resp = self.post(self.dj, admin_request(action))
                self.refused(resp, 403)
                self.assertNotIn("assigned privileges", resp.content.decode())   # the view refused, not middleware

    def test_each_action_needs_its_own_capability(self):
        RoleCapability.objects.filter(role__name="Station Administrator",
                                      capability__slug="remote_dj.force_disconnect").delete()
        self.refused(self.post(self.admin, admin_request("kick")), 403)
        self.assertEqual(self.post(self.admin, admin_request("emergency_mute")).status_code, 202)

    def test_copied_microphone_owner_credential_grants_nothing(self):
        key = gate_mod.control_key_for(self.dj.pk, ATTEMPT_A)
        self.refused(self.post(self.dj, {**admin_request("emergency_mute"), "control_key": key}), 403)
        self.refused(self.post(self.admin, {**admin_request("emergency_mute"), "control_key": key}), 400)

    def test_csrf_malformed_and_unauthenticated_are_refused(self):
        self.refused(self.post(self.admin, admin_request("kick"), csrf=True), 403)
        for body in ({"action": "open_mic"}, {**admin_request("kick"), "extra": 1}, [1],
                     {**admin_request("kick"), "target_session": ATTEMPT_A}):
            with self.subTest(body=body):
                self.refused(self.post(self.admin, body), 400)
        resp = self.post(None, admin_request("kick"))
        self.assertNotIn(resp.status_code, (200, 202))
        self.enqueue.assert_not_called()

    def test_stale_target_engine_and_state_are_refused_and_audited(self):
        self.set_live(self.dj, ATTEMPT_A2)                                   # same user, new attempt
        self.refused(self.post(self.admin, admin_request("kick")), 409, "stale_target")
        self.set_live(None, None, connected=False)
        self.refused(self.post(self.admin, admin_request("emergency_mute")), 409, "stale_target")
        self.set_live(self.dj, ATTEMPT_A, instance="engine-run-2")
        self.refused(self.post(self.admin, admin_request("kick")), 409, "engine_restarted")
        self.set_live(self.dj, ATTEMPT_A, timestamp=time.time() - 30)
        self.refused(self.post(self.admin, admin_request("kick")), 409, "engine_unavailable")
        outcomes = [e["detail"]["outcome"] for e in self.events]
        self.assertEqual(outcomes, ["refused:stale_target", "refused:stale_target",
                                    "refused:engine_restarted", "refused:engine_unavailable"])

    def test_superuser_follows_the_staff_compatibility_rule(self):
        self.assertEqual(self.post(self.su, admin_request("emergency_mute")).status_code, 202)

    def test_status_shows_admin_results_and_latch_only_appropriately(self):
        self.set_live(self.dj, ATTEMPT_A, extra={"emergency_mute": True, "gate_forced": {"reason": "emergency_mute"}})
        for user, sees_result in ((self.admin, True), (self.su, True), (self.dj, False), (self.operator, False)):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                data = client.get("/api/engine/status/").json()
                self.assertEqual("remote_dj_admin_result" in data, sees_result)
                if sees_result:
                    self.assertEqual(data["remote_dj_admin_result"],
                                     {"request_id": "r" * 32, "action": "kick", "outcome": "executed"})
                owner = data["remote_dj_owner"]
                self.assertTrue(owner["emergency_mute"])
                self.assertEqual(owner["gate_forced"], "emergency_mute")
                self.assertNotIn(ATTEMPT_A, json.dumps(data))

    def test_console_renders_admin_controls_only_for_their_holders(self):
        for user, mute, kick in ((self.admin, True, True), (self.su, True, True),
                                 (self.operator, False, False)):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                html = client.get("/").content.decode()
                self.assertEqual('id="rdjEmergencyMuteBtn"' in html, mute)
                self.assertEqual('id="rdjKickBtn"' in html, kick)
        client = Client()
        client.force_login(self.su)
        remote_page = client.get("/remote-dj/").content.decode()     # talent surface: never
        self.assertNotIn('id="rdjAdminBar"', remote_page)


# ---------------------------------------------------------------------------
class AdminEngineTests(GateEngineHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = station_admin("adm217a_eng_admin")
        self.sent = []
        self.retired = []
        self.engine._remote_dj_server.send_json_threadsafe.side_effect = \
            lambda attempt, obj: self.sent.append((attempt, obj, self.gate.get_property("volume")))
        self.engine._remote_dj_server.retire_attempt_threadsafe.side_effect = \
            lambda attempt: self.retired.append((attempt, self.gate.get_property("volume")))

    def run_admin(self, action, attempt_id=ATTEMPT_A, target=None, **kw):
        cmd = admin_command(self.admin, action, attempt_id, target or self.dj_a, **kw)
        return self.engine._remote_dj_admin_command(cmd)

    def admin_events(self, outcome=None):
        events = [e for e in self.events if e.get("title", "").startswith("Remote DJ administrator action")]
        return [e for e in events if outcome is None or e["detail"]["outcome"] == outcome]

    def live(self, user=None, attempt=ATTEMPT_A):
        session = self.connect(user or self.dj_a, attempt)
        self.assertEqual(self.send(user or self.dj_a, attempt, active=True, sequence=1)[0], "applied")
        return session

    # -- Emergency Mute -----------------------------------------------------------
    def test_emergency_mute_silences_and_latches_the_targeted_session(self):
        session = self.live()
        self.assertEqual(self.run_admin("emergency_mute", reason="feedback"), "executed")
        self.assertIs(self.engine.remote_dj_session, session)
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertFalse(self.engine.manual_mode)
        self.assertEqual(self.ramps, [self.DUCKED, 1.0])
        self.assertIsNotNone(session.emergency_latch)
        self.assertEqual(session.emergency_latch["by_user_id"], self.admin.pk)
        owner = self.engine._remote_dj_owner_state()
        self.assertTrue(owner["emergency_mute"])
        self.assertEqual(owner["gate_forced"], {"reason": "emergency_mute"})
        executed = self.admin_events("executed")[0]["detail"]
        self.assertEqual((executed["action"], executed["admin_user_id"], executed["attempt_id"],
                          executed["target_user_id"], executed["reason"]),
                         ("emergency_mute", self.admin.pk, ATTEMPT_A, self.dj_a.pk, "feedback"))
        self.assertEqual(self.engine._remote_dj_admin_result["outcome"], "executed")

    def test_dj_cannot_reopen_while_latched(self):
        session = self.live()
        self.run_admin("emergency_mute")
        time.sleep(0.002)
        outcome, before = self.send(self.dj_a, ATTEMPT_A, active=True, sequence=2)
        self.assert_no_effect(outcome, before, "emergency_muted")
        self.assertEqual(session.gate_result["outcome"], "refused:emergency_muted")
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=False, sequence=3)[0], "applied")   # OFF is fine

    def test_recovery_cannot_undo_the_latch(self):
        session = self.live()
        self.run_admin("emergency_mute")
        self.engine._remote_dj_begin_recovery(session, reason="transport_disconnected")
        session.gate_desired = True          # even a (hypothetically) re-asserted intent
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.gate.get_property("volume"), 0.0)
        self.assertIsNotNone(session.emergency_latch)

    def test_release_lifts_the_latch_but_never_opens_the_mic(self):
        session = self.live()
        self.run_admin("emergency_mute", client_id="mute-press-01")
        latched_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=2)   # pressed while latched
        time.sleep(0.002)
        self.assertEqual(self.run_admin("release_mute", client_id="release-press1"), "executed")
        self.assertIsNone(session.emergency_latch)
        self.assertNotIn("emergency_mute", self.engine._remote_dj_owner_state())   # normal shape again
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(latched_on), before, "superseded_by_mute")
        time.sleep(0.002)
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True, sequence=3)[0], "applied")   # fresh press
        self.assertEqual(self.gate.get_property("volume"), 1.0)

    def test_release_without_a_latch_is_refused(self):
        self.live()
        before = self.snapshot()
        self.assertEqual(self.run_admin("release_mute"), "refused:not_latched")
        self.assertEqual(self.snapshot(), before)

    def test_latch_never_applies_to_a_replacement_session(self):
        self.live()
        self.run_admin("emergency_mute")
        self.engine._remote_dj_session_stop()
        b = self.connect(self.dj_a, ATTEMPT_A2)                       # same user, new attempt
        self.assertIsNone(b.emergency_latch)
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A2, active=True, sequence=1)[0], "applied")

    # -- Kick ------------------------------------------------------------------------
    def test_kick_silences_first_then_retires_the_exact_session(self):
        session = self.live()
        stop_seen = []
        real_stop = self.engine._remote_dj_session_stop
        self.engine._remote_dj_session_stop = lambda attempt_id=None: (
            stop_seen.append((attempt_id, self.gate.get_property("volume"), session.gate_desired)),
            real_stop(attempt_id))[1]
        self.assertEqual(self.run_admin("kick"), "executed")
        self.assertEqual(stop_seen, [(ATTEMPT_A, 0.0, False)])           # physical gate closed BEFORE teardown
        self.assertEqual(self.sent, [(ATTEMPT_A, {"type": "kicked", "reason": "station_administrator"}, 0.0)])
        self.assertEqual(self.retired, [(ATTEMPT_A, 0.0)])               # signaling ownership retired
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertIsNone(self.slot.session)                             # slot released
        self.assertFalse(self.engine.manual_mode)
        self.assertEqual(self.engine._remote_dj_last_attempt.attempt_id, ATTEMPT_A)

    def test_late_callbacks_cannot_resurrect_a_kicked_session(self):
        session = self.live()
        self.engine._remote_dj_begin_recovery(session, reason="transport_disconnected")
        deadline = next(t for t in self.timers if t[0] == 10000)
        self.run_admin("kick")
        b = self.live(self.dj_b, ATTEMPT_B)
        before = self.snapshot()
        deadline[1](*deadline[2])                                        # A's late recovery deadline
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.snapshot(), before)
        self.assertIs(self.engine.remote_dj_session, b)

    # -- targeting, replay, authority -------------------------------------------------
    def test_delayed_mute_or_kick_never_touches_a_replacement(self):
        self.connect(self.dj_a, ATTEMPT_A)
        late = {action: admin_command(self.admin, action, ATTEMPT_A, self.dj_a, client_id=f"late-{action.replace('_', '-')}-1")
                for action in ("emergency_mute", "kick")}
        self.engine._remote_dj_session_stop()
        for user, attempt in ((self.dj_a, ATTEMPT_A2), (self.dj_b, ATTEMPT_B)):   # same user, then another
            with self.subTest(attempt=attempt):
                replacement = self.live(user, attempt)
                for cmd in late.values():
                    before = self.snapshot()
                    self.assertEqual(self.engine._remote_dj_admin_command(dict(cmd)), "stale_target")
                    self.assertEqual(self.snapshot(), before)
                self.assertIsNone(replacement.emergency_latch)
                self.engine._remote_dj_session_stop()
        self.assertEqual(len(self.admin_events("stale_target")), 4)

    def test_a_refused_copy_is_re_evaluated_but_an_executed_one_is_never_repeated(self):
        self.live()
        stale = admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a, client_id="retry-press-01",
                              issued_at_ms=time.time() * 1000 - 6000)
        self.assertEqual(self.engine._remote_dj_admin_command(stale), "refused:stale_command")
        retry = admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a, client_id="retry-press-01")
        self.assertEqual(retry["request_id"], stale["request_id"])
        self.assertEqual(self.engine._remote_dj_admin_command(retry), "executed")
        self.assertEqual(self.engine._remote_dj_admin_command(dict(retry)), "duplicate")

    def test_a_target_user_mismatch_is_a_stale_target(self):
        self.live()
        before = self.snapshot()
        self.assertEqual(self.run_admin("kick", target=self.dj_b), "stale_target")
        self.assertEqual(self.snapshot(), before)

    def test_replayed_stale_restarted_and_forged_commands_fail(self):
        session = self.live()
        cmd = admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a)
        self.assertEqual(self.engine._remote_dj_admin_command(dict(cmd)), "executed")
        self.assertEqual(self.engine._remote_dj_admin_command(dict(cmd)), "duplicate")      # replay
        self.run_admin("release_mute", client_id="release-0001")
        before = self.snapshot()
        cases = {
            "refused:stale_command": admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a,
                                                   client_id="old-press-01", issued_at_ms=time.time() * 1000 - 6000),
            "refused:engine_restarted": admin_command(self.admin, "kick", ATTEMPT_A, self.dj_a,
                                                      client_id="restart-001", instance="engine-run-0"),
            "refused:bad_signature": {**admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a,
                                                      client_id="forge-00001"), "action": "kick"},
            "refused:malformed": {"command": "remote_dj_admin", "action": "kick"},
        }
        for expected, forged in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(self.engine._remote_dj_admin_command(forged), expected)
                self.assertEqual(self.snapshot(), before)
        self.assertIs(self.engine.remote_dj_session, session)

    def test_administrator_authority_is_rechecked_at_execution(self):
        self.live()
        RoleCapability.objects.filter(role__name="Station Administrator",
                                      capability__slug__startswith="remote_dj.").delete()
        before = self.snapshot()
        for action in ("emergency_mute", "kick"):
            self.assertEqual(self.run_admin(action, client_id=f"noauth-{action.replace('_', '-')}"), "refused:not_authorized")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.admin_events("refused:not_authorized")), 2)

    def test_a_remote_host_cannot_be_the_administrator_even_in_a_signed_command(self):
        self.live()
        cmd = admin_command(self.dj_b, "kick", ATTEMPT_A, self.dj_a)
        before = self.snapshot()
        self.assertEqual(self.engine._remote_dj_admin_command(cmd), "refused:not_authorized")
        self.assertEqual(self.snapshot(), before)

    def test_studio_mic_and_operator_manual_are_unaffected(self):
        self.engine._set_manual_mode(True)
        self.engine._set_mic_ptt(True)
        self.live()
        self.run_admin("emergency_mute", client_id="mute-studio-1")
        self.assertEqual((self.engine.mic_live, self.engine.mic_ptt_volume.get_property("volume")), (True, 1.0))
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, False))
        self.run_admin("kick", client_id="kick-studio-1")
        self.assertEqual((self.engine.mic_live, self.engine.manual_mode), (True, True))

    def test_the_old_unbound_disconnect_command_is_gone(self):
        session = self.live()
        before = self.snapshot()
        self.engine._dispatch_engine_command({"command": "remote_dj_disconnect"})
        self.assertIs(self.engine.remote_dj_session, session)
        self.assertEqual(self.snapshot(), before)

    def test_admin_commands_go_through_the_real_dispatcher(self):
        self.live()
        self.engine._dispatch_engine_command(admin_command(self.admin, "kick", ATTEMPT_A, self.dj_a))
        self.assertIsNone(self.engine.remote_dj_session)
