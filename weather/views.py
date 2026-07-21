import json

from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from weather import services


@login_not_required
@csrf_exempt
@require_http_methods(["POST"])
def api_gw3000_ingest(request):
    """Public webhook endpoint for a GW3000/Ecowitt weather gateway.
    (login_not_required) because the caller is the physical Ecowitt/GW3000
    gateway hardware, which can't authenticate -- same reasoning as any
    other hardware-facing webhook. Ecowitt gateways POST form-encoded
    fields by default, so form data is checked first; JSON is accepted
    too in case that's ever reconfigured."""
    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            payload = None
    else:
        payload = request.POST.dict()

    if not isinstance(payload, dict) or not payload:
        return JsonResponse({"error": "Bad Request"}, status=400)

    services.record_gateway_payload(payload)
    services.smooth_wind()
    return JsonResponse({"ok": True})
