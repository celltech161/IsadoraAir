import json
from pathlib import Path

from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import AircheckSession
from .services import recorder
from .services.recorder import classify_finalization, current_session, start_recording, stop_recording

# Path/threshold CONSTANTS are read as recorder.X at call time below,
# never bound to a local name at import time -- tests (and any future
# runtime reconfiguration) patch recorder.AIRCHECK_CURRENT_PATH etc. in
# place, which a `from .services.recorder import AIRCHECK_CURRENT_PATH`
# style import would silently stop seeing (the classic "patch where
# it's used, not where it's defined" gotcha -- a local copy of the
# bare name would no longer track the patched attribute).


def _session_dict(session):
    if session is None:
        return None
    duration_end = session.ended_at or timezone.now()
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
        "finalization": classify_finalization(session),
        "duration_seconds": (duration_end - session.started_at).total_seconds(),
    }


def _most_recently_stopped_session():
    """Newest genuinely-stopped (still_running=False) session. Ordered
    by ended_at first (nulls last -- Postgres' DESC default is nulls
    FIRST, which would otherwise put a hypothetical null-ended_at row
    ahead of real, completed ones), falling back to started_at for the
    tie/legacy case. In current code every still_running=False row also
    has ended_at set (stop_recording sets both together), so this is
    mostly belt-and-braces."""
    return (
        AircheckSession.objects
        .filter(still_running=False)
        .order_by(F("ended_at").desc(nulls_last=True), "-started_at")
        .first()
    )


def _read_buffer_heartbeat():
    """Best-effort read of the maintenance heartbeat written by
    maintain_aircheck_buffer (see recorder.record_buffer_heartbeat).
    Tolerates a missing file, a malformed/truncated one, or any other
    read error -- returns None in every such case so the caller can
    represent "unknown," never a fabricated healthy state, and never a
    500 from this read-only status endpoint."""
    try:
        with open(recorder.AIRCHECK_BUFFER_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _maintenance_dict():
    heartbeat = _read_buffer_heartbeat()
    checked_at = heartbeat.get("checked_at") if heartbeat else None
    if not isinstance(checked_at, (int, float)):
        # No usable heartbeat yet (missing file, malformed JSON, or a
        # checked_at that isn't actually a timestamp) -- unknown, not
        # "not stale". Never invent a healthy state from absence.
        return {"result": None, "checked_at": None, "last_rollover_at": None, "stale": None}
    age_seconds = timezone.now().timestamp() - checked_at
    return {
        "result": heartbeat.get("result"),
        "checked_at": checked_at,
        "last_rollover_at": heartbeat.get("last_rollover_at"),
        "stale": age_seconds > recorder.AIRCHECK_BUFFER_HEARTBEAT_STALE_SECONDS,
    }


def _buffer_dict():
    try:
        size_bytes = Path(recorder.AIRCHECK_CURRENT_PATH).stat().st_size
        exists = True
    except OSError:
        size_bytes = None
        exists = False
    return {
        "exists": exists,
        "size_bytes": size_bytes,
        "max_bytes": recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES,
        "maintenance": _maintenance_dict(),
    }


@require_http_methods(["GET"])
def api_aircheck_status(request):
    """Current recording state -- polled by the /monitoring/ card so
    the button reflects reality even if a session was started from a
    different admin session or reaped after a worker restart. Also
    reports the idle working-buffer's live size relative to the idle
    rollover threshold, the maintenance heartbeat's health, and the
    most recently stopped session's finalization state -- all
    read-only stats/JSON reads, tolerant of anything being missing or
    malformed so this endpoint never 500s over a runtime status file."""
    session = current_session()
    return JsonResponse({
        "recording": session is not None,
        "session": _session_dict(session),
        "server_time": timezone.now().isoformat(),
        "buffer": _buffer_dict(),
        "recent_session": _session_dict(_most_recently_stopped_session()),
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
