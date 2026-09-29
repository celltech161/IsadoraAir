"""Reviewed migration approval -- terminal-job semantics.

manual_intervention_required remains terminal (JobStore.TERMINAL_STATES
is unchanged by this feature); approving a plan never resumes or
mutates the job that discovered it -- it only ever creates a NEW,
independent MigrationPlanApproval row. A brand-new job is required to
actually consume it. Also proves backward compatibility with job-state
files written before this field existed (the real, immutable historical
r0089 job, 0053ede7-ffca-46e8-b163-c3f8aa5226c4, is exactly this shape).
"""
import json
from pathlib import Path
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.jobs import JobStore, TERMINAL_STATES
from updatecenter.models import MigrationPlanApproval, UpdateJob, UpdateJobState

User = get_user_model()


class JobStoreBackwardCompatibilityTests(TestCase):
    """Uses the real, protected-runtime JobStore directly -- not a
    Django model -- to prove root's own on-disk job state (immutable
    once terminal) never needs rewriting for this feature to work."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_manual_intervention_required_is_still_terminal(self):
        self.assertIn("manual_intervention_required", TERMINAL_STATES)

    def test_pre_existing_job_state_file_missing_the_new_field_still_loads(self):
        """Simulates the exact real historical job (0053ede7...): a
        state file written by code that predates migration_plan_review
        entirely. load()/GET_JOB_STATUS must never require that key."""
        job_id = "05300000-0000-4000-8000-000000000000"
        state, _created = self.store.accept(job_id, "r0089", "f" * 64)
        del state["migration_plan_review"]  # simulate the OLD on-disk shape directly
        self.store._atomic_write(self.store._state_path(job_id), state)

        reloaded = self.store.load(job_id)
        self.assertNotIn("migration_plan_review", reloaded)
        # The exact .get() access GET_JOB_STATUS's handler uses -- must
        # not raise, must default sensibly.
        self.assertIsNone(reloaded.get("migration_plan_review"))

        # fail() on this same job (its real historical past) must still
        # work even though the key was absent going in.
        failed = self.store.fail(job_id, "MIGRATION_OPERATION_MANUAL", "test", manual=True)
        self.assertEqual(failed["state"], "manual_intervention_required")

    def test_fail_without_migration_plan_review_leaves_it_unset(self):
        job_id = "05300000-0000-4000-8000-000000000001"
        self.store.accept(job_id, "r0089", "f" * 64)
        state = self.store.fail(job_id, "SAFE_EXECUTION_FAILURE", "unrelated failure", manual=False)
        self.assertIsNone(state.get("migration_plan_review"))

    def test_fail_with_migration_plan_review_persists_it_verbatim(self):
        job_id = "05300000-0000-4000-8000-000000000002"
        self.store.accept(job_id, "r0089", "f" * 64)
        review = {
            "release_id": "r0089", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
            "migration_plan_digest": "d" * 64,
            "manual_operations": [{"ref": "authz.0001_initial", "operation_index": 0, "operation": "AddField", "classification": "manual", "detail": "x"}],
        }
        state = self.store.fail(job_id, "MIGRATION_OPERATION_MANUAL", "test", manual=True, migration_plan_review=review)
        self.assertEqual(state["migration_plan_review"], review)
        reloaded = self.store.load(job_id)
        self.assertEqual(reloaded["migration_plan_review"], review)

    def test_migration_plan_review_must_be_json_serializable(self):
        job_id = "05300000-0000-4000-8000-000000000003"
        self.store.accept(job_id, "r0089", "f" * 64)
        with self.assertRaises(Exception):
            self.store.fail(job_id, "X", "y", manual=True, migration_plan_review={"bad": object()})


@override_settings(SECURE_SSL_REDIRECT=False)
class ApprovalDoesNotMutateSourceJobTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("term_su", "su@example.invalid", "pw")
        self.review = {
            "release_id": "r0089", "target_commit": "2" * 40, "manifest_sha256": "3" * 64,
            "migration_plan_digest": "a" * 64,
            "trusted_plan_fingerprint": "4" * 64,
            "manual_operations": [{"ref": "authz.0001_initial", "operation_index": 0, "operation": "AddField", "classification": "manual", "detail": "x"}],
        }
        self.job = UpdateJob.objects.create(
            installed_release_id="r0088", target_release_id="r0089",
            installed_commit="1" * 40, target_commit="2" * 40,
            state=UpdateJobState.MANUAL_INTERVENTION_REQUIRED,
            failure_classification="MIGRATION_OPERATION_MANUAL",
            migration_plan_review=self.review,
        )
        self.before_state = self.job.state
        self.before_finished_at = self.job.finished_at
        self.before_review = json.loads(json.dumps(self.job.migration_plan_review))
        self.client_patch = mock.patch("updatecenter.views.UpdaterClient")
        client_class = self.client_patch.start()
        client_class.return_value.approve_migration_plan.return_value = {
            "ok": True, "approval": {"approval_id": "root-owned"},
        }

    def tearDown(self):
        self.client_patch.stop()

    def test_approving_never_changes_the_source_jobs_own_fields(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.post(
            f"/updates/jobs/{self.job.id}/migration-review/approve/",
            {"confirmed_migration_plan_digest": "a" * 64, "reason": "safe"},
        )
        self.assertEqual(resp.status_code, 302)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, self.before_state)
        self.assertEqual(self.job.finished_at, self.before_finished_at)
        self.assertEqual(self.job.migration_plan_review, self.before_review)
        # Exactly one NEW, independent row was created -- the job was
        # never resumed and no second job was auto-started.
        self.assertEqual(MigrationPlanApproval.objects.count(), 1)
        self.assertEqual(UpdateJob.objects.count(), 1)

    def test_approval_records_provenance_to_the_source_job(self):
        approval = MigrationPlanApproval.objects.create(
            target_release_id="r0089", target_commit="2" * 40,
            migration_plan_digest="a" * 64, source_job=self.job,
            approved_by=self.superuser, approved_by_username="term_su", reason="x",
        )
        self.assertEqual(approval.source_job_id, self.job.id)

    def test_deleting_the_source_job_does_not_delete_the_approval(self):
        """SET_NULL, not CASCADE -- the approval's own digest is
        self-contained and must survive the bounded root job-state
        retention window pruning its source job eventually."""
        approval = MigrationPlanApproval.objects.create(
            target_release_id="r0089", target_commit="2" * 40,
            migration_plan_digest="a" * 64, source_job=self.job,
            approved_by=self.superuser, approved_by_username="term_su", reason="x",
        )
        self.job.delete()
        approval.refresh_from_db()
        self.assertIsNone(approval.source_job_id)
        self.assertEqual(approval.migration_plan_digest, "a" * 64)
