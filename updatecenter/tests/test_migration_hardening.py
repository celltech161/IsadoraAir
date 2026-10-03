from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock
import uuid

from django.test import SimpleTestCase

from updatecenter.manifest import ManifestError, validate_manifest_dict
from updatecenter.execution_contract import (
    execution_fingerprint_payload, migration_hardening_execution_fingerprint,
)
from updatecenter.management.commands import updatecenter_migration_preflight as preflight

from .phase_b_helpers import config_dict, manifest
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.process import ProcessResult
from isadoraair_updater.staging import StagedSource
from isadoraair_updater.release import (
    ReleaseError, TrustedPlan, fingerprint, load_central_migration_authorization,
)


BASE = "base.0001_initial"
M1 = "sample.0001_first"
M2 = "sample.0002_second"
DIGEST = "d" * 64


def plan(**changes):
    values = dict(
        installed_release_id="r0104", installed_commit="a" * 40,
        target_release_id="r0105", target_commit="b" * 40,
        releases_in_plan=("r0105",), migrations_required=(M2,),
        migration_compatibility="additive", python_requirements_changed=False,
        apt_packages_new=(), systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(), nginx_changed=False,
        runtime_components_changed=False, minimum_updater_protocol_version=6,
        manual_bootstrap_required=False, fingerprint="f" * 64,
    )
    values.update(changes)
    return TrustedPlan(**values)


MANUAL = [{
    "ref": M2, "operation_index": 0, "operation": "RunPython",
    "classification": "manual", "detail": "outside allowlist",
}]


class FakeRepository:
    def __init__(self, record, *, introducing="c" * 40, ancestry=True):
        self.raw = json.dumps(record).encode()
        self.introducing = introducing
        self.ancestry = ancestry

    def read_file(self, tip, path, maximum=65536):
        return self.raw

    def introducing_commit(self, path, tip):
        return self.introducing

    def is_ancestor(self, before, after):
        return self.ancestry


def authorization(**changes):
    value = {
        "schema_version": 1, "release_id": "r0105", "target_commit": "b" * 40,
        "manifest_sha256": "c" * 64, "migration_plan_digest": DIGEST,
        "manual_operations": MANUAL, "trusted_plan_fingerprint": "f" * 64,
    }
    value.update(changes)
    return value


