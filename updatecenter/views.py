"""Managed Update Center UI and durable protected-backend reconciliation."""
from pathlib import Path

from django.contrib import messages
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from isadoraair.version_info import get_checkout_identity
from monitoring.services.release_status import get_release_status

from . import planner, release_chain
from .backend_client import BackendError, PROTOCOL_VERSION, UpdaterClient
from .job_service import JobSubmissionError, create_job, reconcile_job, submit_job
from .models import UpdateJob, UpdateJobState
from .schema_health import SchemaHealthStatus


CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
RELEASES_DIRNAME = release_chain.RELEASES_DIRNAME_DEFAULT


def _permission_check(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return HttpResponseForbidden("The Update Center is staff-only.")
    return None


def _backend_readiness(client: UpdaterClient | None = None) -> dict:
    backend = client or UpdaterClient()
    result = {
        "reachable": False,
        "protocol_compatible": False,
        "protected_runtime_valid": False,
        "config_valid": False,
        "trusted_repository_ready": False,
        "execution_armed": False,
        "ready": False,
        "detail": "Protected updater is unavailable.",
    }
    try:
        ping = backend.ping()
    except BackendError as exc:
        result["detail"] = str(exc)[:500]
        return result
    result.update({
        "reachable": True,
        "protocol_compatible": ping.get("protocol_version") == PROTOCOL_VERSION,
        "protected_runtime_valid": ping.get("protected_runtime_valid") is True,
        "config_valid": ping.get("config_valid") is True,
        "trusted_repository_ready": ping.get("trusted_repository_ready") is True,
        "execution_armed": ping.get("update_execution_enabled") is True,
    })
    result["ready"] = all(result[key] for key in (
        "reachable", "protocol_compatible", "protected_runtime_valid",
        "config_valid", "trusted_repository_ready", "execution_armed",
    ))
    if result["ready"]:
        result["detail"] = "Protected updater is reachable, compatible, ready, and armed."
    elif not result["protocol_compatible"]:
        result["detail"] = "Protected updater protocol is incompatible; manual helper upgrade required."
    elif not result["execution_armed"]:
        result["detail"] = "Protected updater is reachable but root execution is disarmed."
    else:
        result["detail"] = "Protected updater failed one or more protected readiness checks."
    return result


def _execution_blockers(request, plan, readiness: dict, active_job) -> list[str]:
    blockers = []
    if not request.user.is_superuser:
        blockers.append("Only a superuser may start an update.")
    if plan is None:
        blockers.append("No valid update plan is available.")
        return blockers
    if plan.safety_status != planner.SafetyStatus.READY_TO_PLAN:
        blockers.append(f"Planner safety state is {plan.safety_status}.")
    if plan.schema_health_status != SchemaHealthStatus.SCHEMA_CURRENT:
        blockers.append("The current Django schema is not healthy.")
    if not plan.target_release_id or not plan.target_commit:
        blockers.append("No complete newer target is available.")
    if active_job is not None:
        blockers.append(f"Update job {active_job.id} still owns the active lock.")
    if plan.python_requirements_changed:
        blockers.append("Python requirements changes require manual handling.")
    if plan.apt_packages_new:
        blockers.append("OS package prerequisites require manual handling.")
    if plan.migrations and plan.migrations.compatibility == "destructive":
        blockers.append("This migration transition requires a manual gate.")
    if plan.systemd_units_removed_or_renamed:
        blockers.append("Removed or renamed systemd units require manual handling.")
    if plan.nginx_changed:
        blockers.append("nginx changes require manual handling.")
    if plan.runtime_components_changed:
        blockers.append("Native/runtime component changes require manual handling.")
    if plan.manual_bootstrap_required:
        blockers.append("This release explicitly requires manual privileged bootstrap.")
    if plan.minimum_updater_protocol_version > PROTOCOL_VERSION:
        blockers.append("The protected updater must be upgraded manually first.")
    if not readiness["ready"]:
        blockers.append(readiness["detail"])
    return blockers


def _refresh_job(job: UpdateJob, client: UpdaterClient | None = None) -> tuple[UpdateJob, str | None]:
    if job.state in UpdateJobState.TERMINAL:
        return job, None
    backend = client or UpdaterClient()
    try:
        reconcile_job(job, client=backend)
    except (BackendError, JobSubmissionError) as exc:
        return job, str(exc)[:500]
    return job, None


def _build_context(request):
    checkout = get_checkout_identity()
    _checkout_unused, version_lookup = get_release_status()
    try:
        plan = planner.build_plan(CHECKOUT_ROOT, RELEASES_DIRNAME)
        plan_error = None
    except Exception as exc:
        plan = None
        plan_error = str(exc)

    active_job = UpdateJob.objects.filter(active_lock=1).first()
    reconciliation_error = None
    if active_job is not None:
        active_job, reconciliation_error = _refresh_job(active_job)
        if active_job.active_lock is None:
            active_job = None
    shown_job = active_job or UpdateJob.objects.first()
    readiness = _backend_readiness()
    blockers = _execution_blockers(request, plan, readiness, active_job)
    return {
        "checkout": checkout,
        "version_lookup": version_lookup,
        "plan": plan,
        "plan_error": plan_error,
        "backend_readiness": readiness,
        "execution_blockers": blockers,
        "update_eligible": not blockers,
        "active_job": active_job,
        "shown_job": shown_job,
        "reconciliation_error": reconciliation_error,
    }


@ensure_csrf_cookie
@require_http_methods(["GET"])
def updates_dashboard(request):
    denied = _permission_check(request)
    if denied:
        return denied
    return render(request, "updatecenter/updates.html", _build_context(request))


@require_http_methods(["POST"])
def check_for_updates(request):
    denied = _permission_check(request)
    if denied:
        return denied
    planner.fetch_updates(CHECKOUT_ROOT)
    return redirect("updatecenter:dashboard")


@require_http_methods(["POST"])
def start_update(request):
    denied = _permission_check(request)
    if denied:
        return denied
    if not request.user.is_superuser:
        return HttpResponseForbidden("Only a superuser may start an update.")

    fresh_plan = planner.build_plan(CHECKOUT_ROOT, RELEASES_DIRNAME)
    readiness = _backend_readiness()
    active_job = UpdateJob.objects.filter(active_lock=1).first()
    blockers = _execution_blockers(request, fresh_plan, readiness, active_job)
    posted_release = request.POST.get("confirmed_target_release_id", "")
    posted_fingerprint = request.POST.get("confirmed_plan_fingerprint", "")
    if (posted_release != (fresh_plan.target_release_id or "")
            or posted_fingerprint != fresh_plan.fingerprint):
        messages.error(request, "The update plan changed. Review the refreshed plan before confirming again.")
        return redirect("updatecenter:dashboard")
    if blockers:
        messages.error(request, "Update is not eligible: " + " ".join(blockers))
        return redirect("updatecenter:dashboard")

    try:
        job = create_job(plan=fresh_plan, user=request.user)
        outcome = submit_job(job)
    except JobSubmissionError as exc:
        messages.error(request, f"Protected updater rejected the job: {str(exc)[:300]}")
        return redirect("updatecenter:dashboard")
    if outcome.get("submission_uncertain"):
        messages.warning(
            request,
            "Update submission status is temporarily unknown. The active lock is retained "
            "and this page will reconcile the same job ID when the helper returns.",
        )
    return redirect("updatecenter:dashboard")


@require_http_methods(["GET"])
def job_status(request, job_id):
    denied = _permission_check(request)
    if denied:
        return denied
    job = get_object_or_404(UpdateJob, pk=job_id)
    reconciliation_error = None
    if job.state not in UpdateJobState.TERMINAL:
        job, reconciliation_error = _refresh_job(job)

    log_tail = job.completed_log_snapshot if job.state in UpdateJobState.TERMINAL else ""
    if (request.user.is_superuser
            and job.state not in UpdateJobState.TERMINAL
            and reconciliation_error is None):
        try:
            log_tail = UpdaterClient().get_job_log(job.id, max_bytes=32768)
        except BackendError as exc:
            reconciliation_error = reconciliation_error or str(exc)[:500]
    return JsonResponse({
        "job_id": str(job.id),
        "state": job.state,
        "current_step": job.current_step,
        "progress_detail": job.progress_detail,
        "failure_classification": job.failure_classification,
        "failure_detail": job.failure_detail[:4000],
        "requires_manual_intervention": job.requires_manual_intervention,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "terminal": job.state in UpdateJobState.TERMINAL,
        "backend_temporarily_unavailable": reconciliation_error is not None,
        "backend_detail": reconciliation_error or "",
        "log_tail": log_tail[-32768:],
    })
