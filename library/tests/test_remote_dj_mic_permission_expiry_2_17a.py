"""Roadmap 2.17A correction (Codex BLOCK) -- loss of remote_dj.mic_gate while
remote_dj.connect stays authorized must not leave a Remote DJ microphone
live.

The engine's periodic authorization tick evaluates mic_gate independently
and forces the exact current session's microphone off
(PlaybackEngine._remote_dj_force_gate_off): physical gate 0,
gate_desired=False, ducking recomputed, mic-owned Manual released (operator
Manual and a live Studio Mic preserved), an audit event, and a mute floor so
no command issued before the mute can reopen it. The DJ stays connected.

Real seeded Remote Host role, real authz.evaluator.authorize(), real engine
gate/ducking/Manual/recovery/teardown code, synthetic GStreamer volume gate
(GateEngineHarness).
"""
import datetime as dt
from unittest import mock

from django.test import TestCase
from django.utils import timezone

import library.services.engine as eng_module
from authz.models import Capability, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment
from library.tests.test_remote_dj_gate_binding_2_17a import (
    ATTEMPT_A,
    ATTEMPT_B,
    GateEngineHarness,
    command,
)

FORCED_TITLE = "Remote DJ microphone forced off"


class MicPermissionExpiryTests(GateEngineHarness, TestCase):
    # -- helpers ---------------------------------------------------------------
    def revoke_mic_gate(self):
        """Remove ONLY remote_dj.mic_gate from Remote Host; connect stays."""
        deleted, _ = RoleCapability.objects.filter(
            role__name="Remote Host", capability__slug="remote_dj.mic_gate").delete()
        self.assertEqual(deleted, 1)

    def restore_mic_gate(self):
        RoleCapability.objects.create(role=Role.objects.get(name="Remote Host"),
                                      capability=Capability.objects.get(slug="remote_dj.mic_gate"))

    def tick(self):
        self.assertTrue(self.engine._remote_dj_authorization_tick())

    def forced_events(self):
        return [e for e in self.events if e.get("title") == FORCED_TITLE]

    def live_owner(self, user=None, attempt=ATTEMPT_A):
        """Connect and open the mic through the real bound owner command."""
        session = self.connect(user or self.dj_a, attempt)
        self.assertEqual(self.send(user or self.dj_a, attempt, active=True, sequence=1)[0], "applied")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (1.0, True))
        return session

    def assert_forced_off(self, session, *, manual, manual_from_mic=None):
        self.assertIs(self.engine.remote_dj_session, session)              # still connected
        self.assertEqual(session.connection_attempt.status, session.connection_attempt.status)
        self.assertEqual(self.gate.get_property("volume"), 0.0)
        self.assertFalse(session.gate_desired)
        self.assertEqual(self.engine.manual_mode, manual)
        if manual_from_mic is not None:
            self.assertEqual(self.engine._manual_from_mic, manual_from_mic)
        self.assertEqual(session.gate_forced, {"reason": "mic_permission"})
        self.assertEqual(self.engine._remote_dj_owner_state()["gate_forced"], {"reason": "mic_permission"})
        events = self.forced_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual(events[0]["detail"]["attempt_id"], session.connection_attempt.attempt_id)
        self.assertTrue(events[0]["detail"]["reason"].startswith("mic_permission:"))

    # -- revocation while ON / OFF ---------------------------------------------
    def test_revoked_while_on_and_connected_forces_the_mic_off(self):
        session = self.live_owner()
        self.assertEqual(self.ramps, [self.DUCKED])
        self.revoke_mic_gate()
        self.tick()
        self.assert_forced_off(session, manual=False)                       # mic-owned Manual released
        self.assertEqual(self.ramps, [self.DUCKED, 1.0])                     # ducking released
        self.engine._remote_dj_session_stop = mock.MagicMock()
        self.tick()                                                          # repeated ticks: idempotent
        self.engine._remote_dj_session_stop.assert_not_called()
        self.assertEqual(len(self.forced_events()), 1)
        self.assertEqual(self.ramps, [self.DUCKED, 1.0])

    def test_revoked_while_off_changes_nothing_audible(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.revoke_mic_gate()
        before = self.snapshot()
        self.tick()
        after = self.snapshot()
        self.assertEqual(after, before)
        self.assertIs(self.engine.remote_dj_session, session)
        self.assertEqual(self.forced_events(), [])
        self.assertGreater(session.gate_mute_floor_ms, 0)                    # older presses are void

    def test_mic_only_schedule_expiry_forces_the_mic_off(self):
        now = timezone.localtime()
        if now.time() < dt.time(0, 3):
            self.skipTest("the ended-window fixture needs a minute that has passed today")
        session = self.live_owner()
        # connect becomes unscheduled; mic_gate stays schedule-restricted and
        # the DJ's only window ended today at 00:01.
        Capability.objects.filter(slug="remote_dj.connect").update(requires_schedule=False)
        Capability.objects.filter(slug="remote_dj.mic_gate").update(requires_schedule=True)
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.save()
        for dow in range(7):
            TalentAssignment.objects.create(user=self.dj_a, day_of_week=dow,
                                            start_time=dt.time(0, 0), end_time=dt.time(0, 1))
        self.tick()
        self.assert_forced_off(session, manual=False)
        self.assertIn("schedule", self.forced_events()[0]["detail"]["reason"])

    def test_connect_revocation_keeps_the_existing_teardown(self):
        self.live_owner()
        RoleCapability.objects.filter(role__name="Remote Host").delete()
        self.tick()
        self.assertIsNone(self.engine.remote_dj_session)
        self.assertEqual((self.gate.get_property("volume"), self.engine.manual_mode), (0.0, False))
        self.assertTrue(any(e.get("title") == "Remote DJ session terminated: authorization revoked"
                            for e in self.events))

    def test_unverifiable_permission_forces_the_mic_off_without_teardown(self):
        session = self.live_owner()
        with mock.patch("authz.evaluator.authorize", side_effect=RuntimeError("db down")):
            self.tick()
        self.assertIs(self.engine.remote_dj_session, session)
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertEqual(self.forced_events()[0]["detail"]["reason"], "authorization_unverifiable")

    # -- recovery -----------------------------------------------------------------
    def test_recovering_with_desired_on_is_not_restored(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_begin_recovery(session, reason="transport_disconnected")
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True)[0], "applied_held_muted")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, True))
        self.revoke_mic_gate()
        self.tick()
        self.assertFalse(session.gate_desired)
        self.assertEqual(session.connection_attempt.status, "reconnecting")   # no extra recovery state
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.gate.get_property("volume"), 0.0)               # never reopened
        self.assertFalse(self.engine.manual_mode)

    def test_recovering_after_a_live_mic_releases_its_manual_and_stays_off(self):
        session = self.live_owner()
        self.engine._remote_dj_begin_recovery(session, reason="media_liveness")
        # Recovery already forced physical 0 but kept intent and mic-owned Manual.
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, True))
        self.assertTrue(self.engine.manual_mode)
        self.revoke_mic_gate()
        self.tick()
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertFalse(self.engine.manual_mode)
        self.assertEqual(len(self.forced_events()), 1)
        self.engine._remote_dj_complete_recovery(session, trigger="transport_connected")
        self.assertEqual(self.gate.get_property("volume"), 0.0)

    # -- Studio Mic and Manual ownership ----------------------------------------
    def test_live_studio_mic_keeps_its_manual_and_ducking(self):
        self.engine._set_mic_ptt(True)
        session = self.live_owner()
        self.revoke_mic_gate()
        self.tick()
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertEqual((self.engine.mic_live, self.engine.mic_ptt_volume.get_property("volume")), (True, 1.0))
        self.assertTrue(self.engine.manual_mode)                             # Studio Mic still holds it
        self.assertEqual(self.ramps[-1], self.DUCKED)                        # still ducked under the studio mic

    def test_operator_manual_is_preserved(self):
        self.engine._set_manual_mode(True)
        session = self.live_owner()
        self.revoke_mic_gate()
        self.tick()
        self.assert_forced_off(session, manual=True, manual_from_mic=False)

    def test_mic_owned_manual_is_released(self):
        session = self.live_owner()
        self.assertEqual((self.engine.manual_mode, self.engine._manual_from_mic), (True, True))
        self.revoke_mic_gate()
        self.tick()
        self.assert_forced_off(session, manual=False)

    # -- after the mute -------------------------------------------------------------
    def test_restored_permission_does_not_reopen_until_a_fresh_press(self):
        session = self.live_owner()
        self.revoke_mic_gate()
        self.tick()
        self.restore_mic_gate()
        self.tick()
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        # A press made after the mute (explicit time, not a sleep).
        self.assertEqual(self.send(self.dj_a, ATTEMPT_A, active=True, sequence=2,
                                   issued_at_ms=session.gate_mute_floor_ms + 1)[0], "applied")
        self.assertEqual(self.gate.get_property("volume"), 1.0)
        self.assertIsNone(session.gate_forced)

    def test_an_old_pending_on_cannot_reopen_after_the_mute(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        pending_on = command(self.dj_a.pk, ATTEMPT_A, active=True, sequence=1)   # issued before the mute
        self.revoke_mic_gate()
        self.tick()
        self.restore_mic_gate()
        before = self.snapshot()
        self.assert_no_effect(self.engine._remote_dj_gate_command(pending_on), before, "superseded_by_mute")
        self.assertEqual(session.gate_result["outcome"], "refused:superseded_by_mute")

    def test_owner_off_after_revocation_is_refused_but_the_engine_closes_the_mic(self):
        session = self.live_owner()
        self.revoke_mic_gate()
        outcome, _ = self.send(self.dj_a, ATTEMPT_A, active=False, sequence=2)
        self.assertEqual(outcome, "refused:not_authorized")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))
        self.assertFalse(self.engine.manual_mode)
        self.assertEqual(self.forced_events()[0]["detail"]["reason"], "mic_permission:command_check")

    def test_owner_on_after_revocation_is_refused(self):
        session = self.connect(self.dj_a, ATTEMPT_A)
        self.revoke_mic_gate()
        outcome, before = self.send(self.dj_a, ATTEMPT_A, active=True)
        self.assertEqual(outcome, "refused:not_authorized")
        self.assertEqual((self.gate.get_property("volume"), session.gate_desired), (0.0, False))

    # -- replacement ------------------------------------------------------------------
    def test_a_replaced_session_is_never_touched(self):
        old = self.connect(self.dj_a, ATTEMPT_A)
        self.engine._remote_dj_session_stop()
        b = self.live_owner(self.dj_b, ATTEMPT_B)
        before = self.snapshot()
        # A stale forced-off request for A (e.g. a delayed evaluation) is inert toward B.
        self.assertFalse(self.engine._remote_dj_force_gate_off(old, ATTEMPT_A, reason="mic_permission:x"))
        self.assertFalse(self.engine._remote_dj_force_gate_off(b, ATTEMPT_A, reason="mic_permission:x"))
        self.assertEqual(self.snapshot(), before)
        # B's own revocation is enforced on B only.
        self.revoke_mic_gate()
        self.tick()
        self.assert_forced_off(b, manual=False)
        self.assertEqual(self.forced_events()[0]["detail"]["attempt_id"], ATTEMPT_B)