class CentralAuthorizationTests(SimpleTestCase):
    def check(self, record):
        return load_central_migration_authorization(
            FakeRepository(record), "e" * 40,
            plan(migration_authorization="deploy/migration_authorizations/r0105.json"),
            manifest_sha256="c" * 64, migration_plan_digest=DIGEST,
            manual_operations=MANUAL,
        )

    def test_exact_authorization_matches(self):
        self.assertIsNotNone(self.check(authorization()))

    def test_release_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(release_id="r0106")))

    def test_target_commit_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(target_commit="1" * 40)))

    def test_manifest_digest_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(manifest_sha256="1" * 64)))

    def test_plan_digest_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(migration_plan_digest="1" * 64)))

    def test_manual_set_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(manual_operations=[])))

    def test_fingerprint_mismatch_is_unusable(self):
        self.assertIsNone(self.check(authorization(trusted_plan_fingerprint="1" * 64)))

    def test_non_descendant_companion_fails_closed(self):
        with self.assertRaises(ReleaseError):
            load_central_migration_authorization(
                FakeRepository(authorization(), ancestry=False), "e" * 40,
                plan(migration_authorization="deploy/migration_authorizations/r0105.json"),
                manifest_sha256="c" * 64, migration_plan_digest=DIGEST,
                manual_operations=MANUAL,
            )

    def test_exact_central_authorization_bypasses_only_local_approval_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
            job_id = "11111111-1111-1111-1111-111111111111"
            store.accept(job_id, "r0105", "f" * 64)
            executor = Executor(config, store, CommandRunner())
            payload = {
                "nodes": {BASE: [], M1: [BASE], M2: [M1]}, "applied": [BASE],
                "plan": [migration_item(M1, [BASE]), {
                    **migration_item(M2, [M1]),
                    "operations": [{"operation": "RunPython", "classification": "manual", "detail": "outside allowlist"}],
                }],
                "conflicts": {}, "replacements": [], "release_id": "r0105",
                "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
                "migration_plan_digest": DIGEST, "manual_operations": MANUAL, "approval": None,
            }
            with (
                mock.patch("isadoraair_updater.executor.load_central_migration_authorization", return_value=authorization()),
                mock.patch.object(executor.approval_store, "find") as local_find,
            ):
                actual = executor._validate_target_schema(
                    plan(migration_authorization="deploy/migration_authorizations/r0105.json"),
                    payload, {"nodes": {BASE: []}, "applied": [BASE]}, job_id,
                    migration_already_started=False, trusted_tip="e" * 40,
                )
            local_find.assert_not_called()
            self.assertEqual(actual, (M1, M2))
            store.close()

    def test_protocol_six_fingerprint_is_identical_across_trust_boundary(self):
        protected_plan = plan(
            migration_authorization="deploy/migration_authorizations/r0105.json",
            migration_preflight_checks=("library.schedule_block_duplicate_times",),
        )
        values = {
            key: getattr(protected_plan, key)
            for key in (
                "installed_release_id", "installed_commit", "target_release_id", "target_commit",
                "releases_in_plan", "migrations_required", "migration_compatibility",
                "python_requirements_changed", "apt_packages_new", "systemd_units_changed",
                "systemd_units_new_required", "systemd_units_new_optional",
                "systemd_units_removed_or_renamed", "collectstatic_required",
                "services_requiring_restart", "nginx_changed", "runtime_components_changed",
                "minimum_updater_protocol_version", "manual_bootstrap_required",
            )
        }
        django_hash = migration_hardening_execution_fingerprint(
            execution_fingerprint_payload(**values),
            migration_authorization=protected_plan.migration_authorization,
            migration_preflight_checks=protected_plan.migration_preflight_checks,
        )
        self.assertEqual(django_hash, fingerprint(protected_plan.fingerprint_payload()))


class MigrationManifestContractTests(SimpleTestCase):
    def data(self, **changes):
        value = manifest("r0105", "r0104", migrations_required=[M2],
                         migration_compatibility="additive", minimum_updater_protocol_version=6)
        value.update(changes)
        return value

    def test_authorization_path_is_accepted(self):
        parsed = validate_manifest_dict(self.data(
            migration_authorization="deploy/migration_authorizations/r0105.json"))
        self.assertEqual(parsed.migration_authorization, "deploy/migration_authorizations/r0105.json")

    def test_wrong_authorization_path_is_rejected(self):
        with self.assertRaises(ManifestError):
            validate_manifest_dict(self.data(migration_authorization="deploy/migration_authorizations/r9999.json"))

    def test_new_fields_require_protocol_six(self):
        with self.assertRaises(ManifestError):
            validate_manifest_dict(self.data(
                minimum_updater_protocol_version=5,
                migration_preflight_checks=["library.schedule_block_duplicate_times"],
            ))

    def test_unknown_shaped_preflight_is_rejected(self):
        with self.assertRaises(ManifestError):
            validate_manifest_dict(self.data(migration_preflight_checks=["../../shell"]))

    def test_legacy_manifest_defaults_are_unchanged(self):
        parsed = validate_manifest_dict(manifest("r0105", "r0104"))
        self.assertIsNone(parsed.migration_authorization)
        self.assertEqual(parsed.migration_preflight_checks, ())


