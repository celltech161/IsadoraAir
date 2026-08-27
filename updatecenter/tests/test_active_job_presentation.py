"""fix/updatecenter-active-job-presentation -- UI correctness fix.

Observed during REAL successful r0009 installs on both KOGR and WRJE:
/updates/ simultaneously showed

    Installation blocked
    - Update job <uuid> still owns the active lock.

while the live #uc-job progress panel correctly showed

    Update in progress -- r0009
    State: running

The update was proceeding normally. The active-lock execution blocker
is CORRECT and load-bearing (it is exactly what prevents a second
`start_update` POST while a job owns the lock -- see
_execution_blockers in updatecenter/views.py, untouched by this fix).
The bug was pure presentation: the ready_to_plan panel's action area
only ever distinguished two states (update_eligible True/False), and
an active job correctly makes update_eligible False -- so a perfectly
normal in-progress install rendered identically to a genuine inability
to install.

Fix: the ready_to_plan panel's action area now distinguishes THREE
states -- {% if active_job %} (neutral "Update in progress" -- no
duplication of #uc-job's own job id/state/step/log) {% elif
update_eligible %} (unchanged Install form) {% else %} (unchanged
"Installation blocked" + blockers). No view/context changes, no
change to _execution_blockers, no change to UpdateJob.active_lock
semantics, no change to job_status/terminal-reload behavior."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch

from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.tests.test_phase_c_integration import READY_PING, ReadyPlan


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class ActiveJobPresentationTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("uc-active-job-root")
        self.client = Client()
        self.client.force_login(self.superuser)

    def _get_dashboard(self, active_job=None, readiness_ready=True):
        readiness = {**READY_PING, "ready": readiness_ready, "execution_armed": True,
                     "detail": "ready" if readiness_ready else "not ready"}
        patches = [
            patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()),
            patch("updatecenter.views._backend_readiness", return_value=readiness),
        ]
        if active_job is not None:
            patches.append(patch("updatecenter.views._refresh_job", return_value=(active_job, None)))
        for p in patches:
            p.start()
        try:
            response = self.client.get(reverse("updatecenter:dashboard"))
        finally:
            for p in reversed(patches):
                p.stop()
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8")

    def _running_job(self):
        return UpdateJob.objects.create(
            initiated_by_username="uc-active-job-root",
            installed_release_id="r0008", target_release_id="r0009",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.RUNNING, active_lock=1,
        )

    # -- 1. no active job + eligible ------------------------------------

    def test_no_active_job_and_eligible_shows_install_button_only(self):
        content = self._get_dashboard(active_job=None)
        self.assertIn(f'Install {ReadyPlan.target_release_id}', content)
        self.assertIn(
            f'action="{reverse("updatecenter:start-update")}"', content)
        self.assertNotIn("Installation blocked", content)
        self.assertNotIn("Update in progress", content)
        self.assertNotIn("still owns the active lock", content)

    # -- 2. active nonterminal job ---------------------------------------

    def test_active_job_shows_in_progress_not_blocked(self):
        job = self._running_job()
        content = self._get_dashboard(active_job=job)
        # New neutral presentation is present.
        self.assertIn(f"Update in progress — {job.target_release_id}", content)
        self.assertIn(
            "Installation controls are unavailable until it completes.",
            content,
        )
        # The Install form/button must NOT appear.
        self.assertNotIn(f'action="{reverse("updatecenter:start-update")}"', content)
        # Neither spelling of "Installation blocked" appears anywhere --
        # covers both the ready_to_plan action-area heading this fix
        # changes AND the separate non-ready-to-plan blocked section
        # (which shouldn't render here either, since safety_status is
        # still "ready_to_plan" while the checkout hasn't moved yet).
        self.assertNotIn("Installation blocked", content)
        # The raw active-lock blocker text must not appear as an
        # operator-facing warning anywhere on the page.
        self.assertNotIn("still owns the active lock", content)

    def test_active_job_leaves_existing_uc_job_progress_panel_intact(self):
        """The existing #uc-job panel remains the authoritative detail
        source -- job id/state/step/log -- untouched and undupliacted
        by the new neutral action-area presentation."""
        job = self._running_job()
        content = self._get_dashboard(active_job=job)
        self.assertIn('id="uc-job"', content)
        self.assertIn(f'>{job.id}<', content)
        self.assertIn('id="uc-job-state"', content)
        self.assertIn('id="uc-job-step"', content)
        self.assertIn('id="uc-job-log"', content)
        # data-status-url still points at the real job-status endpoint.
        self.assertIn(
            reverse("updatecenter:job-status", args=[job.id]), content,
        )

    def test_active_job_action_area_does_not_duplicate_job_detail(self):
        """The new block must stay a short neutral notice -- it must
        not re-render state/step/log itself (that's #uc-job's job)."""
        job = self._running_job()
        content = self._get_dashboard(active_job=job)
        action_area = content[
            content.index(f"{ReadyPlan.installed_release_id} → {ReadyPlan.target_release_id}"):
            content.index('<details class="uc-details">')
        ]
        self.assertIn("Update in progress", action_area)
        self.assertNotIn("uc-job-log", action_area)
        self.assertNotIn("Current step", action_area)
        self.assertNotIn("uc-job-state", action_area)

    # -- 3. genuine non-active-job blocker --------------------------------

    def test_genuine_blocker_without_active_job_still_shows_blocked(self):
        """A real reason update_eligible is False (here: backend not
        ready) with NO active job must still show the original
        "Installation blocked" presentation and the real blocker
        detail -- this fix must not suppress genuine warnings."""
        content = self._get_dashboard(active_job=None, readiness_ready=False)
        self.assertIn("Installation blocked", content)
        self.assertIn("not ready", content)
        self.assertNotIn("Update in progress", content)

    # -- 4. backend concurrency protection unchanged ----------------------

    def test_start_update_still_rejected_while_active_job_owns_lock(self):
        """Backend must continue to reject a second Start Update POST
        while a job holds active_lock -- this fix touches presentation
        only, never _execution_blockers or the lock itself."""
        job = self._running_job()
        with patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()), \
             patch("updatecenter.views._backend_readiness",
                   return_value={**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}), \
             patch("updatecenter.views.create_job") as create:
            response = self.client.post(
                reverse("updatecenter:start-update"),
                {
                    "confirmed_target_release_id": ReadyPlan.target_release_id,
                    "confirmed_plan_fingerprint": ReadyPlan.fingerprint,
                },
            )
        self.assertEqual(response.status_code, 302)
        create.assert_not_called()
        # The lock is untouched -- still exactly one active job, still
        # owning the lock, exactly as before this fix.
        self.assertEqual(UpdateJob.objects.filter(active_lock=1).count(), 1)
        job.refresh_from_db()
        self.assertEqual(job.active_lock, 1)
