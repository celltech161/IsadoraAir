"""UpdateJob schema and Phase B audit-mirror constraints."""
import uuid
from pathlib import Path

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase

from updatecenter.models import UpdateJob, UpdateJobState


def _make_job(**overrides):
    defaults = dict(
        installed_release_id="r0001", target_release_id="r0002",
        installed_commit="a" * 40, target_commit="b" * 40,
        initiated_by_username="tester",
    )
    defaults.update(overrides)
    return UpdateJob.objects.create(**defaults)


class JobWriteConfinementTests(TestCase):
    def test_zero_jobs_exist_by_default(self):
        self.assertEqual(UpdateJob.objects.count(), 0)

    def test_non_test_writes_are_confined_to_phase_b_job_service(self):
        app_root = Path(__file__).resolve().parents[1]
        for path in app_root.rglob("*.py"):
            if "tests" in path.parts or "migrations" in path.parts or path.name == "models.py":
                continue
            text = path.read_text(encoding="utf-8")
            if path.name == "job_service.py":
                continue
            self.assertNotIn("UpdateJob.objects.create", text, str(path))
            self.assertNotIn("UpdateJob.objects.bulk_create", text, str(path))


class StateVocabularyTests(TestCase):
    def test_default_state_is_queued(self):
        job = _make_job()
        self.assertEqual(job.state, UpdateJobState.QUEUED)

    def test_terminal_states_are_exactly_the_documented_set(self):
        self.assertEqual(
            UpdateJobState.TERMINAL,
            {UpdateJobState.SUCCEEDED, UpdateJobState.FAILED,
             UpdateJobState.MANUAL_INTERVENTION_REQUIRED,
             UpdateJobState.INTERRUPTED, UpdateJobState.CANCELLED},
        )

    def test_every_choice_value_is_a_valid_state_constant(self):
        choice_values = {v for v, _ in UpdateJobState.CHOICES}
        declared = {
            UpdateJobState.QUEUED, UpdateJobState.PLANNED, UpdateJobState.RUNNING,
            UpdateJobState.SUBMISSION_UNCERTAIN,
            UpdateJobState.SUCCEEDED, UpdateJobState.FAILED,
            UpdateJobState.MANUAL_INTERVENTION_REQUIRED, UpdateJobState.INTERRUPTED,
            UpdateJobState.CANCELLED,
        }
        self.assertEqual(choice_values, declared)


class ConcurrencyConstraintTests(TestCase):
    """§18: at most one non-terminal job at a time, enforced at the DB
    layer (a Python-only lock cannot survive multiple Gunicorn workers
    or a restart)."""

    def test_two_active_locks_rejected(self):
        _make_job(active_lock=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_job(active_lock=1)

    def test_multiple_terminal_null_lock_rows_are_fine(self):
        _make_job(active_lock=None, state=UpdateJobState.SUCCEEDED)
        _make_job(active_lock=None, state=UpdateJobState.FAILED)
        _make_job(active_lock=None, state=UpdateJobState.CANCELLED)
        self.assertEqual(UpdateJob.objects.count(), 3)

    def test_active_lock_cannot_use_an_alternate_non_null_value(self):
        """Uniqueness alone would allow active_lock=1 and =2 at the
        same time. The check constraint closes that bypass so the DB
        really enforces at most one active job."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _make_job(active_lock=2)


class IdentityFieldTests(TestCase):
    def test_id_is_a_real_uuid(self):
        job = _make_job()
        self.assertIsInstance(job.id, uuid.UUID)

    def test_two_jobs_get_different_ids(self):
        j1 = _make_job()
        j2 = _make_job()
        self.assertNotEqual(j1.id, j2.id)

    def test_initiated_by_username_survives_user_deletion(self):
        user = User.objects.create_user("willbedeleted", password="x")
        job = _make_job(initiated_by=user, initiated_by_username=user.username)
        user.delete()
        job.refresh_from_db()
        self.assertIsNone(job.initiated_by)
        self.assertEqual(job.initiated_by_username, "willbedeleted")


class PlanSnapshotTests(TestCase):
    def test_plan_snapshot_json_round_trips(self):
        snapshot = {"target_commit": "a" * 40, "releases_in_plan": ["r0002", "r0003"]}
        job = _make_job(plan_snapshot=snapshot, plan_fingerprint="f" * 64)
        job.refresh_from_db()
        self.assertEqual(job.plan_snapshot, snapshot)
        self.assertEqual(job.plan_fingerprint, "f" * 64)

    def test_plan_snapshot_defaults_to_empty_dict(self):
        job = _make_job()
        self.assertEqual(job.plan_snapshot, {})
