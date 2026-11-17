"""Roadmap 1.20 (revised): the shared engine-restart assessment + override contract."""
import time
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from library.services import engine_restart_safety as safety
from monitoring.models import SystemEvent

from .engine_restart_state_support import EngineStateFile, engine_state


class AssessmentTests(SimpleTestCase):
    def setUp(self):
        self.state_file = EngineStateFile(self)

    def assess(self, **overrides):
        self.state_file.write(**overrides)
        return safety.assess_engine_restart()

    # ---- safe / warning
    def test_idle_engine_is_safe_and_carries_identity_and_timestamp(self):
        result = self.assess()
        self.assertEqual(result.level, safety.SAFE)
        self.assertEqual(result.reasons, ())
        self.assertFalse(result.requires_override)
        self.assertEqual(result.engine_instance, "0b0f6a2c-5e6a-4f0e-9c58-3f6f2f3a9d11")
        self.assertEqual(result.state_revision["seq"], 41)
        self.assertIsNotNone(result.observed_at)
        self.assertLess(result.age_seconds, 5)

    def test_operator_manual_mode_alone_is_a_warning_not_a_block(self):
        result = self.assess(manual_mode=True)
        self.assertEqual(result.level, safety.WARNING)
        self.assertEqual(result.reasons, (safety.MANUAL_MODE,))
        self.assertFalse(result.requires_override)

    def test_quarantined_slot_is_a_warning_because_restart_is_its_recovery(self):
        result = self.assess(remote_dj_slot="quarantined")
        self.assertEqual(result.level, safety.WARNING)
        self.assertIn(safety.REMOTE_DJ_SLOT_QUARANTINED, result.reasons)
        self.assertFalse(result.requires_override)

    def test_remote_dj_not_built_into_this_engine_run_is_safe(self):
        self.assertEqual(self.assess(remote_dj_slot=None, remote_dj_configured=False).level, safety.SAFE)

    # ---- blocked
    def test_studio_microphone_live_blocks(self):
        result = self.assess(mic_live=True)
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.STUDIO_MIC_LIVE,))
        self.assertTrue(result.requires_override)

    def test_studio_mic_live_and_manual_reports_only_the_block(self):
        result = self.assess(mic_live=True, manual_mode=True)
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertNotIn(safety.MANUAL_MODE, result.reasons)

    def test_remote_dj_connected_with_microphone_off_still_blocks(self):
        # The central rule: a mic that is Off proves nothing about the session.
        result = self.assess(mic_live=False, remote_dj_connected=True, remote_dj_live=False,
                             remote_dj_slot="occupied")
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.REMOTE_DJ_CONNECTED,))

    def test_remote_dj_microphone_live_blocks(self):
        result = self.assess(remote_dj_connected=True, remote_dj_live=True, remote_dj_slot="occupied")
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.REMOTE_DJ_MIC_LIVE, safety.REMOTE_DJ_CONNECTED))

    def test_remote_dj_reconnecting_blocks(self):
        result = self.assess(remote_dj_connected=True, remote_dj_reconnecting=True,
                             remote_dj_slot="occupied",
                             remote_dj_connection={"status": "reconnecting"})
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertIn(safety.REMOTE_DJ_RECONNECTING, result.reasons)

    def test_reconnecting_flag_alone_blocks_even_if_connected_flag_disagrees(self):
        result = self.assess(remote_dj_reconnecting=True)
        self.assertEqual(result.level, safety.BLOCKED)

    def test_establishing_attempt_before_session_is_published_blocks(self):
        result = self.assess(remote_dj_connection={"status": "connecting"})
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.REMOTE_DJ_ESTABLISHING,))

    def test_finished_attempts_do_not_block(self):
        for status in ("ended", "failed"):
            with self.subTest(status=status):
                self.assertEqual(self.assess(remote_dj_connection={"status": status}).level, safety.SAFE)

    def test_native_cleanup_in_progress_blocks(self):
        result = self.assess(remote_dj_slot="cleaning")
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.REMOTE_DJ_CLEANING,))

    def test_occupied_slot_without_published_session_blocks(self):
        result = self.assess(remote_dj_slot="occupied", remote_dj_connected=False)
        self.assertEqual(result.level, safety.BLOCKED)
        self.assertEqual(result.reasons, (safety.REMOTE_DJ_SLOT_OCCUPIED,))

    # ---- unknown (fail closed)
    def test_missing_status_file_is_unknown(self):
        self.state_file.remove()
        result = safety.assess_engine_restart()
        self.assertEqual(result.level, safety.UNKNOWN)
        self.assertEqual(result.reasons, (safety.STATUS_MISSING,))
        self.assertTrue(result.requires_override)

    def test_unreadable_status_is_unknown(self):
        self.state_file.remove()
        self.state_file.path.mkdir()  # reading a directory raises OSError (IsADirectoryError)
        self.addCleanup(self.state_file.path.rmdir)
        result = safety.assess_engine_restart()
        self.assertEqual(result.level, safety.UNKNOWN)
        self.assertEqual(result.reasons, (safety.STATUS_UNREADABLE,))

    def test_invalid_json_and_non_object_json_are_unknown(self):
        for raw in ("{not json", "[]", "null", "42", "", '"str"'):
            with self.subTest(raw=raw):
                self.state_file.write_raw(raw)
                result = safety.assess_engine_restart()
                self.assertEqual(result.level, safety.UNKNOWN)
                self.assertEqual(result.reasons, (safety.STATUS_MALFORMED,))

    def test_stale_status_is_unknown_even_when_it_says_everything_is_quiet(self):
        result = self.assess(timestamp=time.time() - 60)
        self.assertEqual(result.level, safety.UNKNOWN)
        self.assertEqual(result.reasons, (safety.STATUS_STALE,))
        # Identity of the (old) observation is still reported for the audit trail.
        self.assertIsNotNone(result.observed_at)

    def test_stale_status_never_reports_a_block_either(self):
        # A stale "mic live" is not evidence; it is unknown, handled identically.
        result = self.assess(timestamp=time.time() - 60, mic_live=True)
        self.assertEqual(result.level, safety.UNKNOWN)

    def test_freshness_boundary(self):
        now = 1_000_000.0
        self.state_file.write(timestamp=now - safety.ENGINE_STATE_MAX_AGE_SECONDS + 0.5)
        self.assertEqual(safety.assess_engine_restart(now=now).level, safety.SAFE)
        self.state_file.write(timestamp=now - safety.ENGINE_STATE_MAX_AGE_SECONDS - 0.5)
        self.assertEqual(safety.assess_engine_restart(now=now).level, safety.UNKNOWN)

    def test_timestamp_in_the_future_is_unknown(self):
        result = self.assess(timestamp=time.time() + 3600)
        self.assertEqual(result.level, safety.UNKNOWN)
        self.assertEqual(result.reasons, (safety.STATUS_FUTURE,))

    def test_bad_timestamps_are_unknown(self):
        for bad in (None, "now", True, 0, -5, float("nan"), [1]):
            with self.subTest(bad=bad):
                state = engine_state()
                state["timestamp"] = bad
                self.state_file.write(state)
                self.assertEqual(safety.assess_engine_restart().level, safety.UNKNOWN)
        state = engine_state()
        del state["timestamp"]
        self.state_file.write(state)
        self.assertEqual(safety.assess_engine_restart().level, safety.UNKNOWN)

    def test_missing_required_fields_are_unsupported_not_safe(self):
        for key in ("mic_live", "remote_dj_configured", "remote_dj_connected", "remote_dj_live",
                    "remote_dj_reconnecting", "manual_mode", "transport"):
            with self.subTest(missing=key):
                state = engine_state()
                del state[key]
                self.state_file.write(state)
                result = safety.assess_engine_restart()
                self.assertEqual(result.level, safety.UNKNOWN)
                self.assertEqual(result.reasons, (safety.STATUS_UNSUPPORTED,))

    def test_wrongly_typed_booleans_are_malformed_not_coerced(self):
        for key in ("mic_live", "remote_dj_configured", "remote_dj_connected", "remote_dj_live",
                    "remote_dj_reconnecting", "manual_mode"):
            for bad in ("false", 0, 1, None, "", []):
                with self.subTest(key=key, bad=bad):
                    state = engine_state()
                    state[key] = bad
                    self.state_file.write(state)
                    result = safety.assess_engine_restart()
                    self.assertEqual(result.level, safety.UNKNOWN)
                    self.assertEqual(result.reasons, (safety.STATUS_MALFORMED,))

    def test_unknown_slot_or_connection_shapes_are_malformed(self):
        for overrides in (
            {"remote_dj_slot": "mystery"},
            {"remote_dj_slot": 3},
            {"remote_dj_connection": "connected"},
            {"remote_dj_connection": {"status": "warp"}},
            {"remote_dj_connection": {}},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.assess(**overrides).level, safety.UNKNOWN)

    def test_engine_without_slot_or_connection_fields_is_unsupported_not_safe(self):
        # Codex P1 (independent review of 9e70035): every supported producer
        # publishes both lifecycle keys on every tick, so a missing key is
        # not "an older engine" to be judged on the core booleans.
        for missing in (("remote_dj_slot",), ("remote_dj_connection",),
                        ("remote_dj_slot", "remote_dj_connection")):
            with self.subTest(missing=missing):
                state = engine_state()
                for key in missing:
                    del state[key]
                self.state_file.write(state)
                result = safety.assess_engine_restart()
                self.assertEqual(result.level, safety.UNKNOWN)
                self.assertEqual(result.reasons, (safety.STATUS_UNSUPPORTED,))

    # ---- no secrets
    def test_audit_and_public_views_never_carry_session_secrets(self):
        result = self.assess(remote_dj_connected=True, remote_dj_slot="occupied")
        rendered = repr(result.as_audit_dict()) + repr(result.as_public_dict()) + result.summary
        for secret in ("ATTEMPT-SECRET-0123456789", "REQ-SECRET", "attempt_id", "user_id", "remote_dj_owner",
                       "gate_generation"):
            self.assertNotIn(secret, rendered)

    def test_assessment_never_modifies_the_status_file(self):
        self.state_file.write()
        before = self.state_file.path.read_bytes(), self.state_file.path.stat().st_mtime_ns
        safety.assess_engine_restart()
        after = self.state_file.path.read_bytes(), self.state_file.path.stat().st_mtime_ns
        self.assertEqual(before, after)


