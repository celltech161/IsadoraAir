"""P1 1.17 -- migration hardening through the REAL protected executor.

* central authorization through discovered trusted companions (executor gate);
* the target-code preflight registry contract;
* partial-prefix recovery: crash windows, the in-flight bound, the final
  owned-prefix assertions and their races, and PostgreSQL-not-ready recovery.

Recovery tests drive Executor.execute() itself. Only process boundaries are
replaced: the application command runner, the probe, Git, staging, and the
database recorder -- a stateful FakeRecorder whose rows change exactly when
a (fake) migrate command commits or a test simulates an outside actor.
Companion trust rules: test_migration_authorization_companions.py.
Real-PostgreSQL coverage: test_migration_hardening_postgres.py.
Runtime-10 bootstrap: test_runtime11_bootstrap.py.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import mock
import uuid

from django.test import SimpleTestCase

from updatecenter.management.commands import updatecenter_migration_preflight as preflight

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import OBSERVATION_READINESS_DELAYS, ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import ReleaseError, TrustedPlan
from isadoraair_updater.staging import StagedSource

BASE = "base.0001_initial"
M1 = "sample.0001_first"
M2 = "sample.0002_second"
FULL_DIGEST = "d" * 64
SUFFIX_DIGEST = "3" * 64


def plan(**changes):
    values = dict(
        installed_release_id="r0104", installed_commit="a" * 40,
        target_release_id="r0105", target_commit="b" * 40,
        releases_in_plan=("r0105",), migrations_required=(M2,),
        migration_compatibility="additive", python_requirements_changed=False,
        apt_packages_new=(), systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(), nginx_changed=False,
        runtime_components_changed=False, minimum_updater_protocol_version=5,
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


def make_executor(root: Path):
    config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
    store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
    return config, store, Executor(config, store, CommandRunner())


# ---------------------------------------------------------------------------
# Central authorization through discovered companions (executor gate)
# ---------------------------------------------------------------------------

SHA_A, SHA_B, SHA_C = "a1" * 32, "b2" * 32, "c3" * 32
MA, MB, MC, MD = "app.0101_a", "app.0102_b", "app.0103_c", "app.0104_d"
ID_A = (MA, SHA_A, 0, "RunPython", "manual")
ID_B = (MB, SHA_B, 0, "RunSQL", "manual")
ID_C = (MC, SHA_C, 1, "RunPython", "manual")
RUN_PYTHON = {"operation": "RunPython", "classification": "manual", "detail": "outside allowlist"}
RUN_SQL = {"operation": "RunSQL", "classification": "manual", "detail": "outside allowlist"}
ADDITIVE = {"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}


def station_payload(*migrations):
    items, manual = [], []
    for ref, sha, operations in migrations:
        items.append(migration_item(ref, [], file_sha=sha, operations=operations))
        for index, operation in enumerate(operations):
            if operation["classification"] != "additive":
                manual.append({"ref": ref, "operation_index": index, **operation})
    return {
        "nodes": {item["ref"]: [] for item in items}, "applied": [], "plan": items,
        "conflicts": {}, "replacements": [], "release_id": "r0107", "target_commit": "b" * 40,
        "manifest_sha256": "c" * 64, "migration_plan_digest": "7" * 64,
        "manual_operations": manual, "approval": None,
    }


class CentralAuthorizationGateTests(SimpleTestCase):
    """One release-local companion set {A,B,C} serves every baseline."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config, self.store, self.executor = make_executor(Path(self.temp.name))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def validate(self, payload, *, installed, releases, fingerprint, authorizations):
        job_id = str(uuid.uuid4())
        self.store.accept(job_id, "r0107", fingerprint)
        trusted = plan(
            installed_release_id=installed, releases_in_plan=releases, fingerprint=fingerprint,
            target_release_id="r0107", migrations_required=tuple(item["ref"] for item in payload["plan"]),
        )
        try:
            with (
                mock.patch("isadoraair_updater.executor.load_plan_authorizations", return_value=authorizations) as loader,
                mock.patch.object(self.executor.approval_store, "find", return_value=None) as local_find,
            ):
                try:
                    return self.executor._validate_target_schema(
                        trusted, payload, {"nodes": {}, "applied": []}, job_id,
                        migration_already_started=False, trusted_tip="e" * 40,
                    ), local_find, loader
                except ExecutionError as exc:
                    return exc, local_find, loader
        finally:
            self.store.fail(job_id, "TEST_DONE", "validation-only fixture job", manual=False)

    def test_two_baselines_and_a_skipped_path_are_authorized_by_the_same_companions(self):
        authorizations = {"r0106": frozenset({ID_A, ID_C}), "r0107": frozenset({ID_B})}
        stations = (
            ("r0106", ("r0107",), "1" * 64, station_payload((MB, SHA_B, [RUN_SQL]))),
            ("r0105", ("r0106", "r0107"), "2" * 64,
             station_payload((MA, SHA_A, [RUN_PYTHON]), (MB, SHA_B, [RUN_SQL]), (MC, SHA_C, [ADDITIVE, RUN_PYTHON]))),
            ("r0104", ("r0105", "r0106", "r0107"), "3" * 64, station_payload((MA, SHA_A, [RUN_PYTHON]))),
        )
        for installed, releases, fp, payload in stations:
            with self.subTest(installed=installed):
                result, local_find, loader = self.validate(
                    payload, installed=installed, releases=releases, fingerprint=fp, authorizations=authorizations,
                )
                self.assertIsInstance(result, tuple)
                self.assertEqual(self.executor.last_authorization_source, "central")
                local_find.assert_not_called()
                self.assertEqual(loader.call_args.args[2].releases_in_plan, releases)

    def test_uncovered_operation_falls_back_to_exact_local_approval(self):
        payload = station_payload((MA, SHA_A, [RUN_PYTHON]), (MD, "d4" * 32, [RUN_PYTHON]))
        result, local_find, _loader = self.validate(
            payload, installed="r0104", releases=("r0105",), fingerprint="4" * 64,
            authorizations={"r0105": frozenset({ID_A})},
        )
        local_find.assert_called_once()
        self.assertEqual(result.classification, "MIGRATION_OPERATION_MANUAL")
        self.assertIsNotNone(result.migration_plan_review)

    def test_no_manual_operations_never_consults_companions(self):
        result, local_find, loader = self.validate(
            station_payload((MA, SHA_A, [ADDITIVE])), installed="r0104", releases=("r0105",),
            fingerprint="5" * 64, authorizations={},
        )
        loader.assert_not_called()
        local_find.assert_not_called()
        self.assertEqual(self.executor.last_authorization_source, "not_required")

    def test_bad_companion_fails_closed(self):
        job_id = str(uuid.uuid4())
        self.store.accept(job_id, "r0107", "6" * 64)
        with mock.patch("isadoraair_updater.executor.load_plan_authorizations",
                        side_effect=ReleaseError("companion was modified after introduction")):
            with self.assertRaises(ReleaseError):
                self.executor._validate_target_schema(
                    plan(fingerprint="6" * 64, migrations_required=(MA,)),
                    station_payload((MA, SHA_A, [RUN_PYTHON])), {"nodes": {}, "applied": []}, job_id,
                    migration_already_started=False, trusted_tip="e" * 40,
                )


