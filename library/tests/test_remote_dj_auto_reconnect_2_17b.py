"""Roadmap 2.17 Package B -- automatic fresh-session Remote DJ reconnect.

Server side of the contract (the browser side is exercised in a real Chromium by
library/tests/browser_remote_dj_reconnect_check.py, see docs/REMOTE_DJ_AUTO_RECONNECT.md):

* the engine records WHY each session ended on its connection attempt (published with
  remote_dj_connection) and whether Emergency Mute was latched at that moment;
* the engine publishes the r0132 persistent-slot admission state (available / cleaning /
  quarantined / occupied);
* /api/remote-dj/reconnect-policy/ turns those plus the station switch, the master switch
  and the caller's live permission/schedule into retry / wait / stop -- failing closed;
* RemoteDJConfig.auto_reconnect_enabled defaults Off and lives in the existing fieldset.
"""
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402
from django.test import Client, TestCase  # noqa: E402

import library.services.engine as eng_module  # noqa: E402
import library.views as views  # noqa: E402
from authz.models import Capability, Role, RoleCapability, ScheduleAccessConfig  # noqa: E402
from library.admin import RemoteDJConfigAdmin  # noqa: E402
from library.models import RemoteDJConfig  # noqa: E402
from library.services.remote_dj_connection import (  # noqa: E402
    END_AUTHORIZATION_REVOKED, END_ENGINE_SHUTDOWN, END_FAILED, END_KICKED, END_PEER_CLOSED,
    END_SIGNALING_CLOSED, RemoteDJConnectionAttempt,
)
from library.tests.test_remote_dj_admin_controls_2_17a import admin_command, station_admin  # noqa: E402
from library.tests.test_remote_dj_gate_binding_2_17a import (  # noqa: E402
    ATTEMPT_A, ATTEMPT_B, GateEngineHarness, remote_host_group,
)
from library.tests.test_remote_dj_teardown_r0132b import RemoteDJTeardownTests  # noqa: E402

User = get_user_model()
LOST = "lostAttemptAAAAAAAAAAAAAA"
REPLACEMENT = "replacementBBBBBBBBBBBBBB"
OTHER = "otherBrowserCCCCCCCCCCCCC"


# ---------------------------------------------------------------------------
# Engine: end reasons and slot state
# ---------------------------------------------------------------------------
class _R0132Harness(TestCase):
    """The r0132 Package B real-GStreamer teardown harness, without its tests."""
    setUp = RemoteDJTeardownTests.setUp
    _tmpdir = RemoteDJTeardownTests._tmpdir
    session = RemoteDJTeardownTests.session
    notify = RemoteDJTeardownTests.notify
    run_idle = RemoteDJTeardownTests.run_idle
    finish_cleanup = RemoteDJTeardownTests.finish_cleanup


