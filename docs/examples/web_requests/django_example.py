"""Instructional public-website implementation for the IsadoraAir protocol.

This intentionally combines model and view sketches in one file for reading.
Split it into a normal Django app before use and add site-specific rate limits,
forms, templates, migrations, permissions, and retention policy.
"""

import gzip
import io
import json
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import models, transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST


class RequestableTrack(models.Model):
    remote_id = models.PositiveBigIntegerField(primary_key=True)
    artist = models.CharField(max_length=300)
    title = models.CharField(max_length=300)
    album = models.CharField(max_length=300, blank=True)
    duration_seconds = models.FloatField(null=True)
    active = models.BooleanField(default=True)


class ListenerRequest(models.Model):
    track = models.ForeignKey(RequestableTrack, on_delete=models.PROTECT)
    requester_name = models.CharField(max_length=100, blank=True)
    dedication_message = models.TextField(blank=True)
    submitted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=16, default="pending")
    estimated_play_time = models.DateTimeField(null=True)
    scheduled_at = models.DateTimeField(null=True)
    fulfilled_at = models.DateTimeField(null=True)
    status_updated_at = models.DateTimeField(null=True)


class Availability(models.Model):
    singleton = models.BooleanField(default=True, unique=True)
    grid = models.JSONField(default=list)


def requests_are_open_now():
    """Public-site UX gate using the station-local v1 availability grid.

    Set ISADORAAIR_STATION_TIME_ZONE to the same IANA zone selected in
    IsadoraAir at Config -> Station Time. IsadoraAir remains authoritative
    about actual eligibility and scheduling after submission.
    """
    zone_name = getattr(settings, "ISADORAAIR_STATION_TIME_ZONE", "")
    try:
        local_now = timezone.now().astimezone(ZoneInfo(zone_name))
    except (TypeError, ZoneInfoNotFoundError):
        return False
    grid = Availability.objects.filter(singleton=True).values_list("grid", flat=True).first()
    slot = local_now.weekday() * 24 + local_now.hour
    return isinstance(grid, list) and len(grid) == 168 and grid[slot] is True


def _authorised(request):
    expected = getattr(settings, "ISADORAAIR_API_KEY", "")
    supplied = request.headers.get("X-IsadoraAir-Key", "")
    return bool(expected) and secrets.compare_digest(supplied, expected)


def _api_guard(view):
    def wrapped(request, *args, **kwargs):
        if not _authorised(request):
            return JsonResponse({"success": False}, status=401)
        return view(request, *args, **kwargs)

    return wrapped


@require_POST
def listener_submit(request):
    """Browser-facing: normal Django CSRF middleware applies here."""
    if not requests_are_open_now():
        return HttpResponseBadRequest("Listener requests are currently closed")
    track = get_object_or_404(
        RequestableTrack, remote_id=request.POST.get("track_id"), active=True,
    )
    name = request.POST.get("requester_name", "").strip()
    dedication = request.POST.get("dedication_message", "").strip()
    if len(name) > 100 or len(dedication) > 2000:
        return HttpResponseBadRequest("Invalid request")
    row = ListenerRequest.objects.create(
        track=track, requester_name=name, dedication_message=dedication,
    )
    return redirect("request-status", pk=row.pk)


@require_GET
@csrf_exempt
@_api_guard
def pending_requests(request):
    # Protocol v1 returns only these two public-site statuses. A newer status
    # POST can move a formerly scheduled row back here.
    rows = ListenerRequest.objects.filter(status__in=["pending", "no_slot_soon"])
    return JsonResponse({
        "success": True,
        "requests": [
            {
                # str(row.pk) is only this example's choice. A real site may
                # use its stable UUID/string/primary key (maximum 64 chars).
                "external_request_id": str(row.pk),
                "track_id": row.track_id,
                "requester_name": row.requester_name,
                "dedication_message": row.dedication_message,
                "submitted_at": row.submitted_at.isoformat(),
            }
            for row in rows[:500]
        ],
    })


@require_POST
@csrf_exempt
@_api_guard
def status_update(request):
    try:
        payload = json.loads(request.body)
        updates = payload["updates"]
        if not isinstance(updates, list) or len(updates) > 500:
            raise ValueError
        validated = []
        allowed_statuses = {
            "pending", "no_slot_soon", "scheduled", "fulfilled", "unavailable", "expired",
        }
        for update in updates:
            version = parse_datetime(update.get("status_updated_at", ""))
            if version is None or update.get("status") not in allowed_statuses:
                raise ValueError
            timestamps = {}
            for field in ("estimated_play_time", "scheduled_at", "fulfilled_at"):
                raw = update.get(field)
                timestamps[field] = parse_datetime(raw) if raw else None
                if raw and timestamps[field] is None:
                    raise ValueError
            validated.append((update, version, timestamps))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"success": False}, status=400)

    applied = 0
    with transaction.atomic():
        for update, version, timestamps in validated:
            # A strictly-newer version makes delayed/replayed POSTs harmless.
            applied += ListenerRequest.objects.filter(pk=update.get("external_request_id")).filter(
                models.Q(status_updated_at__isnull=True) | models.Q(status_updated_at__lt=version)
            ).update(
                status=update.get("status"),
                estimated_play_time=timestamps["estimated_play_time"],
                scheduled_at=timestamps["scheduled_at"],
                fulfilled_at=timestamps["fulfilled_at"],
                status_updated_at=version,
            )
    return JsonResponse({"success": True, "updated": applied})


@require_POST
@csrf_exempt
@_api_guard
def catalog_sync(request):
    try:
        if len(request.body) > 10 * 1024 * 1024:
            raise ValueError
        if request.headers.get("Content-Encoding") == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(request.body)) as compressed:
                raw = compressed.read(25 * 1024 * 1024 + 1)
        else:
            raw = request.body
        if len(raw) > 25 * 1024 * 1024:
            raise ValueError
        payload = json.loads(raw)
        tracks = payload["tracks"]
        grid = payload["availability_grid"]
        if (
            not isinstance(tracks, list)
            or len(tracks) > 100000
            or not isinstance(grid, list)
            or len(grid) != 168
            or not all(isinstance(value, bool) for value in grid)
        ):
            raise ValueError
        normalised = []
        for item in tracks:
            remote_id = item.get("id")
            artist = item.get("artist")
            title = item.get("title")
            album = item.get("album")
            duration = item.get("duration_seconds")
            if (
                isinstance(remote_id, bool)
                or not isinstance(remote_id, int)
                or remote_id <= 0
                or not isinstance(artist, str)
                or not artist
                or not isinstance(title, str)
                or not title
                or (album is not None and not isinstance(album, str))
                or (duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))))
            ):
                raise ValueError
            normalised.append((remote_id, artist, title, album or "", duration))
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return JsonResponse({"success": False}, status=400)

    with transaction.atomic():
        # For a large catalog, production code should bulk-create into a
        # staging table and swap/update efficiently. This simple form highlights
        # the full-replacement semantics.
        keep = []
        for remote_id, artist, title, album, duration in normalised:
            keep.append(remote_id)
            RequestableTrack.objects.update_or_create(
                remote_id=remote_id,
                defaults={
                    "artist": artist[:300],
                    "title": title[:300],
                    "album": album[:300],
                    "duration_seconds": duration,
                    "active": True,
                },
            )
        # Keep historical foreign keys valid while removing stale tracks from
        # the browser-facing request form/search.
        RequestableTrack.objects.exclude(remote_id__in=keep).update(active=False)
        Availability.objects.update_or_create(singleton=True, defaults={"grid": grid})
    return JsonResponse({"success": True, "count": len(keep)})
