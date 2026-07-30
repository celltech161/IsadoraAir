import json

from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .models import WebRequestConfig


def _permission_check(request):
    """Staff or superuser only -- same posture and same reasoning as
    library/views.py's _reports_permission_check: this feature writes
    to a publicly-reachable surface (the availability grid + rate caps
    that gate what airs from the public site), so the admin-only bar is
    intentionally as tight as royalty reporting's."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return HttpResponseForbidden("Web Requests configuration is staff-only.")
    return None


@ensure_csrf_cookie
def web_request_page(request):
    denied = _permission_check(request)
    if denied:
        return denied
    config = WebRequestConfig.load()
    return render(request, "webrequests/web_request_page.html", {"config": config})


@require_http_methods(["GET", "PATCH"])
def api_web_request_config(request):
    denied = _permission_check(request)
    if denied:
        return denied

    config = WebRequestConfig.load()

    if request.method == "GET":
        return JsonResponse({
            "enabled": config.enabled,
            "open_slots": config.open_slots,
            "max_fulfilled_per_hour": config.max_fulfilled_per_hour,
            "lookahead_warning_minutes": config.lookahead_warning_minutes,
            "expire_after_hours": config.expire_after_hours,
        })

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "enabled" in body:
        config.enabled = bool(body["enabled"])
    if "max_fulfilled_per_hour" in body:
        config.max_fulfilled_per_hour = max(0, int(body["max_fulfilled_per_hour"]))
    if "lookahead_warning_minutes" in body:
        config.lookahead_warning_minutes = max(1, int(body["lookahead_warning_minutes"]))
    if "expire_after_hours" in body:
        config.expire_after_hours = max(1, int(body["expire_after_hours"]))

    config.save()
    return JsonResponse({"ok": True})


@require_http_methods(["POST"])
def api_open_slot_toggle(request):
    """Mirrors library/views.py's api_track_blocked_slot_toggle exactly,
    just against the singleton WebRequestConfig.open_slots instead of a
    per-Track blocked_slots, and inverted semantics: presence in the
    list means OPEN here, not blocked."""
    denied = _permission_check(request)
    if denied:
        return denied
    config = WebRequestConfig.load()
    try:
        slot = int(json.loads(request.body)["slot"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid slot"}, status=400)
    if not (0 <= slot <= 167):
        return JsonResponse({"error": "slot out of range"}, status=400)

    open_slots = set(config.open_slots)
    if slot in open_slots:
        open_slots.discard(slot)
        now_open = False
    else:
        open_slots.add(slot)
        now_open = True
    config.open_slots = sorted(open_slots)
    config.save()
    return JsonResponse({"slot": slot, "open": now_open})


@require_http_methods(["POST"])
def api_open_slot_toggle_row(request):
    """Flips an entire day-of-week row -- same master-toggle convention
    as the Track grid's toggle_row: if any hour in the row is currently
    closed, the click opens the whole row; if the row is fully open,
    the click closes the whole row."""
    denied = _permission_check(request)
    if denied:
        return denied
    config = WebRequestConfig.load()
    try:
        day_of_week = int(json.loads(request.body)["day_of_week"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid day_of_week"}, status=400)
    if not (0 <= day_of_week <= 6):
        return JsonResponse({"error": "day_of_week out of range"}, status=400)

    row_slots = [day_of_week * 24 + hour for hour in range(24)]
    open_slots = set(config.open_slots)
    # Same "not any()" master-toggle formula as the Track grid's
    # toggle_row, substituting open<->blocked: if NOTHING in the row is
    # currently open, clicking opens the whole row; if ANYTHING is
    # already open (fully or partially), clicking clears/closes the
    # whole row. Deliberately not "not all()" -- that would be a
    # different (bias-toward-filling) convention and break the muscle-
    # memory match with the Track grid's own toggle behavior.
    now_open = not any(s in open_slots for s in row_slots)

    if now_open:
        open_slots.update(row_slots)
    else:
        open_slots.difference_update(row_slots)
    config.open_slots = sorted(open_slots)
    config.save()
    return JsonResponse({"day_of_week": day_of_week, "slots": row_slots, "open": now_open})


@require_http_methods(["POST"])
def api_open_slot_toggle_column(request):
    """Flips an entire hour-of-day column (all 7 days at that hour)."""
    denied = _permission_check(request)
    if denied:
        return denied
    config = WebRequestConfig.load()
    try:
        hour = int(json.loads(request.body)["hour"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid hour"}, status=400)
    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour out of range"}, status=400)

    column_slots = [dow * 24 + hour for dow in range(7)]
    open_slots = set(config.open_slots)
    # Same "not any()" convention as toggle_row above / the Track grid's
    # toggle_column -- see that comment for why not "not all()".
    now_open = not any(s in open_slots for s in column_slots)

    if now_open:
        open_slots.update(column_slots)
    else:
        open_slots.difference_update(column_slots)
    config.open_slots = sorted(open_slots)
    config.save()
    return JsonResponse({"hour": hour, "slots": column_slots, "open": now_open})
