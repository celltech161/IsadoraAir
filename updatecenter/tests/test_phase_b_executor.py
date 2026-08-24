from pathlib import Path
import tempfile
import uuid
from unittest import mock

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import ExecutionError, Executor
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import TrustedPlan
from isadoraair_updater.staging import StagedSource, StagingError


def trusted_plan(**changes):
    data = dict(
        installed_release_id="r0002", installed_commit="a" * 40,
        target_release_id="r0003", target_commit="b" * 40,
        releases_in_plan=("r0003",), migrations_required=("sample.0002_add",),
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


def probe(plan=True, manual=False):
    item = {
        "ref": "sample.0002_add", "dependencies": ["sample.0001_initial"],
        "operations": [{"operation": "RemoveField" if manual else "AddField", "classification": "manual" if manual else "additive", "detail": "test"}],
    }
    return {
        "schema_version": 1, "status": "ok", "plan": [item] if plan else [],
        "nodes": {"sample.0001_initial": [], "sample.0002_add": ["sample.0001_initial"]},
        "applied": ["sample.0001_initial"] + ([] if plan else ["sample.0002_add"]),
        "conflicts": {}, "replacements": [],
    }


class FakeSystemd:
    def __init__(self, events):
        self.events = events

    def reconcile(self, source, plan):
        self.events.append("systemd")
        return {}

    def restart_declared(self, services):
        self.events.append(f"restart:{','.join(services)}")
        return list(services)


class ExecutorSchemaComparisonTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.executor = Executor(self.config, self.store, CommandRunner(), systemd_manager=FakeSystemd([]))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_dependency_closure_exact_match_allowed(self):
        actual = self.executor._validate_target_schema(trusted_plan(), probe(), {"applied": ["sample.0001_initial"]}, migration_already_started=False)
        self.assertEqual(actual, ("sample.0002_add",))

    def test_unexpected_target_migration_rejected(self):
        payload = probe()
        payload["nodes"]["other.0001_initial"] = []
        payload["plan"].append({"ref": "other.0001_initial", "dependencies": [], "operations": []})
        with self.assertRaisesRegex(ExecutionError, "differs"):
            self.executor._validate_target_schema(trusted_plan(), payload, {"applied": ["sample.0001_initial"]}, migration_already_started=False)

    def test_missing_expected_migration_rejected(self):
        payload = probe()
        del payload["nodes"]["sample.0002_add"]
        with self.assertRaisesRegex(ExecutionError, "absent"):
            self.executor._validate_target_schema(trusted_plan(), payload, {"applied": []}, migration_already_started=False)

    def test_manifest_additive_cannot_override_destructive_operation(self):
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(trusted_plan(), probe(manual=True), {"applied": ["sample.0001_initial"]}, migration_already_started=False)
        self.assertTrue(caught.exception.manual)

    def test_conflict_and_replacement_fail_closed(self):
        for key, value in (("conflicts", {"sample": ["0002_a", "0002_b"]}), ("replacements", ["sample.0002_squashed"])):
            payload = probe()
            payload[key] = value
            with self.assertRaises(ExecutionError):
                self.executor._validate_target_schema(trusted_plan(), payload, {"applied": []}, migration_already_started=False)

    def test_preapplied_transition_without_job_milestone_is_manual(self):
        with self.assertRaises(ExecutionError) as caught:
            self.executor._validate_target_schema(trusted_plan(), probe(plan=False), {"applied": ["sample.0001_initial", "sample.0002_add"]}, migration_already_started=False)
        self.assertTrue(caught.exception.manual)

    def test_application_database_identity_must_match_root_config(self):
        env_path = self.config.application_environment_file
        env_path.write_text("SECRET_KEY=x\nDB_NAME=other\nDB_USER=test\nDB_PASSWORD=x\n", encoding="utf-8")
        with self.assertRaisesRegex(ExecutionError, "database identity"):
            self.executor._app_env(self.config.application_root)

    def test_application_environment_must_be_regular_file(self):
        self.config.application_environment_file.unlink()
        self.config.application_environment_file.mkdir()
        with self.assertRaisesRegex(ExecutionError, "regular file"):
            self.executor._app_env(self.config.application_root)

    @mock.patch("isadoraair_updater.executor.http.client.HTTPConnection")
    def test_loopback_postflight_does_not_follow_redirects(self, connection_class):
        response = connection_class.return_value.getresponse.return_value
        response.status = 302
        response.read.return_value = b""
        with self.assertRaises(ExecutionError) as caught:
            self.executor._postflight_http()
        self.assertTrue(caught.exception.manual)
        connection_class.return_value.request.assert_called_once_with(
            "GET", "/login/", headers={"Connection": "close"},
        )


class LiveIdentityDiagnosticTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, "https://example.invalid/trusted.git"),
            allow_local_repository=True,
        )
        self.store = JobStore(
            self.config.jobs_root, self.config.logs_root,
            acquire_daemon_lock=False,
        )
        self.executor = Executor(
            self.config, self.store, CommandRunner(),
            systemd_manager=FakeSystemd([]),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _ok_results(self):
        return [
            ProcessResult(("git",), 0, b"main\n", b""),
            ProcessResult(("git",), 0, b"a" * 40 + b"\n", b""),
            ProcessResult(("git",), 0, b"", b""),
            ProcessResult(("git",), 0, b"https://example.invalid/trusted.git\n", b""),
        ]

    def test_each_failed_probe_has_specific_bounded_classification(self):
        classifications = (
            "LIVE_GIT_BRANCH_FAILED",
            "LIVE_GIT_HEAD_FAILED",
            "LIVE_GIT_STATUS_FAILED",
            "LIVE_GIT_REMOTE_FAILED",
        )
        for index, expected in enumerate(classifications):
            results = self._ok_results()
            results[index] = ProcessResult(
                ("git",), 7, b"SECRET-STDOUT" * 1000,
                b"SECRET-STDERR" * 1000,
            )
            with self.subTest(expected=expected):
                with mock.patch.object(self.executor, "_app_git", side_effect=results):
                    with self.assertRaises(ExecutionError) as caught:
                        self.executor._live_identity()
                self.assertEqual(caught.exception.classification, expected)
                self.assertNotIn("SECRET", caught.exception.detail)
                self.assertLess(len(caught.exception.detail), 300)

    def test_semantic_branch_dirty_and_remote_mismatches_remain_fail_closed(self):
        cases = (
            (0, b"other\n", "LIVE_GIT_INVALID"),
            (2, b"?? unexpected\n", "LIVE_CHECKOUT_DIRTY"),
            (3, b"https://example.invalid/wrong.git\n", "LIVE_REMOTE_MISMATCH"),
        )
        for index, output, expected in cases:
            results = self._ok_results()
            result = results[index]
            results[index] = ProcessResult(
                result.argv, result.returncode, output, result.stderr,
            )
            with self.subTest(expected=expected):
                with mock.patch.object(self.executor, "_app_git", side_effect=results):
                    with self.assertRaises(ExecutionError) as caught:
                        self.executor._live_identity()
                self.assertEqual(caught.exception.classification, expected)


class ExecutorOrderingTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.events = []
        self.executor = Executor(self.config, self.store, CommandRunner(), systemd_manager=FakeSystemd(self.events))
        self.job_id = str(uuid.uuid4())
        self.plan = trusted_plan()
        self.store.accept(self.job_id, "r0003", self.plan.fingerprint)
        self.stage_root = self.root / "fake-stage"
        self.stage_root.mkdir()
        (self.stage_root / "manage.py").touch()
        self.staged = StagedSource(self.root / self.job_id, self.stage_root, self.root / "archive")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _patches(self, *, migration_success=True, current_error=None):
        live = mock.patch.object(self.executor, "_live_identity", side_effect=[{"head": self.plan.installed_commit}, {"head": self.plan.target_commit}])
        fetch = mock.patch.object(self.executor.repository, "fetch", return_value="c" * 40)
        derive = mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan)
        blockers = mock.patch("isadoraair_updater.executor.manual_blockers", return_value=())
        current = mock.patch.object(self.executor, "_validate_current_schema", side_effect=current_error, return_value={"applied": ["sample.0001_initial"]})
        cleanup_patch = mock.patch("isadoraair_updater.executor.cleanup")
        stage = mock.patch("isadoraair_updater.executor.materialize", return_value=self.staged)
        target_probe = mock.patch.object(self.executor, "_probe", side_effect=[probe(), probe(plan=False), probe(plan=False)])
        compare = mock.patch.object(self.executor, "_validate_target_schema", return_value=("sample.0002_add",))
        checkpoint = mock.patch("isadoraair_updater.executor.create_checkpoint", return_value={"valid": True, "dump_file": "x", "size_bytes": 1, "sha256": "d" * 64})
        migrate_result = ProcessResult(("python",), 0 if migration_success else 1, b"ok" if migration_success else b"", b"failed" if not migration_success else b"")
        migrate = mock.patch.object(self.executor, "_run_app", return_value=(migrate_result, {"DB_PASSWORD": "secret", "SECRET_KEY": "key"}))

        def advance(_plan):
            self.events.append("advance")
        advance_patch = mock.patch.object(self.executor, "_advance_source", side_effect=advance)
        return [live, fetch, derive, blockers, current, cleanup_patch, stage, target_probe, compare, checkpoint, migrate, advance_patch]

    def _run_with(self, patches):
        entered = [item.start() for item in patches]
        try:
            return self.executor.execute(self.job_id)
        finally:
            for item in reversed(patches):
                item.stop()

    def test_migration_precedes_source_and_restarts(self):
        state = self._run_with(self._patches())
        self.assertEqual(state["state"], "succeeded")
        milestones = state["milestones"]
        self.assertLess(milestones.index("migration_started"), milestones.index("database_verified"))
        self.assertLess(milestones.index("database_verified"), milestones.index("source_advanced"))
        self.assertEqual(self.events, ["advance", "systemd", "restart:isadoraair-engine"])

    def test_failed_migration_never_advances_source_or_restarts(self):
        state = self._run_with(self._patches(migration_success=False))
        self.assertEqual(state["state"], "manual_intervention_required")
        self.assertNotIn("advance", self.events)
        self.assertNotIn("restart:isadoraair-engine", self.events)
        self.assertNotIn("source_advanced", state["milestones"])

    def test_current_schema_drift_blocks_before_staging(self):
        state = self._run_with(self._patches(current_error=ExecutionError("CURRENT_SCHEMA_UNHEALTHY", "pending webrequests.0008")))
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["failure_classification"], "CURRENT_SCHEMA_UNHEALTHY")
        self.assertNotIn("target_staged", state["milestones"])

    def test_partial_materialization_is_cleaned_after_failure(self):
        patches = self._patches()
        patches.pop(5)  # Exercise the real guarded cleanup path.
        partial = self.config.staging_root / self.job_id

        def fail_materialization(*_args, **_kwargs):
            partial.mkdir(parents=True)
            (partial / "partial").write_text("incomplete", encoding="utf-8")
            raise StagingError("injected staging failure")

        patches[5] = mock.patch(
            "isadoraair_updater.executor.materialize", side_effect=fail_materialization,
        )
        state = self._run_with(patches)
        self.assertEqual(state["state"], "failed")
        self.assertFalse(partial.exists())

    def test_ambiguous_migration_restart_never_reruns(self):
        self.store.milestone(self.job_id, "migration_started")
        state = self.executor.execute(self.job_id)
        self.assertEqual(state["state"], "manual_intervention_required")
        self.assertEqual(state["failure_classification"], "AMBIGUOUS_INTERRUPTED_MIGRATION")

    def test_completed_service_marker_prevents_duplicate_restart(self):
        self.store.milestone(self.job_id, "service_restarted_isadoraair_engine")
        state = self._run_with(self._patches())
        self.assertEqual(state["state"], "succeeded")
        self.assertNotIn("restart:isadoraair-engine", self.events)

    def test_started_service_restart_without_completion_is_manual(self):
        self.store.milestone(self.job_id, "service_restart_started_isadoraair_engine")
        state = self._run_with(self._patches())
        self.assertEqual(state["state"], "manual_intervention_required")
        self.assertEqual(
            state["failure_classification"],
            "AMBIGUOUS_INTERRUPTED_SERVICE_RESTART",
        )
        self.assertNotIn("restart:isadoraair-engine", self.events)

    def test_exact_target_after_verified_database_recovers_source_milestone(self):
        self.store.update(self.job_id, trusted_plan={
            "installed_commit": self.plan.installed_commit,
            "target_commit": self.plan.target_commit,
        })
        self.store.milestone(self.job_id, "database_verified")
        patches = self._patches()
        patches[0] = mock.patch.object(
            self.executor, "_live_identity", return_value={"head": self.plan.target_commit},
        )
        state = self._run_with(patches)
        self.assertEqual(state["state"], "succeeded")
        self.assertNotIn("advance", self.events)
        self.assertIn("source_advanced", state["milestones"])
