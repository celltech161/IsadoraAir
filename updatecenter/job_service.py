"""Django audit-mirror/submission primitives; intentionally not wired to HTTP."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from .backend_client import UpdaterClient
from .models import UpdateJob, UpdateJobState
from .planner import Plan, SafetyStatus


class JobSubmissionError(RuntimeError):
    pass


@transaction.atomic
def create_job(*, plan: Plan, user) -> UpdateJob:
    if plan.safety_status != SafetyStatus.READY_TO_PLAN or not all((plan.installed_release_id, plan.installed_commit, plan.target_release_id, plan.target_commit)):
        raise JobSubmissionError("only a complete ready-to-plan snapshot can create an audit job")
    username = user.get_username()
    try:
        return UpdateJob.objects.create(
            initiated_by=user,
            initiated_by_username=username,
            installed_release_id=plan.installed_release_id,
            target_release_id=plan.target_release_id,
            installed_commit=plan.installed_commit,
            target_commit=plan.target_commit,
            state=UpdateJobState.PLANNED,
            current_step="awaiting_backend_submission",
            progress_detail="Prepared for protected-backend submission; no HTTP execution path exists.",
            plan_snapshot=plan.to_serializable(),
            plan_fingerprint=plan.fingerprint,
            active_lock=1,
        )
    except IntegrityError as exc:
        raise JobSubmissionError("another update job is active") from exc


def submit_job(job: UpdateJob, *, client: UpdaterClient | None = None) -> dict:
    if job.state not in {UpdateJobState.PLANNED, UpdateJobState.RUNNING} or job.active_lock != 1:
        raise JobSubmissionError("job is not eligible for protected-backend submission")
    backend = client or UpdaterClient()
    response = backend.start_update(
        job_id=job.id, target_release_id=job.target_release_id,
        plan_fingerprint=job.plan_fingerprint,
    )
    if job.state != UpdateJobState.RUNNING:
        job.state = UpdateJobState.RUNNING
        job.started_at = timezone.now()
        job.current_step = "submitted_to_protected_backend"
        job.progress_detail = "Accepted by the protected updater; root independently revalidates every authorization fact."
        job.save(update_fields=["state", "started_at", "current_step", "progress_detail"])
    return response


def reconcile_job(job: UpdateJob, *, client: UpdaterClient | None = None) -> UpdateJob:
    backend = client or UpdaterClient()
    response = backend.get_job_status(job.id)
    root_job = response.get("job")
    if not isinstance(root_job, dict) or root_job.get("job_id") != str(job.id):
        raise JobSubmissionError("protected backend returned the wrong job identity")
    mapping = {
        "accepted": UpdateJobState.QUEUED,
        "running": UpdateJobState.RUNNING,
        "succeeded": UpdateJobState.SUCCEEDED,
        "failed": UpdateJobState.FAILED,
        "manual_intervention_required": UpdateJobState.MANUAL_INTERVENTION_REQUIRED,
    }
    root_state = root_job.get("state")
    if root_state not in mapping:
        raise JobSubmissionError("protected backend returned an unknown state")
    trusted_plan = root_job.get("trusted_plan")
    if isinstance(trusted_plan, dict):
        if trusted_plan.get("target_commit") not in {None, job.target_commit} or trusted_plan.get("fingerprint") not in {None, job.plan_fingerprint}:
            raise JobSubmissionError("protected backend independently derived facts that differ from the audit mirror")
    job.state = mapping[root_state]
    job.current_step = str(root_job.get("current_step", ""))[:64]
    job.progress_detail = f"Protected backend: {job.current_step}"[:500]
    job.failure_classification = str(root_job.get("failure_classification", ""))[:64]
    job.failure_detail = str(root_job.get("failure_detail", ""))[:10000]
    terminal = job.state in UpdateJobState.TERMINAL
    job.requires_manual_intervention = job.state == UpdateJobState.MANUAL_INTERVENTION_REQUIRED
    if terminal:
        job.finished_at = timezone.now()
        job.active_lock = None
        job.completed_log_snapshot = backend.get_job_log(job.id, max_bytes=65536)
    job.save()
    return job
