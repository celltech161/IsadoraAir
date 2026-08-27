"""fix/operator-state-correctness -- Fix A: terminal-job page
reconciliation.

Observed on WRJE during a real r0007->r0008 update: the page
simultaneously showed "Installation blocked: Update job <uuid> still
owns the active lock" while the live job panel had already updated to
State: succeeded. Root cause: the job-status poll updated the live
panel's own spans, then simply stopped polling on data.terminal --
everything else on the page (blockers, installed release, available-
update summary) stayed server-rendered from BEFORE the job finished.

Fix: on ANY terminal state, perform one window.location.reload() so
the server recomputes active_job/blockers/plan/schema health fresh --
no new API, no duplicated backend-reconciliation logic in JS, no
special-casing of "succeeded" only.

These tests cover both halves: the JS source itself (static, matching
this project's existing dashboard/updates.html test convention -- see
library/tests/test_dashboard_view.py) and the server-side
reconciliation a reload actually triggers (functional, via the real
view + a real UpdateJob row)."""
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.tests.test_phase_c_integration import READY_PING, ReadyPlan
from unittest.mock import patch


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class TerminalJobReloadScriptTests(TestCase):
    """Static assertions on the poll script itself -- proves the fix is
    actually present and is not special-cased to one terminal state."""

    def setUp(self):
        self.superuser = User.objects.create_superuser("uc-reload-root")
        self.client = Client()
        self.client.force_login(self.superuser)

    def _render_with_active_job(self):
        job = UpdateJob.objects.create(
            initiated_by_username="uc-reload-root",
            installed_release_id="r0007", target_release_id="r0008",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.RUNNING, active_lock=1,
        )
        with patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()), \
             patch("updatecenter.views._backend_readiness",
                   return_value={**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}), \
             patch("updatecenter.views._refresh_job", return_value=(job, None)):
            response = self.client.get(reverse("updatecenter:dashboard"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode("utf-8"), job

    def test_nonterminal_job_still_polls_normally(self):
        """1. nonterminal job continues polling -- the poll script
        renders at all, and its non-terminal branch is unchanged
        (still reschedules via window.setTimeout(poll, delay))."""
        content, job = self._render_with_active_job()
        self.assertIn('id="uc-job"', content)
        self.assertIn("const poll = async () => {", content)
        poll_fn = content[content.index("const poll = async"):content.index("window.setTimeout(poll, delay);\n  })();")]
        # Non-terminal path still falls through to the reschedule at
        # the bottom of poll() -- unchanged behavior.
        self.assertIn("window.setTimeout(poll, delay);", poll_fn)

    def test_terminal_state_triggers_one_authoritative_reload(self):
        """2 & 3. Terminal success AND terminal failure must reach the
        exact same reload call -- proven by there being exactly ONE
        `data.terminal` branch in the whole poll function (no
        succeeded-only special case), and that branch calling
        window.location.reload()."""
        content, job = self._render_with_active_job()
        poll_fn = content[content.index("const poll = async"):content.index("window.setTimeout(poll, delay);\n  })();")]
        # Exactly one terminal check -- not special-cased per state.
        self.assertEqual(poll_fn.count("data.terminal"), 1)
        self.assertNotIn('data.state === "succeeded"', poll_fn)
        self.assertNotIn("data.state == 'succeeded'", poll_fn)
        # The terminal branch reloads instead of just returning.
        terminal_branch = poll_fn[poll_fn.index("if (data.terminal)"):]
        terminal_branch = terminal_branch[:terminal_branch.index("} catch")]
        self.assertIn("window.location.reload();", terminal_branch)

    def test_no_duplicate_api_or_second_polling_mechanism_introduced(self):
        """Explicit constraint: no new endpoint, no new interval, no
        duplicated backend-reconciliation logic in JS."""
        content, job = self._render_with_active_job()
        # Only the existing single interval-equivalent (recursive
        # setTimeout) drives this panel -- no setInterval anywhere in
        # the job panel's own script block.
        script_block = content[content.index('<section class="uc-panel" id="uc-job"'):
                                content.index("})();") + len("})();")]
        self.assertNotIn("setInterval(", script_block)
        # Only ever fetches the one existing status URL.
        self.assertEqual(script_block.count("fetch(panel.dataset.statusUrl"), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class TerminalJobServerReconciliationTests(TestCase):
    """Functional proof that the reload the JS now performs actually
    lands on a correctly reconciled page -- i.e. that Fix A's premise
    ("a plain reload recomputes everything correctly") holds against
    the real view, not just an assumption."""

    def setUp(self):
        self.superuser = User.objects.create_superuser("uc-reload-root2")
        self.client = Client()
        self.client.force_login(self.superuser)

    def _get(self):
        with patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()), \
             patch("updatecenter.views._backend_readiness",
                   return_value={**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}):
            return self.client.get(reverse("updatecenter:dashboard"))

    def test_active_nonterminal_job_shows_live_polling_panel(self):
        """Baseline: BEFORE the job finishes, the page correctly shows
        the live polling panel -- this is the exact state a real
        WRJE/KOGR-style page load shows mid-update.

        [fix/updatecenter-active-job-presentation correction] The raw
        "still owns the active lock" blocker text is deliberately NOT
        asserted here anymore -- a later, separate fix corrected that
        exact presentation bug (a normal in-progress install is no
        longer shown as "Installation blocked"; see
        test_active_job_presentation.py for the full regression
        coverage of that fix). active_job/the active_lock safety
        mechanism itself is untouched -- only what the operator SEES
        while it's held changed."""
        UpdateJob.objects.create(
            initiated_by_username="uc-reload-root2",
            installed_release_id="r0007", target_release_id="r0008",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.RUNNING, active_lock=1,
        )
        with patch("updatecenter.views._refresh_job", side_effect=lambda job: (job, None)):
            response = self._get()
        content = response.content.decode("utf-8")
        self.assertIn('id="uc-job"', content)

    def test_freshly_rendered_page_after_terminal_reconciliation_drops_active_lock_blocker(self):
        """4. The core proof: once the job has genuinely gone terminal
        (active_lock cleared, exactly what job_service does on a real
        terminal transition), a FRESH GET -- i.e. what the JS reload
        performs -- no longer treats it as owning the active lock, no
        longer renders the live polling panel, and shows the Last
        update panel instead."""
        job = UpdateJob.objects.create(
            initiated_by_username="uc-reload-root2",
            installed_release_id="r0007", target_release_id="r0008",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.SUCCEEDED, active_lock=None,
            completed_log_snapshot="finished",
        )
        response = self._get()
        content = response.content.decode("utf-8")
        self.assertNotIn("still owns the active lock", content)
        self.assertNotIn('id="uc-job"', content)
        self.assertIn("Last update", content)
        self.assertIn(job.target_release_id, content)

    def test_terminal_failure_also_drops_blocker_and_shows_failure_presentation(self):
        """3 (server half). A FAILED terminal job reconciles the same
        way as a succeeded one -- not special-cased."""
        UpdateJob.objects.create(
            initiated_by_username="uc-reload-root2",
            installed_release_id="r0007", target_release_id="r0008",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.FAILED, active_lock=None,
            failure_detail="simulated failure for test",
            completed_log_snapshot="boom",
        )
        response = self._get()
        content = response.content.decode("utf-8")
        self.assertNotIn("still owns the active lock", content)
        self.assertNotIn('id="uc-job"', content)
        self.assertIn("Last update", content)
        self.assertIn("simulated failure for test", content)

    def test_manual_intervention_terminal_state_also_reconciles(self):
        """Terminal set includes manual_intervention_required -- must
        not be special-cased away from the same reconciliation path."""
        UpdateJob.objects.create(
            initiated_by_username="uc-reload-root2",
            installed_release_id="r0007", target_release_id="r0008",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.MANUAL_INTERVENTION_REQUIRED, active_lock=None,
            requires_manual_intervention=True,
            failure_detail="operator action required",
        )
        response = self._get()
        content = response.content.decode("utf-8")
        self.assertNotIn("still owns the active lock", content)
        self.assertNotIn('id="uc-job"', content)