# ---------------------------------------------------------------------------
# Target-code preflight registry contract
# ---------------------------------------------------------------------------

class PreflightRegistryContractTests(SimpleTestCase):
    def test_registry_is_explicit_and_keyed_by_pending_migration(self):
        self.assertEqual(set(preflight.REGISTRY), {preflight.M0087, preflight.M0088})

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_unregistered_pending_refs_run_no_checks_inside_a_read_only_transaction(self, connection, transaction):
        connection.vendor = "postgresql"
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = []
        result = preflight.run_preflights(["sample.0001_first"])
        self.assertEqual(result, {"schema_version": 1, "status": "ok", "checks": []})
        self.assertEqual(cursor.execute.call_args_list[0].args, ("SET TRANSACTION READ ONLY",))
        transaction.set_rollback.assert_called_once_with(True)

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_a_registered_failure_reports_migration_and_evidence(self, connection, transaction):
        connection.vendor = "postgresql"
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        with mock.patch.dict(preflight.REGISTRY, {"x.0001_y": [("x.check", lambda cursor, pending: {"ok": False, "ids": [4]})]}):
            result = preflight.run_preflights(["x.0001_y"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"], [{"id": "x.check", "migration": "x.0001_y", "status": "failed",
                                             "evidence": {"ids": [4]}}])

    @mock.patch.object(preflight, "transaction")
    @mock.patch.object(preflight, "connection")
    def test_a_pending_ref_that_is_already_applied_fails_closed(self, connection, transaction):
        connection.vendor = "postgresql"
        connection.cursor.return_value.__enter__.return_value.fetchall.return_value = [("library", "0087_backfill_default_schedule_profile")]
        result = preflight.run_preflights([preflight.M0087])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"][0]["id"], "updater.pending_stage_consistency")

    def _executor_with(self, payload):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        _config, store, executor = make_executor(Path(temp.name))
        self.addCleanup(store.close)
        raw = json.dumps(payload).encode()
        patcher = mock.patch.object(executor, "_run_app", return_value=(ProcessResult(("python",), 0, raw, b""), {}))
        self.run_app = patcher.start()
        self.addCleanup(patcher.stop)
        return executor

    def test_executor_passes_trusted_pending_refs_and_blocks_on_failure(self):
        executor = self._executor_with({"schema_version": 1, "status": "failed", "checks": [
            {"id": "library.0087.global_slot_ambiguity", "migration": preflight.M0087, "status": "failed",
             "evidence": {"offending_group_count": 1}},
        ]})
        with self.assertRaises(ExecutionError) as caught:
            executor._run_migration_preflights(Path("/x"), (preflight.M0087, preflight.M0088))
        self.assertEqual(caught.exception.classification, "MIGRATION_PREFLIGHT_BLOCKED")
        arguments = self.run_app.call_args.args[1]
        self.assertEqual(arguments, ["updatecenter_migration_preflight", "--skip-checks",
                                     "--pending", preflight.M0087, "--pending", preflight.M0088])

    def test_executor_rejects_results_for_unrequested_migrations_or_contradictory_status(self):
        for payload in (
            {"schema_version": 1, "status": "ok", "checks": [
                {"id": "x.check", "migration": "other.0001_x", "status": "passed", "evidence": {}}]},
            {"schema_version": 1, "status": "ok", "checks": [
                {"id": "x.check", "migration": M1, "status": "failed", "evidence": {}}]},
        ):
            with self.subTest(payload=payload):
                executor = self._executor_with(payload)
                with self.assertRaises(ExecutionError) as caught:
                    executor._run_migration_preflights(Path("/x"), (M1,))
                self.assertEqual(caught.exception.classification, "MIGRATION_PREFLIGHT_INVALID")

    def test_preflight_runs_before_migration_started(self):
        source = (Path(__file__).parents[2] / "deploy/updater_runtime/isadoraair_updater/executor.py").read_text()
        block = source[source.index("def execute(self, job_id"):]
        block = block[block.index("if actual_migrations"):]
        self.assertLess(block.index("_run_migration_preflights(staged.source_root, actual_migrations)"),
                        block.index('"migration_started"'))


