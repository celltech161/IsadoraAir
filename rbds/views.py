import json
import time
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

STATE_PATH = Path("/run/isadoraair/rbds_state.json")
STATE_STALE_SECONDS = 5  # 5x the engine's ~1s tick


@ensure_csrf_cookie
def rbds_dashboard(request):
    return render(request, "rbds/dashboard.html", {})


@require_http_methods(["GET"])
def api_rbds_status(request):
    if not STATE_PATH.is_file():
        return JsonResponse({"stale": True})
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return JsonResponse({"stale": True})
    data["stale"] = (time.time() - data.get("timestamp", 0)) > STATE_STALE_SECONDS
    return JsonResponse(data)