class EngineEndReasonAndSlotStateTests(_R0132Harness):
    def last(self):
        return self.engine._remote_dj_last_attempt.snapshot()

    def test_terminal_failed_records_failed_and_the_slot_walks_cleaning_then_available(self):
        self.assertEqual(self.engine._remote_dj_slot_state(), "available")
        session = self.session(ATTEMPT_A)
        self.assertEqual(self.engine._remote_dj_slot_state(), "occupied")
        self.notify(session, ATTEMPT_A, eng_module.GstWebRTC.WebRTCPeerConnectionState.FAILED)
        self.run_idle()
        snapshot = self.last()
        self.assertEqual((snapshot["status"], snapshot["end_reason"]), ("failed", END_FAILED))
        self.assertFalse(snapshot["emergency_mute_at_end"])
        self.assertEqual(self.engine._remote_dj_slot_state(), "cleaning")     # native cleanup still running
        self.finish_cleanup()
        self.assertEqual(self.engine._remote_dj_slot_state(), "available")

    def test_peer_closed_and_a_late_duplicate_stop_keep_the_first_reason(self):
        session = self.session(ATTEMPT_A)
        self.notify(session, ATTEMPT_A, eng_module.GstWebRTC.WebRTCPeerConnectionState.CLOSED)
        self.run_idle()
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_SIGNALING_CLOSED)   # stale duplicate: inert
        self.assertEqual(self.last()["end_reason"], END_PEER_CLOSED)
        self.finish_cleanup()

    def test_signaling_close_records_signaling_closed(self):
        self.session(ATTEMPT_A)
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_SIGNALING_CLOSED)
        self.assertEqual(self.last()["end_reason"], END_SIGNALING_CLOSED)
        self.finish_cleanup()

    def test_blocked_cleanup_publishes_quarantined(self):
        session = self.session(ATTEMPT_A, stuck=True)
        self.notify(session, ATTEMPT_A, eng_module.GstWebRTC.WebRTCPeerConnectionState.FAILED)
        self.run_idle()
        self.finish_cleanup()
        self.assertEqual(self.engine._remote_dj_slot_state(), "quarantined")

    def test_engine_shutdown_reason(self):
        self.session(ATTEMPT_A)
        self.engine._remote_dj_session_stop(None, END_ENGINE_SHUTDOWN)
        self.assertEqual(self.last()["end_reason"], END_ENGINE_SHUTDOWN)
        self.finish_cleanup()

    def test_no_slot_state_when_remote_dj_is_not_built(self):
        self.engine.remote_dj_tee = None
        self.assertIsNone(self.engine._remote_dj_slot_state())

    def test_published_state_carries_slot_state_and_end_reason_without_secrets(self):
        self.session(ATTEMPT_A)
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_SIGNALING_CLOSED)
        snapshot = self.engine._remote_dj_connection_state()
        self.assertEqual(snapshot["end_reason"], END_SIGNALING_CLOSED)
        text = json.dumps(snapshot)
        for secret in ("control_key", "token", "sdp", "candidate"):
            self.assertNotIn(f'"{secret}"', text)
        self.finish_cleanup()


class EngineAdministrativeEndTests(GateEngineHarness, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = station_admin("ar217b_admin")

    def last(self):
        return self.engine._remote_dj_last_attempt.snapshot()

    def test_kick_is_recorded_as_kicked(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.assertEqual(self.engine._remote_dj_admin_command(
            admin_command(self.admin, "kick", ATTEMPT_A, self.dj_a)), "executed")
        self.assertEqual(self.last()["end_reason"], END_KICKED)

    def test_emergency_mute_latched_at_the_end_is_recorded(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_admin_command(admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a))
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_PEER_CLOSED)       # then the link dies
        snapshot = self.last()
        self.assertEqual((snapshot["end_reason"], snapshot["emergency_mute_at_end"]), (END_PEER_CLOSED, True))

    def test_released_emergency_mute_is_not_reported_at_the_end(self):
        self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_admin_command(admin_command(self.admin, "emergency_mute", ATTEMPT_A, self.dj_a))
        self.engine._remote_dj_admin_command(admin_command(self.admin, "release_mute", ATTEMPT_A, self.dj_a,
                                                           client_id="admin-press-0002"))
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_PEER_CLOSED)
        self.assertFalse(self.last()["emergency_mute_at_end"])

    def test_connect_revocation_is_recorded_as_authorization_revoked(self):
        self.connect(self.dj_a, ATTEMPT_A)
        RoleCapability.objects.filter(role__name="Remote Host", capability__slug="remote_dj.connect").delete()
        self.assertTrue(self.engine._remote_dj_authorization_tick())
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual(self.last()["end_reason"], END_AUTHORIZATION_REVOKED)

    def test_a_new_session_starts_with_the_remote_mic_physically_off(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True, sequence=1)[0], "applied")
        self.assertEqual(self.gate.get_property("volume"), 1.0)
        self.engine._remote_dj_session_stop(ATTEMPT_A, END_PEER_CLOSED)
        self.assertEqual(self.gate.get_property("volume"), 0.0)
        self.assertIsNone(self.slot.session)                 # no native elements: released at once
        self.engine._remote_dj_build_session = lambda s: None
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(eng_module.RemoteDJAudioInput, "load", return_value=mock.MagicMock(gain_db=0.0)), \
                mock.patch.object(eng_module, "DJ_DIAG_LOG", Path(tmp.name) / "diag.log"), \
                mock.patch.object(eng_module, "DJ_DUMP_PCM", Path(tmp.name) / "dump.pcm"):
            self.engine._remote_dj_session_start(ATTEMPT_B, int(time.time() * 1000), self.dj_a.pk)
        replacement = self.engine.remote_dj_session
        self.assertIsNot(replacement, session)
        self.assertEqual((self.gate.get_property("volume"), replacement.gate_desired), (0.0, False))
        # The lost session's gate command cannot act on the replacement.
        outcome, _before = self.send(self.dj_a, ATTEMPT_A, active=True, sequence=2,
                                     generation=session.gate_generation)
        self.assertTrue(outcome.startswith("refused"), outcome)
        self.assertEqual(self.gate.get_property("volume"), 0.0)


