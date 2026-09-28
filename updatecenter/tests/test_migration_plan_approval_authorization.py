"""Reviewed migration approval -- authorization boundary.

Mirrors this app's own existing Update Center authorization convention
(staff-or-superuser may VIEW, superuser-only may MUTATE -- see views.py's
_permission_check / start_update's own is_superuser gate) rather than
inventing a new one. Talent Roles/Capabilities (authz app, roadmap 2.5)
are a completely separate authorization system with no bearing on the
Update Center at all -- proven directly here rather than assumed.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings

from authz.models import Capability, GroupRole, Role, RoleCapability
from updatecenter.models import MigrationPlanApproval, UpdateJob, UpdateJobState

User = get_user_model()

DIGEST = "a" * 64


def make_job(*, migration_plan_review=None, state=UpdateJobState.MANUAL_INTERVENTION_REQUIRED):
    return UpdateJob.objects.create(
        installed_release_id="r0088", target_release_id="r0089",
        installed_commit="1" * 40, target_commit="2" * 40,
        state=state,
        failure_classification="MIGRATION_OPERATION_MANUAL" if migration_plan_review else "",
        migration_plan_review=migration_plan_review,
    )


def make_review(digest=DIGEST):
    return {
        "release_id": "r0089", "target_commit": "2" * 40, "manifest_sha256": "3" * 64,
        "migration_plan_digest": digest,
        "manual_operations": [{
            "ref": "authz.0001_initial", "operation_index": 0, "operation": "AddField",
            "classification": "manual", "detail": "non-null AddField uses relational field",
        }],
    }


@override_settings(SECURE_SSL_REDIRECT=False)
class ApprovalAuthorizationTests(TestCase):
    def setUp(self):
        self.job = make_job(migration_plan_review=make_review())

        self.plain_user = User.objects.create_user("plain_uc_user", "plain@example.invalid", "pw")
        self.staff_user = User.objects.create_user("staff_uc_user", "staff@example.invalid", "pw", is_staff=True)
        self.superuser = User.objects.create_superuser("su_uc_user", "su@example.invalid", "pw")

        # Roadmap 2.5 talent roles -- an entirely separate authorization
        # system. Proven here to have no bearing on this one at all.
        self.remote_dj = User.objects.create_user("remote_dj_uc_user", "dj@example.invalid", "pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_dj.groups.add(dj_group)

        self.contributor = User.objects.create_user("contributor_uc_user", "contrib@example.invalid", "pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        # Even a talent Role holding EVERY capability in the vocabulary
        # (Station Administrator) must not gain Update Center authority --
        # the two systems are disjoint by design.
        self.station_admin_talent = User.objects.create_user("station_admin_talent", "sa@example.invalid", "pw")
        sa_group = Group.objects.create(name="Update Center Test Station Admin Group")
        role, _ = Role.objects.get_or_create(name="Station Administrator")
        for capability in Capability.objects.all():
            RoleCapability.objects.get_or_create(role=role, capability=capability)
        GroupRole.objects.get_or_create(group=sa_group, defaults={"role": role})
        self.station_admin_talent.groups.add(sa_group)

    def _approve_url(self, job=None):
        return f"/updates/jobs/{(job or self.job).id}/migration-review/approve/"

    def _review_url(self, job=None):
        return f"/updates/jobs/{(job or self.job).id}/migration-review/"

    def test_anonymous_cannot_view_review_page(self):
        resp = Client().get(self._review_url())
        # LoginRequiredMiddleware redirects anonymous users to the login
        # page (302) before this view's own permission check even runs --
        # same established pattern as test_views.py's own anonymous test.
        self.assertIn(resp.status_code, (302, 403))

    def test_anonymous_cannot_approve(self):
        resp = Client().post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_ordinary_authenticated_user_cannot_view_or_approve(self):
        # library.middleware.GroupBasedAccessMiddleware (pre-existing,
        # unrelated to this feature) redirects any authenticated user
        # outside its group-access allowlist before this view's own
        # staff-or-superuser check would even run -- same established
        # pattern as test_views.py's own test_ordinary_authenticated_
        # non_staff_denied. Either outcome proves the page/action isn't
        # reachable.
        client = Client()
        client.force_login(self.plain_user)
        self.assertIn(client.get(self._review_url()).status_code, (302, 403))
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_remote_host_cannot_approve(self):
        client = Client()
        client.force_login(self.remote_dj)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_contributor_cannot_approve(self):
        client = Client()
        client.force_login(self.contributor)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_talent_role_holding_every_capability_still_cannot_approve(self):
        client = Client()
        client.force_login(self.station_admin_talent)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_staff_can_view_but_cannot_approve(self):
        client = Client()
        client.force_login(self.staff_user)
        self.assertEqual(client.get(self._review_url()).status_code, 200)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "x"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_superuser_can_view_and_approve(self):
        client = Client()
        client.force_login(self.superuser)
        self.assertEqual(client.get(self._review_url()).status_code, 200)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "Reviewed and safe."})
        self.assertEqual(resp.status_code, 302)
        approval = MigrationPlanApproval.objects.get()
        self.assertEqual(approval.target_release_id, "r0089")
        self.assertEqual(approval.migration_plan_digest, DIGEST)
        self.assertEqual(approval.approved_by_username, "su_uc_user")
        self.assertEqual(approval.reason, "Reviewed and safe.")

    def test_superuser_approval_requires_a_written_reason(self):
        client = Client()
        client.force_login(self.superuser)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "   "})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_stale_plan_digest_rejected(self):
        """The review page's own confirmed_migration_plan_digest must
        match the job's CURRENT migration_plan_review -- simulates the
        plan having changed underneath an already-open review tab."""
        client = Client()
        client.force_login(self.superuser)
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": "9" * 64, "reason": "stale"})
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(MigrationPlanApproval.objects.exists())

    def test_forged_approval_row_for_another_release_never_matches(self):
        """Directly proves an approval's identity is release+digest, not
        digest alone -- a row for r0090 with the SAME digest string must
        never satisfy a lookup for r0089."""
        MigrationPlanApproval.objects.create(
            target_release_id="r0090", target_commit="2" * 40,
            migration_plan_digest=DIGEST, approved_by_username="someone", reason="wrong release",
        )
        from updatecenter.management.commands.updatecenter_probe import _lookup_approval
        self.assertEqual(_lookup_approval(release_id="r0089", digest=DIGEST), {"found": False})
        self.assertTrue(
            _lookup_approval(release_id="r0090", digest=DIGEST)["found"]
        )

    def test_job_without_migration_plan_review_redirects_with_message(self):
        empty_job = make_job(migration_plan_review=None, state=UpdateJobState.FAILED)
        client = Client()
        client.force_login(self.superuser)
        resp = client.get(self._review_url(empty_job))
        self.assertEqual(resp.status_code, 302)

    def test_duplicate_approval_of_the_same_exact_plan_does_not_crash(self):
        client = Client()
        client.force_login(self.superuser)
        client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "first"})
        resp = client.post(self._approve_url(), {"confirmed_migration_plan_digest": DIGEST, "reason": "second"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(MigrationPlanApproval.objects.count(), 1)