class PreflightContractTests(SimpleTestCase):
    def test_unknown_check_fails_closed_without_database_access(self):
        result = preflight.run_preflights(["unknown.check"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"][0]["status"], "unknown")

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_postgres_transaction_is_made_read_only(self, connection, transaction):
        connection.vendor = "postgresql"
        cursor = connection.cursor.return_value.__enter__.return_value
        preflight.CHECKS["test.pass"] = lambda value: {"ok": True, "count": 0}
        try:
            result = preflight.run_preflights(["test.pass"])
        finally:
            del preflight.CHECKS["test.pass"]
        cursor.execute.assert_called_once_with("SET TRANSACTION READ ONLY")
        self.assertEqual(result["status"], "ok")

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_failed_check_reports_exact_evidence(self, connection, transaction):
        connection.vendor = "postgresql"
        preflight.CHECKS["test.fail"] = lambda value: {"ok": False, "ids": [7, 9]}
        try:
            result = preflight.run_preflights(["test.fail"])
        finally:
            del preflight.CHECKS["test.fail"]
        self.assertEqual(result["checks"][0]["evidence"], {"ids": [7, 9]})

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_transaction_is_always_rolled_back(self, connection, transaction):
        connection.vendor = "postgresql"
        preflight.CHECKS["test.pass"] = lambda value: {"ok": True}
        try:
            preflight.run_preflights(["test.pass"])
        finally:
            del preflight.CHECKS["test.pass"]
        transaction.set_rollback.assert_called_once_with(True)

    def test_registry_contains_only_explicit_known_check(self):
        self.assertIn("library.schedule_block_duplicate_times", preflight.CHECKS)
        self.assertNotIn("shell", preflight.CHECKS)

    def test_executor_blocks_on_failed_structured_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
            executor = Executor(config, store, CommandRunner())
            raw = json.dumps({
                "schema_version": 1, "status": "failed", "checks": [{
                    "id": "library.schedule_block_duplicate_times", "status": "failed",
                    "evidence": {"offending_group_count": 1, "ids": [4, 8]},
                }],
            }).encode()
            with mock.patch.object(executor, "_run_app", return_value=(ProcessResult(("python",), 0, raw, b""), {})):
                with self.assertRaises(ExecutionError) as caught:
                    executor._run_migration_preflights(
                        root, ("library.schedule_block_duplicate_times",),
                    )
            self.assertEqual(caught.exception.classification, "MIGRATION_PREFLIGHT_BLOCKED")
            self.assertIn("4", caught.exception.detail)
            store.close()

    def test_preflight_call_precedes_migration_started_in_executor(self):
        source = (Path(__file__).parents[2] / "deploy/updater_runtime/isadoraair_updater/executor.py").read_text()
        execute_body = source[source.index("def execute(self, job_id") :]
        migration_block = execute_body[execute_body.index("if actual_migrations") :]
        self.assertLess(migration_block.index("_run_migration_preflights"), migration_block.index('"migration_started"'))

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_r0097_style_conflict_blocks_then_corrected_data_passes(self, connection, transaction):
        connection.vendor = "postgresql"
        outcomes = iter((
            {"ok": False, "offending_group_count": 1, "ids": [4, 8]},
            {"ok": True, "offending_group_count": 0, "ids": []},
        ))
        preflight.CHECKS["test.conflict"] = lambda value: next(outcomes)
        try:
            blocked = preflight.run_preflights(["test.conflict"])
            passed = preflight.run_preflights(["test.conflict"])
        finally:
            del preflight.CHECKS["test.conflict"]
        self.assertEqual(blocked["status"], "failed")
        self.assertEqual(passed["status"], "ok")


def migration_item(ref, dependencies):
    return {
        "ref": ref, "dependencies": dependencies,
        "migration_file_sha256": "0" * 64,
        "operations": [{"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}],
    }


class PartialPrefixRecoveryTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(config, self.store, CommandRunner())
        self.prior = "11111111-1111-1111-1111-111111111111"
        self.store.accept(self.prior, "r0105", "f" * 64)
        self.checkpoint = {
            "valid": True, "job_id": self.prior, "target_release_id": "r0105",
            "target_commit": "b" * 40, "installed_release_id": "r0104",
            "installed_commit": "a" * 40, "dump_file": "x.dump", "size_bytes": 1,
            "sha256": "9" * 64,
        }
        self.payload = {
            "nodes": {BASE: [], M1: [BASE], M2: [M1]},
            "applied": [BASE, M1], "plan": [migration_item(M2, [M1])],
            "conflicts": {}, "replacements": [], "manifest_sha256": "c" * 64,
            "release_id": "r0105", "target_commit": "b" * 40,
            "migration_plan_digest": "2" * 64, "manual_operations": [], "approval": None,
        }
        self.current = {"nodes": {BASE: []}, "applied": [BASE]}
        self.evidence = {
            "schema_version": 1, "classification": "UPDATER_OWNED_PARTIAL_PREFIX",
            "evidence_job_id": self.prior, "prior_job_id": None, "release_id": "r0105",
            "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
            "migration_plan_digest": DIGEST, "trusted_plan_fingerprint": "f" * 64,
            "ordered_target_plan": [M1, M2], "successful_prefix": [M1],
            "checkpoint": self.checkpoint, "failure_classification": "MIGRATION_FAILED",
            "failure_detail": "synthetic M2 failure", "continued_from_job_id": None,
            "authorization_source": "mechanical_additive", "finalized": True,
            "first_remaining_migration": M2, "exact_plan_authorization_matches": True,
            "permitted_action": "retry_same_exact_release",
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def install(self, **changes):
        evidence = {**self.evidence, **changes}
        self.store.update(self.prior, migration_recovery=evidence)
        self.store.fail(self.prior, "MIGRATION_FAILED", "synthetic", manual=True)

    def find(self):
        with mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=True):
            return self.executor._find_partial_recovery(plan(), self.payload, self.current)

    def test_exact_evidence_recognizes_prefix(self):
        self.install()
        self.assertEqual(self.find()["successful_prefix"], [M1])

    def test_no_prior_evidence_is_not_recoverable(self):
        self.assertIsNone(self.find())

    def test_other_release_is_blocked(self):
        self.install(release_id="r0106")
        self.assertIsNone(self.find())

    def test_other_target_commit_is_blocked(self):
        self.install(target_commit="1" * 40)
        self.assertIsNone(self.find())

    def test_other_manifest_is_blocked(self):
        self.install(manifest_sha256="1" * 64)
        self.assertIsNone(self.find())

    def test_other_fingerprint_is_blocked(self):
        self.install(trusted_plan_fingerprint="1" * 64)
        self.assertIsNone(self.find())

    def test_non_contiguous_prefix_is_blocked(self):
        self.install(successful_prefix=[M2])
        self.assertIsNone(self.find())

    def test_full_plan_is_not_a_partial_prefix(self):
        self.install(successful_prefix=[M1, M2])
        self.assertIsNone(self.find())

    def test_unfinalized_evidence_is_blocked(self):
        self.install(finalized=False)
        self.assertIsNone(self.find())

    def test_corrupt_schema_is_blocked(self):
        self.install(schema_version=2)
        self.assertIsNone(self.find())

    def test_invalid_checkpoint_is_blocked(self):
        self.install()
        with mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=False):
            self.assertIsNone(self.executor._find_partial_recovery(plan(), self.payload, self.current))

    def test_finalized_evidence_cannot_be_rewritten(self):
        self.install()
        with self.assertRaisesRegex(Exception, "immutable"):
            self.store.update(self.prior, migration_recovery={**self.evidence, "failure_detail": "changed"})

    def test_unproven_preapplied_transition_retains_hard_failure(self):
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(
                plan(), self.payload, self.current, self.prior,
                migration_already_started=False,
            )
        self.assertEqual(caught.exception.classification, "TARGET_MIGRATION_PREAPPLIED")


class SyntheticContinuationExecutionTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True,
        )
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(self.config, self.store, CommandRunner())
        self.plan = plan()
        self.stage_root = self.root / "stage-source"
        self.stage_root.mkdir()
        (self.stage_root / "manage.py").touch()
        self.staged = StagedSource(self.root / "stage", self.stage_root, self.root / "archive")
        self.checkpoint = {
            "schema_version": 1, "valid": True, "created_at": "2026-10-02T00:00:00+00:00",
            "job_id": "11111111-1111-1111-1111-111111111111",
            "installed_release_id": "r0104", "installed_commit": "a" * 40,
            "target_release_id": "r0105", "target_commit": "b" * 40,
            "dump_file": "checkpoint.dump", "size_bytes": 1, "sha256": "9" * 64,
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def payload(self, *, applied, remaining, recovery=False):
        result = {
            "schema_version": 1, "status": "ok",
            "nodes": {BASE: [], M1: [BASE], M2: [M1]},
            "applied": list(applied), "plan": [migration_item(ref, [BASE] if ref == M1 else [M1]) for ref in remaining],
            "conflicts": {}, "replacements": [], "release_id": "r0105",
            "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
            "migration_plan_digest": DIGEST, "manual_operations": [], "approval": None,
            "recovery_plan": None, "recovery_migration_plan_digest": None,
            "recovery_manual_operations": [],
        }
        if recovery:
            result["recovery_plan"] = [migration_item(M1, [BASE]), migration_item(M2, [M1])]
            result["recovery_migration_plan_digest"] = DIGEST
        return result

    def execute_with(self, job_id, *, current, probes, migrate_results):
        self.store.accept(job_id, "r0105", self.plan.fingerprint)
        with (
            mock.patch.object(self.executor, "_live_identity", side_effect=[{"head": "a" * 40}, {"head": "b" * 40}]),
            mock.patch.object(self.executor.repository, "fetch", return_value="e" * 40),
            mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan),
            mock.patch("isadoraair_updater.executor.manual_blockers", return_value=()),
            mock.patch.object(self.executor, "_validate_current_schema", return_value=current),
            mock.patch("isadoraair_updater.executor.materialize", return_value=self.staged),
            mock.patch("isadoraair_updater.executor.cleanup"),
            mock.patch.object(self.executor, "_probe", side_effect=probes),
            mock.patch("isadoraair_updater.executor.create_checkpoint", return_value=self.checkpoint),
            mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=True),
            mock.patch.object(self.executor, "_run_app", side_effect=[
                (ProcessResult(("python",), code, b"ok" if code == 0 else b"", b"boom" if code else b""), {})
                for code in migrate_results
            ]),
            mock.patch.object(self.executor, "_advance_source"),
            mock.patch.object(self.executor.systemd, "reconcile"),
            mock.patch.object(self.executor.systemd, "restart_declared"),
        ):
            return self.executor.execute(job_id)

    def test_m1_failure_at_m2_then_fresh_job_continues_only_m2(self):
        first = "11111111-1111-1111-1111-111111111111"
        failed = self.execute_with(
            first,
            current={"nodes": {BASE: []}, "applied": [BASE]},
            probes=[
                self.payload(applied=[BASE], remaining=[M1, M2]),
                self.payload(applied=[BASE, M1], remaining=[M2]),
                self.payload(applied=[BASE, M1], remaining=[M2]),
            ],
            migrate_results=[0, 1],
        )
        self.assertEqual(failed["state"], "manual_intervention_required")
        self.assertEqual(failed["migration_recovery"]["successful_prefix"], [M1])
        self.assertEqual(failed["migration_recovery"]["first_remaining_migration"], M2)
        self.assertTrue(failed["migration_recovery"]["finalized"])

        second = str(uuid.uuid4())
        succeeded = self.execute_with(
            second,
            current={"nodes": {BASE: []}, "applied": [BASE, M1]},
            probes=[
                self.payload(applied=[BASE, M1], remaining=[M2]),
                self.payload(applied=[BASE, M1], remaining=[M2], recovery=True),
                self.payload(applied=[BASE, M1, M2], remaining=[]),
                self.payload(applied=[BASE, M1, M2], remaining=[]),
                self.payload(applied=[BASE, M1, M2], remaining=[]),
            ],
            migrate_results=[0],
        )
        self.assertEqual(succeeded["state"], "succeeded")
        self.assertEqual(succeeded["migration_recovery"]["successful_prefix"], [M1, M2])
        self.assertIsNone(succeeded["migration_recovery"]["first_remaining_migration"])
        self.assertEqual(succeeded["migration_recovery"]["permitted_action"], "none")
        self.assertEqual(succeeded["migration_recovery"]["prior_job_id"], first)
        self.assertIn("database_verified", succeeded["milestones"])
        self.assertIn("source_advanced", succeeded["milestones"])
