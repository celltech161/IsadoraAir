"""Reviewed migration approval -- protected executor gate behavior.

Same helper/fixture conventions as test_phase_b_executor.py's
ExecutorSchemaComparisonTests: a bare Executor against a temp-dir
config, hand-built probe() payloads passed directly to
_validate_target_schema (bypassing _strict_probe/subprocess entirely --
that boundary is covered separately by test_migration_plan_digest.py's
canonicalization tests and test_r0089_migration_approval_acceptance.py's
real end-to-end proof).
"""
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import TrustedPlan


def trusted_plan(**changes):
    data = dict(
        installed_release_id="r0088", installed_commit="a" * 40,
        target_release_id="r0089", target_commit="b" * 40,
        releases_in_plan=("r0089",), migrations_required=("sample.0001_initial",),
        migration_compatibility="additive", python_requirements_changed=False,
        apt_packages_new=(), systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=("isadoraair-engine",),
        nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=1, manual_bootstrap_required=False,
        fingerprint="f" * 64,
    )
    data.update(changes)
    return TrustedPlan(**data)


DIGEST = "d" * 64


def probe_with_manual_op(*, approval=None):
    operation = {"operation": "AddField", "classification": "manual", "detail": "non-null AddField uses relational field"}
    item = {
        "ref": "sample.0001_initial", "dependencies": [],
        "migration_file_sha256": "0" * 64, "operations": [operation],
    }
    return {
        "schema_version": 1, "status": "ok", "plan": [item],
        "nodes": {"sample.0001_initial": []},
        "applied": [], "conflicts": {}, "replacements": [],
        "release_id": "r0089", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
        "migration_plan_digest": DIGEST,
        "manual_operations": [{
            "ref": "sample.0001_initial", "operation_index": 0,
            "operation": "AddField", "classification": "manual",
            "detail": "non-null AddField uses relational field",
        }],
        "approval": approval,
    }


def probe_all_additive():
    item = {
        "ref": "sample.0001_initial", "dependencies": [],
        "migration_file_sha256": "0" * 64,
        "operations": [{"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}],
    }
    return {
        "schema_version": 1, "status": "ok", "plan": [item],
        "nodes": {"sample.0001_initial": []},
        "applied": [], "conflicts": {}, "replacements": [],
        "release_id": "r0089", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
        "migration_plan_digest": None, "manual_operations": [], "approval": None,
    }


class FakeSystemd:
    def reconcile(self, source, plan):
        return {}

    def restart_declared(self, services):
        return list(services)


class ExecutorApprovalGateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(self.config, self.store, CommandRunner(), systemd_manager=FakeSystemd())
        self.job_id = "11111111-1111-1111-1111-111111111111"
        self.store.accept(self.job_id, "r0089", "f" * 64)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_no_manual_operations_is_unchanged_automatic_behavior(self):
        actual = self.executor._validate_target_schema(
            trusted_plan(), probe_all_additive(), {"applied": []}, self.job_id,
            migration_already_started=False,
        )
        self.assertEqual(actual, ("sample.0001_initial",))

    def test_manual_operation_with_no_approval_stops_manual_with_evidence(self):
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                trusted_plan(), probe_with_manual_op(approval={"found": False}), {"applied": []}, self.job_id,
                migration_already_started=False,
            )
        exc = caught.exception
        self.assertEqual(exc.classification, "MIGRATION_OPERATION_MANUAL")
        self.assertTrue(exc.manual)
        self.assertIsNotNone(exc.migration_plan_review)
        self.assertEqual(exc.migration_plan_review["release_id"], "r0089")
        self.assertEqual(exc.migration_plan_review["migration_plan_digest"], DIGEST)
        self.assertEqual(len(exc.migration_plan_review["manual_operations"]), 1)

    def test_manual_operation_with_no_approval_key_at_all_stops_manual(self):
        """approval=None (the exact shape a probe payload has when no
        release context was even available) is treated identically to
        an explicit not-found -- never silently permissive."""
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                trusted_plan(), probe_with_manual_op(approval=None), {"applied": []}, self.job_id,
                migration_already_started=False,
            )
        self.assertTrue(caught.exception.manual)

    def test_exact_matching_approval_proceeds_without_raising(self):
        actual = self.executor._validate_target_schema(
            trusted_plan(),
            probe_with_manual_op(approval={"found": True, "id": "approval-1", "approved_by": "op", "approved_at": "2026-01-01T00:00:00+00:00"}),
            {"applied": []}, self.job_id,
            migration_already_started=False,
        )
        self.assertEqual(actual, ("sample.0001_initial",))

    def test_approval_never_bypasses_the_destructive_manifest_gate(self):
        """Tier 3 (approval) must never override a manifest that
        declares the release destructive/incompatible -- this check
        fires BEFORE the approval-gate code even runs."""
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                trusted_plan(migration_compatibility="destructive"),
                probe_with_manual_op(approval={"found": True, "id": "x", "approved_by": "op", "approved_at": "now"}),
                {"applied": []}, self.job_id,
                migration_already_started=False,
            )
        self.assertEqual(caught.exception.classification, "MIGRATION_NOT_AUTOMATABLE")

    def test_approval_never_bypasses_conflict_detection(self):
        payload = probe_with_manual_op(approval={"found": True, "id": "x", "approved_by": "op", "approved_at": "now"})
        payload["conflicts"] = {"sample": ["0001_a", "0001_b"]}
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                trusted_plan(), payload, {"applied": []}, self.job_id, migration_already_started=False,
            )
        self.assertEqual(caught.exception.classification, "TARGET_MIGRATION_CONFLICT")

    def test_approval_never_bypasses_dependency_closure_mismatch(self):
        payload = probe_with_manual_op(approval={"found": True, "id": "x", "approved_by": "op", "approved_at": "now"})
        payload["nodes"]["other.0001_initial"] = []
        payload["plan"].append({
            "ref": "other.0001_initial", "dependencies": [],
            "migration_file_sha256": "1" * 64, "operations": [],
        })
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                trusted_plan(), payload, {"applied": []}, self.job_id, migration_already_started=False,
            )
        self.assertEqual(caught.exception.classification, "TARGET_MIGRATION_MISMATCH")
