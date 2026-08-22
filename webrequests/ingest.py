"""Public-site transport and import helpers for listener song requests.

The public website owns submission/storage; IsadoraAir polls it and maps each
external id into the existing SongRequest lifecycle.  Scheduling deliberately
stays in webrequests.services and refresh_song_request_statuses.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from library.models import Track
from monitoring.models import SystemEvent, emit_event
from webrequests.models import SongRequest, WebRequestConfig


AUTH_HEADER = "X-IsadoraAir-Key"
CATALOG_PATH = "/api/isadoraair/catalog-sync/"
PENDING_PATH = "/api/isadoraair/requests/pending/"
STATUS_PATH = "/api/isadoraair/requests/status/"
MAX_BATCH_ITEMS = 500
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_DEDICATION_LENGTH = 2000
STATUS_SAFETY_WINDOW = timedelta(hours=1)
FAILURE_COALESCE_WINDOW = timedelta(hours=6)


class WebRequestIntegrationError(Exception):
    """Base class whose messages are safe to show in logs."""


class WebRequestConfigurationError(WebRequestIntegrationError):
    pass


class RemoteSiteError(WebRequestIntegrationError):
    pass


class RemoteProtocolError(WebRequestIntegrationError):
    pass


class PendingRequestValidationError(WebRequestIntegrationError):
    pass


def _validated_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if not value:
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_URL is required when Web Requests is enabled")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_URL must use HTTPS")
    if not parsed.hostname or parsed.username or parsed.password:
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_URL must be an HTTPS origin without userinfo")
    if parsed.query or parsed.fragment:
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_URL must not contain a query string or fragment")
    return value


def _validated_api_key(value: str) -> str:
    value = value or ""
    if not value:
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_API_KEY is required when Web Requests is enabled")
    if len(value) > 512 or "\r" in value or "\n" in value:
        raise WebRequestConfigurationError("WEB_REQUESTS_INGEST_API_KEY is invalid")
    return value


@dataclass(frozen=True)
class PublicSiteClient:
    """Small bounded client for the framework-independent public-site API."""

    base_url: str
    api_key: str
    connect_timeout: float = 5.0
    read_timeout: float = 20.0
    max_response_bytes: int = MAX_RESPONSE_BYTES
    session: object | None = None

    def __post_init__(self):
        object.__setattr__(self, "base_url", _validated_base_url(self.base_url))
        object.__setattr__(self, "api_key", _validated_api_key(self.api_key))
        if self.connect_timeout <= 0 or self.read_timeout <= 0:
            raise WebRequestConfigurationError("web-request HTTP timeouts must be positive")
        if self.max_response_bytes <= 0:
            raise WebRequestConfigurationError("web-request response limit must be positive")
        if self.session is None:
            object.__setattr__(self, "session", requests.Session())

    @classmethod
    def from_settings(cls):
        return cls(
            base_url=getattr(settings, "WEB_REQUESTS_INGEST_URL", ""),
            api_key=getattr(settings, "WEB_REQUESTS_INGEST_API_KEY", ""),
            connect_timeout=getattr(settings, "WEB_REQUESTS_INGEST_CONNECT_TIMEOUT", 5.0),
            read_timeout=getattr(settings, "WEB_REQUESTS_INGEST_READ_TIMEOUT", 20.0),
            max_response_bytes=getattr(settings, "WEB_REQUESTS_INGEST_MAX_RESPONSE_BYTES", MAX_RESPONSE_BYTES),
        )

    def _request_json(self, method: str, path: str, *, body: bytes | None = None, extra_headers=None):
        headers = {
            AUTH_HEADER: self.api_key,
            "Accept": "application/json",
            **(extra_headers or {}),
        }
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                data=body,
                headers=headers,
                timeout=(self.connect_timeout, self.read_timeout),
                stream=True,
            )
        except requests.RequestException as exc:
            raise RemoteSiteError(f"{path} request failed ({type(exc).__name__})") from exc

        try:
            if not 200 <= response.status_code < 300:
                raise RemoteSiteError(f"{path} returned HTTP {response.status_code}")
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise RemoteProtocolError(f"{path} did not return JSON")
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    if int(raw_length) > self.max_response_bytes:
                        raise RemoteProtocolError(f"{path} response exceeded the size limit")
                except ValueError as exc:
                    raise RemoteProtocolError(f"{path} returned an invalid Content-Length") from exc
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise RemoteProtocolError(f"{path} response exceeded the size limit")
                chunks.append(chunk)
            try:
                payload = json.loads(b"".join(chunks))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RemoteProtocolError(f"{path} returned malformed JSON") from exc
            if not isinstance(payload, dict):
                raise RemoteProtocolError(f"{path} response must be a JSON object")
            if payload.get("success") is not True:
                raise RemoteProtocolError(f"{path} reported failure")
            return payload
        finally:
            response.close()

    def fetch_pending_requests(self):
        payload = self._request_json("GET", PENDING_PATH)
        items = payload.get("requests")
        if not isinstance(items, list):
            raise RemoteProtocolError(f"{PENDING_PATH} response requires a requests array")
        if len(items) > MAX_BATCH_ITEMS:
            raise RemoteProtocolError(f"{PENDING_PATH} returned too many requests")
        return items

    def push_status_updates(self, updates):
        if not updates:
            return 0
        accepted = 0
        for offset in range(0, len(updates), MAX_BATCH_ITEMS):
            body = json.dumps(
                {"updates": updates[offset:offset + MAX_BATCH_ITEMS]}, separators=(",", ":"),
            ).encode("utf-8")
            payload = self._request_json(
                "POST", STATUS_PATH, body=body, extra_headers={"Content-Type": "application/json"},
            )
            updated = payload.get("updated")
            if isinstance(updated, bool) or not isinstance(updated, int) or updated < 0:
                raise RemoteProtocolError(f"{STATUS_PATH} response requires a non-negative updated count")
            accepted += updated
        return accepted

    def push_catalog(self, catalog):
        body = gzip.compress(json.dumps(catalog, separators=(",", ":")).encode("utf-8"))
        payload = self._request_json(
            "POST",
            CATALOG_PATH,
            body=body,
            extra_headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        count = payload.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RemoteProtocolError(f"{CATALOG_PATH} response requires a non-negative count")
        return count


def _normalise_pending_item(item, position):
    if not isinstance(item, dict):
        raise PendingRequestValidationError(f"request {position} must be a JSON object")

    raw_external_id = item.get("external_request_id")
    if isinstance(raw_external_id, bool) or not isinstance(raw_external_id, (str, int)):
        raise PendingRequestValidationError(f"request {position} has an invalid external_request_id")
    external_id = str(raw_external_id)
    if (
        not external_id
        or external_id != external_id.strip()
        or len(external_id) > 64
        or any(ord(char) < 32 for char in external_id)
    ):
        raise PendingRequestValidationError(f"request {position} has an invalid external_request_id")

    track_id = item.get("track_id")
    if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id <= 0:
        raise PendingRequestValidationError(f"request {position} has an invalid track_id")

    requester_name = item.get("requester_name")
    dedication_message = item.get("dedication_message")
    requester_name = "" if requester_name is None else requester_name
    dedication_message = "" if dedication_message is None else dedication_message
    if not isinstance(requester_name, str) or len(requester_name) > 100:
        raise PendingRequestValidationError(f"request {position} has an invalid requester_name")
    if not isinstance(dedication_message, str) or len(dedication_message) > MAX_DEDICATION_LENGTH:
        raise PendingRequestValidationError(f"request {position} has an invalid dedication_message")

    submitted_at_raw = item.get("submitted_at")
    try:
        submitted_at = parse_datetime(submitted_at_raw) if isinstance(submitted_at_raw, str) else None
    except ValueError:
        submitted_at = None
    if submitted_at is None or timezone.is_naive(submitted_at):
        raise PendingRequestValidationError(f"request {position} has an invalid submitted_at timestamp")

    return {
        "external_request_id": external_id,
        "track_id": track_id,
        "requester_name": requester_name,
        "dedication_message": dedication_message,
        "submitted_at": submitted_at,
    }


def ingest_pending_items(items):
    """Validate and atomically import a batch, returning created/skipped counts."""
    if not isinstance(items, list):
        raise PendingRequestValidationError("requests must be a JSON array")
    if len(items) > MAX_BATCH_ITEMS:
        raise PendingRequestValidationError("request batch exceeds the maximum size")
    normalised = [_normalise_pending_item(item, index) for index, item in enumerate(items)]
    eligible_tracks = {
        track.id: track
        for track in Track.objects.filter(
            id__in={item["track_id"] for item in normalised},
            category__kind__code="music",
            ready2air=True,
        )
    }
    now = timezone.now()
    created_count = 0
    skipped_count = 0
    with transaction.atomic():
        for item in normalised:
            track = eligible_tracks.get(item["track_id"])
            status = "pending" if track is not None else "unavailable"
            _request, created = SongRequest.objects.get_or_create(
                external_request_id=item["external_request_id"],
                defaults={
                    "track": track,
                    "requester_name": item["requester_name"],
                    "dedication_message": item["dedication_message"],
                    "submitted_at": item["submitted_at"],
                    "status": status,
                    "resolved_at": now if status == "unavailable" else None,
                    "status_updated_at": now,
                },
            )
            if created:
                created_count += 1
            else:
                skipped_count += 1
    return {"created": created_count, "skipped": skipped_count}


def build_catalog_payload(cfg=None):
    cfg = cfg or WebRequestConfig.load()
    open_slots = set(cfg.open_slots)
    tracks = (
        Track.objects.filter(category__kind__code="music", ready2air=True)
        .select_related("artist", "album")
        .only("id", "title", "artist__name", "album__title", "duration_seconds")
    )
    return {
        "tracks": [
            {
                "id": track.id,
                "artist": track.artist.name,
                "title": track.title,
                "album": track.album.title if track.album_id else None,
                "duration_seconds": track.duration_seconds,
            }
            for track in tracks.iterator()
        ],
        "availability_grid": [slot in open_slots for slot in range(168)],
    }


def build_status_payload():
    cutoff = timezone.now() - STATUS_SAFETY_WINDOW
    requests_to_report = SongRequest.objects.filter(
        Q(status__in=SongRequest.ACTIVE_STATUSES) | Q(resolved_at__gte=cutoff)
    )
    return [
        {
            "external_request_id": request.external_request_id,
            "status": request.status,
            "estimated_play_time": request.estimated_play_time.isoformat() if request.estimated_play_time else None,
            "scheduled_at": request.scheduled_at.isoformat() if request.scheduled_at else None,
            "fulfilled_at": request.fulfilled_at.isoformat() if request.fulfilled_at else None,
            "status_updated_at": request.status_updated_at.isoformat(),
        }
        for request in requests_to_report
    ]


def safe_error_message(exc):
    if isinstance(exc, WebRequestIntegrationError):
        return str(exc)
    return f"operation failed ({type(exc).__name__})"


def report_failure(operation, exc, cfg=None):
    """Coalesce repeated failures for six hours and send at most one email per window."""
    message = safe_error_message(exc)
    key = f"webrequests|{operation}"[:200]
    detail = {"operation": operation, "error": message}
    now = timezone.now()
    try:
        recent = SystemEvent.objects.filter(
            dedupe_key=key, created_at__gte=now - FAILURE_COALESCE_WINDOW,
        ).order_by("-created_at").first()
        if recent is not None:
            SystemEvent.objects.filter(pk=recent.pk).update(
                repeat_count=models.F("repeat_count") + 1,
                last_repeated_at=now,
                detail=detail,
            )
            return
        emit_event(
            "webrequests",
            f"Public-site {operation} failed",
            level="error",
            detail=detail,
            dedupe_key=key,
        )
        cfg = cfg or WebRequestConfig.load()
        if cfg.notify_email:
            send_mail(
                f"IsadoraAir web requests: {operation} failed",
                message,
                settings.DEFAULT_FROM_EMAIL,
                [cfg.notify_email],
                fail_silently=True,
            )
    except Exception:
        # The command's journal/exit status remains the fallback if the DB or
        # mail path is unavailable. Never let diagnostics mask the real error.
        return
