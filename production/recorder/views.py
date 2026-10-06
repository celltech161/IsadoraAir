"""Generic iPortal recorder/editor endpoints (2.22B B1, B5, B7, B15, B16).

Every view takes an ``adapter`` key from the URL configuration of the
consuming domain (see production.recorder.urls.recorder_urlpatterns), so one
implementation serves every consumer under the consumer's own URL space.

Security model:
* normal Django session authentication and CSRF (no exemptions); every write
  is a POST that carries the CSRF token header;
* every operation is authorized server-side by the domain adapter -- hiding a
  button is never authorization;
* the browser never names a path, a storage key, a destination or a model:
  only a registered adapter key, the adapter's own subject parameters and
  ProductionMedia UUIDs, each of which is authorization-checked;
* uploaded bytes stream (bounded, chunked, exact Content-Length) straight into
  production.services.intake -- never into memory, /tmp or permanent storage
  directly -- and are validated under OS resource confinement before any row
  can become bindable.
"""
from __future__ import annotations

import json
import time
import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import salted_hmac
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from ..errors import IntakeError, MediaInconsistent, MediaNotValidated, MediaPurged, MediaRejected, RangeNotSatisfiable
from ..models import ProductionMedia
from ..services import intake, media_io, reconcile, validation
from . import registry
from .contracts import Conflict, Forbidden, RecorderError, SubjectNotFound

# What a browser may declare. Advisory only: the bytes are sniffed and
# validated server-side regardless; this just refuses obvious non-audio early.
ALLOWED_CONTENT_TYPES = frozenset({
    "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave", "audio/webm", "audio/ogg", "audio/mp4",
    "audio/x-m4a", "audio/m4a", "audio/mpeg", "audio/mp3", "audio/flac", "audio/x-flac", "audio/aac",
    "audio/aiff", "audio/x-aiff", "application/octet-stream",
})
MODES = {
    "record": ProductionMedia.KIND_RECORDING,
    "import": ProductionMedia.KIND_UPLOAD,
    "edit": ProductionMedia.KIND_EDIT,
}
EDITOR_RECIPE_KEY = "iportal.editor"
EDITOR_RECIPE_VERSION = 1
MAX_OPERATIONS = 64

# Abandoned server-side recorder artifacts (interrupted uploads leave .part
# files in incoming/) are reclaimed by the Phase-A sweeper with its normal
# grace, at most once per interval per process -- bounded retention without a
# new scheduler.
SWEEP_INTERVAL_SECONDS = 15 * 60
_last_sweep = {"at": 0.0}


def _error(exc: RecorderError) -> JsonResponse:
    payload = {"ok": False, "error": exc.code, "message": exc.message}
    payload.update(exc.detail)
    return JsonResponse(payload, status=exc.status)


def _adapter_and_subject(adapter_key, params, user, operation):
    adapter = registry.get(adapter_key)
    if not adapter.authorize(user, operation):
        raise Forbidden("forbidden", "you are not allowed to do that here")
    return adapter, adapter.resolve(params)


