import json
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from django.utils import timezone as django_tz

from .models import ListenerPeak, MonitorCheck, SystemEvent

STATE_PATH = Path("/run/isadoraair/monitoring_state.json")
LISTENER_STATE_PATH = Path("/run/isadoraair/listeners.json")
STATE_STALE_SECONDS = 30  # 3x the poller's 10s cadence


@ensure_csrf_cookie
def monitoring_dashboard(request):
    return render(request, "monitoring/dashboard.html", {})


@require_http_methods(["GET"])
def api_monitoring_status(request):
    if not STATE_PATH.is_file():
        return JsonResponse({"checks": [], "timestamp": 0, "stale": True})
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return JsonResponse({"checks": [], "timestamp": 0, "stale": True})
    data.pop("_cooldowns", None)  # internal bookkeeping, not for the browser
    data["stale"] = (time.time() - data.get("timestamp", 0)) > STATE_STALE_SECONDS
    return JsonResponse(data)


@require_http_methods(["POST"])
def api_restart_check(request, check_id):
    """Restart the systemd unit backing one MonitorCheck card. The unit
    name always comes from the check row itself (never from the
    request) -- the client only ever supplies a check id, so this can't
    be used to restart an arbitrary unit on the box. Fire-and-forget
    Popen, same as the original engine-restart button this replaces:
    a blocking subprocess.run here would tie up a gunicorn worker for
    the duration of the stop/start (or hang the request entirely if the
    unit's stop-timeout is ever hit, as happened for real with the
    playback engine earlier -- see PROJECT_NOTES.md)."""
    check = get_object_or_404(MonitorCheck, pk=check_id, kind="systemd")
    if not check.systemd_unit:
        return JsonResponse({"error": "No systemd unit configured for this check"}, status=400)
    subprocess.Popen(["sudo", "systemctl", "restart", check.systemd_unit])
    return JsonResponse({"ok": True, "unit": check.systemd_unit})


