"""Django audit-mirror, idempotent submission, and reconciliation primitives."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from .backend_client import (
    BackendError,
    BackendRejectedError,
    BackendTransportError,
    UpdaterClient,
)
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
            progress_detail="Prepared for one narrow protected-backend submission.",
            plan_snapshot=plan.to_serializable(),
            plan_fingerprint=plan.fingerprint,
            active_lock=1,
        )
    except IntegrityError as exc:
        raise JobSubmissionError("another update job is active") from exc


def submit_job(job: UpdateJob, *, client: UpdaterClient | None = None) -> dict:
    if job.state not in {
        UpdateJobState.PLANNED,
        UpdateJobState.RUNNING,
        UpdateJobState.SUBMISSION_UNCERTAIN,
    } or job.active_lock != 1:
        raise JobSubmissionError("job is not eligible for protected-backend submission")
    backend = client or UpdaterClient()
    arguments = {
        "job_id": job.id,
        "target_release_id": job.target_release_id,
        "plan_fingerprint": job.plan_fingerprint,
    }

    # One retry with the SAME UUID resolves both common transport races:
    # never-delivered requests are submitted, and accepted-but-response-lost
    # requests are acknowledged idempotently by the root store.
    for attempt in range(2):
        try:
            response = backend.start_update(**arguments)
        except BackendRejectedError:
            # A negative START_UPDATE response is not proof that durable root
            # acceptance did not already happen. JobStore.accept() publishes
            # state before later durability work such as its audit-log append,
            # so every generic refusal must be resolved through root status.
            break
        except BackendTransportError:
            if attempt == 0:
                continue
            break
        else:
            _mark_accepted(job)
            return response

    # A status response is root truth. Only the precise root-side "does not
    # exist" result proves that neither same-ID submission was accepted.
    try:
        response = backend.get_job_status(job.id)
    except BackendRejectedError as exc:
        if exc.error_code == "JobError" and str(exc) == "job does not exist":
            _mark_definitely_not_accepted(job, "Protected updater proved the job was never accepted.")
            raise JobSubmissionError("protected updater proved the job was never accepted") from exc
        _mark_submission_uncertain(job)
        return {"ok": False, "submission_uncertain": True}
    except BackendError:
        _mark_submission_uncertain(job)
        return {"ok": False, "submission_uncertain": True}

    _reconcile_response(job, response, backend=backend)
    return {"ok": True, "reconciled_after_uncertain_submission": True}


def _mark_accepted(job: UpdateJob):
    if job.state == UpdateJobState.RUNNING:
        return
    job.state = UpdateJobState.RUNNING
    job.started_at = job.started_at or timezone.now()
    job.current_step = "submitted_to_protected_backend"
    job.progress_detail = (
        "Accepted by the protected updater; root independently revalidates every authorization fact."
    )
    job.save(update_fields=["state", "started_at", "current_step", "progress_detail"])


def _mark_submission_uncertain(job: UpdateJob):
    job.state = UpdateJobState.SUBMISSION_UNCERTAIN
    job.current_step = "submission_status_unknown"
    job.progress_detail = (
        "The protected updater response was unavailable. The active lock remains held "
        "until root truth can be reconciled."
    )
    job.save(update_fields=["state", "current_step", "progress_detail"])


def _mark_definitely_not_accepted(job: UpdateJob, detail: str):
    job.state = UpdateJobState.FAILED
    job.current_step = "submission_rejected"
    job.progress_detail = "The protected updater definitively rejected this submission."
    job.failure_classification = "SUBMISSION_REJECTED"
    job.failure_detail = str(detail)[:10000]
    job.finished_at = timezone.now()
    job.active_lock = None
    job.save(update_fields=[
        "state", "current_step", "progress_detail", "failure_classification",
        "failure_detail", "finished_at", "active_lock",
    ])


def reconcile_job(job: UpdateJob, *, client: UpdaterClient | None = None) -> UpdateJob:
    backend = client or UpdaterClient()
    try:
        response = backend.get_job_status(job.id)
    except BackendRejectedError as exc:
        if (job.state in {UpdateJobState.PLANNED, UpdateJobState.SUBMISSION_UNCERTAIN}
                and exc.error_code == "JobError"
                and str(exc) == "job does not exist"):
            _mark_definitely_not_accepted(
                job, "Protected updater proved the uncertain job was never accepted."
            )
            return job
        raise
    return _reconcile_response(job, response, backend=backend)


def _reconcile_response(job: UpdateJob, response: dict, *, backend: UpdaterClient) -> UpdateJob:
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
        try:
            job.completed_log_snapshot = backend.get_job_log(job.id, max_bytes=65536)
        except BackendError:
            # Terminal root state is sufficient to release the concurrency
            # lock. A transient log-tail failure must not fabricate activity.
            pass
    job.save()
    return job
