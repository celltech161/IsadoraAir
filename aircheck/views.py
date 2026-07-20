from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .services.recorder import current_session, start_recording, stop_recording


def _session_dict(session):
    if session is None:
        return None
    return {
        "id": session.id,
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "filename": session.filename,
        "audio_format": session.audio_format,
        "bitrate": session.bitrate,
        "source_device": session.source_device,
        "still_running": session.still_running,
        "size_bytes": session.size_bytes,
        "exit_note": session.exit_note,
    }


@require_http_methods(["GET"])
def api_aircheck_status(request):
    """Current recording state -- polled by the /monitoring/ card so
    the button reflects reality even if a session was started from a
    different admin session or reaped after a worker restart."""
    session = current_session()
    return JsonResponse({
        "recording": session is not None,
        "session": _session_dict(session),
        "server_time": timezone.now().isoformat(),
    })


@require_http_methods(["POST"])
def api_aircheck_start(request):
    session, error = start_recording()
    if session is None:
        return JsonResponse({"error": error or "start failed"}, status=500)
    payload = {"ok": True, "session": _session_dict(session)}
    if error:  # "already recording" case -- session is populated but note it
        payload["note"] = error
    return JsonResponse(payload)


@require_http_methods(["POST"])
def api_aircheck_stop(request):
    session, error = stop_recording()
    if session is None:
        return JsonResponse({"error": error or "stop failed"}, status=409)
    return JsonResponse({"ok": True, "session": _session_dict(session)})