@require_http_methods(["GET"])
def api_listener_status(request):
    """Return the current Shoutcast listener state written by the
    monitor tick to `LISTENER_STATE_PATH`. On a missing/stale/malformed
    file the dashboard just sees `stale=true` and can gray the widget
    out without a JS error path."""
    if not LISTENER_STATE_PATH.is_file():
        return JsonResponse({
            "led": "red", "current_total": 0, "peak_total": 0,
            "peak_since_at": None, "peak_reached_at": None,
            "tlh_hours": 0.0, "tlh_since_at": None,
            "encoders": [], "timestamp": 0, "stale": True,
        })
    try:
        data = json.loads(LISTENER_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return JsonResponse({
            "led": "red", "current_total": 0, "peak_total": 0,
            "peak_since_at": None, "peak_reached_at": None,
            "tlh_hours": 0.0, "tlh_since_at": None,
            "encoders": [], "timestamp": 0, "stale": True,
        })
    data["stale"] = (time.time() - data.get("timestamp", 0)) > STATE_STALE_SECONDS
    return JsonResponse(data)


@require_http_methods(["POST"])
def api_listener_peak_reset(request):
    """Double-click on the dashboard widget's peak count POSTs here.
    Reset the peak to the CURRENT live total (not zero) so the next
    monitor tick doesn't immediately raise the number and make the
    reset feel laggy -- the operator's mental model is "start tracking
    peak fresh from RIGHT NOW", not "zero out and race the tick."

    Reads current_total from LISTENER_STATE_PATH rather than re-polling
    Shoutcast because we're already in the request/response path and
    an extra sync HTTP GET here would double our exposure to network
    hiccups. Stale state is fine -- it's at most 10 seconds old."""
    current_total = 0
    if LISTENER_STATE_PATH.is_file():
        try:
            data = json.loads(LISTENER_STATE_PATH.read_text(encoding="utf-8"))
            current_total = int(data.get("current_total", 0))
        except (OSError, ValueError):
            pass
    peak = ListenerPeak.load()
    peak.peak_total = current_total
    peak.peak_since_at = django_tz.now()
    # Clear peak_reached_at -- there's no "since I hit this number" for a
    # freshly-reset window until we actually see the current_total value
    # get exceeded (or matched, on the next tick).
    peak.peak_reached_at = None
    peak.save(update_fields=["peak_total", "peak_since_at", "peak_reached_at"])
    return JsonResponse({
        "ok": True,
        "peak_total": peak.peak_total,
        "peak_since_at": peak.peak_since_at.isoformat(),
    })


@require_http_methods(["POST"])
def api_listener_tlh_reset(request):
    """Double-click on the dashboard widget's TLH count POSTs here, for
    a manual mid-cycle reset -- MonitorManager also resets TLH
    automatically at midnight on the 1st of each month (see
    _maybe_reset_tlh_for_new_month in monitoring/services/monitor.py),
    this endpoint just lets the operator start a fresh window early.
    Unlike peak (reset to the current live total), TLH is a cumulative
    SUM with no "current value" to reset to -- resetting means starting
    the running total fresh at 0.0 hours from right now."""
    tlh = ListenerPeak.load()
    tlh.tlh_hours = 0.0
    tlh.tlh_since_at = django_tz.now()
    tlh.save(update_fields=["tlh_hours", "tlh_since_at"])
    return JsonResponse({
        "ok": True,
        "tlh_hours": tlh.tlh_hours,
        "tlh_since_at": tlh.tlh_since_at.isoformat(),
    })


# Windows the operator can pick in the Recent Events dropdown, mapped to
# the "created_at >= now() - N" cutoff used in the query. Kept small and
# capped -- the whole point of this feed is a glanceable summary, not a
# deep-dive log viewer (that lives in the Django admin, one link away).
_EVENT_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
_EVENT_LEVELS = {"info", "warning", "error", "critical"}
# Levels shown when the operator picks "warning+" or "error+" -- cheaper
# to enumerate here than to build a min-level comparator in SQL over the
# CharField.
_EVENT_LEVEL_FILTERS = {
    "all": ("info", "warning", "error", "critical"),
    "warning+": ("warning", "error", "critical"),
    "error+": ("error", "critical"),
}
_EVENT_LIMIT = 100


@require_http_methods(["GET"])
def api_recent_events(request):
    """Recent Events feed for /monitoring/. Query params (all optional):
      * window: 1h / 24h / 7d (default 24h)
      * level:  all / warning+ / error+ (default all)
      * category: exact category slug filter (e.g. "engine", "monitor")

    Returns at most _EVENT_LIMIT rows, newest first. Deliberately no
    pagination -- if the operator needs more, they go to the admin.
    """
    window_key = request.GET.get("window", "24h")
    window = _EVENT_WINDOWS.get(window_key, _EVENT_WINDOWS["24h"])
    level_key = request.GET.get("level", "all")
    levels = _EVENT_LEVEL_FILTERS.get(level_key, _EVENT_LEVEL_FILTERS["all"])
    category = request.GET.get("category", "").strip()

    cutoff = django_tz.now() - window
    qs = SystemEvent.objects.filter(created_at__gte=cutoff, level__in=levels)
    if category:
        qs = qs.filter(category=category)

    events = list(qs.order_by("-created_at")[:_EVENT_LIMIT])
    return JsonResponse({
        "events": [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat(),
                "last_repeated_at": e.last_repeated_at.isoformat() if e.last_repeated_at else None,
                "level": e.level,
                "category": e.category,
                "title": e.title,
                "detail": e.detail,
                "source": e.source,
                "repeat_count": e.repeat_count,
            }
            for e in events
        ],
        # Distinct category slugs currently seen in the window, so the
        # category filter dropdown can be populated dynamically instead
        # of hard-coding a list that goes stale as subsystems start
        # emitting.
        "categories": sorted(
            SystemEvent.objects
            .filter(created_at__gte=cutoff)
            .values_list("category", flat=True)
            .distinct()
        ),
    })


@require_http_methods(["GET"])
def api_recent_logins(request):
    try:
        from axes.models import AccessAttempt
    except ImportError:
        return JsonResponse({"attempts": []})

    attempts = AccessAttempt.objects.order_by("-attempt_time")[:10]
    return JsonResponse({
        "attempts": [
            {
                "username": a.username,
                "ip_address": a.ip_address,
                "attempt_time": a.attempt_time.isoformat(),
                "failures_since_start": a.failures_since_start,
            }
            for a in attempts
        ],
    })