class ServiceIdentityTests(SimpleTestCase):
    def test_only_the_engine_unit_is_the_engine(self):
        self.assertTrue(safety.is_engine_service("isadoraair-engine"))
        self.assertTrue(safety.is_engine_service("isadoraair-engine.service"))
        for other in ("isadoraair-gunicorn", "isadoraair-gunicorn.service", "isadoraair-encoders.service",
                      "isadoraair-engine-boot-restart.service", "isadoraair-engine.timer",
                      "isadoraair-engine.service.bak", "x-isadoraair-engine", "", None, 7):
            with self.subTest(other=other):
                self.assertFalse(safety.is_engine_service(other))

    def test_plan_restarts_engine_reads_the_trusted_restart_identities(self):
        from types import SimpleNamespace as NS
        self.assertTrue(safety.plan_restarts_engine(NS(services_requiring_restart=("isadoraair-gunicorn", "isadoraair-engine"))))
        self.assertFalse(safety.plan_restarts_engine(NS(services_requiring_restart=("isadoraair-gunicorn",))))
        self.assertFalse(safety.plan_restarts_engine(NS(services_requiring_restart=())))
        self.assertFalse(safety.plan_restarts_engine(NS()))
        self.assertFalse(safety.plan_restarts_engine(None))


class OverrideContractTests(TestCase):
    def setUp(self):
        self.state_file = EngineStateFile(self)
        self.superuser = User.objects.create_superuser("rs-root", password="x")
        self.staff = User.objects.create_user("rs-staff", password="x", is_staff=True)
        self.state_file.write(mic_live=True)
        self.target = {"unit": "isadoraair-engine.service"}

    def admit(self, user, **kwargs):
        return safety.admit_engine_restart(user, entry_point="test", target=self.target, **kwargs)

    def events(self, title=None):
        qs = SystemEvent.objects.filter(category="restart_safety").order_by("pk")
        return [e for e in qs if title is None or e.title == title]

    def test_safe_and_warning_states_need_no_override_and_record_nothing(self):
        for overrides in ({}, {"manual_mode": True}):
            with self.subTest(overrides=overrides):
                self.state_file.write(**overrides)
                admission = self.admit(self.staff)
                self.assertTrue(admission.allowed)
                self.assertFalse(admission.override_used)
        self.assertEqual(self.events(), [])

    def test_blocked_without_override_is_refused_and_audited(self):
        admission = self.admit(self.superuser)
        self.assertFalse(admission.allowed)
        self.assertEqual(admission.code, safety.OVERRIDE_REQUIRED)
        (event,) = self.events("Engine restart refused by restart safeguard")
        self.assertEqual(event.detail["decision"], safety.OVERRIDE_REQUIRED)
        self.assertEqual(event.detail["outcome"], "not_submitted")
        self.assertEqual(event.detail["operator"]["username"], "rs-root")
        self.assertEqual(event.detail["engine_state"]["reasons"], [safety.STUDIO_MIC_LIVE])

    def test_non_superuser_cannot_override(self):
        for user in (self.staff, None):
            with self.subTest(user=user):
                admission = self.admit(user, override_requested=True, reason="a long enough reason")
                self.assertFalse(admission.allowed)
                self.assertEqual(admission.code, safety.OVERRIDE_FORBIDDEN)
        # An inactive superuser is not a superuser for this purpose.
        self.superuser.is_active = False
        admission = self.admit(self.superuser, override_requested=True, reason="a long enough reason")
        self.assertEqual(admission.code, safety.OVERRIDE_FORBIDDEN)

    def test_superuser_override_requires_a_written_reason(self):
        for reason in ("", "   ", "short", None):
            with self.subTest(reason=reason):
                admission = self.admit(self.superuser, override_requested=True, reason=reason)
                self.assertFalse(admission.allowed)
                self.assertEqual(admission.code, safety.REASON_REQUIRED)

    def test_superuser_override_is_admitted_and_audited_with_state_and_reason(self):
        admission = self.admit(self.superuser, override_requested=True, reason="  Emergency   engine fix  ")
        self.assertTrue(admission.allowed)
        self.assertTrue(admission.override_used)
        self.assertEqual(admission.reason, "Emergency engine fix")
        (event,) = self.events("Superuser override of engine restart safeguard")
        detail = event.detail
        self.assertEqual(detail["operator"]["username"], "rs-root")
        self.assertEqual(detail["reason"], "Emergency engine fix")
        self.assertEqual(detail["target"], self.target)
        self.assertEqual(detail["engine_state"]["reasons"], [safety.STUDIO_MIC_LIVE])
        self.assertEqual(detail["engine_state"]["engine_instance"], "0b0f6a2c-5e6a-4f0e-9c58-3f6f2f3a9d11")
        self.assertEqual(detail["outcome"], "pending_submission")

    def test_override_for_unknown_state_is_possible_so_a_failed_engine_is_recoverable(self):
        self.state_file.remove()
        admission = self.admit(self.superuser, override_requested=True, reason="engine is hung, no status")
        self.assertTrue(admission.allowed)
        self.assertEqual(admission.assessment.level, safety.UNKNOWN)

    def test_override_is_not_recorded_when_none_was_needed(self):
        self.state_file.write()
        admission = self.admit(self.superuser, override_requested=True, reason="not actually needed ok")
        self.assertTrue(admission.allowed)
        self.assertFalse(admission.override_used)
        self.assertEqual(self.events(), [])

    def test_each_call_reassesses_so_nothing_carries_over(self):
        first = self.admit(self.superuser, override_requested=True, reason="first override reason")
        self.assertTrue(first.allowed)
        # No override this time: the earlier one confers nothing.
        second = self.admit(self.superuser)
        self.assertFalse(second.allowed)

    def test_status_change_between_two_assessments_is_seen(self):
        self.state_file.write()
        self.assertTrue(self.admit(self.staff).allowed)
        self.state_file.write(mic_live=True)
        self.assertFalse(self.admit(self.staff).allowed)

    def test_outcome_row_only_for_overridden_requests_and_never_claims_completion(self):
        admission = self.admit(self.superuser, override_requested=True, reason="outcome row test reason")
        safety.record_restart_outcome(self.superuser, admission, entry_point="test", target=self.target,
                                      outcome="submitted", detail={"operation_id": "op-1"})
        (event,) = self.events("Overridden engine restart request outcome")
        self.assertEqual(event.detail["outcome"], "submitted")
        self.assertIs(event.detail["completion_claimed"], False)
        # A non-overridden admission records nothing.
        self.state_file.write()
        plain = self.admit(self.staff)
        before = len(self.events())
        safety.record_restart_outcome(self.staff, plain, entry_point="test", target=self.target,
                                      outcome="submitted")
        self.assertEqual(len(self.events()), before)

    def test_audit_rows_are_never_coalesced(self):
        for _ in range(3):
            self.admit(self.superuser)
        self.assertEqual(len(self.events("Engine restart refused by restart safeguard")), 3)

    def test_audit_rows_carry_no_session_secrets(self):
        self.state_file.write(remote_dj_connected=True, remote_dj_slot="occupied")
        self.admit(self.superuser)
        self.admit(self.superuser, override_requested=True, reason="secrets must not leak")
        blob = repr([e.detail for e in self.events()])
        for secret in ("ATTEMPT-SECRET-0123456789", "REQ-SECRET", "gate_generation"):
            self.assertNotIn(secret, blob)


class ScopeContractTests(SimpleTestCase):
    """The safeguard must stay a read-only assessment: it must not reach the
    broker, systemd, the protected updater, WebRTC, or write engine state."""

    def test_module_has_no_side_effect_surfaces(self):
        source = Path(safety.__file__).read_text(encoding="utf-8")
        for forbidden in ("UpdaterClient", "subprocess", "systemctl", "write_text", "write_bytes",
                          "os.kill", "import signal", "webrtc", "import gi", "restart_operator_service",
                          "open("):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_systemd_automatic_recovery_contract_is_unchanged(self):
        root = Path(__file__).resolve().parents[2]
        unit = (root / "deploy" / "isadoraair-engine.service").read_text(encoding="utf-8")
        self.assertIn("Restart=on-failure", unit)
        for name in ("isadoraair-engine-boot-restart.service", "isadoraair-engine-boot-restart.sh",
                     "isadoraair-engine-boot-restart.timer"):
            self.assertTrue((root / "deploy" / name).is_file(), name)
        # The safeguard is application-level only: no unit file refers to it.
        for path in (root / "deploy").glob("isadoraair-engine*"):
            self.assertNotIn("restart_safety", path.read_text(encoding="utf-8", errors="replace"))
