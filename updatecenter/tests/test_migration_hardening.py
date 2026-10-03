"""P1 1.17 -- protocol-6 migration hardening: contracts, fleet authorization,
and partial-prefix recovery through the REAL protected executor.

Recovery tests drive Executor.execute() itself. Only process boundaries are
replaced: the application command runner, the probe, Git, staging, and the
database recorder -- which is a stateful FakeRecorder whose rows change
exactly when a (fake) migrate command commits, so crash/resume/retry
behavior is exercised against one consistent database story.
Real-PostgreSQL coverage lives in test_migration_hardening_postgres.py.
"""
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
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.staging import StagedSource
from isadoraair_updater.release import (
    ReleaseError, TrustedPlan, fingerprint, load_central_migration_authorization,
)

BASE = "base.0001_initial"
M1 = "sample.0001_first"
M2 = "sample.0002_second"
FULL_DIGEST = "d" * 64
SUFFIX_DIGEST = "3" * 64
AUTH_PATH = "deploy/migration_authorizations/r0105.json"


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


def migration_item(ref, dependencies, *, file_sha="0" * 64, operations=None):
    return {
        "ref": ref, "dependencies": dependencies, "migration_file_sha256": file_sha,
        "operations": operations or [
            {"operation": "CreateModel", "classification": "additive", "detail": "new table/model"},
        ],
    }


# ---------------------------------------------------------------------------
# B2 -- target-side central authorization (fleet / skipped releases)
# ---------------------------------------------------------------------------

SHA_A, SHA_B, SHA_C, SHA_D = "a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32
MA, MB, MC, MD = "app.0101_a", "app.0102_b", "app.0103_c", "app.0104_d"
RUN_PYTHON = {"operation": "RunPython", "classification": "manual", "detail": "outside allowlist"}
RUN_SQL = {"operation": "RunSQL", "classification": "manual", "detail": "outside allowlist"}
OP_A = {"ref": MA, "migration_file_sha256": SHA_A, "operation_index": 0, "operation": "RunPython", "classification": "manual"}
OP_B = {"ref": MB, "migration_file_sha256": SHA_B, "operation_index": 0, "operation": "RunSQL", "classification": "manual"}
OP_C = {"ref": MC, "migration_file_sha256": SHA_C, "operation_index": 1, "operation": "RunPython", "classification": "manual"}


class FakeRepository:
    """Git boundary only. The companion's provenance/immutability rules are
    applied by the real load_central_migration_authorization()."""

    def __init__(self, record, *, introducing="c" * 40, ancestry=True):
        self.raw = record if isinstance(record, bytes) else json.dumps(record).encode()
        self.introducing = introducing
        self.ancestry = ancestry

    def read_file(self, tip, path, maximum=65536):
        return self.raw

    def introducing_commit(self, path, tip):
        return self.introducing

    def is_ancestor(self, before, after):
        return self.ancestry


def central_record(operations=(OP_A, OP_B, OP_C), **changes):
    value = {
        "schema_version": 1, "release_id": "r0105", "target_commit": "b" * 40,
        "manifest_sha256": "c" * 64, "authorized_manual_operations": [dict(op) for op in operations],
    }
    value.update(changes)
    return value


def station_plan(*migrations):
    """A station's own derived plan: [(ref, file_sha, operations)]."""
    items, manual = [], []
    for ref, sha, operations in migrations:
        items.append(migration_item(ref, [], file_sha=sha, operations=operations))
        for index, operation in enumerate(operations):
            if operation["classification"] != "additive":
                manual.append({"ref": ref, "operation_index": index, **operation})
    return items, manual


