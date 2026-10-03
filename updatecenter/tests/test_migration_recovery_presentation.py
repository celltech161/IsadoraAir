"""P1 1.17 S1 -- the partial-prefix recovery UI must never overstate evidence.

Renders the real /updates/ dashboard. The recovery block may appear only for
a failed job whose root-owned evidence is finalized, proven and actionable;
never for a successful job, never for unfinalized evidence, and the
authorization line shows the executor-recorded source, not a guess.
"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.tests.test_phase_c_integration import READY_PING, ReadyPlan

M1 = "sample.0001_first"
M2 = "sample.0002_second"
BANNER = "UPDATER_OWNED_PARTIAL_PREFIX"
RETRY = "retry the same exact release"


def evidence(**changes):
    value = {
        "schema_version": 1, "classification": BANNER,
        "evidence_job_id": "11111111-1111-1111-1111-111111111111", "prior_job_id": None,
        "release_id": "r0105", "target_commit": "b" * 40, "manifest_sha256": "c" * 64,
        "migration_plan_digest": "d" * 64, "trusted_plan_fingerprint": "f" * 64,
        "ordered_target_plan": [M1, M2], "successful_prefix": [M1],
        "prefix_records": {M1: {"id": 41, "applied": "2026-10-03T12:00:41.000041Z"}},
        "checkpoint": {"sha256": "9" * 64}, "failure_classification": "MIGRATION_FAILED",
        "failure_detail": "synthetic M2 failure", "continued_from_job_id": None,
        "authorization_source": "not_required", "finalized": True,
        "first_remaining_migration": M2, "permitted_action": "retry_same_exact_release",
    }
    value.update(changes)
    return value


@override_settings(SECURE_SSL_REDIRECT=False)
class RecoveryPresentationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.client.force_login(User.objects.create_superuser("uc-recovery-root"))

    def render(self, *, state, recovery):
        UpdateJob.objects.create(
            initiated_by_username="uc-recovery-root",
            installed_release_id="r0104", target_release_id="r0105",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=state, migration_recovery=recovery,
        )
        readiness = {**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}
        with (
            patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()),
            patch("updatecenter.views._backend_readiness", return_value=readiness),
        ):
            response = self.client.get(reverse("updatecenter:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def test_successful_migration_job_shows_no_recovery_or_retry(self):
        content = self.render(state=UpdateJobState.SUCCEEDED, recovery=evidence(
            successful_prefix=[M1, M2], first_remaining_migration=None, permitted_action="none",
            failure_classification="", failure_detail="",
            prefix_records={M1: {"id": 41, "applied": "2026-10-03T12:00:41.000041Z"},
                            M2: {"id": 42, "applied": "2026-10-03T12:00:42.000042Z"}},
        ))
        self.assertNotIn(BANNER, content)
        self.assertNotIn(RETRY, content)

    def test_finalized_partial_prefix_failure_shows_the_proven_recovery(self):
        content = self.render(state=UpdateJobState.MANUAL_INTERVENTION_REQUIRED, recovery=evidence())
        self.assertIn(BANNER, content)
        self.assertIn(RETRY, content)
        self.assertIn(M1, content)
        self.assertIn("not required (no manual operations)", content)

    def test_unfinalized_evidence_claims_nothing(self):
        content = self.render(state=UpdateJobState.FAILED, recovery=evidence(finalized=False))
        self.assertNotIn(BANNER, content)
        self.assertNotIn("against the database's own migration records", content)
        self.assertNotIn(RETRY, content)

    def test_authorization_source_is_rendered_as_recorded(self):
        for source, label in (
            ("central", "central (trusted companion authorization)"),
            ("local", "local (exact station approval)"),
        ):
            with self.subTest(source=source):
                UpdateJob.objects.all().delete()
                content = self.render(state=UpdateJobState.FAILED, recovery=evidence(authorization_source=source))
                self.assertIn(label, content)

    def test_unknown_authorization_source_or_classification_is_not_presented(self):
        for change in ({"authorization_source": "central_or_exact_local"}, {"classification": "SOMETHING_ELSE"},
                       {"permitted_action": "none"}, {"successful_prefix": []}):
            with self.subTest(change=change):
                UpdateJob.objects.all().delete()
                self.assertNotIn(BANNER, self.render(state=UpdateJobState.FAILED, recovery=evidence(**change)))

    def test_property_is_none_for_running_job(self):
        job = UpdateJob(state=UpdateJobState.RUNNING, migration_recovery=evidence())
        self.assertIsNone(job.partial_prefix_recovery)
