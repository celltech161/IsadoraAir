"""Read-only /updates/ status UI -- [P0] 1.1 Phase A.

Two views total, both non-privileged, neither mutates anything except
the one explicit, POST-only, CSRF-protected "Check for Updates"
action, which performs a `git fetch` (remote-tracking refs only --
see git_adapter.fetch_remote's own docstring) and nothing else. There
is no view, endpoint, or code path anywhere in this app that changes
the checkout, runs a migration, runs pip, installs/reloads systemd,
restarts a service, writes nginx config, or runs apt."""
from pathlib import Path

from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from isadoraair.version_info import get_checkout_identity
from monitoring.services.release_status import get_release_status

from . import planner, release_chain

# Matches isadoraair/version_info.py's own PROJECT_ROOT derivation
# exactly -- never a hardcoded /opt/isadoraair or /home/jreed/...
# path, so this works unmodified on any station regardless of its
# physical checkout location (ARCHITECTURE_REPORT.md §21).
CHECKOUT_ROOT = Path(__file__).resolve().parent.parent
# A repo-relative path STRING, not a filesystem Path -- planner.build_plan
# reads the release set from git history at a resolved ref, never from
# the literal working-tree disk (see planner.build_plan's own docstring).
RELEASES_DIRNAME = release_chain.RELEASES_DIRNAME_DEFAULT


def _permission_check(request):
    """Staff or superuser only -- matches webrequests/views.py's
    _permission_check exactly (this task's §2.5: v1 read-only access is
    staff-or-superuser; there is no broader "any authenticated user"
    tier for this page, unlike monitoring/'s own dashboard, since this
    page reveals deployment/version internals monitoring's general
    audience doesn't need)."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return HttpResponseForbidden("The Update Center is staff-only.")
    return None


def _build_context():
    """Everything the template needs, computed fresh on every request
    -- Phase A never persists a Plan or an UpdateJob row, so there is
    nothing to go stale between requests beyond the underlying git/
    manifest state itself (see planner.build_plan's own docstring:
    this never fetches, so it only ever reflects whatever was already
    known locally as of the last explicit Check for Updates)."""
    checkout = get_checkout_identity()
    _checkout_unused, version_lookup = get_release_status()
    try:
        plan = planner.build_plan(CHECKOUT_ROOT, RELEASES_DIRNAME)
        plan_error = None
    except Exception as exc:  # noqa: BLE001 -- a planning bug must render a safe page, never a 500
        plan = None
        plan_error = str(exc)
    return {
        "checkout": checkout,
        "version_lookup": version_lookup,
        "plan": plan,
        "plan_error": plan_error,
    }


@ensure_csrf_cookie
@require_http_methods(["GET"])
def updates_dashboard(request):
    denied = _permission_check(request)
    if denied:
        return denied
    return render(request, "updatecenter/updates.html", _build_context())


@require_http_methods(["POST"])
def check_for_updates(request):
    """The ONLY code path in this app that performs a network
    operation. POST-only (Django's CSRF middleware protects this by
    default -- no @csrf_exempt anywhere in this app), staff-or-
    superuser, and still cannot deploy anything: a fetch only updates
    this checkout's knowledge of what `origin` currently has, exactly
    like an operator running `git fetch` by hand would. See this
    task's §16 / docs/UPDATE_CENTER.md for why this is deliberately
    NOT triggered by the ordinary page GET."""
    denied = _permission_check(request)
    if denied:
        return denied
    planner.fetch_updates(CHECKOUT_ROOT)
    return redirect("updatecenter:dashboard")
