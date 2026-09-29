"""Generation-9 protected-runtime replay/idempotency regression coverage."""
from __future__ import annotations

from pathlib import Path
import tempfile
import types
import uuid
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import ProtectedRuntimeTransition, TrustedPlan
from protected_bootstrap.manifest_field import ProtectedRuntimeField


TARGET_DESCRIPTOR = "8" * 64
OTHER_DESCRIPTOR = "9" * 64


def _field(*, generation=8, descriptor=TARGET_DESCRIPTOR):
    return ProtectedRuntimeField(
        generation=generation,
        descriptor_path="deploy/updater_runtime/protected-runtime-descriptor.json",
        descriptor_sha256=descriptor,
        minimum_bootstrap_protocol_version=1,
        runtime_version=9,
        manifest_protocol_version=5,
        supported_wire_protocols=(3, 4),
        attestations=("deploy/updater_attestations/r0092-primary.json",),
    )


def _plan(*, generation=8, descriptor=TARGET_DESCRIPTOR, fingerprint="f" * 64):
    field = _field(generation=generation, descriptor=descriptor)
    return TrustedPlan(
        installed_release_id="r0088", installed_commit="a" * 40,
        target_release_id="r0092", target_commit="b" * 40,
        releases_in_plan=("r0089", "r0090", "r0091", "r0092"),
        migrations_required=(), migration_compatibility=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(),
        nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=5, manual_bootstrap_required=False,
        fingerprint=fingerprint,
        protected_runtime_transition=ProtectedRuntimeTransition(
            field=field, release_id="r0092", previous_release_id="r0091", commit="b" * 40,
        ),
    )


class FakeSupervisorClient:
    states = []
    requests = []

    def __init__(self, socket_path):
        self.socket_path = socket_path

    def get_runtime_state(self):
        if len(self.states) > 1:
            return self.states.pop(0)
        return self.states[0]

    def request_activation(self, **kwargs):
        self.requests.append(kwargs)
        return {"ok": True}


def _state(generation, descriptor, slot="B", *, activation_in_flight=False):
    return {
        "ok": True, "active_slot": slot, "active_generation": generation,
        "active_descriptor_sha256": descriptor, "activation_in_flight": activation_in_flight,
        "phase": None, "runtime_activation_accepted": False,
    }


class RuntimeIdempotencyTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        data = config_dict(self.root, str(self.root / "upstream.git"))
        data["phase_d_supervisor_slots_root"] = str(self.root / "slots")
        data["phase_d_supervisor_activation_socket"] = str(self.root / "activation.sock")
        self.config = validate_config_dict(data, allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)
        self.executor = Executor(self.config, self.store, CommandRunner())
        FakeSupervisorClient.requests = []

    def _accepted(self, plan):
        job_id = str(uuid.uuid4())
        self.store.accept(job_id, plan.target_release_id, plan.fingerprint)
        return job_id

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_exact_generation_and_descriptor_persist_distinct_gate_without_staging(self):
        plan = _plan()
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [_state(8, TARGET_DESCRIPTOR)]
        with patch("isadoraair_updater.executor.new_supervisor_staging_directory") as stage, \
                patch("isadoraair_updater.executor.publish_to_candidate_slot") as publish:
            result = self.executor._execute_runtime_handoff(
                job_id, plan, plan.protected_runtime, set(),
            )
        self.assertIsNone(result)
        state = self.store.load(job_id)
        self.assertIn("runtime_already_authoritative", state["milestones"])
        self.assertNotIn("runtime_activation_requested", state["milestones"])
        self.assertIsNone(state["protected_runtime_candidate"])
        self.assertEqual(state["protected_runtime_satisfaction"]["generation"], 8)
        self.assertEqual(
            state["protected_runtime_satisfaction"]["trusted_plan_fingerprint"], plan.fingerprint,
        )
        stage.assert_not_called()
        publish.assert_not_called()
        self.executor._require_mutation_allowed(plan, state["milestones"])

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_equal_generation_different_descriptor_fails_before_slot_touch(self):
        self._assert_replay_refused(active_generation=8, active_descriptor=OTHER_DESCRIPTOR)

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_lower_target_generation_fails_before_slot_touch(self):
        self._assert_replay_refused(active_generation=9, active_descriptor=OTHER_DESCRIPTOR)

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_activation_in_flight_refuses_before_slot_touch(self):
        plan = _plan(generation=9)
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [
            _state(8, OTHER_DESCRIPTOR, activation_in_flight=True),
        ]
        with patch("isadoraair_updater.executor.new_supervisor_staging_directory") as stage, \
                self.assertRaises(ExecutionError) as caught:
            self.executor._execute_runtime_handoff(job_id, plan, plan.protected_runtime, set())
        self.assertEqual(caught.exception.classification, "PROTECTED_RUNTIME_ACTIVATION_IN_FLIGHT")
        stage.assert_not_called()

    def _assert_replay_refused(self, *, active_generation, active_descriptor):
        plan = _plan()
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [_state(active_generation, active_descriptor)]
        with patch("isadoraair_updater.executor.new_supervisor_staging_directory") as stage, \
                patch("isadoraair_updater.executor.publish_to_candidate_slot") as publish, \
                self.assertRaises(ExecutionError) as caught:
            self.executor._execute_runtime_handoff(job_id, plan, plan.protected_runtime, set())
        self.assertEqual(caught.exception.classification, "PROTECTED_RUNTIME_REPLAY_OR_ROLLBACK")
        stage.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(FakeSupervisorClient.requests, [])

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_invalid_newer_candidate_does_not_replace_previous_lkg_slot(self):
        plan = _plan(generation=9)
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [_state(8, OTHER_DESCRIPTOR)]
        materialized = types.SimpleNamespace(descriptor_bytes=b"{}", descriptor_sha256=TARGET_DESCRIPTOR)
        failed = types.SimpleNamespace(ok=False, reasons=("signature threshold not satisfied",), candidate_policy=None)
        with patch("isadoraair_updater.executor.materialize_candidate", return_value=materialized), \
                patch("isadoraair_updater.executor.stage_attestations"), \
                patch("isadoraair_updater.executor.stage_descriptor"), \
                patch.object(self.executor, "_load_phase_d_trust_policy", return_value=object()), \
                patch("isadoraair_updater.executor.verify_candidate_independently", return_value=failed), \
                patch("isadoraair_updater.executor.publish_to_candidate_slot") as publish, \
                self.assertRaises(ExecutionError) as caught:
            self.executor._execute_runtime_handoff(job_id, plan, plan.protected_runtime, set())
        self.assertEqual(caught.exception.classification, "CANDIDATE_INDEPENDENT_VERIFICATION_FAILED")
        publish.assert_not_called()
        self.assertEqual(FakeSupervisorClient.requests, [])
        self.assertFalse((self.root / "slots" / "A").exists())

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_authoritative_state_change_during_preflight_refuses_publication(self):
        plan = _plan(generation=9)
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [
            _state(8, OTHER_DESCRIPTOR, slot="B"),
            _state(8, "7" * 64, slot="B"),
        ]
        materialized = types.SimpleNamespace(descriptor_bytes=b"{}", descriptor_sha256=TARGET_DESCRIPTOR)
        verified = types.SimpleNamespace(ok=True, reasons=(), candidate_policy=None)
        with patch("isadoraair_updater.executor.materialize_candidate", return_value=materialized), \
                patch("isadoraair_updater.executor.stage_attestations"), \
                patch("isadoraair_updater.executor.stage_descriptor"), \
                patch.object(self.executor, "_load_phase_d_trust_policy", return_value=object()), \
                patch("isadoraair_updater.executor.verify_candidate_independently", return_value=verified), \
                patch("isadoraair_updater.executor.publish_to_candidate_slot") as publish, \
                self.assertRaises(ExecutionError) as caught:
            self.executor._execute_runtime_handoff(job_id, plan, plan.protected_runtime, set())
        self.assertEqual(caught.exception.classification, "RUNTIME_STATE_CHANGED_DURING_PREFLIGHT")
        publish.assert_not_called()
        self.assertEqual(FakeSupervisorClient.requests, [])

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_resume_rederives_trusted_plan_and_revalidates_exact_active_evidence(self):
        plan = _plan()
        job_id = self._accepted(plan)
        FakeSupervisorClient.states = [_state(8, TARGET_DESCRIPTOR)]
        self.executor.repository.fetch = Mock(return_value=plan.target_commit)
        self.executor._live_identity = Mock(return_value={"branch": "main", "head": plan.installed_commit})

        class AtMutationGate(BaseException):
            pass

        with patch("isadoraair_updater.executor.derive_plan", return_value=plan), \
                patch("isadoraair_updater.executor.materialize_candidate") as materialize_runtime, \
                patch.object(self.executor, "_enter_mutation_phase", side_effect=AtMutationGate):
            with self.assertRaises(AtMutationGate):
                self.executor.execute(job_id)
            first = self.store.load(job_id)
            self.assertIn("runtime_already_authoritative", first["milestones"])
            with self.assertRaises(AtMutationGate):
                self.executor.execute(job_id)
        materialize_runtime.assert_not_called()
        self.assertEqual(FakeSupervisorClient.requests, [])

        # The persisted marker cannot authorize a different active descriptor.
        FakeSupervisorClient.states = [_state(8, OTHER_DESCRIPTOR)]
        with patch("isadoraair_updater.executor.derive_plan", return_value=plan):
            failed = self.executor.execute(job_id)
        self.assertEqual(
            failed["failure_classification"], "RUNTIME_AUTHORITY_EVIDENCE_MISMATCH",
            failed.get("failure_detail"),
        )

    @patch("isadoraair_updater.executor.SupervisorClient", FakeSupervisorClient)
    def test_matching_active_runtime_cannot_bypass_plan_fingerprint_validation(self):
        accepted_plan = _plan(fingerprint="a" * 64)
        derived_plan = _plan(fingerprint="b" * 64)
        job_id = self._accepted(accepted_plan)
        FakeSupervisorClient.states = [_state(8, TARGET_DESCRIPTOR)]
        self.executor.repository.fetch = Mock(return_value=derived_plan.target_commit)
        self.executor._live_identity = Mock(
            return_value={"branch": "main", "head": derived_plan.installed_commit},
        )
        with patch("isadoraair_updater.executor.derive_plan", return_value=derived_plan), \
                patch("isadoraair_updater.executor.materialize_candidate") as materialize_runtime:
            result = self.executor.execute(job_id)
        self.assertEqual(result["failure_classification"], "PLAN_FINGERPRINT_MISMATCH")
        materialize_runtime.assert_not_called()
        self.assertNotIn("runtime_already_authoritative", result["milestones"])
