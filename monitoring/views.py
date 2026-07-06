import json
import time
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

STATE_PATH = Path("/run/isadoraair/monitoring_state.json")
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
