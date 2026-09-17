"""r0084 regression: skipped protected-runtime transitions stay authoritative.

The historical test names the WRJE incident classification explicitly:
``MANUAL_PREREQUISITE: UNKNOWN_MANAGED_UNIT``.  Under r0083 code the
r0081 -> r0083 plan aggregated r0082's Aircheck units but discarded its
generation-4 runtime transition, so the active generation-3 policy rejected
those units.  The corrected plan retains r0082's exact provenance.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from .phase_b_helpers import PROJECT_ROOT, git  # noqa: F401

from updatecenter.execution_contract import (
    intermediate_protected_runtime_fingerprint_payload as django_v4_payload,
)
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.executor import Executor
from isadoraair_updater.release import (
    ChainEntry,
    ProtectedRuntimeTransition,
    TrustedPlan,
    TrustedRepository,
    _effective_protected_runtime_transition,
    derive_plan,
    fingerprint,
    manual_blockers,
    protected_runtime_fingerprint_payload,
)
from protected_bootstrap.manifest_field import ProtectedRuntimeField
from protected_bootstrap.policy import parse_policy_dict


R0081 = "880770f34d3c90040fe48e0235e80eefde4cc0cd"
R0082 = "8b34f0842eccbfce668d448e65e0ad2a0ee621bd"
R0083 = "726c294face82e48203d1edb6cba06ee73866011"
AIRCHECK_UNITS = frozenset({
    "isadoraair-aircheck-buffer.service",
    "isadoraair-aircheck-buffer.timer",
    "isadoraair-aircheck-recovery.service",
    "isadoraair-aircheck-recovery.timer",
})


def _known_units(repository: TrustedRepository, commit: str) -> frozenset[str]:
    raw = repository.read_file(commit, "deploy/updater_runtime/protected-policy.json")
    return frozenset(parse_policy_dict(json.loads(raw)).as_mapping())


def _field(*, generation=5, descriptor="d") -> ProtectedRuntimeField:
    return ProtectedRuntimeField(
        generation=generation,
        descriptor_path="deploy/updater_runtime/protected-runtime-descriptor.json",
        descriptor_sha256=descriptor * 64,
        minimum_bootstrap_protocol_version=1,
        runtime_version=6,
        manifest_protocol_version=5,
        supported_wire_protocols=(3,),
        attestations=("deploy/updater_attestations/r0084-primary.json",),
    )


def _direct_bridge_plan(installed_release_id: str) -> TrustedPlan:
    transition = ProtectedRuntimeTransition(
        field=_field(), release_id="r0084",
        # Canonical r0084 manifest ancestry is r0083 even when the
        # installed station is the WRJE r0081 bridge case. This fact is
        # deliberately absent from direct-target v3 fingerprint bytes.
        previous_release_id="r0083", commit="c" * 40,
    )
    wrje = installed_release_id == "r0081"
    return TrustedPlan(
        installed_release_id=installed_release_id, installed_commit="a" * 40,
        target_release_id="r0084", target_commit="c" * 40,
        releases_in_plan=(("r0082", "r0083", "r0084") if wrje else ("r0084",)),
        migrations_required=(("hardware.0011_duckingconfig_ptt_auto_manual_enabled",) if wrje else ()),
        migration_compatibility=("additive" if wrje else None),
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(tuple(sorted(AIRCHECK_UNITS)) if wrje else ()),
        systemd_units_new_required=(), systemd_units_new_optional=(),
        systemd_units_removed_or_renamed=(), collectstatic_required=False,
        services_requiring_restart=(), nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=5, manual_bootstrap_required=False,
        fingerprint="", protected_runtime_transition=transition,
    )


class HistoricalWRJESkippedRuntimeTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        common_dir = Path(git(PROJECT_ROOT, "rev-parse", "--git-common-dir"))
        if not common_dir.is_absolute():
            common_dir = PROJECT_ROOT / common_dir
        cls.repository = TrustedRepository(common_dir, "unused", "main", CommandRunner())
        cls.r0081_units = _known_units(cls.repository, R0081)
        cls.r0082_units = _known_units(cls.repository, R0082)
        cls.skipped = derive_plan(
            cls.repository, R0083, R0081, "r0083", known_units=cls.r0081_units,
        )
        cls.direct = derive_plan(
            cls.repository, R0082, R0081, "r0082", known_units=cls.r0081_units,
        )
        cls.already_crossed = derive_plan(
            cls.repository, R0083, R0082, "r0083", known_units=cls.r0082_units,
        )

    def test_wrje_r0081_to_r0083_retains_r0082_runtime_and_aircheck_authority(self):
        """Regression for MANUAL_PREREQUISITE: UNKNOWN_MANAGED_UNIT."""
        plan = self.skipped
        transition = plan.protected_runtime_transition
        self.assertEqual(plan.releases_in_plan, ("r0082", "r0083"))
        self.assertEqual(plan.target_release_id, "r0083")
        self.assertEqual(plan.target_commit, R0083)
        self.assertEqual(transition.release_id, "r0082")
        self.assertEqual(transition.previous_release_id, "r0081")
        self.assertEqual(transition.commit, R0082)
        self.assertEqual(transition.field.generation, 4)
        self.assertEqual(
            AIRCHECK_UNITS,
            frozenset(plan.systemd_units_changed) | frozenset(plan.systemd_units_new_required),
        )
        self.assertNotIn("UNKNOWN_MANAGED_UNIT", manual_blockers(plan, known_units=self.r0081_units))
        self.assertEqual(plan.fingerprint_payload()["contract_version"], 4)

    def test_r0081_to_r0082_remains_a_direct_v3_protected_target(self):
        self.assertEqual(self.direct.protected_runtime_transition.release_id, "r0082")
        self.assertEqual(self.direct.fingerprint_payload()["contract_version"], 3)
        self.assertEqual(manual_blockers(self.direct, known_units=self.r0081_units), ())

    def test_r0082_to_r0083_does_not_replay_generation_four(self):
        self.assertIsNone(self.already_crossed.protected_runtime_transition)
        self.assertEqual(self.already_crossed.fingerprint_payload()["contract_version"], 2)

    def test_intermediate_fingerprint_binds_each_provenance_fact(self):
        plan = self.skipped
        baseline = fingerprint(plan.fingerprint_payload())
        transition = plan.protected_runtime_transition
        substitutions = (
            dataclasses.replace(transition, release_id="r0081"),
            dataclasses.replace(transition, previous_release_id="r0080"),
            dataclasses.replace(transition, commit=R0083),
        )
        for substituted in substitutions:
            with self.subTest(substituted=substituted):
                tampered = dataclasses.replace(plan, protected_runtime_transition=substituted)
                self.assertNotEqual(fingerprint(tampered.fingerprint_payload()), baseline)

    def test_worker_and_django_v4_payloads_are_byte_identical(self):
        worker_payload = self.skipped.fingerprint_payload()
        transition = self.skipped.protected_runtime_transition
        runtime = transition.field
        values = {
            key: value for key, value in dataclasses.asdict(self.skipped).items()
            if key not in {"fingerprint", "protected_runtime_transition"}
        }
        django_payload = django_v4_payload(
            **values,
            protected_runtime_generation=runtime.generation,
            protected_runtime_descriptor_sha256=runtime.descriptor_sha256,
            protected_runtime_minimum_bootstrap_protocol_version=runtime.minimum_bootstrap_protocol_version,
            protected_runtime_runtime_version=runtime.runtime_version,
            protected_runtime_manifest_protocol_version=runtime.manifest_protocol_version,
            protected_runtime_supported_wire_protocols=runtime.supported_wire_protocols,
            protected_runtime_release_id=transition.release_id,
            protected_runtime_previous_release_id=transition.previous_release_id,
            protected_runtime_commit=transition.commit,
        )
        self.assertEqual(worker_payload, django_payload)


class TransitionSelectionAndBootstrapCompatibilityTests(SimpleTestCase):
    @staticmethod
    def _entry(release_id, previous, commit, runtime=None):
        manifest = SimpleNamespace(
            release_id=release_id, previous_release_id=previous, protected_runtime=runtime,
        )
        return ChainEntry(manifest=manifest, index=0, commit=commit)

    def test_latest_of_multiple_protected_transitions_wins(self):
        earlier = _field(generation=6, descriptor="a")
        later = _field(generation=7, descriptor="b")
        transitions = [
            self._entry("r0085", "r0084", "1" * 40, earlier),
            self._entry("r0086", "r0085", "2" * 40),
            self._entry("r0087", "r0086", "3" * 40, later),
            self._entry("r0088", "r0087", "4" * 40),
        ]
        selected = _effective_protected_runtime_transition(transitions)
        self.assertEqual(selected.release_id, "r0087")
        self.assertEqual(selected.previous_release_id, "r0086")
        self.assertEqual(selected.commit, "3" * 40)
        self.assertEqual(selected.field.generation, 7)

    def test_future_ordinary_transition_requires_no_handoff(self):
        transitions = [self._entry("r0085", "r0084", "1" * 40)]
        self.assertIsNone(_effective_protected_runtime_transition(transitions))

    def test_substituting_earlier_transition_changes_authorization_fingerprint(self):
        earlier = ProtectedRuntimeTransition(
            field=_field(generation=6, descriptor="a"), release_id="r0085",
            previous_release_id="r0084", commit="1" * 40,
        )
        later = ProtectedRuntimeTransition(
            field=_field(generation=7, descriptor="b"), release_id="r0087",
            previous_release_id="r0086", commit="3" * 40,
        )
        base = dataclasses.replace(
            _direct_bridge_plan("r0084"), target_release_id="r0088", target_commit="4" * 40,
            releases_in_plan=("r0085", "r0086", "r0087", "r0088"),
            protected_runtime_transition=later,
        )
        substituted = dataclasses.replace(base, protected_runtime_transition=earlier)
        self.assertNotEqual(
            fingerprint(base.fingerprint_payload()),
            fingerprint(substituted.fingerprint_payload()),
        )

    def test_direct_r0084_bridge_keeps_legacy_v3_bytes_for_r0081_and_r0083(self):
        # Golden digests were generated with the exact r0083 runtime's
        # contract-v3 implementation. They make this a cross-version byte
        # compatibility test, not two calls through one mutable helper.
        r0083_runtime_goldens = {
            "r0081": "8a6e65b6fa2fa11abafc7a32b4980f1dbfd47fe80f424156e1a6ae9c0320c0a7",
            "r0083": "e30c1fcd5fc115211e5298c2c65f598eb053e64e68068371d34dc0a9c38cdef8",
        }
        for predecessor in ("r0081", "r0083"):
            with self.subTest(predecessor=predecessor):
                plan = _direct_bridge_plan(predecessor)
                transition = plan.protected_runtime_transition
                runtime = transition.field
                payload = plan.fingerprint_payload()
                values = {
                    key: value for key, value in dataclasses.asdict(plan).items()
                    if key not in {"fingerprint", "protected_runtime_transition"}
                }
                legacy = protected_runtime_fingerprint_payload(
                    **values,
                    protected_runtime_generation=runtime.generation,
                    protected_runtime_descriptor_sha256=runtime.descriptor_sha256,
                    protected_runtime_minimum_bootstrap_protocol_version=runtime.minimum_bootstrap_protocol_version,
                    protected_runtime_runtime_version=runtime.runtime_version,
                    protected_runtime_manifest_protocol_version=runtime.manifest_protocol_version,
                    protected_runtime_supported_wire_protocols=runtime.supported_wire_protocols,
                )
                self.assertEqual(payload, legacy)
                self.assertEqual(payload["contract_version"], 3)
                self.assertEqual(fingerprint(payload), fingerprint(legacy))
                self.assertEqual(fingerprint(payload), r0083_runtime_goldens[predecessor])


class IntermediateHandoffProvenanceTests(SimpleTestCase):
    class Store:
        def __init__(self):
            self.state = {"milestones": [], "protected_runtime_candidate": None}
            self.closed = False

        def milestone(self, _job_id, name):
            if name not in self.state["milestones"]:
                self.state["milestones"].append(name)

        def update(self, _job_id, **changes):
            self.state.update(changes)

        def load(self, _job_id):
            return self.state

        def append_log(self, _job_id, _message):
            pass

        def close(self):
            self.closed = True

    class Client:
        def __init__(self):
            self.activation = None

        def get_runtime_state(self):
            return {"active_generation": 4, "active_slot": "A"}

        def request_activation(self, **kwargs):
            self.activation = kwargs

    def test_staging_and_both_verifiers_use_intermediate_release_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            descriptor_path = root / "descriptor.json"
            descriptor_path.write_bytes(b"{}")
            transition = ProtectedRuntimeTransition(
                field=_field(), release_id="r0085",
                previous_release_id="r0084", commit="5" * 40,
            )
            plan = dataclasses.replace(
                _direct_bridge_plan("r0084"),
                target_release_id="r0086", target_commit="6" * 40,
                releases_in_plan=("r0085", "r0086"),
                protected_runtime_transition=transition,
            )
            store = self.Store()
            client = self.Client()
            executor = object.__new__(Executor)
            executor.store = store
            executor.repository = object()
            executor.active_policy = None
            executor.config = SimpleNamespace(
                phase_d_supervisor_slots_root=root,
                phase_d_supervisor_activation_socket=root / "activation.sock",
            )
            executor._resolve_candidate_slot = lambda _socket: ("A", "B", client)
            executor._load_phase_d_trust_policy = lambda: object()
            materialized = SimpleNamespace(descriptor_bytes=b"{}", descriptor_sha256="d" * 64)
            verification = SimpleNamespace(ok=True, reasons=(), candidate_policy=None)

            with (
                patch("isadoraair_updater.executor.new_supervisor_staging_directory", return_value=root / "stage"),
                patch("isadoraair_updater.executor.materialize_candidate", return_value=materialized) as materialize,
                patch("isadoraair_updater.executor.stage_attestations") as attestations,
                patch("isadoraair_updater.executor.stage_descriptor"),
                patch("isadoraair_updater.executor.publish_to_candidate_slot"),
                patch("isadoraair_updater.executor.descriptor_staging_path", return_value=descriptor_path),
                patch("isadoraair_updater.executor.attestations_staging_directory", return_value=root),
                patch("isadoraair_updater.executor.verify_candidate_independently", return_value=verification) as verify,
            ):
                executor._execute_runtime_handoff("job", plan, transition.field, set())

            self.assertEqual(materialize.call_args.args[2], transition.commit)
            self.assertNotEqual(materialize.call_args.args[2], plan.target_commit)
            self.assertEqual(attestations.call_args.args[2], transition.commit)
            self.assertEqual(verify.call_args.kwargs["release_id"], "r0085")
            self.assertEqual(verify.call_args.kwargs["previous_release_id"], "r0084")
            self.assertEqual(client.activation["release_id"], "r0085")
            self.assertEqual(client.activation["previous_release_id"], "r0084")
            self.assertTrue(store.closed)