# ---------------------------------------------------------------------------
# Policy endpoint
# ---------------------------------------------------------------------------
def engine_state(*, connected=False, attempt=LOST, status="ended", end_reason=END_SIGNALING_CLOSED,
                 slot="available", configured=True, failure=None, emergency=False, **extra):
    connection = None if attempt is None else {
        "attempt_id": attempt, "status": status, "end_reason": end_reason,
        "emergency_mute_at_end": emergency, "failure": failure,
    }
    state = {"remote_dj_configured": configured, "remote_dj_connected": connected,
             "remote_dj_connection": connection, "remote_dj_slot": slot, **extra}
    return state


class ReconnectPolicyEndpointTests(TestCase):
    URL = "/api/remote-dj/reconnect-policy/"

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.dj = User.objects.create_user("ar217b_dj", password="x")
        self.dj.groups.add(remote_host_group())
        self.config = RemoteDJConfig.load()
        self.config.enabled = True
        self.config.auto_reconnect_enabled = True
        self.config.save()
        self.client = Client()
        self.client.force_login(self.dj)
        self.state = engine_state()
        patcher = mock.patch.object(views, "_read_fresh_engine_state", side_effect=lambda age: self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, attempts=(LOST,), client=None):
        response = (client or self.client).get(self.URL, {"attempts": ",".join(attempts)}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.json()
        self.assertEqual(set(body), {"decision", "reason"})
        return body["decision"], body["reason"]

    def test_involuntary_finalized_loss_with_a_released_slot_is_retry(self):
        for end_reason, status in ((END_SIGNALING_CLOSED, "ended"), (END_PEER_CLOSED, "ended"),
                                   (END_FAILED, "failed")):
            with self.subTest(end_reason=end_reason):
                self.state = engine_state(end_reason=end_reason, status=status)
                self.assertEqual(self.ask(), ("retry", "ok"))

    def test_default_off_and_live_switch_off_stop(self):
        self.config.auto_reconnect_enabled = False
        self.config.save()
        self.assertEqual(self.ask(), ("stop", "auto_reconnect_disabled"))
        self.config.auto_reconnect_enabled = True
        self.config.save()
        self.assertEqual(self.ask(), ("retry", "ok"))                    # read fresh, every request
        RemoteDJConfig.objects.filter(pk=1).update(auto_reconnect_enabled=False)
        self.assertEqual(self.ask(), ("stop", "auto_reconnect_disabled"))

    def test_a_fresh_install_defaults_off(self):
        RemoteDJConfig.objects.all().delete()
        self.assertFalse(RemoteDJConfig.load().auto_reconnect_enabled)

    def test_master_switch_off_stops(self):
        self.config.enabled = False
        self.config.save()
        self.assertEqual(self.ask(), ("stop", "remote_dj_disabled"))

    def test_lost_permission_or_schedule_stops(self):
        RoleCapability.objects.filter(role__name="Remote Host", capability__slug="remote_dj.connect").delete()
        self.assertEqual(self.ask(), ("stop", "not_authorized"))
        RoleCapability.objects.create(role=Role.objects.get(name="Remote Host"),
                                      capability=Capability.objects.get(slug="remote_dj.connect"))
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True        # no TalentAssignment in its window
        cfg.save()
        self.assertEqual(self.ask(), ("stop", "not_authorized"))

    def test_unavailable_engine_status_waits_never_retries(self):
        self.state = None
        self.assertEqual(self.ask(), ("wait", "engine_status_unavailable"))

    def test_quarantine_and_unbuilt_remote_dj_stop(self):
        self.state = engine_state(slot="quarantined")
        self.assertEqual(self.ask(), ("stop", "remote_dj_unavailable"))
        self.state = engine_state(configured=False, slot=None)
        self.assertEqual(self.ask(), ("stop", "remote_dj_not_running"))

    def test_an_engine_that_cannot_report_slot_release_stops(self):
        self.state = engine_state()
        del self.state["remote_dj_slot"]
        self.assertEqual(self.ask(), ("stop", "engine_unsupported"))

    def test_cleanup_in_progress_waits(self):
        self.state = engine_state(slot="cleaning")
        self.assertEqual(self.ask(), ("wait", "previous_session_cleanup"))

    def test_the_lost_session_still_finalizing_waits(self):
        self.state = engine_state(connected=True, status="reconnecting", end_reason=None, slot="occupied")
        self.assertEqual(self.ask(), ("wait", "previous_session_finalizing"))
        self.state = engine_state(connected=False, status="connected", end_reason=None, slot="occupied")
        self.assertEqual(self.ask(), ("wait", "previous_session_finalizing"))

    def test_another_owner_or_an_unrecognized_session_stops(self):
        self.state = engine_state(connected=True, attempt=OTHER, status="connected", end_reason=None,
                                  slot="occupied")
        self.assertEqual(self.ask(), ("stop", "remote_dj_in_use"))
        self.state = engine_state(attempt=OTHER)                          # someone else's attempt since
        self.assertEqual(self.ask(), ("stop", "session_not_recognized"))
        self.state = engine_state(attempt=None)                           # engine restarted: no record
        self.assertEqual(self.ask(), ("stop", "session_not_recognized"))

    def test_deliberate_ends_never_retry(self):
        for end_reason in (END_KICKED, END_AUTHORIZATION_REVOKED, END_ENGINE_SHUTDOWN):
            with self.subTest(end_reason=end_reason):
                self.state = engine_state(end_reason=end_reason)
                self.assertEqual(self.ask(), ("stop", end_reason))
        self.state = engine_state(end_reason="session_stopped")
        self.assertEqual(self.ask(), ("stop", "session_end_unclassified"))

    def test_emergency_mute_at_the_end_stops(self):
        self.state = engine_state(end_reason=END_PEER_CLOSED, emergency=True)
        self.assertEqual(self.ask(), ("stop", "emergency_mute"))

    def test_a_refused_token_failure_stops(self):
        self.state = engine_state(status="failed", end_reason=None,
                                  failure={"class": "authorization_token", "reason": "x"})
        self.assertEqual(self.ask((LOST, REPLACEMENT)), ("stop", "not_authorized"))

    def test_a_replacement_refused_at_admission_while_cleaning_may_retry_once_released(self):
        self.state = engine_state(attempt=REPLACEMENT, status="failed", end_reason=None,
                                  failure={"class": "signaling_session_busy", "reason": "cleanup in progress"},
                                  slot="cleaning")
        self.assertEqual(self.ask((LOST, REPLACEMENT)), ("wait", "previous_session_cleanup"))
        self.state["remote_dj_slot"] = "available"
        self.assertEqual(self.ask((LOST, REPLACEMENT)), ("retry", "ok"))

    def test_malformed_or_oversized_requests_stop(self):
        for attempts in ((), ("short",), ("bad attempt id with spaces",),
                         (LOST, REPLACEMENT, OTHER, ATTEMPT_A, ATTEMPT_B)):
            with self.subTest(attempts=attempts):
                self.assertEqual(self.ask(attempts), ("stop", "invalid_request"))

    def test_anonymous_is_refused(self):
        response = Client().get(self.URL, {"attempts": LOST}, secure=True)
        self.assertIn(response.status_code, (302, 401, 403))


class ReconnectPolicyRealStateFileTests(TestCase):
    """The decision reads the real engine_state.json (no helper patched)."""

    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.dj = User.objects.create_user("ar217b_dj_file", password="x")
        self.dj.groups.add(remote_host_group())
        RemoteDJConfig.objects.filter(pk=RemoteDJConfig.load().pk).update(enabled=True, auto_reconnect_enabled=True)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "engine_state.json"
        patcher = mock.patch.object(views, "ENGINE_STATE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = Client()
        self.client.force_login(self.dj)

    def decide(self, age):
        self.path.write_text(json.dumps({**engine_state(), "timestamp": time.time() - age}))
        return self.client.get("/api/remote-dj/reconnect-policy/", {"attempts": LOST}, secure=True).json()["decision"]

    def test_fresh_retries_and_stale_waits(self):
        self.assertEqual(self.decide(1), "retry")
        self.assertEqual(self.decide(60), "wait")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
class AutoReconnectAdminTests(TestCase):
    def test_field_follows_reconnect_grace_in_the_existing_fieldset_with_its_label(self):
        fieldsets = dict(RemoteDJConfigAdmin.fieldsets)
        self.assertEqual(fieldsets["Reconnect / recovery"]["fields"],
                         ["reconnect_grace_seconds", "auto_reconnect_enabled"])
        field = RemoteDJConfig._meta.get_field("auto_reconnect_enabled")
        self.assertEqual(field.verbose_name, "Enable automatic Remote DJ reconnect")
        self.assertFalse(field.default)
        self.assertIn("same-session recovery", field.help_text)

    def test_help_text_states_the_switch_semantics_truthfully_in_model_and_migration(self):
        # Codex review F4: switching it off prevents SUBSEQUENT attempts at their next policy decision; it
        # does not necessarily cancel an attempt already negotiating. The migration states the same text.
        import importlib
        field = RemoteDJConfig._meta.get_field("auto_reconnect_enabled")
        self.assertIn("When disabled, prevents subsequent automatic reconnect attempts at their next policy "
                      "decision. It does not necessarily cancel a connection attempt already negotiating.",
                      field.help_text)
        self.assertNotIn("stops a reconnect already in progress", field.help_text)
        migration = importlib.import_module("library.migrations.0103_remotedjconfig_auto_reconnect_enabled")
        (operation,) = migration.Migration.operations
        self.assertEqual(operation.name, "auto_reconnect_enabled")
        self.assertEqual(operation.field.help_text, field.help_text)
        self.assertIs(operation.field.default, False)
        self.assertEqual(operation.field.verbose_name, field.verbose_name)
        self.assertEqual(migration.Migration.dependencies, [("library", "0102_uitheme_log_display_appearance")])

    def test_admin_change_page_toggles_and_audits(self):
        admin_user = User.objects.create_superuser("ar217b_super", password="x")
        client = Client()
        client.force_login(admin_user)
        config = RemoteDJConfig.load()
        page = client.get(f"/admin/library/remotedjconfig/{config.pk}/change/", secure=True)
        self.assertContains(page, "Enable automatic Remote DJ reconnect")
        data = {"enabled": "", "stun_server": config.stun_server, "ice_udp_min_port": config.ice_udp_min_port,
                "ice_udp_max_port": config.ice_udp_max_port,
                "reconnect_grace_seconds": config.reconnect_grace_seconds, "auto_reconnect_enabled": "on"}
        with self.captureOnCommitCallbacks(execute=True):        # the audit is emitted on commit
            response = client.post(f"/admin/library/remotedjconfig/{config.pk}/change/", data, secure=True)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None))
        self.assertTrue(RemoteDJConfig.load().auto_reconnect_enabled)
        data.pop("auto_reconnect_enabled")
        with self.captureOnCommitCallbacks(execute=True):
            client.post(f"/admin/library/remotedjconfig/{config.pk}/change/", data, secure=True)
        self.assertFalse(RemoteDJConfig.load().auto_reconnect_enabled)
        from monitoring.models import SystemEvent
        events = SystemEvent.objects.filter(title="Remote DJ configuration updated").order_by("id")
        self.assertEqual([e.detail["changed_fields"] for e in events],
                         [["auto_reconnect_enabled"], ["auto_reconnect_enabled"]])