ADDITIVE = {"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}
STATION_A = station_plan((MA, SHA_A, [RUN_PYTHON]), (MB, SHA_B, [RUN_SQL]))                       # r0104 -> target
STATION_B = station_plan((MA, SHA_A, [RUN_PYTHON]), (MB, SHA_B, [RUN_SQL]), (MC, SHA_C, [ADDITIVE, RUN_PYTHON]))  # r0103 -> target
STATION_C = station_plan((MA, SHA_A, [RUN_PYTHON]))                                               # another baseline


class TargetSideCentralAuthorizationTests(SimpleTestCase):
    def check(self, station, record=None, *, repository=None, **plan_changes):
        items, manual = station
        return load_central_migration_authorization(
            repository or FakeRepository(record or central_record()), "e" * 40,
            plan(migration_authorization=AUTH_PATH, **plan_changes),
            manifest_sha256="c" * 64, plan_items=items, manual_operations=manual,
        )

    def test_f1_station_subset_of_authorized_set_is_approved(self):
        self.assertIsNotNone(self.check(STATION_A))

    def test_f2_skipped_release_baseline_with_every_authorized_op_is_approved(self):
        self.assertIsNotNone(self.check(
            STATION_B, installed_release_id="r0103", installed_commit="9" * 40,
            releases_in_plan=("r0104", "r0105"), fingerprint="1" * 64,
        ))

    def test_f3_other_baseline_with_single_op_is_approved(self):
        self.assertIsNotNone(self.check(STATION_C, installed_release_id="r0102", fingerprint="2" * 64))

    def test_f4_unauthorized_manual_op_is_not_covered(self):
        station = station_plan((MA, SHA_A, [RUN_PYTHON]), (MD, SHA_D, [RUN_PYTHON]))
        self.assertIsNone(self.check(station))

    def test_f5_same_ref_and_index_but_changed_file_bytes_is_rejected(self):
        station = station_plan((MA, "ee" * 32, [RUN_PYTHON]))
        self.assertIsNone(self.check(station))

    def test_f6_same_operation_at_different_index_is_rejected(self):
        station = station_plan((MA, SHA_A, [ADDITIVE, RUN_PYTHON]))
        self.assertIsNone(self.check(station))

    def test_different_operation_type_at_same_position_is_rejected(self):
        station = station_plan((MA, SHA_A, [RUN_SQL]))
        self.assertIsNone(self.check(station))

    def test_release_target_and_manifest_identity_must_match(self):
        for changes in ({"release_id": "r0106"}, {"target_commit": "1" * 40}, {"manifest_sha256": "1" * 64}):
            with self.subTest(**changes):
                self.assertIsNone(self.check(STATION_A, central_record(**changes)))

    def test_no_manual_operations_needs_no_central_authorization(self):
        self.assertIsNone(self.check(station_plan((MA, SHA_A, [ADDITIVE]))))

    def test_companion_without_unique_unmodified_introduction_is_unusable(self):
        self.assertIsNone(self.check(STATION_A, repository=FakeRepository(central_record(), introducing=None)))

    def test_non_descendant_companion_fails_closed(self):
        with self.assertRaises(ReleaseError):
            self.check(STATION_A, repository=FakeRepository(central_record(), ancestry=False))

    def test_companion_in_the_target_commit_itself_fails_closed(self):
        with self.assertRaises(ReleaseError):
            self.check(STATION_A, repository=FakeRepository(central_record(), introducing="b" * 40))

    def test_malformed_records_fail_closed(self):
        cases = {
            "legacy baseline-bound schema": {
                "schema_version": 1, "release_id": "r0105", "target_commit": "b" * 40,
                "manifest_sha256": "c" * 64, "migration_plan_digest": FULL_DIGEST,
                "manual_operations": [], "trusted_plan_fingerprint": "f" * 64,
            },
            "duplicate operation": central_record(operations=(OP_A, OP_A)),
            "additive classification": central_record(operations=({**OP_A, "classification": "additive"},)),
            "boolean index": central_record(operations=({**OP_A, "operation_index": True},)),
            "extra key": central_record(operations=({**OP_A, "detail": "x"},)),
            "empty set": central_record(operations=()),
            "bad file sha": central_record(operations=({**OP_A, "migration_file_sha256": "x"},)),
        }
        for label, record in cases.items():
            with self.subTest(label), self.assertRaises(ReleaseError):
                self.check(STATION_A, record)

    def test_one_authorization_serves_two_different_baselines_through_the_executor(self):
        """Executor-level fleet proof: KOGR-style r0104 and WRJE-style r0103
        stations have different plans and fingerprints; the same companion
        satisfies both without consulting local approval."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
            executor = Executor(config, store, CommandRunner())
            executor.repository = FakeRepository(central_record())
            stations = (
                ("r0104", "a" * 40, ("r0105",), "f" * 64, STATION_A),
                ("r0103", "9" * 40, ("r0104", "r0105"), "1" * 64, STATION_B),
            )
            try:
                for installed, installed_commit, releases, plan_fp, (items, manual) in stations:
                    with self.subTest(installed=installed):
                        job_id = str(uuid.uuid4())
                        store.accept(job_id, "r0105", plan_fp)
                        refs = [item["ref"] for item in items]
                        nodes = {ref: [] for ref in refs}
                        payload = {
                            "nodes": nodes, "applied": [], "plan": items, "conflicts": {}, "replacements": [],
                            "release_id": "r0105", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
                            "migration_plan_digest": "7" * 64, "manual_operations": manual, "approval": None,
                        }
                        trusted = plan(
                            installed_release_id=installed, installed_commit=installed_commit,
                            releases_in_plan=releases, migrations_required=tuple(refs),
                            fingerprint=plan_fp, migration_authorization=AUTH_PATH,
                        )
                        with mock.patch.object(executor.approval_store, "find") as local_find:
                            actual = executor._validate_target_schema(
                                trusted, payload, {"nodes": {}, "applied": []}, job_id,
                                migration_already_started=False, trusted_tip="e" * 40,
                            )
                        local_find.assert_not_called()
                        self.assertEqual(actual, tuple(refs))
                        self.assertEqual(executor.last_authorization_source, "central")
                        store.fail(job_id, "TEST_DONE", "validation-only fixture job", manual=False)
            finally:
                store.close()

    def test_uncovered_plan_falls_back_to_exact_local_approval_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
            executor = Executor(config, store, CommandRunner())
            executor.repository = FakeRepository(central_record())
            job_id = str(uuid.uuid4())
            store.accept(job_id, "r0105", "f" * 64)
            items, manual = station_plan((MA, SHA_A, [RUN_PYTHON]), (MD, SHA_D, [RUN_PYTHON]))
            payload = {
                "nodes": {MA: [], MD: []}, "applied": [], "plan": items, "conflicts": {}, "replacements": [],
                "release_id": "r0105", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
                "migration_plan_digest": "7" * 64, "manual_operations": manual, "approval": None,
            }
            try:
                with mock.patch.object(executor.approval_store, "find", return_value=None) as local_find:
                    with self.assertRaises(ExecutionError) as caught:
                        executor._validate_target_schema(
                            plan(migrations_required=(MA, MD), migration_authorization=AUTH_PATH),
                            payload, {"nodes": {}, "applied": []}, job_id,
                            migration_already_started=False, trusted_tip="e" * 40,
                        )
                local_find.assert_called_once()
                self.assertEqual(caught.exception.classification, "MIGRATION_OPERATION_MANUAL")
                self.assertIsNotNone(caught.exception.migration_plan_review)
            finally:
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



# ---------------------------------------------------------------------------
# B1 / S3 -- partial-prefix recovery through the real executor
# ---------------------------------------------------------------------------

class _Crash(BaseException):
    """SIGKILL / power loss: escapes every except clause in the worker."""


class FakeRecorder:
    """One consistent stand-in for the station database's django_migrations."""

    def __init__(self):
        self.rows = {}
        self._next = 40

    def apply(self, ref):
        self._next += 1
        self.rows[ref] = {"id": self._next, "applied": f"2026-10-03T12:00:{self._next % 60:02d}.{self._next:06d}Z"}

    def unapply(self, ref):
        del self.rows[ref]

    def observe(self, refs):
        return {ref: dict(self.rows[ref]) for ref in refs if ref in self.rows}


class PartialPrefixRecoveryExecutionTests(SimpleTestCase):
    """M1 -> M2 transition from r0104 to r0105 (M2 depends on M1)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True,
        )
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(self.config, self.store, CommandRunner())
        self.plan = plan()
        self.recorder = FakeRecorder()
        self.head = "a" * 40
        self.migrate_calls = []
        stage_root = self.root / "stage-source"
        stage_root.mkdir()
        (stage_root / "manage.py").touch()
        self.staged = StagedSource(self.root / "stage", stage_root, self.root / "archive")
        self.checkpoint = {
            "schema_version": 1, "valid": True, "created_at": "2026-10-03T00:00:00+00:00",
            "job_id": "11111111-1111-1111-1111-111111111111",
            "installed_release_id": "r0104", "installed_commit": "a" * 40,
            "target_release_id": "r0105", "target_commit": "b" * 40,
            "dump_file": "checkpoint.dump", "size_bytes": 1, "sha256": "9" * 64,
        }

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    # -- process boundaries ------------------------------------------------
    def _applied(self):
        return [BASE, *[ref for ref in (M1, M2) if ref in self.recorder.rows]]

    def _probe(self, source, *, release_id=None, target_commit=None, recovery_plan_refs=()):
        remaining = [ref for ref in (M1, M2) if ref not in self.recorder.rows]
        payload = {
            "schema_version": 1, "status": "ok",
            "nodes": {BASE: [], M1: [BASE], M2: [M1]}, "applied": self._applied(),
            "plan": [migration_item(ref, [BASE] if ref == M1 else [M1]) for ref in remaining],
            "conflicts": {}, "replacements": [],
            "release_id": release_id, "target_commit": target_commit,
            "manifest_sha256": "c" * 64 if release_id else None,
            # A REAL forward probe digests only the remaining plan.
            "migration_plan_digest": (FULL_DIGEST if remaining == [M1, M2] else SUFFIX_DIGEST) if release_id else None,
            "manual_operations": [], "approval": None,
        }
        if recovery_plan_refs:
            payload["recovery_plan"] = [
                migration_item(ref, [BASE] if ref == M1 else [M1]) for ref in recovery_plan_refs
            ]
            payload["recovery_migration_plan_digest"] = FULL_DIGEST if list(recovery_plan_refs) == [M1, M2] else "8" * 64
            payload["recovery_manual_operations"] = []
        return payload

    def _current(self):
        return {"nodes": {BASE: []}, "applied": self._applied()}

    def run_job(self, job_id, *, outcomes=None, accept=True):
        """outcomes: ref -> "ok" | "fail" | "crash_after_commit" | "oserror"."""
        outcomes = outcomes or {}
        if accept:
            self.store.accept(job_id, "r0105", self.plan.fingerprint)

        def run_app(source, arguments, *, timeout):
            if arguments[0] != "migrate":
                raise AssertionError(f"unexpected application command {arguments!r}")
            ref = f"{arguments[1]}.{arguments[2]}"
            self.migrate_calls.append(ref)
            outcome = outcomes.get(ref, "ok")
            if outcome == "oserror":
                raise OSError(24, "Too many open files")
            if outcome in {"ok", "crash_after_commit"}:
                self.recorder.apply(ref)
            if outcome == "crash_after_commit":
                raise _Crash()
            return ProcessResult(tuple(arguments), 0 if outcome == "ok" else 1, b"", b"" if outcome == "ok" else b"boom"), {}

        def advance(_plan):
            self.head = "b" * 40

        with (
            mock.patch.object(self.executor, "_live_identity", side_effect=lambda: {"head": self.head}),
            mock.patch.object(self.executor.repository, "fetch", return_value="e" * 40),
            mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan),
            mock.patch("isadoraair_updater.executor.manual_blockers", return_value=()),
            mock.patch.object(self.executor, "_validate_current_schema", side_effect=self._current),
            mock.patch("isadoraair_updater.executor.materialize", return_value=self.staged),
            mock.patch("isadoraair_updater.executor.cleanup"),
            mock.patch.object(self.executor, "_probe", side_effect=self._probe),
            mock.patch.object(self.executor, "_observe_migration_records", side_effect=self.recorder.observe),
            mock.patch("isadoraair_updater.executor.create_checkpoint", return_value=self.checkpoint),
            mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=True),
            mock.patch.object(self.executor, "_run_app", side_effect=run_app),
            mock.patch.object(self.executor, "_advance_source", side_effect=advance),
            mock.patch.object(self.executor.systemd, "reconcile"),
            mock.patch.object(self.executor.systemd, "restart_declared"),
        ):
            return self.executor.execute(job_id)

    def fresh_retry_succeeds_with_only(self, expected_calls, prior_job):
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["state"], "succeeded", retry.get("failure_detail"))
        self.assertEqual(self.migrate_calls, expected_calls)
        evidence = retry["migration_recovery"]
        self.assertEqual(evidence["prior_job_id"], prior_job)
        self.assertEqual(evidence["successful_prefix"], [M1, M2])
        self.assertEqual(evidence["permitted_action"], "none")
        self.assertEqual(set(evidence["prefix_records"]), {M1, M2})
        self.assertEqual(evidence["prefix_records"][M1], self.recorder.rows[M1])
        self.assertIn("database_verified", retry["milestones"])
        self.assertIn("source_advanced", retry["milestones"])
        self.assertLess(retry["milestones"].index("database_verified"), retry["milestones"].index("source_advanced"))
        return retry

    # -- crash windows -----------------------------------------------------
    def test_h1_m1_commits_then_hard_kill_then_resume_then_fresh_retry_runs_only_m2(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        crashed = self.store.load(job)
        self.assertEqual(crashed["state"], "running")
        self.assertEqual(crashed["migration_recovery"]["successful_prefix"], [])   # evidence write never happened
        self.assertIn(M1, self.recorder.rows)                                      # but M1 committed

        resumed = self.run_job(job, accept=False)          # daemon restart resumes the same job
        self.assertEqual(resumed["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")
        evidence = resumed["migration_recovery"]
        self.assertTrue(evidence["finalized"])
        self.assertEqual(evidence["successful_prefix"], [M1])
        self.assertEqual(evidence["prefix_records"], {M1: self.recorder.rows[M1]})
        self.assertEqual(evidence["first_remaining_migration"], M2)
        self.assertEqual(evidence["permitted_action"], "retry_same_exact_release")
        self.assertEqual(self.migrate_calls, [M1])         # the resumed job ran nothing

        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_h1_crash_after_last_migration_leaves_a_complete_prefix_that_finishes_without_migrating(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        resumed = self.run_job(job, accept=False)
        evidence = resumed["migration_recovery"]
        self.assertEqual(evidence["successful_prefix"], [M1, M2])
        self.assertIsNone(evidence["first_remaining_migration"])
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["state"], "succeeded", retry.get("failure_detail"))
        self.assertEqual(self.migrate_calls, [])
        self.assertIn("source_advanced", retry["milestones"])

    def test_h2_oserror_invoking_m2_after_durable_m1_then_fresh_retry_runs_only_m2(self):
        job = str(uuid.uuid4())
        failed = self.run_job(job, outcomes={M2: "oserror"})
        self.assertEqual(failed["failure_classification"], "SAFE_EXECUTION_FAILURE")
        evidence = failed["migration_recovery"]
        self.assertTrue(evidence["finalized"])
        self.assertEqual(evidence["successful_prefix"], [M1])
        self.assertEqual(evidence["failure_classification"], "SAFE_EXECUTION_FAILURE")
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_ordinary_m2_failure_then_fresh_retry_runs_only_m2(self):
        job = str(uuid.uuid4())
        failed = self.run_job(job, outcomes={M2: "fail"})
        self.assertEqual(failed["failure_classification"], "MIGRATION_FAILED")
        self.assertEqual(failed["migration_recovery"]["successful_prefix"], [M1])
        self.assertTrue(failed["migration_recovery"]["finalized"])
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_h1_repeated_five_times_is_deterministic(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                self.recorder = FakeRecorder()
                self.head = "a" * 40
                self.migrate_calls = []
                job = str(uuid.uuid4())
                with self.assertRaises(_Crash):
                    self.run_job(job, outcomes={M1: "crash_after_commit"})
                self.run_job(job, accept=False)
                self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    # -- ownership must not be inferred -------------------------------------
    def _failed_after_m1(self):
        job = str(uuid.uuid4())
        self.run_job(job, outcomes={M2: "fail"})
        return job

    def test_s3_reapplied_m1_with_new_row_identity_is_not_updater_owned(self):
        self._failed_after_m1()
        self.recorder.unapply(M1)
        self.recorder.apply(M1)                     # same name, new id/applied
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["failure_classification"], "TARGET_MIGRATION_PREAPPLIED")
        self.assertEqual(self.migrate_calls, [])

    def test_s3_rewritten_applied_timestamp_is_not_updater_owned(self):
        self._failed_after_m1()
        self.recorder.rows[M1]["applied"] = "2030-01-01T00:00:00.000000Z"
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["failure_classification"], "TARGET_MIGRATION_PREAPPLIED")

    def test_manually_applied_remaining_migration_is_not_updater_owned(self):
        self._failed_after_m1()
        self.recorder.apply(M2)                     # operator ran `migrate` by hand
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["failure_classification"], "TARGET_MIGRATION_PREAPPLIED")
        self.assertEqual(self.migrate_calls, [])

    def test_unfinalized_evidence_is_never_accepted(self):
        job = self._failed_after_m1()
        state = self.store.load(job)
        evidence = dict(state["migration_recovery"])
        # Simulate legacy/stranded evidence that never reached finalization.
        path = self.config.jobs_root / f"{job}.json"
        raw = json.loads(path.read_text())
        raw["migration_recovery"] = {**evidence, "finalized": False}
        path.write_text(json.dumps(raw))
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["failure_classification"], "TARGET_MIGRATION_PREAPPLIED")

    def test_other_identity_fields_block_recovery(self):
        cases = {
            "other release": {"release_id": "r0106"},
            "other target": {"target_commit": "1" * 40},
            "other manifest": {"manifest_sha256": "1" * 64},
            "other fingerprint": {"trusted_plan_fingerprint": "1" * 64},
            "noncontiguous prefix": {"successful_prefix": [M2], "prefix_records": {}},
            "missing records": {"prefix_records": {}},
            "foreign evidence job": {"evidence_job_id": "22222222-2222-2222-2222-222222222222"},
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.recorder = FakeRecorder()
                self.head = "a" * 40
                job = self._failed_after_m1()
                path = self.config.jobs_root / f"{job}.json"
                raw = json.loads(path.read_text())
                raw["migration_recovery"] = {**raw["migration_recovery"], **change}
                path.write_text(json.dumps(raw))
                retry = self.run_job(str(uuid.uuid4()))
                self.assertEqual(retry["failure_classification"], "TARGET_MIGRATION_PREAPPLIED")
                # mark the retry terminal so the next subTest starts clean
                for state in self.store.list_states():
                    self.assertNotEqual(state["state"], "running")

    # -- finalize_from_observation fails closed -------------------------------
    def _unfinalized_after_crash(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        return job

    def test_finalization_refuses_noncontiguous_observation(self):
        job = self._unfinalized_after_crash()
        self.recorder.unapply(M1)
        self.recorder.apply(M2)                     # M2 applied without M1
        resumed = self.run_job(job, accept=False)
        self.assertFalse(resumed["migration_recovery"]["finalized"])

    def test_finalization_refuses_when_recorded_row_changed(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        # M1's row identity was durably recorded before the crash on M2.
        self.assertEqual(self.store.load(job)["migration_recovery"]["successful_prefix"], [M1])
        self.recorder.unapply(M1)
        self.recorder.apply(M1)
        resumed = self.run_job(job, accept=False)
        self.assertFalse(resumed["migration_recovery"]["finalized"])

    def test_finalization_refuses_corrupt_identity(self):
        job = self._unfinalized_after_crash()
        path = self.config.jobs_root / f"{job}.json"
        raw = json.loads(path.read_text())
        raw["migration_recovery"]["release_id"] = "r0999"
        path.write_text(json.dumps(raw))
        resumed = self.run_job(job, accept=False)
        self.assertFalse(resumed["migration_recovery"]["finalized"])

    def test_finalization_never_masks_the_original_failure(self):
        job = self._unfinalized_after_crash()
        with mock.patch.object(FakeRecorder, "observe", side_effect=RuntimeError("psql unavailable")):
            resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")
        self.assertFalse(resumed["migration_recovery"]["finalized"])

    def test_finalized_evidence_cannot_be_rewritten(self):
        job = self._failed_after_m1()
        with self.assertRaises(Exception):
            self.store.update(job, migration_recovery={**self.store.load(job)["migration_recovery"], "successful_prefix": []})

    def test_recorded_authorization_source_is_the_real_one(self):
        job = self._failed_after_m1()
        self.assertEqual(self.store.load(job)["migration_recovery"]["authorization_source"], "not_required")
        self.assertNotIn("exact_plan_authorization_matches", self.store.load(job)["migration_recovery"])


class ProbeContextShapeTests(SimpleTestCase):
    """Runtime 11 requires exactly the shape the caller requested."""

    def payload(self, *, recovery=False):
        value = {
            "schema_version": 1, "status": "ok", "nodes": {BASE: [], M1: [BASE]}, "applied": [BASE],
            "plan": [migration_item(M1, [BASE])], "conflicts": {}, "replacements": [],
            "release_id": "r0105", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
            "migration_plan_digest": FULL_DIGEST, "manual_operations": [], "approval": None,
        }
        if recovery:
            value.update(recovery_plan=[migration_item(M1, [BASE])], recovery_migration_plan_digest=FULL_DIGEST,
                         recovery_manual_operations=[])
        return json.dumps(value).encode()

    def test_ordinary_review_probe_is_exactly_thirteen_keys(self):
        from isadoraair_updater.executor import _REVIEW_PROBE_KEYS, _strict_probe
        self.assertEqual(set(_strict_probe(self.payload(), review_context=True)), _REVIEW_PROBE_KEYS)
        with self.assertRaises(ExecutionError):
            _strict_probe(self.payload(recovery=True), review_context=True)

    def test_recovery_context_requires_recovery_shape(self):
        from isadoraair_updater.executor import _strict_probe
        self.assertIn("recovery_plan", _strict_probe(self.payload(recovery=True), review_context=True, recovery_context=True))
        with self.assertRaises(ExecutionError):
            _strict_probe(self.payload(), review_context=True, recovery_context=True)