# ---------------------------------------------------------------------------
# Partial-prefix recovery through the real executor
# ---------------------------------------------------------------------------

class _Crash(BaseException):
    """SIGKILL / power loss: escapes every except clause in the worker."""


class FakeRecorder:
    """One consistent stand-in for the station database's django_migrations."""

    def __init__(self):
        self.rows = {}
        self._next = 40
        self.unavailable_attempts = 0

    def apply(self, ref):
        self._next += 1
        self.rows[ref] = {"id": self._next, "applied": f"2026-10-03T12:00:{self._next % 60:02d}.{self._next:06d}Z"}

    def unapply(self, ref):
        del self.rows[ref]

    def observe(self, refs):
        if self.unavailable_attempts:
            self.unavailable_attempts -= 1
            raise ExecutionError("MIGRATION_OBSERVATION_FAILED", "the database system is starting up", manual=True)
        return {ref: dict(self.rows[ref]) for ref in refs if ref in self.rows}


class PartialPrefixRecoveryExecutionTests(SimpleTestCase):
    """M1 -> M2 transition from r0104 to r0105 (M2 depends on M1)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, self.store, self.executor = make_executor(self.root)
        self.sleeps = []
        self.executor._sleep = self.sleeps.append
        self.plan = plan()
        self.recorder = FakeRecorder()
        self.head = "a" * 40
        self.migrate_calls = []
        self.probe_hooks = {}
        self.preflight_hook = None
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
        self.probe_count += 1
        hook = self.probe_hooks.get(self.probe_count)
        if hook is not None and hook[0] == "before":
            hook[1]()
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
            payload["recovery_plan"] = [migration_item(ref, [BASE] if ref == M1 else [M1]) for ref in recovery_plan_refs]
            payload["recovery_migration_plan_digest"] = FULL_DIGEST if list(recovery_plan_refs) == [M1, M2] else "8" * 64
            payload["recovery_manual_operations"] = []
        if hook is not None and hook[0] == "after":
            hook[1]()
        return payload

    def _current(self):
        return {"nodes": {BASE: []}, "applied": self._applied()}

    def run_job(self, job_id, *, outcomes=None, accept=True):
        """outcomes: ref -> "ok" | "fail" | "crash_after_commit" | "crash_before_commit" | "oserror"."""
        outcomes = outcomes or {}
        self.probe_count = 0
        if accept:
            self.store.accept(job_id, "r0105", self.plan.fingerprint)

        def run_app(source, arguments, *, timeout):
            if arguments[0] == "updatecenter_migration_preflight":
                if self.preflight_hook is not None:
                    self.preflight_hook()
                return ProcessResult(tuple(arguments), 0, b'{"checks":[],"schema_version":1,"status":"ok"}', b""), {}
            if arguments[0] != "migrate":
                raise AssertionError(f"unexpected application command {arguments!r}")
            ref = f"{arguments[1]}.{arguments[2]}"
            self.migrate_calls.append(ref)
            outcome = outcomes.get(ref, "ok")
            if outcome == "oserror":
                raise OSError(24, "Too many open files")
            if outcome == "crash_before_commit":
                raise _Crash()
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
        self.assertIsNone(evidence["in_flight_migration"])
        self.assertEqual(evidence["prefix_records"][M1], self.recorder.rows[M1])
        self.assertLess(retry["milestones"].index("database_verified"), retry["milestones"].index("source_advanced"))
        return retry

    def failed_after_m1(self):
        job = str(uuid.uuid4())
        failed = self.run_job(job, outcomes={M2: "fail"})
        self.assertEqual(failed["migration_recovery"]["successful_prefix"], [M1])
        return job

    def assert_blocked(self, result, *, no_migration=True):
        self.assertEqual(result["failure_classification"], "TARGET_MIGRATION_PREAPPLIED", result.get("failure_detail"))
        self.assertNotIn("source_advanced", result["milestones"])
        if no_migration:
            self.assertEqual(self.migrate_calls, [])

    # -- B1 crash windows ------------------------------------------------------
    def test_h1_m1_commits_then_hard_kill_then_resume_then_fresh_retry_runs_only_m2(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        crashed = self.store.load(job)["migration_recovery"]
        self.assertEqual((crashed["successful_prefix"], crashed["in_flight_migration"]), ([], M1))
        resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")
        evidence = resumed["migration_recovery"]
        self.assertTrue(evidence["finalized"])
        self.assertEqual(evidence["successful_prefix"], [M1])
        self.assertEqual(evidence["prefix_records"], {M1: self.recorder.rows[M1]})
        self.assertIsNone(evidence["in_flight_migration"])
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_h1_repeated_five_times_is_deterministic(self):
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                self.recorder, self.head, self.migrate_calls = FakeRecorder(), "a" * 40, []
                job = str(uuid.uuid4())
                with self.assertRaises(_Crash):
                    self.run_job(job, outcomes={M1: "crash_after_commit"})
                self.run_job(job, accept=False)
                self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_h2_oserror_invoking_m2_after_durable_m1_then_fresh_retry_runs_only_m2(self):
        job = str(uuid.uuid4())
        failed = self.run_job(job, outcomes={M2: "oserror"})
        self.assertEqual(failed["failure_classification"], "SAFE_EXECUTION_FAILURE")
        self.assertTrue(failed["migration_recovery"]["finalized"])
        self.assertEqual(failed["migration_recovery"]["successful_prefix"], [M1])
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_ordinary_m2_failure_then_fresh_retry_runs_only_m2(self):
        job = self.failed_after_m1()
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_crash_after_the_last_migration_finishes_without_re_running_and_verifies_first(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["migration_recovery"]["successful_prefix"], [M1, M2])
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assertEqual(retry["state"], "succeeded", retry.get("failure_detail"))
        self.assertEqual(self.migrate_calls, [])
        # target, recovery, the complete-prefix VERIFICATION probe (before
        # database_verified -- see race 4), then the ordinary postflight probe.
        self.assertEqual(self.probe_count, 4)
        self.assertLess(retry["milestones"].index("database_verified"), retry["milestones"].index("source_advanced"))

    # -- in-flight bound -------------------------------------------------------
    def test_committed_in_flight_migration_may_be_claimed(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        self.assertEqual(self.store.load(job)["migration_recovery"]["in_flight_migration"], M2)
        resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["migration_recovery"]["successful_prefix"], [M1, M2])

    def test_crash_then_manual_next_migration_cannot_be_claimed(self):
        """M1 recorded, crash before M2 was ever marked in-flight, operator
        applies M2 by hand before restart: M2 must never become updater-owned."""
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_before_commit"})
        path = self.config.jobs_root / f"{job}.json"
        raw = json.loads(path.read_text())
        raw["migration_recovery"]["in_flight_migration"] = None      # died before the M2 in-flight write
        path.write_text(json.dumps(raw))
        self.recorder.apply(M2)                                       # operator: manage.py migrate
        resumed = self.run_job(job, accept=False)
        self.assertFalse(resumed["migration_recovery"]["finalized"])
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    # -- S3 / final ownership assertions and their races -----------------------
    def test_s3_reapplied_m1_with_new_row_identity_is_not_updater_owned(self):
        self.failed_after_m1()
        self.recorder.unapply(M1)
        self.recorder.apply(M1)
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_s3_rewritten_applied_timestamp_is_not_updater_owned(self):
        self.failed_after_m1()
        self.recorder.rows[M1]["applied"] = "2030-01-01T00:00:00.000000Z"
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_manually_applied_remaining_migration_is_not_updater_owned(self):
        self.failed_after_m1()
        self.recorder.apply(M2)
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_race_1_extra_migration_between_target_probe_and_recovery_observation(self):
        self.failed_after_m1()
        self.probe_hooks = {1: ("after", lambda: self.recorder.apply(M2))}
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_race_2_extra_migration_after_recovery_selection_before_recovery_probe(self):
        self.failed_after_m1()
        self.probe_hooks = {2: ("before", lambda: self.recorder.apply(M2))}
        self.migrate_calls.clear()
        self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_race_3_extra_migration_after_target_validation_before_migration_started(self):
        self.failed_after_m1()
        self.preflight_hook = lambda: self.recorder.apply(M2)
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assert_blocked(retry)
        self.assertNotIn("migration_started", retry["milestones"])

    def test_race_4_complete_prefix_row_changes_before_final_verification(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        self.run_job(job, accept=False)

        def rewrite():
            self.recorder.unapply(M1)
            self.recorder.apply(M1)
        self.probe_hooks = {3: ("after", rewrite)}     # the complete-prefix verification probe
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assert_blocked(retry)
        self.assertNotIn("database_verified", retry["milestones"])

    def test_identity_mismatches_block_recovery(self):
        cases = {
            "other release": {"release_id": "r0106"},
            "other target": {"target_commit": "1" * 40},
            "other manifest": {"manifest_sha256": "1" * 64},
            "other fingerprint": {"trusted_plan_fingerprint": "1" * 64},
            "noncontiguous prefix": {"successful_prefix": [M2], "prefix_records": {}},
            "missing records": {"prefix_records": {}},
            "foreign evidence job": {"evidence_job_id": "22222222-2222-2222-2222-222222222222"},
            "impossible in-flight": {"in_flight_migration": M1},
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.recorder, self.head, self.migrate_calls = FakeRecorder(), "a" * 40, []
                job = self.failed_after_m1()
                path = self.config.jobs_root / f"{job}.json"
                raw = json.loads(path.read_text())
                raw["migration_recovery"] = {**raw["migration_recovery"], **change}
                path.write_text(json.dumps(raw))
                self.migrate_calls.clear()
                self.assert_blocked(self.run_job(str(uuid.uuid4())))

    def test_unprovable_unfinalized_evidence_is_never_accepted(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        self.store.fail(job, "AMBIGUOUS_INTERRUPTED_MIGRATION", "worker died", manual=True)
        self.recorder.apply(M2)                         # beyond the single in-flight migration
        self.migrate_calls.clear()
        retry = self.run_job(str(uuid.uuid4()))
        self.assert_blocked(retry)
        self.assertFalse(self.store.load(job)["migration_recovery"]["finalized"])

    # -- finalization fails closed ---------------------------------------------
    def test_finalization_refuses_noncontiguous_observation(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        self.recorder.unapply(M1)
        self.recorder.apply(M2)
        self.assertFalse(self.run_job(job, accept=False)["migration_recovery"]["finalized"])

    def test_finalization_refuses_when_a_recorded_row_changed(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M2: "crash_after_commit"})
        self.recorder.unapply(M1)
        self.recorder.apply(M1)
        self.assertFalse(self.run_job(job, accept=False)["migration_recovery"]["finalized"])

    def test_finalization_never_masks_the_original_failure(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        with mock.patch.object(FakeRecorder, "observe", side_effect=RuntimeError("psql unavailable")):
            resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")
        self.assertFalse(resumed["migration_recovery"]["finalized"])

    # -- PostgreSQL not ready after a power loss ---------------------------------
    def test_database_back_within_the_bounded_wait_finalizes_at_resume(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        self.recorder.unavailable_attempts = 3
        resumed = self.run_job(job, accept=False)
        self.assertTrue(resumed["migration_recovery"]["finalized"])
        self.assertEqual(self.sleeps, list(OBSERVATION_READINESS_DELAYS[:3]))
        self.fresh_retry_succeeds_with_only([M2], prior_job=job)

    def test_database_still_down_then_later_exact_retry_finalizes_and_continues(self):
        job = str(uuid.uuid4())
        with self.assertRaises(_Crash):
            self.run_job(job, outcomes={M1: "crash_after_commit"})
        self.recorder.unavailable_attempts = len(OBSERVATION_READINESS_DELAYS) + 1     # outlasts the bounded wait
        resumed = self.run_job(job, accept=False)
        self.assertEqual(resumed["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")
        self.assertFalse(resumed["migration_recovery"]["finalized"])              # 2. could not reach the DB
        self.assertEqual(self.sleeps, list(OBSERVATION_READINESS_DELAYS))        #    bounded, then fail safe
        retry = self.fresh_retry_succeeds_with_only([M2], prior_job=job)           # 3-5. DB up: finalize + continue
        finalized = self.store.load(job)["migration_recovery"]
        self.assertTrue(finalized["finalized"])
        self.assertEqual(finalized["successful_prefix"], [M1])
        self.assertEqual(retry["state"], "succeeded")

    # -- evidence integrity --------------------------------------------------------
    def test_finalized_evidence_cannot_be_rewritten(self):
        job = self.failed_after_m1()
        with self.assertRaises(Exception):
            self.store.update(job, migration_recovery={**self.store.load(job)["migration_recovery"], "successful_prefix": []})

    def test_recorded_authorization_source_is_the_real_one(self):
        job = self.failed_after_m1()
        evidence = self.store.load(job)["migration_recovery"]
        self.assertEqual(evidence["authorization_source"], "not_required")
        self.assertNotIn("exact_plan_authorization_matches", evidence)


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


class NonAtomicClassificationTests(SimpleTestCase):
    """Every operation of an atomic=False migration is manual, in the forward
    plan and in the reconstructed plan alike; atomic migrations are untouched."""

    def _migration(self, *, atomic):
        from django.db import migrations, models

        class Migration(migrations.Migration):
            operations = [migrations.CreateModel("Widget", [("id", models.BigAutoField(primary_key=True))])]
        migration = Migration("0002_widget", "sample")
        migration.atomic = atomic
        return migration

    def _forward(self, migration):
        from django.db.migrations.state import ProjectState
        from updatecenter.management.commands.updatecenter_probe import _classify_migration_operation
        state = ProjectState()
        result = []
        for operation in migration.operations:
            operation.state_forwards(migration.app_label, state)
            result.append(_classify_migration_operation(migration, operation, after_state=state))
        return result

    def _reconstructed(self, migration):
        from django.db.migrations.state import ProjectState
        from updatecenter.management.commands import updatecenter_probe
        loader = mock.Mock()
        loader.disk_migrations = {("sample", "0002_widget"): migration}
        loader.graph.node_map = {("sample", "0002_widget"): mock.Mock(parents=[])}
        loader.project_state.return_value = ProjectState()
        with mock.patch.object(updatecenter_probe, "_migration_file_sha256", return_value="0" * 64):
            return updatecenter_probe._serialize_exact_plan(loader, ["sample.0002_widget"])[0]["operations"]

    def test_non_atomic_migration_is_manual_in_forward_and_reconstructed_plans(self):
        from updatecenter.management.commands.updatecenter_probe import NON_ATOMIC_DETAIL
        migration = self._migration(atomic=False)
        expected = [{"operation": "CreateModel", "classification": "manual", "detail": NON_ATOMIC_DETAIL}]
        self.assertEqual(self._forward(migration), expected)
        self.assertEqual(self._reconstructed(self._migration(atomic=False)), expected)

    def test_atomic_twin_keeps_its_ordinary_classification_and_digest(self):
        from updatecenter.management.commands.updatecenter_probe import _classify_operation, compute_migration_plan_digest
        from django.db.migrations.state import ProjectState
        migration = self._migration(atomic=True)
        classified = self._forward(migration)
        state = ProjectState()
        operation = self._migration(atomic=True).operations[0]
        operation.state_forwards("sample", state)
        self.assertEqual(classified, [_classify_operation(operation, app_label="sample", after_state=state)])
        self.assertEqual(classified[0]["classification"], "additive")
        item = {"ref": "sample.0002_widget", "migration_file_sha256": "0" * 64, "operations": classified}
        legacy = {"ref": "sample.0002_widget", "migration_file_sha256": "0" * 64,
                  "operations": [_classify_operation(operation, app_label="sample", after_state=state)]}
        kwargs = dict(release_id="r0105", target_commit="b" * 40, manifest_sha256="c" * 64)
        self.assertEqual(compute_migration_plan_digest(plan=[item], **kwargs),
                         compute_migration_plan_digest(plan=[legacy], **kwargs))
