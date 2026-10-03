"""Production-faithful r0088 -> r0092 reviewed-migration lifecycle.

The filename is retained to avoid silently dropping the former principal
acceptance suite.  Unlike that suite, this fixture explicitly rolls
updatecenter back to 0002 as well as authz/library, proving the target probe
has no approval table available.
"""
from pathlib import Path
import json
import tempfile
import types
from unittest import mock
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.graph import MigrationGraph
from django.test import TransactionTestCase

from .phase_b_helpers import PROJECT_ROOT, config_dict, orm_migration_records
from .test_gen7_heterogeneous_probe_compatibility import _run_real_legacy_probe
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.executor import Executor, _strict_probe
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import ProtectedRuntimeTransition, TrustedPlan
from isadoraair_updater.staging import StagedSource
from protected_bootstrap.manifest_field import parse_protected_runtime_field
from updatecenter import release_chain
from updatecenter.management.commands.updatecenter_probe import build_probe_payload

# This is a historical r0088 -> r0092 simulation, but the probe always reports
# every pending migration in the checkout it inspects and this suite runs it
# against the live working tree. Migrations added after r0092 (3.1A's
# library.0086-0088, and any later ones) therefore must not leak into the
# simulated r0092 plan: pin the library leaf to the one r0092 shipped. Every
# other app already sits at its r0092-era leaf.
R0092_LIBRARY_LEAF = ("library", "0085_remote_dj_queue_set_next_access")


def _r0092_leaf_nodes(nodes):
    return [
        R0092_LIBRARY_LEAF if node[0] == "library"
        else ("updatecenter", "0003_updatejob_migration_plan_review_and_more")
        if node[0] == "updatecenter" else node
        for node in nodes
    ]


def r0092_tree_probe(**kwargs):
    original = MigrationGraph.leaf_nodes

    def leaf_nodes(graph, app=None):
        return _r0092_leaf_nodes(original(graph, app))

    with mock.patch.object(MigrationGraph, "leaf_nodes", leaf_nodes):
        return build_probe_payload(**kwargs)


R0088_COMMIT = "edc5d5c8f345ba18646db66a9a050093d1a076b0"
R0092_TARGET_COMMIT = "abb3a54df5d7de6da1af07a43324e42d602ed75c"
R0092_DESCRIPTOR = "89fe055e69769e4a5716f8c3ce010fe7a33b3cb3a58f400ca93401296764d4ce"


class PersistentSupervisor:
    """Authoritative supervisor double persisted across Job A and Job B."""
    active_generation = 7
    active_descriptor = "7" * 64
    active_slot = "A"
    activation_requests = []
    acceptance_confirmations = []

    def __init__(self, socket_path):
        self.socket_path = socket_path

    def get_runtime_state(self):
        return {
            "ok": True, "active_slot": type(self).active_slot,
            "active_generation": type(self).active_generation,
            "active_descriptor_sha256": type(self).active_descriptor,
            "activation_in_flight": bool(type(self).activation_requests)
            and not bool(type(self).acceptance_confirmations),
            "phase": None,
            "runtime_activation_accepted": bool(type(self).acceptance_confirmations),
        }

    def request_activation(self, **kwargs):
        type(self).activation_requests.append(kwargs)
        return {"ok": True}

    def confirm_runtime_acceptance(self, **kwargs):
        type(self).acceptance_confirmations.append(kwargs)
        type(self).active_generation = kwargs["candidate_generation"]
        type(self).active_descriptor = kwargs["candidate_descriptor_sha256"]
        type(self).active_slot = kwargs["candidate_slot"]
        return {"ok": True}


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

    def __init__(self, *args, leaf_targets, transition_refs, **kwargs):
        super().__init__(*args, **kwargs)
        self.leaf_targets = leaf_targets
        self.transition_refs = set(transition_refs)
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
        # The subprocess fixture proves the legacy wire shape, while this
        # graph models the real r0088 source boundary for protocol-6 prefix
        # ownership checks (the tiny fixture intentionally has only 3 nodes).
        target_nodes = r0092_tree_probe()["nodes"]
        return {
            **self.legacy_current,
            "nodes": {ref: deps for ref, deps in target_nodes.items() if ref not in self.transition_refs},
        }

    def _probe(self, source, *, release_id=None, target_commit=None, recovery_plan_refs=()):
        if recovery_plan_refs:
            raise AssertionError("historical r0092 acceptance does not enter partial-prefix recovery")
        if release_id is not None:
            return r0092_tree_probe(release_id=release_id, target_commit=target_commit)
        return r0092_tree_probe()

    def _run_app(self, source, arguments, *, timeout):
        if arguments and arguments[0] == "migrate":
            self.migrate_calls += 1
            MigrationExecutor(connection).migrate([(arguments[1], arguments[2])])
            return ProcessResult(tuple(arguments), 0, b"", b""), {}
        raise AssertionError(f"unexpected application mutation: {arguments!r}")

    def _observe_migration_records(self, refs):
        # The REAL test-database recorder rows these in-process migrations wrote.
        return orm_migration_records(refs)

    def _advance_source(self, plan):
        self.source_advance_calls += 1
        self.live_head = plan.target_commit

    def _postflight_http(self):
        self.http_calls += 1


class R0092ProductionBootstrapAcceptanceTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        migration_executor = MigrationExecutor(connection)
        self.current_leaf_targets = migration_executor.loader.graph.leaf_nodes()
        self.leaf_targets = _r0092_leaf_nodes(self.current_leaf_targets)
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
        config = config_dict(self.root, str(self.root / "upstream.git"))
        config["phase_d_supervisor_slots_root"] = str(self.root / "runtime-slots")
        config["phase_d_supervisor_activation_socket"] = str(self.root / "activation.sock")
        self.config = validate_config_dict(config, allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.systemd = RecordingSystemd()
        r0092_data = json.loads(
            (PROJECT_ROOT / "deploy" / "releases" / "r0092.json").read_text(encoding="utf-8")
        )
        protected_runtime = parse_protected_runtime_field(r0092_data["protected_runtime"])
        self.assertEqual(protected_runtime.generation, 8)
        self.assertEqual(protected_runtime.runtime_version, 9)
        self.assertEqual(protected_runtime.descriptor_sha256, R0092_DESCRIPTOR)
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
            protected_runtime_transition=ProtectedRuntimeTransition(
                field=protected_runtime, release_id="r0092", previous_release_id="r0091",
                commit=R0092_TARGET_COMMIT,
            ),
        )
        self.executor = ProductionStateExecutor(
            self.config, self.store, CommandRunner(), systemd_manager=self.systemd,
            leaf_targets=self.leaf_targets, transition_refs=self.migration_refs,
        )
        self.executor.repository.fetch = lambda: R0092_TARGET_COMMIT
        PersistentSupervisor.active_generation = 7
        PersistentSupervisor.active_descriptor = "7" * 64
        PersistentSupervisor.active_slot = "A"
        PersistentSupervisor.activation_requests = []
        PersistentSupervisor.acceptance_confirmations = []

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.current_leaf_targets)
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
        first_payload = r0092_tree_probe(
            release_id="r0092", target_commit=R0092_TARGET_COMMIT,
        )
        self.assertEqual(tuple(item["ref"] for item in first_payload["plan"]), self.migration_refs)
        self.assertEqual(len(first_payload["manual_operations"]), 6)
        self.assertIsNone(first_payload["approval"])

        job_a = self._accept()
        verification = types.SimpleNamespace(ok=True, reasons=(), candidate_policy=None)
        runtime_materialized = types.SimpleNamespace(
            descriptor_bytes=b"{}", descriptor_sha256=R0092_DESCRIPTOR,
        )
        runtime_publications = []
        with mock.patch("isadoraair_updater.executor.SupervisorClient", PersistentSupervisor), \
                mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan), \
                mock.patch("isadoraair_updater.executor.materialize_candidate", return_value=runtime_materialized), \
                mock.patch("isadoraair_updater.executor.stage_attestations"), \
                mock.patch("isadoraair_updater.executor.verify_candidate_independently", return_value=verification), \
                mock.patch.object(self.executor, "_load_phase_d_trust_policy", return_value=object()), \
                mock.patch("isadoraair_updater.executor.publish_to_candidate_slot",
                           side_effect=lambda *args, **kwargs: runtime_publications.append((args, kwargs))):
            yielded = self.executor.execute(job_a)

        self.assertEqual(yielded["state"], "running")
        self.assertIn("runtime_activation_requested", yielded["milestones"])
        self.assertEqual(len(PersistentSupervisor.activation_requests), 1)
        self.assertEqual(len(runtime_publications), 1)

        # A separately constructed generation-8 worker resumes the same
        # durable Job A, accepts the activation, and only then reaches review.
        candidate_store = JobStore(
            self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False,
        )
        self.addCleanup(candidate_store.close)
        candidate = ProductionStateExecutor(
            self.config, candidate_store, CommandRunner(), systemd_manager=self.systemd,
            leaf_targets=self.leaf_targets, transition_refs=self.migration_refs,
            expected_handoff_generation=8,
            expected_handoff_descriptor_sha256=R0092_DESCRIPTOR,
            expected_resumable_job_uuid=job_a,
        )
        candidate.repository.fetch = lambda: R0092_TARGET_COMMIT
        with mock.patch("isadoraair_updater.executor.SupervisorClient", PersistentSupervisor), \
                mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan), \
                mock.patch("isadoraair_updater.executor.materialize", side_effect=lambda *args: self._staged(job_a)), \
                mock.patch("isadoraair_updater.executor.cleanup"), \
                mock.patch("isadoraair_updater.executor.create_checkpoint", return_value={"schema_version": 1}), \
                mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=False):
            result_a = candidate.execute(job_a)


        self.assertEqual(result_a["state"], "manual_intervention_required")
        self.assertEqual(result_a["failure_classification"], "MIGRATION_OPERATION_MANUAL")
        self.assertIn("target_staged", result_a["milestones"])
        self.assertNotIn("migration_started", result_a["milestones"])
        self.assertNotIn("source_advanced", result_a["milestones"])
        self.assertEqual(candidate.migrate_calls, 0)
        self.assertEqual(candidate.source_advance_calls, 0)
        self.assertEqual(self.systemd.reconciled, 0)
        self.assertEqual(PersistentSupervisor.active_generation, 8)
        self.assertEqual(PersistentSupervisor.active_descriptor, R0092_DESCRIPTOR)
        self.assertEqual(len(PersistentSupervisor.acceptance_confirmations), 1)
        self.assertNotIn("updatecenter_migrationplanapproval", connection.introspection.table_names())

        review_before = dict(result_a["migration_plan_review"])
        approval, created = candidate.approval_store.create_from_job(
            job_a,
            confirmed_migration_plan_digest=review_before["migration_plan_digest"],
            approved_by_username="release_operator",
            reason="Reviewed all six operations against the r0088 schema and data.",
        )
        self.assertTrue(created)
        self.assertEqual(self.store.load(job_a)["migration_plan_review"], review_before)

        job_b = self._accept()
        self.assertNotEqual(job_a, job_b)
        job_b_executor = ProductionStateExecutor(
            self.config, self.store, CommandRunner(), systemd_manager=self.systemd,
            leaf_targets=self.leaf_targets, transition_refs=self.migration_refs,
        )
        job_b_executor.repository.fetch = lambda: R0092_TARGET_COMMIT
        with mock.patch("isadoraair_updater.executor.SupervisorClient", PersistentSupervisor), \
                mock.patch("isadoraair_updater.executor.derive_plan", return_value=self.plan), \
                mock.patch("isadoraair_updater.executor.materialize_candidate") as restage_runtime, \
                mock.patch("isadoraair_updater.executor.publish_to_candidate_slot") as republish_runtime, \
                mock.patch("isadoraair_updater.executor.materialize", side_effect=lambda *args: self._staged(job_b)), \
                mock.patch("isadoraair_updater.executor.cleanup"), \
                mock.patch("isadoraair_updater.executor.create_checkpoint", return_value={"schema_version": 1}), \
                mock.patch("isadoraair_updater.executor.verify_checkpoint", return_value=False):
            result_b = job_b_executor.execute(job_b)

        self.assertEqual(result_b["state"], "succeeded")
        self.assertEqual(job_b_executor.migrate_calls, len(self.migration_refs))
        self.assertEqual(job_b_executor.source_advance_calls, 1)
        self.assertEqual(self.systemd.reconciled, 1)
        self.assertEqual(self.systemd.restarted, ["isadoraair-gunicorn", "isadoraair-engine"])
        self.assertIn("database_verified", result_b["milestones"])
        self.assertIn("source_advanced", result_b["milestones"])
        self.assertIn("runtime_already_authoritative", result_b["milestones"])
        self.assertNotIn("runtime_activation_requested", result_b["milestones"])
        restage_runtime.assert_not_called()
        republish_runtime.assert_not_called()
        self.assertEqual(len(PersistentSupervisor.activation_requests), 1)
        self.assertIn("updatecenter_migrationplanapproval", connection.introspection.table_names())
        after = r0092_tree_probe()
        self.assertEqual(after["plan"], [])
        self.assertTrue(set(self.migration_refs).issubset(set(after["applied"])))
        self.assertEqual(approval["source_job_id"], job_a)