def _parse_media_id(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise SubjectNotFound("unknown_media", "no such take") from None


def _accessible_media(adapter, user, subject, media_id) -> ProductionMedia:
    media = ProductionMedia.objects.filter(pk=_parse_media_id(media_id)).first()
    if media is None or not adapter.can_access_media(user, subject, media):
        raise SubjectNotFound("unknown_media", "no such take")       # never reveal existence
    return media


def _json_body(request) -> dict:
    if len(request.body or b"") > 64 * 1024:
        raise RecorderError("body_too_large", "request body too large")
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        raise RecorderError("bad_json", "malformed JSON body") from None
    if not isinstance(data, dict):
        raise RecorderError("bad_json", "malformed JSON body")
    return data


def _media_json(media: ProductionMedia, outcome=None) -> dict:
    state = media.validation_state
    retryable = state == ProductionMedia.VALIDATION_UNVALIDATED
    code = media.validation_code or (outcome.code if outcome is not None else "")
    return {
        "media_id": str(media.pk), "kind": media.kind, "validation_state": state, "validation_code": code,
        "retryable": retryable,
        "decoded_duration_seconds": float(media.decoded_duration_seconds)
        if media.decoded_duration_seconds is not None else None,
        "byte_size": media.byte_size, "sha256": media.sha256, "container": media.container,
        "codec": media.codec, "channels": media.channels, "sample_rate": media.sample_rate,
        "derived_from": str(media.derived_from_id) if media.derived_from_id else None,
    }


class _ExactLengthReader:
    """Reads exactly ``length`` bytes from the request body; a body that ends
    early (cancelled upload, dropped connection) raises, so intake removes its
    partial file and creates nothing."""

    def __init__(self, stream, length: int):
        self._stream = stream
        self._remaining = length

    def read(self, size=-1):
        if self._remaining <= 0:
            return b""
        size = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._stream.read(size)
        if not chunk:
            raise IOError("upload ended before Content-Length bytes arrived")
        self._remaining -= len(chunk)
        return chunk


def _maybe_sweep_abandoned():
    now = time.monotonic()
    if now - _last_sweep["at"] < SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep["at"] = now
    try:
        reconcile.sweep_stale_parts(apply=True)
        reconcile.sweep_stale_work(apply=True)
    except Exception:  # noqa: BLE001 -- housekeeping must never break an upload
        pass


# -- pages ---------------------------------------------------------------------

@login_required
@require_GET
def workspace_index(request):
    """The iPortal workspace hub: the media tools this user may use."""
    tools = [adapter for adapter in registry.all_adapters() if adapter.authorize(request.user, "open")]
    return render(request, "production/iportal/index.html", {"tools": tools})


@login_required
@ensure_csrf_cookie
@require_GET
def workstation(request, adapter):
    try:
        adapter_obj, subject = _adapter_and_subject(adapter, request.GET, request.user, "open")
        context = adapter_obj.context(request, subject)
    except RecorderError as exc:
        return render(request, "production/iportal/unavailable.html",
                      {"message": exc.message, "code": exc.code}, status=exc.status)
    return render(request, "production/iportal/workstation.html", {
        "recording_context": context.as_json(), "api": api_urls(request, adapter), "adapter": adapter_obj,
        "page": {"draft_namespace": draft_namespace(request.user)},
    })


def draft_namespace(user) -> str:
    """An opaque, stable per-user prefix for the browser's local draft keys, so
    two people sharing one browser profile never see each other's unsaved
    audio. Derived server-side from the authenticated user (never from
    anything the browser says). Isolation, not secrecy or authorization: the
    drafts never leave the browser and every server action is authorized
    on its own."""
    return salted_hmac("production.recorder.draft-namespace", str(user.pk)).hexdigest()[:32]


API_ENDPOINTS = ("context", "take", "revalidate", "commit", "remove", "source")
_PLACEHOLDER_ID = "00000000-0000-0000-0000-000000000000"


def url_name(adapter_key: str, endpoint: str) -> str:
    """Route names are fixed per adapter (see production.recorder.urls)."""
    return f"iportal-{adapter_key}-{endpoint}"


def api_urls(request, adapter_key: str) -> dict:
    namespace = request.resolver_match.namespace if request.resolver_match else ""
    prefix = f"{namespace}:" if namespace else ""
    urls = {name: reverse(prefix + url_name(adapter_key, name)) for name in API_ENDPOINTS}
    urls["media"] = reverse(prefix + url_name(adapter_key, "media"),
                            kwargs={"media_id": _PLACEHOLDER_ID}).replace(_PLACEHOLDER_ID + "/", "")
    return urls


# -- JSON API ------------------------------------------------------------------

@login_required
@require_GET
def api_context(request, adapter):
    try:
        adapter_obj, subject = _adapter_and_subject(adapter, request.GET, request.user, "open")
        return JsonResponse({"ok": True, "context": adapter_obj.context(request, subject).as_json()})
    except RecorderError as exc:
        return _error(exc)


@login_required
@require_POST
def api_take(request, adapter):
    """Stream one take into ProductionMedia. Query string: the adapter's
    subject parameters, ``mode`` (record|import|edit), and for ``edit`` the
    ``derived_from`` media UUID plus an ``operations`` JSON list (provenance).
    Body: the audio bytes, exactly Content-Length long. Never binds anything."""
    mode = request.GET.get("mode", "")
    if mode not in MODES:
        return _error(RecorderError("bad_mode", "mode must be record, import or edit"))
    try:
        adapter_obj, subject = _adapter_and_subject(adapter, request.GET, request.user, mode)
        policy = adapter_obj.media_policy(subject)
        content_type = (request.META.get("CONTENT_TYPE") or "").split(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise RecorderError("unsupported_type", "that file type is not accepted", status=415)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or "")
        except ValueError:
            raise RecorderError("length_required", "Content-Length is required", status=411) from None
        if length <= 0:
            raise RecorderError("empty", "the upload is empty")
        if length > policy.max_bytes:
            raise RecorderError("too_large", "the upload exceeds the size limit", status=413,
                                max_bytes=policy.max_bytes)
        parent = None
        recipe = {}
        if mode == "edit":
            parent = _accessible_media(adapter_obj, request.user, subject, request.GET.get("derived_from"))
            try:
                operations = json.loads(request.GET.get("operations") or "[]")
            except ValueError:
                raise RecorderError("bad_operations", "malformed edit operations") from None
            if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS \
                    or not all(isinstance(op, str) and len(op) <= 32 for op in operations):
                raise RecorderError("bad_operations", "malformed edit operations")
            recipe = dict(recipe_key=EDITOR_RECIPE_KEY, recipe_version=EDITOR_RECIPE_VERSION,
                          recipe_params={"operations": operations}, toolchain="iportal-browser-editor")
        _maybe_sweep_abandoned()
        source = _ExactLengthReader(request, length)
        filename = (request.headers.get("X-Recorder-Filename") or "")[:255]
        common = dict(owner=request.user, original_filename=filename, declared_content_type=content_type,
                      policy=policy)
        if parent is not None:
            result = intake.ingest_derivative(parent, source, **recipe, **common)
        else:
            result = intake.ingest_stream(source, kind=MODES[mode], **common)
    except RecorderError as exc:
        return _error(exc)
    except MediaRejected as exc:
        return JsonResponse({"ok": False, "error": exc.code, "message": "the audio was rejected",
                             "retryable": False}, status=422)
    except IntakeError as exc:
        status = 400 if exc.code in ("source_read_failed",) else 409 if exc.code.startswith("parent_") else 500
        return JsonResponse({"ok": False, "error": exc.code, "message": str(exc), "retryable": status != 409},
                            status=status)
    media = result.media
    payload = {"ok": True, "media": _media_json(media, result.outcome)}
    return JsonResponse(payload, status=201 if media.is_valid else 202)


@login_required
@require_POST
def api_revalidate(request, adapter):
    """Retry validation of a take whose validation hit a station/infrastructure
    problem (the media itself was never judged)."""
    try:
        body = _json_body(request)
        adapter_obj, subject = _adapter_and_subject(adapter, body.get("subject") or {}, request.user, "save")
        media = _accessible_media(adapter_obj, request.user, subject, body.get("media_id"))
        if media.owner_id != request.user.pk:
            raise SubjectNotFound("unknown_media", "no such take")
        outcome = validation.validate_media(media, require_engine_decode=adapter_obj.media_policy(subject)
                                            .require_engine_decode)
        media.refresh_from_db()
        return JsonResponse({"ok": True, "media": _media_json(media, outcome)})
    except RecorderError as exc:
        return _error(exc)
    except (MediaPurged, MediaInconsistent):
        return _error(SubjectNotFound("unknown_media", "no such take"))


@login_required
@require_POST
def api_commit(request, adapter):
    """Make a validated take the subject's audio (the domain decides what that
    means). ``revision`` must match the context the editor was opened with."""
    try:
        body = _json_body(request)
        adapter_obj, subject = _adapter_and_subject(adapter, body.get("subject") or {}, request.user, "save")
        media = _accessible_media(adapter_obj, request.user, subject, body.get("media_id"))
        adapter_obj.commit(request.user, subject, media, str(body.get("revision") or ""))
        return JsonResponse({"ok": True, "context": adapter_obj.context(request, adapter_obj.resolve(
            adapter_obj.subject_params(subject))).as_json()})
    except Conflict as exc:
        response = _error(exc)
        return response
    except RecorderError as exc:
        return _error(exc)


@login_required
@require_POST
def api_remove(request, adapter):
    try:
        body = _json_body(request)
        adapter_obj, subject = _adapter_and_subject(adapter, body.get("subject") or {}, request.user, "remove")
        adapter_obj.remove(request.user, subject, str(body.get("revision") or ""))
        return JsonResponse({"ok": True, "context": adapter_obj.context(request, adapter_obj.resolve(
            adapter_obj.subject_params(subject))).as_json()})
    except RecorderError as exc:
        return _error(exc)


def _stream_media(request, opened, *, download_name=None):
    try:
        byte_range = media_io.parse_byte_range(request.headers.get("Range"), opened.size)
    except RangeNotSatisfiable:
        opened.close()
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{opened.size}"
        return response
    start, end = byte_range if byte_range is not None else (0, opened.size - 1)

    def body():
        try:
            yield from media_io.iter_range(opened.file, start, end)
        finally:
            opened.close()

    response = StreamingHttpResponse(body(), status=206 if byte_range is not None else 200,
                                     content_type=opened.content_type)
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(end - start + 1)
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    if byte_range is not None:
        response["Content-Range"] = f"bytes {start}-{end}/{opened.size}"
    if download_name:
        response["Content-Disposition"] = f'attachment; filename="{download_name}"'
    return response


@login_required
@require_GET
def api_media(request, adapter, media_id):
    """Authorization-checked, Range-capable preview (or, with ?download=1 and
    the export permission, download) of one take, via the Phase-A safe open."""
    try:
        operation = "export" if request.GET.get("download") else "open"
        adapter_obj, subject = _adapter_and_subject(adapter, request.GET, request.user, operation)
        media = _accessible_media(adapter_obj, request.user, subject, media_id)
        opened = media_io.open_media(media, require_valid=True)
    except RecorderError as exc:
        return _error(exc)
    except (MediaPurged, MediaNotValidated, MediaInconsistent):
        return _error(SubjectNotFound("unknown_media", "no such take"))
    name = f"iportal-take-{media.pk}.{media.container or 'audio'}" if request.GET.get("download") else None
    return _stream_media(request, opened, download_name=name)


@login_required
@require_GET
def api_source(request, adapter):
    """The subject's current audio, for loading into the editor. A bound
    ProductionMedia streams through the safe open; a legacy file streams from
    the path the DOMAIN resolved (never a client-supplied path)."""
    try:
        adapter_obj, subject = _adapter_and_subject(adapter, request.GET, request.user, "edit")
        source = adapter_obj.source(subject)
        if source is None:
            raise SubjectNotFound("no_source", "there is no current audio to edit")
        origin, value = source
        if origin == "production_media":
            opened = media_io.open_media(value, require_valid=True)
            return _stream_media(request, opened)
        if origin == "legacy":
            handle = open(value, "rb")                     # domain-owned legacy path, resolved server-side

            def body():
                try:
                    yield from iter(lambda: handle.read(64 * 1024), b"")
                finally:
                    handle.close()

            response = StreamingHttpResponse(body(), content_type="application/octet-stream")
            response["Cache-Control"] = "private, no-store"
            response["X-Content-Type-Options"] = "nosniff"
            return response
        raise SubjectNotFound("no_source", "there is no current audio to edit")
    except RecorderError as exc:
        return _error(exc)
    except (MediaPurged, MediaNotValidated, MediaInconsistent, OSError):
        return _error(SubjectNotFound("no_source", "the current audio is not available"))
