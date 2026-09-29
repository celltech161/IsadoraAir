"""Production-faithful r0088 -> r0092 reviewed-migration lifecycle.

The filename is retained to avoid silently dropping the former principal
acceptance suite.  Unlike that suite, this fixture explicitly rolls
updatecenter back to 0002 as well as authz/library, proving the target probe
has no approval table available.
"""
from pathlib import Path
import tempfile
from unittest import mock
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from .phase_b_helpers import PROJECT_ROOT, config_dict
from .test_gen7_heterogeneous_probe_compatibility import _run_real_legacy_probe
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import Executor, _strict_probe
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import TrustedPlan
from isadoraair_updater.staging import StagedSource
from updatecenter import release_chain
from updatecenter.management.commands.updatecenter_probe import build_probe_payload


R0088_COMMIT = "edc5d5c8f345ba18646db66a9a050093d1a076b0"
R0092_TARGET_COMMIT = "b" * 40


class RecordingSystemd:
    def __init__(self):
        self.reconciled = 0
        self.restarted = []

    def reconcile(self, source, plan):
        self.reconciled += 1
        return {}

    def restart_declared(self, services):
        self.restarted.extend(services)
        return list(services)


class ProductionStateExecutor(Executor):
    """Real execute() state machine with only host mutations test-doubled."""

    def __init__(self, *args, leaf_targets, **kwargs):
        super().__init__(*args, **kwargs)
        self.leaf_targets = leaf_targets
        self.live_head = R0088_COMMIT
        self.migrate_calls = 0
        self.source_advance_calls = 0
        self.http_calls = 0
        self.legacy_current = _strict_probe(_run_real_legacy_probe(), review_context=False)

    def _live_identity(self):
        return {"branch": "main", "head": self.live_head}

    def _validate_current_schema(self):
        # Actual r0088 script bytes, executed as a subprocess by the helper.
        self.assert_current_probe_was_legacy = set(self.legacy_current) == {
            "schema_version", "status", "plan", "nodes", "applied", "conflicts", "replacements",
        }
        return self.legacy_current

    def _probe(self, source, *, release_id=None, target_commit=None):
        if release_id is not None:
            return build_probe_payload(release_id=release_id, target_commit=target_commit)
        return build_probe_payload()

    def _run_app(self, source, arguments, *, timeout):
        if arguments[:2] == ["migrate", "--noinput"]:
            self.migrate_calls += 1
            MigrationExecutor(connection).migrate(self.leaf_targets)
            return ProcessResult(tuple(arguments), 0, b"", b""), {}
        raise AssertionError(f"unexpected application mutation: {arguments!r}")

    def _advance_source(self, plan):
        self.source_advance_calls += 1
        self.live_head = plan.target_commit

    def _postflight_http(self):
        self.http_calls += 1


class R0092ProductionBootstrapAcceptanceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        migration_executor = MigrationExecutor(connection)
        self.leaf_targets = migration_executor.loader.graph.leaf_nodes()
        production_targets = []
        for app_label, migration_name in self.leaf_targets:
            if app_label == "authz":
                continue
            if app_label == "updatecenter":
                migration_name = "0002_alter_updatejob_state"
            elif app_label == "library":
                migration_name = "0084_alter_uitheme_logo_alter_uitheme_station_logo"
            production_targets.append((app_label, migration_name))
        # Build forward from an actually empty disposable schema. This avoids
        # relying on reversibility of data migrations and produces the exact
        # migration-recorder state production has, including zero authz rows.
        with connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")
        MigrationExecutor(connection).migrate(production_targets)
        self.assertNotIn("updatecenter_migrationplanapproval", connection.introspection.table_names())

        manifests = release_chain.load_manifest_files(PROJECT_ROOT / "deploy" / "releases")
        chain = release_chain.build_chain(manifests)
        transition = [
            item.manifest for item in chain
            if "r0088" < item.manifest.release_id <= "r0092"
        ]
        self.migration_refs = tuple(
            ref for manifest in transition for ref in manifest.migrations_required
        )
        self.assertEqual(len(self.migration_refs), 9)
        self.assertEqual(self.migration_refs[-1], "updatecenter.0003_updatejob_migration_plan_review_and_more")

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True,
        )
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.systemd = RecordingSystemd()
        self.plan = TrustedPlan(
            installed_release_id="r0088", installed_commit=R0088_COMMIT,
            target_release_id="r0092", target_commit=R0092_TARGET_COMMIT,
            releases_in_plan=tuple(item.release_id for item in transition),
            migrations_required=self.migration_refs,
            migration_compatibility="additive", python_requirements_changed=False,
            apt_packages_new=(), systemd_units_changed=(), systemd_units_new_required=(),
            systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
            collectstatic_required=False,
            services_requiring_restart=("isadoraair-gunicorn", "isadoraair-engine"),
            nginx_changed=False, runtime_components_changed=False,
            minimum_updater_protocol_version=5, manual_bootstrap_required=False,
            fingerprint="f" * 64,
            # Runtime handoff itself has dedicated integration coverage. This
            # fixture starts at the accepted generation-8 mutation boundary.
            protected_runtime_transition=None,
        )
        self.executor = ProductionStateExecutor(
            self.config, self.store, CommandRunner(), systemd_manager=self.systemd,
            leaf_targets=self.leaf_targets,
        )
        self.executor.repository.fetch = lambda: R0092_TARGET_COMMIT

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.leaf_targets)
        self.store.close()
        self.temp.cleanup()

    def _accept(self):
        job_id = str(uuid.uuid4())
        self.store.accept(job_id, "r0092", self.plan.fingerprint)
        return job_id

    def _staged(self, job_id):
        return StagedSource(
            job_root=self.config.staging_root / job_id,
            source_root=PROJECT_ROOT,
            archive_path=self.config.staging_root / job_id / "target.tar",
        )

    def test_two_distinct_jobs_discover_approve_recompute_and_apply_all_nine(self):
        first_payload = build_probe_payload(
            release_id="r0092", target_commit=R0092_TARGET_COMMIT,
        )
        self.assertEqual(tuple(item["ref"] for item in first_payload["plan"]), self.migration_refs)
        self.assertEqual(len(first_payload["manual_operations"]), 6)
        self.assertIsNone(first_payload["approval"])

        job_a = self._accept()
        with mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan), \
                mock.patch("isadoraair_updater.executor.materialize", side_effect=lambda *args: self._staged(job_a)), \
                mock.patch("isadoraair_updater.executor.cleanup"), \
                mock.patch("isadoraair_updater.executor.create_checkpoint", return_value={"schema_version": 1}), \
                mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=False):
            result_a = self.executor.execute(job_a)

        self.assertEqual(result_a["state"], "manual_intervention_required")
        self.assertEqual(result_a["failure_classification"], "MIGRATION_OPERATION_MANUAL")
        self.assertIn("target_staged", result_a["milestones"])
        self.assertNotIn("migration_started", result_a["milestones"])
        self.assertNotIn("source_advanced", result_a["milestones"])
        self.assertEqual(self.executor.migrate_calls, 0)
        self.assertEqual(self.executor.source_advance_calls, 0)
        self.assertEqual(self.systemd.reconciled, 0)
        self.assertNotIn("updatecenter_migrationplanapproval", connection.introspection.table_names())

        review_before = dict(result_a["migration_plan_review"])
        approval, created = self.executor.approval_store.create_from_job(
            job_a,
            confirmed_migration_plan_digest=review_before["migration_plan_digest"],
            approved_by_username="release_operator",
            reason="Reviewed all six operations against the r0088 schema and data.",
        )
        self.assertTrue(created)
        self.assertEqual(self.store.load(job_a)["migration_plan_review"], review_before)

        job_b = self._accept()
        self.assertNotEqual(job_a, job_b)
        with mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan), \
                mock.patch("isadoraair_updater.executor.materialize", side_effect=lambda *args: self._staged(job_b)), \
                mock.patch("isadoraair_updater.executor.cleanup"), \
                mock.patch("isadoraair_updater.executor.create_checkpoint", return_value={"schema_version": 1}), \
                mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=False):
            result_b = self.executor.execute(job_b)

        self.assertEqual(result_b["state"], "succeeded")
        self.assertEqual(self.executor.migrate_calls, 1)
        self.assertEqual(self.executor.source_advance_calls, 1)
        self.assertEqual(self.systemd.reconciled, 1)
        self.assertEqual(self.systemd.restarted, ["isadoraair-gunicorn", "isadoraair-engine"])
        self.assertIn("database_verified", result_b["milestones"])
        self.assertIn("source_advanced", result_b["milestones"])
        self.assertIn("updatecenter_migrationplanapproval", connection.introspection.table_names())
        after = build_probe_payload()
        self.assertEqual(after["plan"], [])
        self.assertTrue(set(self.migration_refs).issubset(set(after["applied"])))
        self.assertEqual(approval["source_job_id"], job_a)
