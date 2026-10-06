import json
import os
import re
import shutil
import time as time_mod
from datetime import date as date_type, time
from pathlib import Path

from django.http import FileResponse, Http404, JsonResponse, HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django.shortcuts import get_object_or_404

from authz.evaluator import authorize, forbidden_response

from isadoraair.engine_commands import EngineCommandError, enqueue_engine_command

from .models import (
    Artist, Album, Category, CategoryKind, Genre, Holiday, LogItem, Playlist,
    PlaylistItem, PlaylistLog, Rotation, RotationSlot, ScheduleBlock,
    ScheduleProfile, ScheduleProfileState, Track,
)
from .services.log_builder import (
    LOCK_CONTENDED, _build_from_playlist, build_hour_log_for_admin, get_active_schedule_profile,
    preview_hour_log,
)
from .services.remote_dj_connection import browser_ice_servers, mint_remote_dj_token
from .services.related_artists import (
    autofill_related_artists_for_queryset, canonicalize_related_artists,
    format_related_artists, resolve_fallback_metadata,
)
from .services.track_filters import filter_tracks
from .services.schedule_resolution import (
    detail_counts_for_date, effective_segments, load_hour_rows,
    load_weekly_hour_rows, minute_map,
)
from .services.schedule_profiles import (
    ProfileLifecycleError, activate_profile, archive_profile, clone_profile,
    create_profile, delete_profile, edit_profile, restore_profile,
    set_default_profile,
)

# _read_engine_state (not the whole module) -- webrequests.services
# already defines the one shared "is the running engine's state fresh
# enough to trust" reader; duplicating that staleness logic here would
# be exactly the kind of drift-prone parallel reimplementation the rest
# of this codebase avoids. No circularity: webrequests.services imports
# from library.models/library.services.*, never from library.views.
from webrequests.services import _read_engine_state


def _enqueue_engine_command_response(payload):
    """Return a truthful 503 response on bounded IPC failure, else ``None``."""

    try:
        enqueue_engine_command(payload)
    except EngineCommandError as exc:
        return JsonResponse(
            {"error": f"engine command dispatch failed: {exc}"}, status=503
        )
    return None


@ensure_csrf_cookie
def dashboard_page(request):
    from library.models import AnalysisConfig, FXCart
    from hardware.models import AudioPipeline

    playlists = Playlist.objects.all().order_by("name")
    fx_carts = FXCart.objects.filter(enabled=True).order_by("sort_order", "name")
    return render(request, "library/dashboard.html", {
        "playlists": playlists,
        "analysis_config": AnalysisConfig.load(),
        "vu_min_db": AudioPipeline.load().vu_meter_min_db,
        "mode": "full",
        "fx_carts": fx_carts,
    })


@ensure_csrf_cookie
def schedule_page(request):
    rotations = Rotation.objects.all().order_by("name")
    playlists = Playlist.objects.all().order_by("name")
    return render(request, "library/schedule.html", {"rotations": rotations, "playlists": playlists})


def _block_to_dict(b):
    """Serialize a ScheduleBlock, including either rotation or playlist details."""
    content_kind = b.content_kind
    content = b.content
    return {
        "id": b.id,
        "day_of_week": b.day_of_week,
        "start_hour": b.start_time.hour,
        "start_minute": b.start_time.minute,
        "content_kind": content_kind,
        "content_id": content.id if content else None,
        "content_name": content.name if content else None,
    }


def _json_body(request):
    try:
        value = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise ValidationError("Invalid JSON")
    if not isinstance(value, dict):
        raise ValidationError("JSON body must be an object")
    return value


def _lifecycle_error_response(exc):
    payload = {"error": exc.message}
    if exc.blockers:
        payload["blockers"] = exc.blockers
    if exc.current_active_uuid:
        payload["current_active_profile_uuid"] = exc.current_active_uuid
    return JsonResponse(payload, status=exc.status)


def _schedule_edit_denial(request):
    result = authorize(request.user, "schedule.edit")
    if result:
        return None
    return forbidden_response(result, user=request.user, capability_slug="schedule.edit")


def _profile_for_uuid(value):
    try:
        return ScheduleProfile.objects.get(uuid=value)
    except (ScheduleProfile.DoesNotExist, ValidationError, ValueError):
        raise Http404("Schedule profile not found")


def _selected_profile(request, body=None):
    value = request.GET.get("profile")
    if body is not None:
        value = body.get("profile_uuid", value)
    return _profile_for_uuid(value) if value else get_active_schedule_profile()


def _profile_to_dict(profile, state, *, block_count=None, log_count=None):
    if block_count is None:
        block_count = profile.schedule_blocks.count()
    if log_count is None:
        log_count = profile.playlist_logs.count()
    is_active = state.active_profile_id == profile.pk
    is_default = state.default_profile_id == profile.pk
    return {
        "uuid": str(profile.uuid),
        "name": profile.name,
        "description": profile.description,
        "sort_order": profile.sort_order,
        "is_archived": profile.is_archived,
        "is_active": is_active,
        "is_default": is_default,
        "block_count": block_count,
        "playlist_log_count": log_count,
        "can_delete": not is_active and not is_default and block_count == 0 and log_count == 0,
    }


def _parse_hour(value):
    try:
        hour = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("hour must be 0-23") from exc
    if not 0 <= hour <= 23:
        raise ValidationError("hour must be 0-23")
    return hour


def _parse_minute(value):
    """Minute-of-hour 0..59, default 0 when omitted. Strict: integers only (or
    an integer string); floats and other values are rejected, never rounded."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValidationError("minute must be an integer 0-59")
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or not 0 <= value <= 59:
        raise ValidationError("minute must be an integer 0-59")
    return value


def _parse_iso_date(value):
    try:
        return date_type.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError("date must be a valid ISO YYYY-MM-DD date") from exc


def _effective_date_payload(profile, target_date):
    weekly = {
        row.start_time: row
        for row in ScheduleBlock.objects.filter(
            profile=profile, day_of_week=target_date.weekday(), specific_date__isnull=True,
        ).select_related("rotation", "playlist")
    }
    overrides = {
        row.start_time: row
        for row in ScheduleBlock.objects.filter(
            profile=profile, specific_date=target_date, day_of_week__isnull=True,
        ).select_related("rotation", "playlist")
    }
    detail_counts = detail_counts_for_date(profile, target_date)
    cells = []
    for hour in range(24):
        slot = time(hour, 0)
        explicit = overrides.get(slot)
        inherited = weekly.get(slot)
        effective = explicit or inherited
        cell = {
            "hour": hour,
            "origin": "date_override" if explicit else ("weekly" if inherited else "none"),
            "explicit_block_id": explicit.pk if explicit else None,
            "inherited_block_id": inherited.pk if inherited else None,
            "effective_block": _block_to_dict(effective) if effective else None,
            # Server-derived (layered resolver): effective transitions after
            # the base in this hour, for the "detailed hour" indicator.
            "detail_count": detail_counts.get(hour, 0),
        }
        cells.append(cell)
    return {
        "profile_uuid": str(profile.uuid),
        "date": target_date.isoformat(),
        "weekday": target_date.weekday(),
        "cells": cells,
    }


@require_http_methods(["GET", "POST"])
def api_schedule_profiles(request):
    if request.method == "GET":
        state = ScheduleProfileState.load()
        profiles = ScheduleProfile.objects.annotate(
            _block_count=Count("schedule_blocks", distinct=True),
            _log_count=Count("playlist_logs", distinct=True),
        ).order_by("sort_order", "name")
        return JsonResponse({
            "active_profile_uuid": str(state.active_profile.uuid),
            "default_profile_uuid": str(state.default_profile.uuid),
            "profiles": [
                _profile_to_dict(p, state, block_count=p._block_count, log_count=p._log_count)
                for p in profiles
            ],
        })
    denied = _schedule_edit_denial(request)
    if denied:
        return denied
    try:
        body = _json_body(request)
        sort_order = body.get("sort_order")
        if sort_order is not None:
            sort_order = int(sort_order)
        profile = create_profile(
            name=body.get("name"), description=body.get("description", ""),
            sort_order=sort_order, actor=request.user,
        )
    except (ProfileLifecycleError, ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ProfileLifecycleError):
            return _lifecycle_error_response(exc)
        return JsonResponse({"error": str(exc)}, status=400)
    state = ScheduleProfileState.load()
    return JsonResponse(_profile_to_dict(profile, state), status=201)


@require_http_methods(["GET", "PATCH", "DELETE"])
def api_schedule_profile_detail(request, profile_uuid):
    profile = _profile_for_uuid(profile_uuid)
    state = ScheduleProfileState.load()
    if request.method == "GET":
        return JsonResponse(_profile_to_dict(profile, state))
    denied = _schedule_edit_denial(request)
    if denied:
        return denied
    try:
        if request.method == "DELETE":
            identity = delete_profile(profile, actor=request.user)
            return JsonResponse({"ok": True, **identity})
        profile = edit_profile(profile, changes=_json_body(request), actor=request.user)
        return JsonResponse(_profile_to_dict(profile, ScheduleProfileState.load()))
    except (ProfileLifecycleError, ValidationError) as exc:
        if isinstance(exc, ProfileLifecycleError):
            return _lifecycle_error_response(exc)
        return JsonResponse({"error": str(exc)}, status=400)


@require_http_methods(["POST"])
def api_schedule_profile_action(request, profile_uuid, action):
    denied = _schedule_edit_denial(request)
    if denied:
        return denied
    profile = _profile_for_uuid(profile_uuid)
    try:
        body = _json_body(request)
        if action == "clone":
            clone = clone_profile(
                profile, name=body.get("name"), description=body.get("description"),
                sort_order=body.get("sort_order"),
                include_date_overrides=body.get("include_date_overrides", False) is True,
                actor=request.user,
            )
            return JsonResponse(_profile_to_dict(clone, ScheduleProfileState.load()), status=201)
        if action == "activate":
            state, changed = activate_profile(
                profile, expected_active_profile_uuid=body.get("expected_active_profile_uuid"), actor=request.user,
            )
        elif action == "set-default":
            state, changed = set_default_profile(profile, actor=request.user)
        elif action == "archive":
            profile, changed = archive_profile(profile, actor=request.user)
            state = ScheduleProfileState.load()
        elif action == "restore":
            profile, changed = restore_profile(profile, actor=request.user)
            state = ScheduleProfileState.load()
        else:
            raise Http404("Unknown profile action")
        payload = _profile_to_dict(profile, state)
        payload["changed"] = changed
        return JsonResponse(payload)
    except (ProfileLifecycleError, ValidationError, TypeError, ValueError) as exc:
        if isinstance(exc, ProfileLifecycleError):
            return _lifecycle_error_response(exc)
        return JsonResponse({"error": str(exc)}, status=400)


@require_http_methods(["GET", "POST"])
def api_schedule_list(request):
    # 3.1A: the ordinary /schedule/ page still shows ONE schedule -- the
    # active profile's. The profile is read once per request and used for
    # both the read and the write below; the request/response shape is
    # unchanged from r0095.
    if request.method == "GET":
        profile = _selected_profile(request)
        if request.GET.get("date") is not None:
            try:
                target_date = _parse_iso_date(request.GET.get("date"))
            except ValidationError as exc:
                return JsonResponse({"error": exc.message}, status=400)
            return JsonResponse(_effective_date_payload(profile, target_date))
        blocks = (
            ScheduleBlock.objects
            .filter(profile=profile, day_of_week__isnull=False)
            .select_related("rotation", "playlist")
            .order_by("day_of_week", "start_time")
        )
        return JsonResponse({"blocks": [_block_to_dict(b) for b in blocks]})

    denied = _schedule_edit_denial(request)
    if denied:
        return denied

    try:
        body = _json_body(request)
    except ValidationError as exc:
        return JsonResponse({"error": exc.message}, status=400)

    profile = _selected_profile(request, body)

    day_of_week = body.get("day_of_week")
    specific_date_value = body.get("specific_date")
    try:
        hour = _parse_hour(body.get("hour"))
        minute = _parse_minute(body.get("minute"))
    except ValidationError as exc:
        return JsonResponse({"error": exc.message}, status=400)
    # Accept either {"rotation_id": ...} or {"playlist_id": ...}. The
    # schedule grid UI only writes rotations right now; playlist-backed
    # blocks come from admin and are read-only via this endpoint.
    rotation_id = body.get("rotation_id")
    playlist_id = body.get("playlist_id")

    if day_of_week is None and specific_date_value is None:
        return JsonResponse({"error": "day_of_week or specific_date is required"}, status=400)
    if day_of_week is not None and specific_date_value is not None:
        return JsonResponse({"error": "day_of_week and specific_date are mutually exclusive"}, status=400)
    if (rotation_id is None) == (playlist_id is None):
        return JsonResponse({"error": "exactly one of rotation_id or playlist_id is required"}, status=400)

    target_date = None
    if specific_date_value is not None:
        try:
            target_date = _parse_iso_date(specific_date_value)
        except ValidationError as exc:
            return JsonResponse({"error": exc.message}, status=400)
        day_of_week = None
    else:
        try:
            day_of_week = int(day_of_week)
        except (TypeError, ValueError):
            return JsonResponse({"error": "day_of_week must be 0-6"}, status=400)
        if not 0 <= day_of_week <= 6:
            return JsonResponse({"error": "day_of_week must be 0-6"}, status=400)

    defaults = {"end_time": time((hour + 1) % 24, 0)}
    if rotation_id is not None:
        try:
            defaults["rotation"] = Rotation.objects.get(id=rotation_id)
        except Rotation.DoesNotExist:
            return JsonResponse({"error": "Rotation not found"}, status=404)
        defaults["playlist"] = None
    else:
        try:
            defaults["playlist"] = Playlist.objects.get(id=playlist_id)
        except Playlist.DoesNotExist:
            return JsonResponse({"error": "Playlist not found"}, status=404)
        defaults["rotation"] = None

    with transaction.atomic():
        profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
        if profile.is_archived:
            return JsonResponse({"error": "Archived profiles are read-only."}, status=409)
        block, created = ScheduleBlock.objects.update_or_create(
            profile=profile,
            day_of_week=day_of_week,
            start_time=time(hour, minute),
            specific_date=target_date,
            defaults=defaults,
        )

    payload = _block_to_dict(block)
    payload["created"] = created
    return JsonResponse(payload)


@require_http_methods(["DELETE"])
def api_schedule_delete(request, pk):
    denied = _schedule_edit_denial(request)
    if denied:
        return denied

    profile = _selected_profile(request)
    with transaction.atomic():
        profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
        if profile.is_archived:
            return JsonResponse({"error": "Archived profiles are read-only."}, status=409)

        # Scope both the block identity and the selected profile. A stale id
        # from another profile can never delete that other profile's row.
        blocks = ScheduleBlock.objects.filter(pk=pk, profile=profile)
        if request.GET.get("date") is not None:
            try:
                target_date = _parse_iso_date(request.GET.get("date"))
            except ValidationError as exc:
                return JsonResponse({"error": exc.message}, status=400)
            blocks = blocks.filter(specific_date=target_date)
        row = blocks.select_related("profile").first()
        if row is None:
            return JsonResponse({"ok": True, "deleted": False})
        # Exactly this one explicit row; never the base or sibling transitions.
        deleted, _ = ScheduleBlock.objects.filter(pk=row.pk, profile=profile).delete()
    return JsonResponse({"ok": True, "deleted": deleted > 0})


@require_http_methods(["GET"])
def api_schedule_hour_detail(request):
    """Server-derived minute detail for ONE hour of one profile.

    Weekly mode (?day_of_week=N) or Date mode (?date=YYYY-MM-DD), plus
    ?hour=H and optional ?profile=<uuid>. The layered resolver is
    authoritative; the browser only renders what is returned here.
    """
    profile = _selected_profile(request)
    try:
        hour = _parse_hour(request.GET.get("hour"))
        date_value = request.GET.get("date")
        if date_value is not None:
            target_date = _parse_iso_date(date_value)
            weekly_rows, dated_rows = load_hour_rows(profile, target_date, hour)
            layer, day_of_week = "date", target_date.weekday()
        else:
            try:
                day_of_week = int(request.GET.get("day_of_week"))
            except (TypeError, ValueError):
                raise ValidationError("day_of_week must be 0-6")
            if not 0 <= day_of_week <= 6:
                raise ValidationError("day_of_week must be 0-6")
            target_date = None
            weekly_rows, dated_rows = load_weekly_hour_rows(profile, day_of_week, hour), []
            layer = "weekly"
    except ValidationError as exc:
        return JsonResponse({"error": exc.message}, status=400)

    entries = minute_map(weekly_rows, dated_rows, layer=layer)
    segments = effective_segments(weekly_rows, dated_rows)
    return JsonResponse({
        "profile_uuid": str(profile.uuid),
        "is_archived": profile.is_archived,
        "mode": layer,
        "day_of_week": day_of_week,
        "date": target_date.isoformat() if target_date else None,
        "hour": hour,
        "has_base": bool(segments and segments[0].start_minute == 0),
        "is_partial_hour": bool(segments and segments[0].start_minute > 0),
        "takeover_minute": segments[0].start_minute if segments else None,
        "minutes": [
            {
                "minute": entry["minute"],
                "origin": entry["origin"],
                "effective_block": _block_to_dict(entry["effective_block"]) if entry["effective_block"] else None,
                "explicit_block_id": entry["explicit_block"].pk if entry["explicit_block"] else None,
                "inherited_transition": entry["inherited_transition"],
                "segment_start": entry["segment_start"],
                "continuation": entry["continuation"],
                "orphan": entry["orphan"],
            }
            for entry in entries
        ],
        "segments": [
            {
                "start_minute": segment.start_minute,
                "start_time": f"{hour:02d}:{segment.start_minute:02d}",
                "origin": segment.origin,
                "block": _block_to_dict(segment.block),
            }
            for segment in segments
        ],
    })


# ---------------------------------------------------------------
# Categories
# ---------------------------------------------------------------

def _blank_to_none(value):
    """0 is a meaningful separation-hours value (no gap required), so a
    plain `value or None` would wrongly collapse it to None -- only an
    actually-blank field (None/"") should mean 'use the global default'."""
    return None if value in (None, "") else value


def _category_to_dict(category):
    return {
        "id": category.id,
        "code": category.code,
        "name": category.name,
        "kind_id": category.kind_id,
        "kind_code": category.kind.code if category.kind_id else "",
        "kind_name": category.kind.name if category.kind_id else "",
        "description": category.description,
        "color": category.color,
        "sort_order": category.sort_order,
        "recency_mode": category.recency_mode,
        "artist_separation": category.artist_separation,
        "title_separation": category.title_separation,
        "next_start_threshold_db_override": category.next_start_threshold_db_override,
        "cue_in_threshold_db_override": category.cue_in_threshold_db_override,
        "rbds_pty_override": category.rbds_pty_override,
        "rbds_ptyn": category.rbds_ptyn,
        "track_count": getattr(category, "_track_count", None),
    }


@ensure_csrf_cookie
def categories_page(request):
    from rbds.services.rbds_pty import CATEGORY_PTY_OVERRIDE_CHOICES

    kinds = CategoryKind.objects.order_by("sort_order", "name")
    return render(request, "library/categories.html", {
        "kinds": kinds,
        "pty_choices": CATEGORY_PTY_OVERRIDE_CHOICES,
    })


@require_http_methods(["GET", "POST"])
def api_category_list(request):
    if request.method == "GET":
        categories = (
            Category.objects.select_related("kind")
            .annotate(_track_count=Count("tracks", distinct=True))
            .order_by("sort_order", "code")
        )
        return JsonResponse({"categories": [_category_to_dict(c) for c in categories]})

    # Roadmap 2.5A: category CREATE requires library.manage_categories.
    # Reads (the GET branch above) are deliberately left open -- the
    # /library/import/ upload page's category dropdown depends on
    # Contributor accounts being able to read this list.
    result = authorize(request.user, "library.manage_categories")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_categories")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    code = (body.get("code") or "").strip()
    name = (body.get("name") or "").strip()
    kind_id = body.get("kind_id")

    if not code or not name or not kind_id:
        return JsonResponse({"error": "code, name, and kind_id are required"}, status=400)

    if Category.objects.filter(code=code).exists():
        return JsonResponse({"error": "A category with that code already exists"}, status=400)

    kind = get_object_or_404(CategoryKind, pk=kind_id)

    category = Category(
        code=code,
        name=name,
        kind=kind,
        description=body.get("description", ""),
        color=body.get("color", ""),
        sort_order=body.get("sort_order") or 0,
        recency_mode=body.get("recency_mode") or "time",
        artist_separation=_blank_to_none(body.get("artist_separation")),
        title_separation=_blank_to_none(body.get("title_separation")),
        next_start_threshold_db_override=_blank_to_none(body.get("next_start_threshold_db_override")),
        cue_in_threshold_db_override=_blank_to_none(body.get("cue_in_threshold_db_override")),
        rbds_pty_override=_blank_to_none(body.get("rbds_pty_override")),
        rbds_ptyn=body.get("rbds_ptyn", ""),
    )
    try:
        category.full_clean()
    except ValidationError as e:
        return JsonResponse({"error": "; ".join(e.messages)}, status=400)
    category.save()

    category = Category.objects.select_related("kind").annotate(_track_count=Count("tracks", distinct=True)).get(pk=category.pk)
    return JsonResponse(_category_to_dict(category))


@require_http_methods(["GET", "PATCH", "DELETE"])
def api_category_detail(request, pk):
    category = get_object_or_404(
        Category.objects.select_related("kind").annotate(_track_count=Count("tracks", distinct=True)),
        pk=pk,
    )

    if request.method == "GET":
        data = _category_to_dict(category)
        # Roadmap 4.2: which Rotations currently reference this Category,
        # for the "Used by rotations" section on the Category edit pane.
        # Deliberately only computed here (the single-category detail
        # fetch), not folded into _category_to_dict itself -- that
        # function is also used by api_category_list's per-row dict for
        # EVERY category, where adding this query per row would be an
        # N+1. Rotation.slots is the real (only) ownership relationship
        # -- a RotationSlot referencing this Category -- so .distinct()
        # collapses a Rotation that uses the Category in multiple slots
        # down to one entry, matching Rotation's own default `name`
        # ordering (Rotation.Meta.ordering) for a stable list.
        data["used_by_rotations"] = [
            {"id": r.id, "name": r.name}
            for r in Rotation.objects.filter(slots__category=category).distinct()
        ]
        return JsonResponse(data)

    # Roadmap 2.5A: category UPDATE/DELETE require library.manage_categories.
    # Previously reachable by any Contributor account (their GroupAccess
    # prefix /api/categories/ was granted only for the GET branch above,
    # to populate the upload page's dropdown) with zero other check --
    # see PROJECT_NOTES.md's "Roadmap 2.5" section.
    result = authorize(request.user, "library.manage_categories")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_categories")

    if request.method == "DELETE":
        try:
            category.delete()
        except ProtectedError:
            slot_count = category.rotation_slots.count()
            return JsonResponse({
                "error": f"Category is used by {slot_count} rotation slot(s). Remove those first.",
            }, status=400)
        return JsonResponse({"ok": True})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "code" in body:
        code = (body["code"] or "").strip()
        if not code:
            return JsonResponse({"error": "code cannot be blank"}, status=400)
        if Category.objects.exclude(pk=category.pk).filter(code=code).exists():
            return JsonResponse({"error": "A category with that code already exists"}, status=400)
        category.code = code
    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            return JsonResponse({"error": "name cannot be blank"}, status=400)
        category.name = name
    if "kind_id" in body:
        category.kind = get_object_or_404(CategoryKind, pk=body["kind_id"])
    if "description" in body:
        category.description = body["description"]
    if "color" in body:
        category.color = body["color"]
    if "sort_order" in body:
        category.sort_order = body["sort_order"] or 0
    if "recency_mode" in body:
        category.recency_mode = body["recency_mode"]
    if "artist_separation" in body:
        category.artist_separation = _blank_to_none(body["artist_separation"])
    if "title_separation" in body:
        category.title_separation = _blank_to_none(body["title_separation"])
    if "next_start_threshold_db_override" in body:
        category.next_start_threshold_db_override = _blank_to_none(body["next_start_threshold_db_override"])
    if "cue_in_threshold_db_override" in body:
        category.cue_in_threshold_db_override = _blank_to_none(body["cue_in_threshold_db_override"])
    if "rbds_pty_override" in body:
        category.rbds_pty_override = _blank_to_none(body["rbds_pty_override"])
    if "rbds_ptyn" in body:
        category.rbds_ptyn = body["rbds_ptyn"]

    try:
        category.full_clean()
    except ValidationError as e:
        return JsonResponse({"error": "; ".join(e.messages)}, status=400)
    category.save()

    return JsonResponse(_category_to_dict(category))


@require_http_methods(["POST"])
def api_track_repick_cue_points(request, pk):
    """Fast cue-point repick for a single track: reads the saved envelope
    from the track's waveform JSON, applies the track's effective
    thresholds (category overrides on top of global AnalysisConfig
    defaults), writes the new cue_in_seconds / next_start_seconds back
    to both the DB and the JSON. No ffmpeg decode -- seconds instead of
    minutes.

    Returns 400 when the track's waveform JSON is missing or lacks
    envelope data (pre-envelope-persistence): the client-side handler
    surfaces the error message so the operator knows to use "Reanalyze
    Track" (which does the full decode + envelope pass) instead.

    Roadmap 2.5A: requires library.manage_tracks -- previously reachable
    by any Contributor/remote_dj account via their /api/tracks/
    GroupAccess prefix with zero other check. See PROJECT_NOTES.md's
    "Roadmap 2.5" section."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    from library.management.commands.analyze_tracks import (
        apply_category_thresholds, repick_cue_points_from_json,
    )
    from library.models import AnalysisConfig
    track = get_object_or_404(Track.objects.select_related("category"), pk=pk)
    if not track.waveform_path or not Path(track.waveform_path).is_file():
        return JsonResponse({
            "error": "No waveform data for this track yet -- use \"Reanalyze "
                     "Track\" to run a full analysis pass first.",
        }, status=400)
    cfg = AnalysisConfig.load()
    _sr, _ws, _tp, ns_db, ci_db, ci_min = apply_category_thresholds(
        (cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
         cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds),
        track.category.next_start_threshold_db_override if track.category_id else None,
        track.category.cue_in_threshold_db_override if track.category_id else None,
    )
    try:
        next_start, cue_in, payload = repick_cue_points_from_json(
            track.waveform_path, ns_db, ci_db, ci_min,
        )
    except LookupError:
        return JsonResponse({
            "error": "This track's waveform JSON pre-dates envelope "
                     "persistence -- use \"Reanalyze Track\" first to "
                     "populate envelope data. Future repicks on it will "
                     "then be instant.",
        }, status=400)
    Track.objects.filter(id=track.id).update(
        next_start_seconds=next_start, cue_in_seconds=cue_in,
    )
    try:
        Path(track.waveform_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        print(f"  [track repick] track {track.id} JSON write failed: {exc}")
    track.refresh_from_db()
    return JsonResponse(_track_to_dict(track))


@require_http_methods(["POST"])
def api_category_repick_cue_points(request, pk):
    """Smart cue-point refresh for every track in this category. For each
    track, fast-paths against the saved mono envelope in its waveform
    JSON when present -- reads the envelope, applies the category's
    effective thresholds (with per-category overrides folded in on top
    of the global AnalysisConfig defaults), writes the new cue-in /
    next-start values back to the DB and the JSON. Order of seconds
    per hundred tracks; no ffmpeg re-decode.

    Falls back to the slow path (NULL `next_start_seconds` so the
    analyze timer re-runs analyze_one_track, which does re-decode) for
    tracks whose JSON pre-dates envelope persistence, or is missing
    entirely, or fails to parse. Newly-analyzed tracks after this
    commit land with envelope data automatically -- backfill happens
    organically as tracks cycle through the analyzer for other reasons
    (uploads, edits, previous slow-path repicks).

    Returns a per-path breakdown so the operator sees fast-path coverage
    grow over time as the library backfills organically.

    Roadmap 2.5A: requires library.manage_categories -- same
    reachability-only gap as api_category_detail (see its comment)."""
    result = authorize(request.user, "library.manage_categories")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_categories")

    from library.management.commands.analyze_tracks import (
        apply_category_thresholds, repick_cue_points_from_json,
    )
    from library.models import AnalysisConfig, Track
    category = get_object_or_404(Category, pk=pk)
    cfg = AnalysisConfig.load()
    _sr, _ws, _tp, ns_db, ci_db, ci_min = apply_category_thresholds(
        (cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
         cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds),
        category.next_start_threshold_db_override,
        category.cue_in_threshold_db_override,
    )
    tracks = list(Track.objects.filter(category=category).values_list(
        "id", "waveform_path",
    ))
    repicked = 0
    queued_full = 0
    for track_id, waveform_path in tracks:
        if not waveform_path or not Path(waveform_path).is_file():
            # No JSON at all -- fall through to full analyze.
            Track.objects.filter(id=track_id).update(next_start_seconds=None)
            queued_full += 1
            continue
        try:
            next_start, cue_in, payload = repick_cue_points_from_json(
                waveform_path, ns_db, ci_db, ci_min,
            )
        except LookupError:
            # Pre-envelope-persistence JSON -- can't fast-path this
            # track. Queue it for the analyze timer which will land
            # envelope data on the way through, enabling fast-path
            # repicks in the future.
            Track.objects.filter(id=track_id).update(next_start_seconds=None)
            queued_full += 1
            continue
        except Exception as exc:
            # Corrupt/unreadable JSON -- also fall through to slow path
            # rather than failing the whole batch on one bad file.
            print(f"  [repick] track {track_id} JSON error: {exc}; queuing full re-analyze")
            Track.objects.filter(id=track_id).update(next_start_seconds=None)
            queued_full += 1
            continue
        Track.objects.filter(id=track_id).update(
            next_start_seconds=next_start, cue_in_seconds=cue_in,
        )
        try:
            Path(waveform_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            # DB is authoritative; JSON is display cache. Log and move on.
            print(f"  [repick] track {track_id} JSON write failed: {exc}")
        repicked += 1
    total = repicked + queued_full
    if queued_full:
        suffix = (
            f" {queued_full} track(s) missing envelope data are queued "
            f"for a full re-analyze (the timer will pick them up in the "
            f"next minute) -- future repicks on those will be instant."
        )
    else:
        suffix = ""
    return JsonResponse({
        "ok": True,
        "repicked": repicked,
        "queued_full_analyze": queued_full,
        "total": total,
        "message": (
            f"Category '{category.code}': repicked {repicked} track(s) "
            f"instantly from saved envelope data.{suffix}"
        ),
    })


@require_http_methods(["POST"])
def api_category_reset_analysis(request, pk):
    """Clear the analysis marks (`next_start_seconds` = NULL) on every
    track in this category so `isadoraair-analyze.timer` re-picks them
    up within the minute and re-runs the cue-point analysis using the
    category's current threshold overrides.

    Only touches `next_start_seconds` because that's the field the
    analyze timer's queryset filters on (`.filter(
    next_start_seconds__isnull=True)`). analyze_one_track always
    recomputes both `cue_in_seconds` AND `next_start_seconds` when it
    runs, so setting one to NULL is enough to get both refreshed --
    no need to also clear cue_in_seconds separately.

    Manual cue-point edits (`intro_until_seconds`, `sweep_start_seconds`,
    `outro_starts_seconds`, `hook_in_seconds`, `hook_out_seconds`) are
    NEVER written by analyze_one_track and so are preserved -- an
    operator's careful hand-tuning of a hook or outro survives a
    category-wide re-analysis untouched.

    Deliberately fires the work asynchronously (via the existing
    analyze timer) rather than blocking the HTTP request on a
    potentially-minutes-long re-analysis loop -- same pattern as
    api_library_upload since 178dc70.

    Roadmap 2.5A: requires library.manage_categories -- same
    reachability-only gap as api_category_detail (see its comment)."""
    result = authorize(request.user, "library.manage_categories")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_categories")

    category = get_object_or_404(Category, pk=pk)
    from library.models import Track
    count = Track.objects.filter(category=category).update(next_start_seconds=None)
    return JsonResponse({
        "ok": True,
        "queued": count,
        "message": (
            f"{count} track(s) in '{category.code}' queued for re-analysis. "
            f"The analyze timer will process them over the next minute."
        ),
    })


@require_http_methods(["GET", "POST"])
def api_rotation_list(request):
    if request.method == "GET":
        rotations = Rotation.objects.all().order_by("name").annotate(_slot_count=Count("slots"))
        data = [
            {"id": r.id, "name": r.name, "description": r.description, "slot_count": r._slot_count}
            for r in rotations
        ]
        return JsonResponse({"rotations": data})

    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)

    if Rotation.objects.filter(name=name).exists():
        return JsonResponse({"error": "A rotation with that name already exists"}, status=400)

    rotation = Rotation.objects.create(name=name, description=body.get("description", ""))
    return JsonResponse(_rotation_to_dict(rotation))


def _rotation_to_dict(rotation):
    slots = (
        rotation.slots
        .select_related("category", "track", "track__artist", "track__category")
        .order_by("position")
    )
    slot_data = []
    for slot in slots:
        if slot.track_id:
            slot_data.append({
                "id": slot.id,
                "position": slot.position,
                "slot_type": "track",
                "track_id": slot.track_id,
                "title": slot.track.title,
                "artist": slot.track.artist.name if slot.track.artist else "",
                "category_code": slot.track.category.code if slot.track.category else "",
            })
        else:
            slot_data.append({
                "id": slot.id,
                "position": slot.position,
                "slot_type": "category",
                "category_id": slot.category_id,
                "category_code": slot.category.code,
                "category_name": slot.category.name,
            })
    return {
        "id": rotation.id,
        "name": rotation.name,
        "description": rotation.description,
        "slots": slot_data,
    }


@require_http_methods(["GET", "PATCH", "DELETE"])
def api_rotation_detail(request, pk):
    if request.method == "GET":
        rotation = get_object_or_404(Rotation, pk=pk)
        return JsonResponse(_rotation_to_dict(rotation))

    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    rotation = get_object_or_404(Rotation, pk=pk)

    if request.method == "DELETE":
        try:
            rotation.delete()
        except ProtectedError:
            block_count = rotation.schedule_blocks.count()
            return JsonResponse({
                "error": f"Rotation is used by {block_count} schedule block(s). Remove those from the schedule first.",
            }, status=400)
        return JsonResponse({"ok": True})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            return JsonResponse({"error": "name cannot be blank"}, status=400)
        rotation.name = name
    if "description" in body:
        rotation.description = body["description"]
    rotation.save()

    return JsonResponse(_rotation_to_dict(rotation))


@require_http_methods(["POST"])
def api_rotation_add_slot(request, pk):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    rotation = get_object_or_404(Rotation, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    category_id = body.get("category_id")
    track_id = body.get("track_id")
    if bool(category_id) == bool(track_id):
        return JsonResponse({"error": "Provide exactly one of category_id or track_id"}, status=400)

    next_position = rotation.slots.count()
    if track_id:
        track = get_object_or_404(Track, pk=track_id)
        slot = RotationSlot(rotation=rotation, position=next_position, track=track)
    else:
        category = get_object_or_404(Category, pk=category_id)
        slot = RotationSlot(rotation=rotation, position=next_position, category=category)

    try:
        slot.full_clean()
    except ValidationError as e:
        return JsonResponse({"error": "; ".join(e.messages)}, status=400)
    slot.save()

    return JsonResponse(_rotation_to_dict(rotation))


@require_http_methods(["DELETE"])
def api_rotation_remove_slot(request, slot_id):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    slot = get_object_or_404(RotationSlot.objects.select_related("rotation"), pk=slot_id)
    rotation = slot.rotation
    slot.delete()

    remaining = list(rotation.slots.order_by("position"))
    _reposition_items(remaining, model=RotationSlot)

    return JsonResponse(_rotation_to_dict(rotation))


@require_http_methods(["POST"])
def api_rotation_reorder(request, pk):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    rotation = get_object_or_404(Rotation, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    order = body.get("order")
    if not order or not isinstance(order, list):
        return JsonResponse({"error": "order (list of slot IDs) is required"}, status=400)

    slots_by_id = {slot.id: slot for slot in rotation.slots.all()}
    ordered = [slots_by_id[i] for i in order if i in slots_by_id]
    _reposition_items(ordered, model=RotationSlot)

    return JsonResponse(_rotation_to_dict(rotation))


@require_http_methods(["POST"])
def api_rotation_copy(request, pk):
    """Duplicate a rotation and all its slots under a new name -- for
    building a variant rotation without starting from an empty slot list."""
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    source = get_object_or_404(Rotation, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)
    if Rotation.objects.filter(name=name).exists():
        return JsonResponse({"error": "A rotation with that name already exists"}, status=400)

    new_rotation = Rotation.objects.create(name=name, description=source.description)
    RotationSlot.objects.bulk_create([
        RotationSlot(
            rotation=new_rotation,
            position=slot.position,
            category_id=slot.category_id,
            track_id=slot.track_id,
        )
        for slot in source.slots.order_by("position")
    ])

    return JsonResponse(_rotation_to_dict(new_rotation))


@ensure_csrf_cookie
def rotations_page(request):
    categories = Category.objects.order_by("name")
    return render(request, "library/rotations.html", {"categories": categories})


@require_http_methods(["GET", "POST"])
def api_playlist_list(request):
    if request.method == "GET":
        playlists = Playlist.objects.all().order_by("name").annotate(_item_count=Count("items"))
        data = [
            {"id": p.id, "name": p.name, "description": p.description, "item_count": p._item_count}
            for p in playlists
        ]
        return JsonResponse({"playlists": data})

    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)

    if Playlist.objects.filter(name=name).exists():
        return JsonResponse({"error": "A playlist with that name already exists"}, status=400)

    playlist = Playlist.objects.create(name=name, description=body.get("description", ""))
    return JsonResponse(_playlist_to_dict(playlist))


def _playlist_to_dict(playlist):
    items = (
        playlist.items
        .select_related("track", "track__artist", "track__category")
        .order_by("position")
    )
    return {
        "id": playlist.id,
        "name": playlist.name,
        "description": playlist.description,
        "items": [
            {
                "id": item.id,
                "position": item.position,
                "track_id": item.track_id,
                "title": item.track.title,
                "artist": item.track.artist.name if item.track.artist else "",
                "duration_seconds": item.track.duration_seconds,
                "next_start_seconds": item.track.next_start_seconds,
                "category_code": item.track.category.code if item.track.category else "",
            }
            for item in items
        ],
    }


@require_http_methods(["GET", "PATCH", "DELETE"])
def api_playlist_detail(request, pk):
    if request.method == "GET":
        playlist = get_object_or_404(Playlist, pk=pk)
        return JsonResponse(_playlist_to_dict(playlist))

    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    playlist = get_object_or_404(Playlist, pk=pk)

    if request.method == "DELETE":
        try:
            playlist.delete()
        except ProtectedError:
            block_count = playlist.schedule_blocks.count()
            return JsonResponse({
                "error": f"Playlist is used by {block_count} schedule block(s). Remove those from the schedule first.",
            }, status=400)
        return JsonResponse({"ok": True})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    if "name" in body:
        name = (body["name"] or "").strip()
        if not name:
            return JsonResponse({"error": "name cannot be blank"}, status=400)
        playlist.name = name
    if "description" in body:
        playlist.description = body["description"]
    playlist.save()

    return JsonResponse(_playlist_to_dict(playlist))


@require_http_methods(["POST"])
def api_playlist_add_item(request, pk):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    playlist = get_object_or_404(Playlist, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    track_id = body.get("track_id")
    if not track_id:
        return JsonResponse({"error": "track_id is required"}, status=400)

    track = get_object_or_404(Track, pk=track_id)

    next_position = playlist.items.count()
    PlaylistItem.objects.create(playlist=playlist, position=next_position, track=track)

    return JsonResponse(_playlist_to_dict(playlist))


@require_http_methods(["DELETE"])
def api_playlist_remove_item(request, item_id):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    item = get_object_or_404(PlaylistItem.objects.select_related("playlist"), pk=item_id)
    playlist = item.playlist
    item.delete()

    remaining = list(playlist.items.order_by("position"))
    _reposition_items(remaining, model=PlaylistItem)

    return JsonResponse(_playlist_to_dict(playlist))


@require_http_methods(["POST"])
def api_playlist_reorder(request, pk):
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    playlist = get_object_or_404(Playlist, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    order = body.get("order")
    if not order or not isinstance(order, list):
        return JsonResponse({"error": "order (list of item IDs) is required"}, status=400)

    items_by_id = {item.id: item for item in playlist.items.all()}
    ordered = [items_by_id[i] for i in order if i in items_by_id]
    _reposition_items(ordered, model=PlaylistItem)

    return JsonResponse(_playlist_to_dict(playlist))


@require_http_methods(["POST"])
def api_playlist_copy(request, pk):
    """Duplicate a playlist and all its items under a new name -- for
    building a variant playlist without starting from an empty list."""
    result = authorize(request.user, "rotations_playlists.edit")
    if not result:
        return forbidden_response(
            result, user=request.user, capability_slug="rotations_playlists.edit"
        )

    source = get_object_or_404(Playlist, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    name = (body.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "name is required"}, status=400)
    if Playlist.objects.filter(name=name).exists():
        return JsonResponse({"error": "A playlist with that name already exists"}, status=400)

    new_playlist = Playlist.objects.create(name=name, description=source.description)
    PlaylistItem.objects.bulk_create([
        PlaylistItem(playlist=new_playlist, position=item.position, track_id=item.track_id)
        for item in source.items.order_by("position")
    ])

    return JsonResponse(_playlist_to_dict(new_playlist))


@csrf_exempt
@require_http_methods(["POST"])
def api_playlist_play_now(request, pk):
    """Force the engine to play this playlist immediately, replacing
    whatever's assigned to the current hour rather than waiting for a
    scheduled slot.

    Roadmap 2.5C: requires playout.queue_manage. Found during the
    mandated repository-wide re-sweep -- same shape as api_engine_set_next/
    api_engine_insert_track (remote_dj_page's own docstring names "Play
    Now" as an intended Remote Host feature; previously reachable only
    via the seeded regex GroupAccess grant, with no other check at
    all)."""
    result = authorize(request.user, "playout.queue_manage")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.queue_manage")

    playlist = get_object_or_404(Playlist, pk=pk)

    now = timezone.localtime()
    log, error = _build_from_playlist(now.date(), now.hour, playlist)
    if error:
        return JsonResponse({"error": error}, status=400)

    log.status = "approved"
    log.save(update_fields=["status"])

    error_response = _enqueue_engine_command_response(
        {"command": "reload_current_log"}
    )
    if error_response is not None:
        return error_response

    return JsonResponse({"ok": True, "log_id": log.id, "item_count": log.items.count()})


@ensure_csrf_cookie
def playlists_page(request):
    categories = Category.objects.order_by("name")
    return render(request, "library/playlists.html", {"categories": categories})


def welcome_page(request):
    """Landing page for authenticated users who don't belong to any
    recognized group -- shown instead of the studio-operator dashboard
    they'd otherwise land on. Content is intentionally minimal: the
    station's brand, a short "you don't have privileges" message, and
    contact details so the user can reach out if that's unexpected.

    Also reachable directly by anyone (staff/superuser included) via
    /welcome/ -- there's no reason to hide it, and a superuser can use
    the URL to preview what an unprivileged user would see."""
    return render(request, "library/welcome.html", {})


def library_page(request):
    categories = Category.objects.order_by("name")
    holidays = Holiday.objects.order_by("month", "day")
    return render(request, "library/library.html", {
        "categories": categories,
        "holidays": holidays,
    })


TRACK_SORT_FIELDS = {
    "title": "title",
    "artist": "artist__name",
    "album": "album__title",
    "category": "category__code",
    "duration": "duration_seconds",
    "ready2air": "ready2air",
    "format": "format",
}


@require_http_methods(["GET"])
def api_track_list(request):
    qs = Track.objects.select_related("artist", "album", "category")

    q = request.GET.get("q", "").strip()
    cat_id = request.GET.get("category")
    ready = request.GET.get("ready2air")
    ready2air = True if ready == "true" else False if ready == "false" else None
    # Shared with the related-artists autofill endpoint/command -- see
    # track_filters.filter_tracks's docstring for exactly what "+" and
    # category_id mean here.
    qs = filter_tracks(qs, q=q, category_id=cat_id, ready2air=ready2air)

    sort_field = request.GET.get("sort", "title")
    sort_dir = request.GET.get("dir", "asc")
    db_field = TRACK_SORT_FIELDS.get(sort_field, "title")
    if sort_dir == "desc":
        db_field = "-" + db_field
    qs = qs.order_by(db_field)

    per_page = min(int(request.GET.get("per_page", 50)), 200)
    page_num = int(request.GET.get("page", 1))
    paginator = Paginator(qs, per_page)
    page = paginator.get_page(page_num)

    items = [
        {
            "id": t.id,
            "title": t.title,
            "artist": t.artist.name if t.artist else "",
            "album": t.album.title if t.album else "",
            "category_code": t.category.code if t.category else "",
            "category_name": t.category.name if t.category else "",
            "duration_seconds": t.duration_seconds,
            "next_start_seconds": t.next_start_seconds,
            "format": t.format,
            "ready2air": t.ready2air,
        }
        for t in page
    ]

    return JsonResponse({
        "items": items,
        "total": paginator.count,
        "page": page.number,
        "pages": paginator.num_pages,
        "per_page": per_page,
    })


@require_http_methods(["POST"])
def api_track_bulk(request):
    """Roadmap 2.5A: requires library.manage_tracks -- includes a bulk
    "delete" action, previously reachable by any Contributor/remote_dj
    account via their /api/tracks/ GroupAccess prefix with zero other
    check. See PROJECT_NOTES.md's "Roadmap 2.5" section."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    action = body.get("action")
    ids = body.get("ids", [])

    if not ids:
        return JsonResponse({"error": "No track IDs provided"}, status=400)

    qs = Track.objects.filter(id__in=ids)

    if action == "ready2air_on":
        updated = qs.update(ready2air=True)
    elif action == "ready2air_off":
        updated = qs.update(ready2air=False)
    elif action == "set_category":
        cat_id = body.get("category_id")
        if cat_id:
            try:
                Category.objects.get(id=cat_id)
            except Category.DoesNotExist:
                return JsonResponse({"error": "Category not found"}, status=404)
        updated = qs.update(category_id=cat_id)
    elif action == "add_additional_category":
        # ADD (not replace) the category to each selected track's
        # additional_categories M2M -- same non-destructive semantics
        # as add_holiday. A track that's already tagged with this cat
        # as an additional (or has it as primary) is skipped silently
        # via ignore_conflicts + the same-as-primary guard used in
        # track_update. Bulk replace would be a foot-gun; if that's
        # ever wanted it needs its own action.
        cat_id = body.get("category_id")
        if not cat_id:
            return JsonResponse({"error": "category_id required"}, status=400)
        try:
            Category.objects.get(id=cat_id)
        except Category.DoesNotExist:
            return JsonResponse({"error": "Category not found"}, status=404)
        # Exclude tracks whose PRIMARY category is already this cat --
        # M2M row for (track, cat) makes no sense when the FK already
        # points there, and the track_update path guards the same way.
        target_ids = list(qs.exclude(category_id=cat_id).values_list("id", flat=True))
        through = Track.additional_categories.through
        existing = set(
            through.objects.filter(track_id__in=target_ids, category_id=cat_id)
            .values_list("track_id", flat=True)
        )
        rows = [through(track_id=tid, category_id=cat_id)
                for tid in target_ids if tid not in existing]
        through.objects.bulk_create(rows, ignore_conflicts=True)
        updated = len(rows)
    elif action == "add_holiday":
        # ADD the holiday to each selected track's holidays M2M --
        # doesn't clobber existing tags, so a Halloween-tagged track
        # can also pick up a Christmas tag in a later bulk operation.
        code = body.get("holiday_code")
        if not code:
            return JsonResponse({"error": "holiday_code required"}, status=400)
        try:
            holiday = Holiday.objects.get(code=code)
        except Holiday.DoesNotExist:
            return JsonResponse({"error": "Holiday not found"}, status=404)
        # Bulk-add through the M2M via the through model, ignoring
        # rows that already have the pair. Django's .add(*iterable)
        # is per-object and would issue N INSERTs; going through
        # bulk_create with ignore_conflicts=True lets us do one
        # INSERT and skip existing rows silently.
        through = Track.holidays.through
        existing = set(
            through.objects.filter(track_id__in=list(qs.values_list("id", flat=True)),
                                    holiday_id=code)
            .values_list("track_id", flat=True)
        )
        rows = [through(track_id=tid, holiday_id=code)
                for tid in qs.values_list("id", flat=True) if tid not in existing]
        through.objects.bulk_create(rows, ignore_conflicts=True)
        updated = len(rows)
    elif action == "delete":
        # Best-effort per-track: PROTECT-FK blockers on ONE track
        # (still in a Playlist or Rotation slot) shouldn't prevent
        # the rest of the selection from being deleted. Report both
        # counts and per-blocker detail so the operator sees exactly
        # which ones need manual cleanup first.
        deleted = 0
        blocked = []
        for t in qs.select_related("artist"):
            ok, reason = _delete_track_and_file(t)
            if ok:
                deleted += 1
            else:
                blocked.append({
                    "id": t.id,
                    "title": t.title,
                    "artist": t.artist.name if t.artist else "",
                    "reason": reason,
                })
        return JsonResponse({"ok": True, "deleted": deleted, "blocked": blocked})
    else:
        return JsonResponse({"error": "Unknown action"}, status=400)

    return JsonResponse({"ok": True, "updated": updated})


@require_http_methods(["POST"])
def api_track_autofill_related_artists(request):
    """/library/'s "Auto-fill Related Artists" toolbar action. Applies
    automatic related-artist discovery to the COMPLETE queryset
    matching the posted filter (search/category/ready2air) -- not a
    page, not a selection, the same filter_tracks() helper
    api_track_list uses so this always matches what the operator sees
    filtered to. Calls the shared related_artists service directly
    (no subprocess, no manage.py invocation) -- the exact same function
    the autofill_related_artists management command's --apply path
    calls, so the two can never drift apart in behavior.

    Always applies (apply=True) -- the frontend's own confirm() dialog
    is this endpoint's "are you sure", there's no separate dry-run mode
    over HTTP (that's what the management command is for)."""
    # Roadmap 2.5E: replaces the prior "allowed unless Contributor/
    # remote_dj" negative check with a positive capability requirement.
    # Behaviorally identical for the two seeded non-staff Groups (neither
    # holds library.manage_tracks, so both remain blocked exactly as
    # before) but closes the gap for any future Group whose GroupAccess
    # reaches this URL without also being granted track-management
    # authority. This is a bulk operation over a filtered queryset, not
    # scoped to any one owned object, so no additional resource/ownership
    # rule applies here.
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    q = (body.get("q") or "").strip()
    cat_id = body.get("category") or None
    ready_raw = body.get("ready2air")
    ready2air = True if ready_raw == "true" else False if ready_raw == "false" else None

    qs = filter_tracks(Track.objects.all(), q=q, category_id=cat_id, ready2air=ready2air)
    result = autofill_related_artists_for_queryset(qs, apply=True)

    return JsonResponse({
        "ok": True,
        "scanned": result["scanned"],
        "changed": result["changed"],
        "appended": result["appended"],
        "unchanged_no_discoveries": result["unchanged_no_discoveries"],
        "unchanged_already_current": result["unchanged_already_current"],
        "unchanged_overflow": result["unchanged_overflow"],
        "overflow_skipped": result["overflow_skipped"],
        "errors": result["errors"],
        "samples": result["samples"],
    })


@ensure_csrf_cookie
def track_detail_page(request, pk):
    from library.models import AnalysisConfig, VoiceTrack

    track = get_object_or_404(
        Track.objects.select_related("artist", "album", "genre", "category")
        .prefetch_related("additional_categories", "holidays"),
        pk=pk,
    )
    categories = Category.objects.order_by("name")
    holidays = Holiday.objects.order_by("month", "day")
    selected_additional_cat_ids = set(track.additional_categories.values_list("id", flat=True))
    selected_holiday_codes = set(track.holidays.values_list("code", flat=True))
    voicetracks = {
        vt.position: vt for vt in VoiceTrack.objects.filter(track=track)
    }
    return render(request, "library/track_detail.html", {
        "track": track,
        "track_source_filename": Path(track.filepath).name if track.filepath else "",
        "categories": categories,
        "holidays": holidays,
        "selected_additional_cat_ids": selected_additional_cat_ids,
        "selected_holiday_codes": selected_holiday_codes,
        "voicetracks": voicetracks,
        "can_edit_voicetracks": _can_edit_voicetracks(request.user),
        "energy_choices": Track.ENERGY_CHOICES,
        "vocal_type_choices": Track.VOCAL_TYPE_CHOICES,
        "end_type_choices": Track.END_TYPE_CHOICES,
        "analysis_config": AnalysisConfig.load(),
    })


def _delete_track_and_file(track):
    """Delete a Track's row + on-disk file + waveform cache. Returns
    (ok, blocked_reason). blocked_reason is populated when the delete
    is refused by a PROTECT FK (Track is still in a Playlist or
    Rotation slot -- user has to remove it from those first). Mirrors
    the standalone `remove_track_file` management command's cleanup
    so the same shape works whether an operator triggers it from the
    UI or an ingest pipeline recalls a delivered file."""
    from django.db.models.deletion import ProtectedError
    from library.models import PlaylistItem, RotationSlot
    filepath = track.filepath
    waveform_path = track.waveform_path
    try:
        track.delete()
    except ProtectedError as exc:
        # Django raises this when a PROTECT FK still points at this
        # Track (Playlist.items.track and RotationSlot.track -- see
        # library/models.py). Turn it into a user-actionable message
        # rather than a 500, naming the specific parent Rotation /
        # Playlist so the operator can jump straight to it instead of
        # hunting through every rotation for the offending slot.
        rotations = set()
        playlists = set()
        other = {}
        for p in exc.protected_objects:
            if isinstance(p, RotationSlot):
                if p.rotation_id:
                    rotations.add(p.rotation.name)
            elif isinstance(p, PlaylistItem):
                if p.playlist_id:
                    playlists.add(p.playlist.name)
            else:
                kind = type(p).__name__
                other[kind] = other.get(kind, 0) + 1

        def _joined(kind_singular, names):
            names = sorted(names)
            label = kind_singular if len(names) == 1 else kind_singular + "s"
            quoted = ", ".join(f"'{n}'" for n in names)
            return f"{label} {quoted}"

        parts = []
        if rotations:
            parts.append(_joined("rotation", rotations))
        if playlists:
            parts.append(_joined("playlist", playlists))
        for kind, n in other.items():
            parts.append(f"{n} {kind}")
        return False, f"still referenced by {', '.join(parts)} -- remove from those first"

    # Only clean up on-disk artifacts after the DB delete succeeded --
    # a failed track.delete() means nothing on disk should change.
    if waveform_path:
        try:
            Path(waveform_path).unlink(missing_ok=True)
        except OSError as e:
            # Best-effort -- the Track row is already gone, so a
            # stranded waveform cache is a cosmetic issue at worst.
            print(f"  [delete_track] could not remove waveform {waveform_path}: {e}")
    if filepath:
        try:
            Path(filepath).unlink(missing_ok=True)
        except OSError as e:
            print(f"  [delete_track] could not remove file {filepath}: {e}")
    return True, None


@require_http_methods(["GET", "PATCH", "DELETE"])
def api_track_detail(request, pk):
    from library.middleware import user_is_contributor, user_is_library_read_only

    track = get_object_or_404(
        Track.objects.select_related("artist", "album", "genre", "category"), pk=pk
    )

    if request.method == "GET":
        return JsonResponse(_track_to_dict(track))

    # Library-read-only roles (Contributor OR remote_dj) are blocked from
    # every write EXCEPT one Contributor-specific carve-out: a Contributor
    # may DELETE their own not-yet-reviewed upload. remote_dj is fully
    # read-only -- they can only ever GET here. Staff/superuser bypass.
    if user_is_library_read_only(request.user):
        if user_is_contributor(request.user):
            is_own_upload = (
                track.uploaded_by_id == request.user.id
                and track.ready2air is False
            )
            if request.method == "DELETE" and not is_own_upload:
                return JsonResponse({"error": "You can only delete your own not-yet-approved uploads."}, status=403)
            if request.method not in ("GET", "DELETE"):
                return JsonResponse({"error": "Read-only for this account."}, status=403)
            # Roadmap 2.5E: the ownership check above is a RESOURCE
            # restriction (which track), not an authority check (whether
            # this account may ever retract an upload). That authority
            # comes from the same library.upload capability that let a
            # Contributor create the upload in the first place -- this
            # vocabulary has no narrower "manage own uploads" slug, and
            # granting library.manage_tracks here would overgrant full
            # track-management authority for an unrelated reason. See
            # docs/AUTHORIZATION.md.
            result = authorize(request.user, "library.upload")
            if not result:
                return forbidden_response(result, user=request.user, capability_slug="library.upload")
        else:
            # remote_dj (or any future non-Contributor read-only role)
            return JsonResponse({"error": "Read-only for this account."}, status=403)
    else:
        # Roadmap 2.5E: previously zero check here -- any account NOT in
        # the literal Contributor/remote_dj groups (e.g. a future Group
        # whose GroupAccess widens to reach /api/tracks/) got unrestricted
        # mutate access. Staff/superuser continue to bypass via
        # authorize() itself.
        result = authorize(request.user, "library.manage_tracks")
        if not result:
            return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    if request.method == "DELETE":
        ok, reason = _delete_track_and_file(track)
        if not ok:
            return JsonResponse({"error": f"Cannot delete: {reason}"}, status=409)
        return JsonResponse({"ok": True})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    DIRECT_FIELDS = {
        "title", "year", "composer", "publisher", "record_label", "comments",
        "rotation_weight", "ready2air", "energy", "vocal_type", "end_type",
        "cue_in_seconds", "cue_out_seconds", "next_start_seconds",
        "intro_until_seconds", "sweep_start_seconds", "outro_starts_seconds",
        "hook_in_seconds", "hook_out_seconds",
        "alt_send_enabled", "alt_send_text",
    }

    # Captured, not applied immediately -- canonicalizing needs the
    # FINAL track.artist (which "artist" in this same request body may
    # still change below), so it's applied once after the loop instead
    # of depending on JSON key order.
    pending_related_artists = None

    for field, value in body.items():
        if field in DIRECT_FIELDS:
            setattr(track, field, value)
        elif field == "related_artists":
            pending_related_artists = value
        elif field == "artist":
            artist_obj, _ = Artist.get_or_create_ci(value)
            track.artist = artist_obj
        elif field == "album":
            if value:
                # Album.unique_together = (title, album_artist), so
                # looking up by title alone crashes with
                # MultipleObjectsReturned when two different artists
                # released same-named albums (Buckingham Nicks vs the
                # ~11 different "Greatest Hits" in this library, etc).
                # Preserve the current track's album_artist so we stay
                # on the same album row rather than getting reassigned
                # to a same-titled album owned by someone else.
                current_aa = track.album.album_artist if track.album else ""
                album_obj, _ = Album.objects.get_or_create(
                    title=value, album_artist=current_aa,
                )
                track.album = album_obj
            else:
                track.album = None
        elif field == "genre":
            if value:
                genre_obj, _ = Genre.objects.get_or_create(name=value)
                track.genre = genre_obj
            else:
                track.genre = None
        elif field == "category_id":
            if value:
                try:
                    track.category = Category.objects.get(id=value)
                except Category.DoesNotExist:
                    return JsonResponse({"error": "Category not found"}, status=404)
            else:
                track.category = None

    if pending_related_artists is not None:
        # Canonicalized through the same formatter every automatic path
        # uses (whitespace/comma-space cleanup, case-insensitive dedupe,
        # drop an entry that exactly duplicates the primary artist) --
        # applied AFTER track.artist is fully resolved above, so a PATCH
        # that changes both artist and related_artists in the same
        # request excludes against the NEW artist, not the stale one.
        canonical_related = format_related_artists(
            canonicalize_related_artists(
                pending_related_artists,
                primary_artist_name=track.artist.name if track.artist_id else None,
            )
        )
        # Track.related_artists is a real DB varchar(500) -- an
        # over-limit manual value would otherwise reach track.save()
        # below and raise an unhandled django.db.utils.DataError (a
        # bare 500), not a clean validation response. Checked BEFORE
        # any of the below (file-relocation side effects, save()) so a
        # too-long value never has a partial effect -- request is
        # rejected outright, nothing is written or moved.
        max_related_len = Track._meta.get_field("related_artists").max_length
        if len(canonical_related) > max_related_len:
            return JsonResponse(
                {
                    "error": (
                        f"Related Artists is too long ({len(canonical_related)} characters "
                        f"after cleanup; the limit is {max_related_len}). Remove an entry "
                        f"and try again."
                    )
                },
                status=400,
            )
        track.related_artists = canonical_related

    # Auto-relocate the file if primary category changed (Part D).
    # Same convention as find_category_drift: files live at
    # LIBRARY_ROOT/<Category.code>/<basename>. Runs BEFORE save() so a
    # filesystem-side failure aborts the DB update too, keeping the
    # two in sync. If the move fails, return an error and DON'T save
    # the field change -- otherwise the DB says one thing and disk
    # says another and every future load hits a MISSING file. Save
    # only succeeds when disk is in the new place.
    from django.conf import settings as django_settings
    from library.middleware import user_is_contributor as _uic
    if track.pk and track.category_id:
        old_track = Track.objects.filter(pk=track.pk).only("filepath", "category_id").first()
        if old_track and old_track.category_id != track.category_id:
            library_root = Path(getattr(django_settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()
            old_path = Path(track.filepath) if track.filepath else None
            if old_path and old_path.is_file():
                try:
                    old_path.resolve().relative_to(library_root)
                    inside_root = True
                except ValueError:
                    inside_root = False
                if inside_root:
                    new_dir = library_root / track.category.code
                    new_path = new_dir / old_path.name
                    if new_path.exists() and new_path != old_path:
                        return JsonResponse(
                            {"error": f"Cannot relocate: destination already exists ({new_path.name} in {track.category.code}). Rename the existing file first."},
                            status=409,
                        )
                    try:
                        new_dir.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_path), str(new_path))
                        track.filepath = str(new_path)
                    except OSError as exc:
                        return JsonResponse(
                            {"error": f"Failed to relocate file: {exc}"},
                            status=500,
                        )

    track.save()

    # M2M fields have to be set AFTER save() because they need the pk.
    # Not part of DIRECT_FIELDS since setattr on an M2M raises. Both
    # accept the same-shaped input the frontend already sends: a list
    # of ids (categories) or codes (holidays). Missing key = no change;
    # explicit empty list = clear the M2M.
    if "additional_category_ids" in body:
        ids = body.get("additional_category_ids") or []
        cats = list(Category.objects.filter(id__in=ids))
        if len(cats) != len(ids):
            return JsonResponse({"error": "One or more additional categories not found"}, status=404)
        # Guard against a track being listed in its own additional_categories
        # -- that would double-count it in _tracks_for_category's OR
        # branch. Silently drop the primary category if it snuck in.
        if track.category_id is not None:
            cats = [c for c in cats if c.id != track.category_id]
        track.additional_categories.set(cats)

    if "holiday_codes" in body:
        codes = body.get("holiday_codes") or []
        holidays = list(Holiday.objects.filter(code__in=codes))
        if len(holidays) != len(codes):
            return JsonResponse({"error": "One or more holidays not found"}, status=404)
        track.holidays.set(holidays)

    track.refresh_from_db()
    return JsonResponse(_track_to_dict(track))


def _track_to_dict(track):
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist.name if track.artist else "",
        "album": track.album.title if track.album else "",
        "genre": track.genre.name if track.genre else "",
        "year": track.year,
        "category_id": track.category_id,
        "category_code": track.category.code if track.category else "",
        "category_name": track.category.name if track.category else "",
        "duration_seconds": track.duration_seconds,
        "format": track.format,
        "filepath": track.filepath,
        "sample_rate": track.sample_rate,
        "channels": track.channels,
        "bit_depth": track.bit_depth,
        "cue_in_seconds": track.cue_in_seconds,
        "cue_out_seconds": track.cue_out_seconds,
        "next_start_seconds": track.next_start_seconds,
        "intro_until_seconds": track.intro_until_seconds,
        "sweep_start_seconds": track.sweep_start_seconds,
        "outro_starts_seconds": track.outro_starts_seconds,
        "hook_in_seconds": track.hook_in_seconds,
        "hook_out_seconds": track.hook_out_seconds,
        "rotation_weight": track.rotation_weight,
        "ready2air": track.ready2air,
        "energy": track.energy,
        "vocal_type": track.vocal_type,
        "end_type": track.end_type,
        "play_count": track.play_count,
        "last_played_at": track.last_played_at.isoformat() if track.last_played_at else None,
        "related_artists": track.related_artists,
        "composer": track.composer,
        "publisher": track.publisher,
        "record_label": track.record_label,
        "comments": track.comments,
        "alt_send_enabled": track.alt_send_enabled,
        "alt_send_text": track.alt_send_text,
        "created_at": track.created_at.isoformat(),
        "updated_at": track.updated_at.isoformat(),
        "additional_category_ids": list(track.additional_categories.values_list("id", flat=True)),
        "holiday_codes": list(track.holidays.values_list("code", flat=True)),
    }


@require_http_methods(["POST"])
def api_track_reanalyze(request, pk):
    """Force a fresh waveform + cue-point (re)analysis of exactly this
    track -- same analyze_one_track() call api_library_upload uses right
    after a new upload, just targeted at an existing row instead. Only
    ever touches next_start_seconds/cue_in_seconds/waveform_path/
    related_artists/duration_seconds -- the manually-set marks (intro,
    sweep, outro, hooks) are never written by analyze_one_track, so this
    can't clobber a human's own cue-point edits.

    Roadmap 2.5A: requires library.manage_tracks (see api_track_bulk's
    comment for the reachability gap this closes)."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    from library.management.commands.analyze_tracks import (
        analyze_one_track, apply_category_thresholds, get_waveforms_dir,
    )
    from library.models import AnalysisConfig

    track = get_object_or_404(Track.objects.select_related("artist", "category"), pk=pk)

    cfg = AnalysisConfig.load()
    cfg_values = (
        cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
        cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
    )
    # Fold in the track's category-level threshold overrides if it has
    # a category and that category has non-null overrides configured;
    # otherwise the base cfg passes through untouched.
    if track.category_id:
        cfg_values = apply_category_thresholds(
            cfg_values,
            track.category.next_start_threshold_db_override,
            track.category.cue_in_threshold_db_override,
        )
    wave_dir = get_waveforms_dir()
    row = (track.id, track.filepath, track.filename, track.duration_seconds,
           track.title, track.artist.name if track.artist_id else "", track.related_artists)

    analyzed = analyze_one_track(row, cfg_values, wave_dir, force=True)
    if not analyzed:
        return JsonResponse({"error": "Analysis failed -- check the file is readable by ffmpeg"}, status=400)

    track.refresh_from_db()
    return JsonResponse(_track_to_dict(track))


@require_http_methods(["POST"])
def api_track_read_metadata(request, pk):
    """Read tags currently embedded in the file on disk -- NOT the DB --
    and return them for the form to display for review. Reuses
    import_songs.py's parse_tags(), the same multi-format (ID3/Vorbis/
    MP4) frame-name fallback logic already proven there. Deliberately
    does not touch the Track row itself -- the user reviews/edits in the
    form first, then Save Changes or Write Track Metadata commits it."""
    from library.management.commands.import_songs import parse_tags

    track = get_object_or_404(Track, pk=pk)
    fp = Path(track.filepath)
    if not fp.is_file():
        return JsonResponse({"error": "File not found on disk"}, status=400)

    tags, _info = parse_tags(fp)
    return JsonResponse({
        "title": tags.get("title"),
        "artist": tags.get("artist"),
        "album": tags.get("album"),
        "genre": tags.get("genre"),
        "year": tags.get("year"),
    })


# DB field -> mutagen "easy" tag key. Not every field has a reliable
# cross-format equivalent: EasyMP4 (m4a) has no composer/organization
# key at all, "comment" is only a valid easy key on FLAC, and
# record_label has no key of its own anywhere (ID3's TPUB is already
# "organization"/publisher -- reusing it for both would silently
# conflate two different DB fields). Those are simply never attempted;
# api_track_write_metadata reports exactly which fields made it into
# the file vs. were skipped, rather than guessing.
_METADATA_TAG_MAP = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "genre": "genre",
    "composer": "composer",
    "publisher": "organization",
    "comments": "comment",
}


def _write_file_tags(filepath, values):
    import mutagen

    audio = mutagen.File(str(filepath), easy=True)
    if audio is None:
        raise ValueError("Unrecognized or unreadable audio file")

    written, skipped = [], []
    for field, key in _METADATA_TAG_MAP.items():
        value = values.get(field)
        if not value:
            continue
        try:
            audio[key] = str(value)
            written.append(field)
        except Exception:
            skipped.append(field)

    year = values.get("year")
    if year:
        try:
            audio["date"] = str(year)
            written.append("year")
        except Exception:
            skipped.append("year")

    audio.save()
    return written, skipped


@require_http_methods(["POST"])
def api_track_write_metadata(request, pk):
    """Write the posted field values into the file's embedded tags AND
    save them to the Track row in the same action, so the file and DB
    can't drift apart from each other the way a file-only or DB-only
    save would risk. record_label is always DB-only (see
    _METADATA_TAG_MAP) -- still saved to the Track row, just never
    written into the file.

    Roadmap 2.5A: requires library.manage_tracks (see api_track_bulk's
    comment for the reachability gap this closes)."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    track = get_object_or_404(Track.objects.select_related("artist", "album", "genre"), pk=pk)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    fp = Path(track.filepath)
    if not fp.is_file():
        return JsonResponse({"error": "File not found on disk"}, status=400)

    try:
        written, skipped = _write_file_tags(fp, body)
    except Exception as exc:
        return JsonResponse({"error": f"Failed to write tags: {exc}"}, status=400)

    if body.get("title"):
        track.title = body["title"]
    if body.get("artist"):
        artist_obj, _ = Artist.get_or_create_ci(body["artist"])
        track.artist = artist_obj
    if "album" in body:
        if body["album"]:
            # See api_track_detail comment on album lookup: scope by
            # (title, album_artist) to respect the model's
            # unique_together, preserving the current track's
            # album_artist so we don't jump to a different owner's
            # same-titled album.
            current_aa = track.album.album_artist if track.album else ""
            album_obj, _ = Album.objects.get_or_create(
                title=body["album"], album_artist=current_aa,
            )
            track.album = album_obj
        else:
            track.album = None
    if "genre" in body:
        if body["genre"]:
            genre_obj, _ = Genre.objects.get_or_create(name=body["genre"])
            track.genre = genre_obj
        else:
            track.genre = None
    for field in ("year", "composer", "publisher", "record_label", "comments"):
        if field in body:
            setattr(track, field, body[field])

    track.save()
    track.refresh_from_db()

    result = _track_to_dict(track)
    result["written"] = written
    result["skipped"] = skipped
    return JsonResponse(result)


# ---------------------------------------------------------------
# Log builder
# ---------------------------------------------------------------

@ensure_csrf_cookie
def logs_page(request):
    return render(request, "library/logs.html")


def _log_to_dict(log):
    items = (
        log.items
        .select_related("track", "track__artist", "category")
        .order_by("position")
    )
    return {
        "id": log.id,
        "date": log.date.isoformat(),
        "hour": log.hour,
        "status": log.status,
        "generated_at": log.generated_at.isoformat(),
        "items": [
            {
                "id": item.id,
                "position": item.position,
                "scheduled_time": item.scheduled_time.isoformat(),
                "track_id": item.track_id,
                "title": item.track.title if item.track else item.track_title,
                "artist": (item.track.artist.name if item.track.artist else "") if item.track else item.track_artist,
                "category": item.category.code if item.category else "",
                "duration": (item.track.next_start_seconds or item.track.duration_seconds or 0) if item.track else 0,
            }
            for item in items
        ],
    }


def _is_active_or_imminent_hour(target_date, hour):
    """Narrow guardrail (1.1 spec -- the relevant slice of roadmap item
    1.7, not the full feature): true if (target_date, hour) is the hour
    currently on air, or the hour the engine has already advanced its
    active queue to ahead of the real top-of-hour (early rollover --
    see engine.py's _advance_to_next_hour_log). No override workflow,
    no broader "imminent" lookahead window beyond what the engine
    itself has already committed to -- just enough to stop a
    destructive delete-and-rebuild (build_hour_log_for_admin always
    rebuilds, discarding whatever's there) from landing under a log
    that's actually on air right now.

    Falls back to comparing against wall-clock "now" when engine_state.
    json is missing/stale (engine down or hasn't ticked recently) --
    still refuses to rebuild the CURRENT wall-clock hour even with no
    live engine state to consult, since that hour is the one that
    SHOULD be on air regardless of whether the engine is currently
    reporting in."""
    now = timezone.localtime()
    if (target_date, hour) == (now.date(), now.hour):
        return True
    state = _read_engine_state()
    if state and state.get("date") and state.get("hour") is not None:
        try:
            active_date = date_type.fromisoformat(state["date"])
        except (ValueError, TypeError):
            return False
        return (target_date, hour) == (active_date, state["hour"])
    return False


@require_http_methods(["POST"])
def api_log_build(request):
    result = authorize(request.user, "schedule.edit")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="schedule.edit")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    date_str = body.get("date")
    hour = body.get("hour")

    if not date_str or hour is None:
        return JsonResponse({"error": "date and hour are required"}, status=400)

    try:
        target_date = date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, status=400)

    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour must be 0-23"}, status=400)

    if _is_active_or_imminent_hour(target_date, hour):
        return JsonResponse(
            {"error": "This hour is currently active/on-air -- rebuilding it would destroy the live log. "
                      "Wait until it's no longer the current hour before rebuilding."},
            status=409,
        )

    log, error = build_hour_log_for_admin(target_date, hour)
    if error == LOCK_CONTENDED:
        return JsonResponse({"error": "This hour is currently being built by the engine -- try again shortly"}, status=409)
    if error:
        return JsonResponse({"error": error}, status=400)

    return JsonResponse(_log_to_dict(log))


@require_http_methods(["POST"])
def api_log_preview(request):
    """Dry-run build for rotation/playlist health-checking -- calls
    preview_hour_log, which never touches PlaylistLog/LogItem, so this is
    safe to call against any date/hour (including one that's already
    live/approved/on-air) without side effects."""
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    date_str = body.get("date")
    hour = body.get("hour")

    if not date_str or hour is None:
        return JsonResponse({"error": "date and hour are required"}, status=400)

    try:
        target_date = date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, status=400)

    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour must be 0-23"}, status=400)

    result, error = preview_hour_log(target_date, hour)
    if error:
        return JsonResponse({"error": error}, status=400)

    return JsonResponse(result)


@require_http_methods(["GET"])
def api_log_get(request, date_str, hour):
    try:
        target_date = date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid date"}, status=400)

    log = PlaylistLog.objects.filter(date=target_date, hour=hour).first()
    if not log:
        return JsonResponse({"error": "No log for this hour"}, status=404)

    return JsonResponse(_log_to_dict(log))


@require_http_methods(["GET"])
def api_log_list_date(request, date_str):
    try:
        target_date = date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid date"}, status=400)

    logs = PlaylistLog.objects.filter(date=target_date).order_by("hour")
    return JsonResponse({
        "date": target_date.isoformat(),
        "logs": [
            {
                "id": log.id,
                "hour": log.hour,
                "status": log.status,
                "item_count": log.items.count(),
            }
            for log in logs
        ],
    })


@require_http_methods(["PATCH"])
def api_log_update(request, pk):
    result = authorize(request.user, "schedule.edit")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="schedule.edit")

    log = get_object_or_404(PlaylistLog, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    status = body.get("status")
    if status and status in ("draft", "approved"):
        log.status = status
        log.save(update_fields=["status"])

    return JsonResponse(_log_to_dict(log))


@require_http_methods(["DELETE"])
def api_log_delete(request, pk):
    result = authorize(request.user, "schedule.edit")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="schedule.edit")

    deleted, _ = PlaylistLog.objects.filter(pk=pk, status="draft").delete()
    return JsonResponse({"ok": True, "deleted": deleted > 0})


@require_http_methods(["PATCH"])
def api_log_item_swap(request, item_id):
    result = authorize(request.user, "schedule.edit")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="schedule.edit")

    item = get_object_or_404(LogItem.objects.select_related("playlist_log"), pk=item_id)
    if item.playlist_log.status != "draft":
        return JsonResponse({"error": "Cannot modify an approved log"}, status=400)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    track_id = body.get("track_id")
    if not track_id:
        return JsonResponse({"error": "track_id is required"}, status=400)

    try:
        track = Track.objects.get(id=track_id)
    except Track.DoesNotExist:
        return JsonResponse({"error": "Track not found"}, status=404)

    item.track = track
    item.track_title = track.title
    item.track_artist = track.artist.name if track.artist_id else ""
    item.save(update_fields=["track", "track_title", "track_artist"])

    return JsonResponse(_log_to_dict(item.playlist_log))


@require_http_methods(["POST"])
def api_log_reorder(request, pk):
    result = authorize(request.user, "schedule.edit")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="schedule.edit")

    log = get_object_or_404(PlaylistLog, pk=pk)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    order = body.get("order")
    if not order or not isinstance(order, list):
        return JsonResponse({"error": "order (list of item IDs) is required"}, status=400)

    all_items = list(log.items.order_by("position"))
    items_by_id = {item.id: item for item in all_items}
    reordered = [items_by_id[i] for i in order if i in items_by_id]
    if not reordered:
        return JsonResponse({"error": "No matching items found"}, status=400)

    # `order` may only cover part of the log (e.g. the dashboard only
    # sends the currently-visible "coming up" slice, not the whole
    # hour). Splice the reordered slice back into the same span of
    # positions it came from — items before that span (already played
    # or claimed by a deck) and after it (further out in the queue than
    # what's currently rendered) both keep their place. Anything inside
    # the span that wasn't in `order` (shouldn't normally happen) rides
    # along right after the reordered items rather than being dropped.
    reordered_ids = {item.id for item in reordered}
    positions = [item.position for item in all_items if item.id in reordered_ids]
    start, end = min(positions), max(positions)

    before = [item for item in all_items if item.position < start]
    after = [item for item in all_items if item.position > end]
    stragglers = [
        item for item in all_items
        if start <= item.position <= end and item.id not in reordered_ids
    ]

    _reposition_items(before + reordered + stragglers + after)

    return JsonResponse(_log_to_dict(log))


def _read_engine_queue_state():
    """Read the engine state to find the current log's items in order,
    and the queue cursor (position of the first not-yet-claimed item —
    i.e. one past whatever the two decks currently hold)."""
    state_path = Path("/run/isadoraair/engine_state.json")
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("log_id"):
            log = PlaylistLog.objects.get(id=data["log_id"])
            items = list(log.items.order_by("position"))
            cursor = data.get("queue_cursor", len(items))
            return items, cursor
    except Exception:
        pass
    return [], 0


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_set_next(request):
    """Roadmap 2.5C: requires playout.queue_manage (schedule-restricted
    for non-staff/superuser). Force a queued item to play next -- the
    remote_dj-mode "force-next" button (dashboard.html's setNext())
    posts here; remote_dj_page's own docstring documents this as
    intended Remote Host behavior, distinct from playout.control (seek/
    deck transport), which stays operator-only. See authz.migrations.
    0006_correct_remote_host_playout_capability."""
    result = authorize(request.user, "playout.queue_manage")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.queue_manage")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    item_id = body.get("item_id")
    if not item_id:
        return JsonResponse({"error": "item_id required"}, status=400)

    all_items, cursor = _read_engine_queue_state()
    if not all_items:
        return JsonResponse({"error": "No active log"}, status=400)

    src_idx = None
    for i, li in enumerate(all_items):
        if li.id == item_id:
            src_idx = i
            break

    if src_idx is None:
        return JsonResponse({"error": "Item not found"}, status=404)

    target_idx = cursor
    if src_idx <= target_idx:
        return JsonResponse({"ok": True})

    moved = all_items.pop(src_idx)
    all_items.insert(target_idx, moved)

    _reposition_items(all_items)

    return JsonResponse({"ok": True})


def _reposition_items(items, model=LogItem):
    """Two-pass position update to avoid unique constraint violations
    when reordering items (LogItem or PlaylistItem) that have a
    unique_together on (parent, position)."""
    from django.db import transaction
    OFFSET = 100000
    with transaction.atomic():
        for i, li in enumerate(items):
            li.position = i + OFFSET
        model.objects.bulk_update(items, ["position"])
        for i, li in enumerate(items):
            li.position = i
        model.objects.bulk_update(items, ["position"])


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_insert_track(request):
    """Roadmap 2.5C: requires playout.queue_manage (see api_engine_set_next's
    comment)."""
    result = authorize(request.user, "playout.queue_manage")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.queue_manage")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    track_id = body.get("track_id")
    if not track_id:
        return JsonResponse({"error": "track_id required"}, status=400)

    position = body.get("position", "next")
    if position not in ("next", "end"):
        return JsonResponse({"error": "position must be 'next' or 'end'"}, status=400)

    all_items, cursor = _read_engine_queue_state()
    if not all_items:
        return JsonResponse({"error": "No active log"}, status=400)

    track = get_object_or_404(Track, pk=track_id)
    log = all_items[0].playlist_log

    new_item = LogItem.objects.create(
        playlist_log=log,
        position=9999,
        scheduled_time=timezone.now(),
        track=track,
        track_title=track.title,
        track_artist=track.artist.name if track.artist_id else "",
        category=track.category,
    )

    insert_idx = cursor if position == "next" else len(all_items)
    all_items.insert(insert_idx, new_item)
    _reposition_items(all_items)

    return JsonResponse({"ok": True, "item_id": new_item.id})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_seek(request):
    """Roadmap 2.5C: requires playout.control -- operator/Station-
    Administrator only. remote_dj_page's own docstring documents
    "waveform click-seek" as hidden in remote_dj mode; this is not
    granted to the Remote Host Role."""
    result = authorize(request.user, "playout.control")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.control")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    position = body.get("position")
    if position is None:
        return JsonResponse({"error": "position required"}, status=400)

    cmd = {"command": "seek", "position": float(position)}
    slot = body.get("slot")
    if slot:
        cmd["slot"] = slot.upper()

    error_response = _enqueue_engine_command_response(cmd)
    if error_response is not None:
        return error_response
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_deck_command(request, slot):
    """Roadmap 2.5C: requires playout.control -- operator/Station-
    Administrator only. remote_dj_page's own docstring documents "deck
    eject/pause" as hidden in remote_dj mode; this is not granted to the
    Remote Host Role."""
    result = authorize(request.user, "playout.control")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.control")

    slot = slot.upper()
    if slot not in ("A", "B"):
        return JsonResponse({"error": "slot must be A or B"}, status=400)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    action = body.get("action")
    if action not in ("pause", "resume", "eject"):
        return JsonResponse({"error": "action must be pause, resume, or eject"}, status=400)

    error_response = _enqueue_engine_command_response(
        {"command": f"deck_{action}", "slot": slot}
    )
    if error_response is not None:
        return error_response
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_mic_ptt(request):
    """Roadmap 2.5C: requires studio.mic_ptt -- operator/Station-
    Administrator only. remote_dj_page's own docstring documents
    "Studio Mic PTT" as hidden in remote_dj mode (dashboard.html's
    micPttBtn is wrapped in {% if mode != 'remote_dj' %}); not granted
    to the Remote Host Role."""
    result = authorize(request.user, "studio.mic_ptt")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="studio.mic_ptt")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    active = body.get("active")
    if not isinstance(active, bool):
        return JsonResponse({"error": "active must be a boolean"}, status=400)

    error_response = _enqueue_engine_command_response(
        {"command": "mic_ptt", "active": active}
    )
    if error_response is not None:
        return error_response
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_remote_dj_gate(request):
    """Gate toggle for the currently-connected remote DJ's mic. Usable
    by either the operator (console) OR the connected remote DJ
    themselves (dashboard.html's remoteDjGateBtn is NOT hidden in
    remote_dj mode -- both parties may see and use this control); the
    engine ignores it if no remote-DJ session is active.

    Roadmap 2.5C: requires remote_dj.mic_gate (schedule-restricted for
    non-staff/superuser)."""
    result = authorize(request.user, "remote_dj.mic_gate")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="remote_dj.mic_gate")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    active = body.get("active")
    if not isinstance(active, bool):
        return JsonResponse({"error": "active must be a boolean"}, status=400)

    error_response = _enqueue_engine_command_response(
        {"command": "remote_dj_gate", "active": active}
    )
    if error_response is not None:
        return error_response
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_manual_mode(request):
    """Roadmap 2.5C: requires playout.manual_mode (schedule-restricted
    for non-staff/superuser). dashboard.html's manualModeBtn is shown in
    both console modes -- Remote Host holds this capability."""
    result = authorize(request.user, "playout.manual_mode")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="playout.manual_mode")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    active = body.get("active")
    if not isinstance(active, bool):
        return JsonResponse({"error": "active must be a boolean"}, status=400)

    error_response = _enqueue_engine_command_response(
        {"command": "set_manual_mode", "active": active}
    )
    if error_response is not None:
        return error_response
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def api_engine_status(request):
    state_path = Path("/run/isadoraair/engine_state.json")
    if not state_path.is_file():
        return JsonResponse({"transport": "OFFLINE", "decks": {"A": None, "B": None}, "queue": [], "fx_fires": []})
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if time_mod.time() - data.get("timestamp", 0) > 10:
            data["transport"] = "STALE"
        return JsonResponse(data)
    except Exception:
        return JsonResponse({"transport": "ERROR", "decks": {"A": None, "B": None}, "queue": [], "fx_fires": []})


@require_http_methods(["GET"])
def api_engine_levels(request):
    """Serves the pre-processor VU meter payload written by the engine's
    output_level bus handler (see engine.py::LEVELS_PATH). Polled from
    the dashboard at ~100ms cadence; the engine emits every 50ms, so
    the client will typically see fresh values on every poll. Returns
    an empty {} if the file doesn't exist yet or is currently mid-write
    (JSON parse error caught -- next poll retries)."""
    levels_path = Path("/run/isadoraair/levels.json")
    try:
        return JsonResponse(json.loads(levels_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return JsonResponse({})


@ensure_csrf_cookie
def remote_dj_page(request):
    """Remote-DJ-facing console: renders dashboard.html in `remote_dj`
    mode, which hides the operator-only controls (Studio Mic PTT,
    per-track edit links, deck eject/pause, waveform click-seek) and
    slims Deck B on mobile portrait, but keeps search-to-add, Play
    Now, drag-to-reorder queue, and force-next buttons available
    (same as the full console) -- a remote DJ is trusted to queue,
    reorder, and jump ahead in their own show.

    Roadmap 2.5C: gated on the remote_dj.connect CAPABILITY (was: the
    literal `remote_dj` group). Deliberately schedule_policy="ignore" --
    this is page reachability, not the privileged operation itself (see
    authz.evaluator's "safe activation" docstring section): a Remote
    Host should be able to see their own console/status at any time,
    including outside their scheduled window, so they can watch their
    upcoming show approach. The actual privileged action -- minting a
    connect token -- is gated with the real schedule policy in
    api_remote_dj_token, which is where an outside-window Remote Host
    actually gets turned away with a specific reason."""
    from authz.evaluator import SCHEDULE_POLICY_IGNORE
    from library.models import AnalysisConfig, FXCart, Playlist
    from hardware.models import AudioPipeline
    result = authorize(request.user, "remote_dj.connect", schedule_policy=SCHEDULE_POLICY_IGNORE)
    if not result:
        return render(request, "library/remote_dj_unauthorized.html")
    return render(request, "library/dashboard.html", {
        "playlists": Playlist.objects.all().order_by("name"),
        "analysis_config": AnalysisConfig.load(),
        "vu_min_db": AudioPipeline.load().vu_meter_min_db,
        "mode": "remote_dj",
        "fx_carts": FXCart.objects.filter(enabled=True).order_by("sort_order", "name"),
    })


FX_CART_UPLOAD_DIR = Path("/srv/isadoraair/carts")


def _can_edit_voicetracks(user):
    """Roadmap 2.5C: requires voicetrack.record (staff/superuser bypass
    unconditionally, per authorize()'s own compatibility rule; Contributor
    is library-management, not on-air, so doesn't hold it). Not schedule-
    restricted -- a host may prepare voice tracks any time, not only
    during their show (was: staff/superuser or literal `remote_dj` group
    membership; unchanged effective behavior, now capability-based)."""
    return bool(authorize(user, "voicetrack.record"))


@ensure_csrf_cookie
def voicetracks_page(request):
    """All-voicetracks index. Same access as VT recording (staff or
    remote_dj); Contributor gets redirected by the group middleware
    before reaching here since /voicetracks/ isn't in their access
    prefixes."""
    from library.models import VoiceTrack
    if not _can_edit_voicetracks(request.user):
        return HttpResponseForbidden("Voice-track management requires staff or remote_dj.")

    qs = (
        VoiceTrack.objects
        .select_related("track", "track__artist", "recorded_by")
        .order_by("-recorded_at")
    )

    q = (request.GET.get("q") or "").strip()
    if q:
        qs = qs.filter(
            Q(track__title__icontains=q) |
            Q(track__artist__name__icontains=q)
        )
    position = request.GET.get("position", "")
    if position in ("intro", "outro"):
        qs = qs.filter(position=position)

    paginator = Paginator(qs, 100)
    page = paginator.get_page(request.GET.get("page"))
    return render(request, "library/voicetracks.html", {
        "voicetracks": page,
        "q": q,
        "position_filter": position,
        "total_count": paginator.count,
    })


@require_http_methods(["GET"])
def api_voicetrack_audio(request, pk):
    """Read-only preview of a VoiceTrack's CURRENT audio (what airs). Streams
    through Django so the access check is enforced. 2.22B: a VoiceTrack bound
    to an iPortal take streams that immutable ProductionMedia through the
    Phase-A safe open; a legacy VoiceTrack streams its legacy file as before.
    Recording, editing, saving and removal live in the shared iPortal
    recorder (/voicetracks/studio/) -- the destructive pre-Phase-B upload /
    save-edited / delete endpoints are retired."""
    from django.http import FileResponse
    from library.models import VoiceTrack
    from production.recorder.views import _stream_media
    from production.services import media_io

    if not _can_edit_voicetracks(request.user):
        return HttpResponseForbidden("Not authorized.")

    vt = get_object_or_404(VoiceTrack.objects.select_related("media"), pk=pk)
    audio = vt.playable_audio()
    if audio is None:
        return HttpResponseNotFound("Voice-track audio missing.")
    if audio.origin == "production_media":
        try:
            return _stream_media(request, media_io.open_media(vt.media, require_valid=True))
        except Exception:  # noqa: BLE001 -- purged/inconsistent: nothing to preview
            return HttpResponseNotFound("Voice-track audio missing.")
    return FileResponse(open(audio.path, "rb"), content_type="audio/wav")


@csrf_exempt
@require_http_methods(["POST"])
def api_fx_cart_upload(request):
    """Accept a single audio file, save it under FX_CART_UPLOAD_DIR
    with a filesystem-safe name, and return the absolute path so the
    admin form's drag-drop widget can drop it into FXCart.filepath.

    Staff/superuser only -- placing files anywhere on the filesystem
    is not something Contributor or remote_dj should be able to do.
    Multiple uploads with the same name auto-suffix (' (1)', ' (2)',
    ...) same as the /library/import/ pattern -- friendlier than
    overwriting a cart that's already in rotation.

    Format check is extension-only (no magic-byte sniff) since the
    server has to trust its own admin-authorized uploaders anyway,
    and mutagen at FXCart.save() time is the real validity gate for
    'will this play' (bad files fail duration_seconds population and
    show a ⚠ badge in the list view).
    """
    from library.management.commands.import_songs import SUPPORTED_EXT
    if not (request.user.is_authenticated and (request.user.is_staff or request.user.is_superuser)):
        return HttpResponseForbidden("Admin only.")
    f = request.FILES.get("file")
    if f is None:
        return JsonResponse({"error": "No file uploaded"}, status=400)

    from django.utils.text import get_valid_filename
    original = get_valid_filename(f.name) or "cart.audio"
    ext = Path(original).suffix.lower()
    if ext not in SUPPORTED_EXT:
        return JsonResponse({
            "error": f"Unsupported extension {ext!r}. Allowed: {sorted(SUPPORTED_EXT)}",
        }, status=400)

    FX_CART_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = FX_CART_UPLOAD_DIR / original
    if dest.exists():
        stem = Path(original).stem
        n = 1
        while True:
            dest = FX_CART_UPLOAD_DIR / f"{stem} ({n}){ext}"
            if not dest.exists():
                break
            n += 1

    with open(dest, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)

    return JsonResponse({
        "ok": True,
        "filepath": str(dest),
        "filename": dest.name,
        "size_bytes": dest.stat().st_size,
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_fx_fire(request):
    """Fires an FXCart into the on-air mix.

    Roadmap 2.5C: requires fx.fire (schedule-restricted for non-staff/
    superuser). Was: reachable by anyone whose group granted /api/fx/
    with no other check (staff/superuser via bypass, remote_dj via
    GroupAccess; Contributor never reached it because it lacked the
    dashboard/remote-dj prefixes already).

    Retrigger / polyphony / access are enforced engine-side (single source
    of truth). This view just relays cart_id via the engine command file --
    an engine restart would drop any in-flight fires the same way it drops
    everything else, which is acceptable for one-shot audio."""
    result = authorize(request.user, "fx.fire")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="fx.fire")

    from library.models import FXCart
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    cart_id = body.get("cart_id")
    if not cart_id:
        return JsonResponse({"error": "cart_id required"}, status=400)
    cart = FXCart.objects.filter(id=cart_id, enabled=True).only(
        "id", "duration_seconds", "retrigger_mode"
    ).first()
    if cart is None:
        return JsonResponse({"error": "cart not found or disabled"}, status=404)

    error_response = _enqueue_engine_command_response(
        {"command": "fx_fire", "cart_id": int(cart_id)}
    )
    if error_response is not None:
        return error_response
    return JsonResponse({
        "ok": True,
        "cart_id": cart_id,
        "duration_seconds": cart.duration_seconds,
        "retrigger_mode": cart.retrigger_mode,
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_remote_dj_token(request):
    """Mints a short-lived signaling token for the Remote DJ over WebRTC
    feature. The token only needs to survive the signaling websocket's
    handshake (validated with the same max_age on the other end) -- the
    open socket itself is the session after that, not the token.

    Roadmap 2.5C: requires remote_dj.connect (was: the literal `remote_dj`
    group). Schedule-restricted -- when ScheduleAccessConfig.
    scheduled_enforcement_enabled is ON, this ALSO requires the caller's
    current station-local time to fall inside an active TalentAssignment's
    effective window; when OFF, capability possession alone is sufficient
    (compatibility policy for stations that haven't configured
    TalentAssignments yet -- see docs/AUTHORIZATION.md's "Safe
    activation" section). The denial reason (result.reason) is
    deliberately generic/operator-facing text, not raw internal state --
    see forbidden_response and AuthzResult.reason's own construction;
    nothing here exposes signing keys, other users' schedules, or
    anything beyond what this account itself needs to understand why it
    was refused."""
    result = authorize(request.user, "remote_dj.connect")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="remote_dj.connect")

    from library.models import RemoteDJConfig

    try:
        ice_servers = browser_ice_servers(RemoteDJConfig.load().stun_server)
    except ValueError as exc:
        print(f"  Remote DJ token refused: invalid configured STUN server ({exc})")
        return JsonResponse(
            {"error": "Remote DJ STUN configuration is invalid"}, status=503
        )

    token, payload = mint_remote_dj_token(request.user.id)
    attempt_id = payload["attempt_id"]
    print(f"  Remote DJ token issued: attempt={attempt_id} user_id={request.user.id}")
    return JsonResponse({
        "token": token,
        "attempt_id": attempt_id,
        "ice_servers": ice_servers,
    })


@require_http_methods(["GET"])
def api_waveform(request, track_id):
    from django.conf import settings as django_settings
    wave_dir = Path(getattr(django_settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"))
    wave_file = wave_dir / f"{track_id}.json"

    if not wave_file.is_file():
        return JsonResponse({"error": "Waveform not found"}, status=404)

    try:
        data = json.loads(wave_file.read_text(encoding="utf-8"))
    except Exception:
        return JsonResponse({"error": "Failed to read waveform"}, status=500)

    return JsonResponse(data)


@require_http_methods(["GET"])
def api_album_art(request, track_id):
    from library.services.album_art import resolve_album_art

    track = get_object_or_404(
        Track.objects.select_related("artist", "album", "category", "category__kind"), pk=track_id
    )
    result = resolve_album_art(track)
    return JsonResponse(result)


@ensure_csrf_cookie
def library_import_page(request):
    from library.models import UploadConfig

    categories = Category.objects.select_related("kind").order_by("kind__sort_order", "name")
    upload_cfg = UploadConfig.load()
    return render(request, "library/import.html", {
        "categories": categories,
        "max_batch_size_mb": upload_cfg.max_batch_size_mb,
    })


def _unique_destination(dest_dir, filename):
    """If dest_dir/filename already exists, auto-suffix (song.mp3 ->
    song (1).mp3, song (2).mp3, ...) rather than overwriting real content
    or rejecting the upload outright -- friendliest default for a live
    broadcast library."""
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        candidate = dest_dir / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@require_http_methods(["POST"])
def api_library_upload(request):
    from django.conf import settings as django_settings
    from django.utils.text import get_valid_filename

    from library.management.commands.import_songs import SUPPORTED_EXT, parse_tags
    from library.middleware import user_is_contributor
    from library.models import UploadConfig

    # Roadmap 2.5E: this endpoint previously had no capability check at
    # all -- Contributor's category-auto-pin rule below is a RESOURCE
    # restriction (which category), not an authority check (whether the
    # caller may upload in the first place). Contributor and Station
    # Administrator already hold library.upload (authz.0002/0007), so
    # this changes nothing for either; it closes the gap for any future
    # Group whose GroupAccess happens to reach this URL without also
    # being deliberately granted upload authority.
    result = authorize(request.user, "library.upload")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.upload")

    is_contributor = user_is_contributor(request.user)

    if is_contributor:
        # Contributors' category is always their username-matched
        # category, regardless of what the client posts. This is BOTH
        # a UX shortcut (the /library/import/ page auto-fills the
        # dropdown for them, so no user action needed) AND a security
        # enforcement (a Contributor can't drop tracks into someone
        # else's category by tampering with the form). Case-insensitive
        # match on Category.code, per the design decision -- the
        # Category must exist ahead of time (the operator creates it
        # when adding the Contributor to the group; there's no auto-
        # creation), and its code must equal the Contributor's
        # lowercased username.
        target_username = (request.user.username or "").lower()
        category = Category.objects.filter(code__iexact=target_username).first()
        if category is None:
            return JsonResponse({
                "error": f"No category is configured for your account. "
                         f"Ask the station operator to create a Category "
                         f"with code '{target_username}'.",
            }, status=403)
    else:
        category_id = request.POST.get("category_id")
        if not category_id:
            return JsonResponse({"error": "category_id required"}, status=400)
        category = get_object_or_404(Category, pk=category_id)

    uploaded_files = request.FILES.getlist("files")
    if not uploaded_files:
        return JsonResponse({"error": "No files uploaded"}, status=400)

    # Checked before any file is written to disk -- all files in one
    # drag-and-drop/browse action count as a single batch, not per-track
    # (nginx's own client_max_body_size ceiling works the same way; this
    # is a separate, admin-configurable limit underneath it).
    upload_cfg = UploadConfig.load()
    max_batch_bytes = upload_cfg.max_batch_size_mb * 1024 * 1024
    total_bytes = sum(f.size for f in uploaded_files)
    if total_bytes > max_batch_bytes:
        return JsonResponse({
            "error": f"Batch too large: {total_bytes / (1024 * 1024):.1f}MB "
                     f"exceeds the {upload_cfg.max_batch_size_mb}MB limit "
                     f"(Config > Upload Configuration in admin).",
        }, status=413)

    library_root = Path(getattr(django_settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))
    dest_dir = library_root / category.code
    dest_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for uploaded in uploaded_files:
        # get_valid_filename strips path separators and anything else
        # unsafe for a filesystem name -- the client-supplied name is
        # untrusted input, this is the only thing standing between it and
        # a path-traversal attempt.
        safe_name = get_valid_filename(uploaded.name)
        ext = Path(safe_name).suffix.lower()
        if ext not in SUPPORTED_EXT:
            results.append({"filename": uploaded.name, "ok": False, "error": f"Unsupported file type: {ext or '(none)'}"})
            continue

        dest_path = _unique_destination(dest_dir, safe_name)
        try:
            with open(dest_path, "wb") as f:
                for chunk in uploaded.chunks():
                    f.write(chunk)
        except OSError as exc:
            results.append({"filename": uploaded.name, "ok": False, "error": f"Failed to write file: {exc}"})
            continue

        tags, info = parse_tags(dest_path)

        def clean(val):
            return val.replace("\x00", "").strip() if val else val

        # Fallback metadata comes from the ORIGINAL client filename
        # (uploaded.name), not dest_path.stem -- dest_path went through
        # get_valid_filename() for on-disk safety, which replaces
        # spaces with underscores; using it as a metadata fallback
        # would land literal underscores in the DB (e.g.
        # "Pink_Floyd_-_Time" instead of "Pink Floyd" / "Time"). The
        # saved file's actual name is unaffected either way -- this
        # only changes what fills the title/artist fields when tags
        # are missing.
        title, artist_name = resolve_fallback_metadata(
            Path(uploaded.name).stem, clean(tags.get("title")), clean(tags.get("artist")),
        )
        album_title = clean(tags.get("album")) or ""
        album_artist_name = clean(tags.get("album_artist")) or ""
        genre_name = clean(tags.get("genre")) or ""

        artist_obj, _ = Artist.get_or_create_ci(artist_name)

        album_obj = None
        if album_title:
            album_obj, _ = Album.objects.get_or_create(
                title=album_title, album_artist=album_artist_name,
                defaults={"year": tags.get("year")},
            )

        genre_obj = None
        if genre_name:
            genre_obj, _ = Genre.objects.get_or_create(name=genre_name)

        track = Track.objects.create(
            filepath=str(dest_path),
            filename=dest_path.name,
            format=ext.lstrip("."),
            title=title,
            artist=artist_obj,
            album=album_obj,
            genre=genre_obj,
            year=tags.get("year"),
            track_number=tags.get("track_number"),
            disc_number=tags.get("disc_number"),
            duration_seconds=info.get("duration_seconds"),
            sample_rate=info.get("sample_rate"),
            channels=info.get("channels"),
            bit_depth=info.get("bit_depth"),
            category=category,
            uploaded_by=request.user if request.user.is_authenticated else None,
        )

        # Analysis (waveform + cue points, now a mono AND a stereo ffmpeg
        # decode pass per track) deliberately does NOT run inline here
        # anymore -- a big batch's cumulative analysis time was blowing
        # past gunicorn's default 30s worker timeout, killing the upload
        # request outright (files already written/tracks already created
        # survive since there's no wrapping transaction, but the response
        # never comes back and remaining files in the batch never get
        # processed). A freshly-created Track naturally has
        # next_start_seconds=None, which is exactly what the
        # isadoraair-analyze.timer's periodic `analyze_tracks` run (no
        # --force) already selects on -- no new flag or field needed,
        # just leaving analysis for that pass to pick up within a minute.
        results.append({
            "filename": uploaded.name,
            "ok": True,
            "track_id": track.id,
            "title": track.title,
            "artist": artist_obj.name,
            "saved_as": dest_path.name,
            "analyzed": False,
        })

    if is_contributor:
        _notify_contributor_upload(request.user, category, results)

    return JsonResponse({"results": results})


def _notify_contributor_upload(user, category, results):
    """Email the station operator about a successful Contributor
    upload batch. Delivery is best-effort: an SMTP failure logs a
    console line and returns without raising -- an upload succeeding
    on disk + in the DB must not be walked back just because the
    notification email couldn't send. Uses the same notification-
    recipient list the /monitoring/ alerts use (NotificationConfig)
    so there's a single place to change the reviewing address."""
    from django.conf import settings
    from django.core.mail import send_mail
    from monitoring.models import NotificationConfig

    ok_results = [r for r in results if r.get("ok")]
    if not ok_results:
        return

    recipients = NotificationConfig.load().recipient_list()
    if not recipients:
        return

    subject = f"[IsadoraAir] {user.username} uploaded {len(ok_results)} track(s)"
    lines = [
        f"Contributor: {user.username}",
        f"Category:    {category.code} ({category.name})",
        f"Uploaded:    {len(ok_results)} track(s)",
        "",
        "Tracks:",
    ]
    for r in ok_results:
        lines.append(f"  - {r.get('artist','?')} — {r.get('title','?')}   [track id {r.get('track_id')}]")
    lines.append("")
    lines.append("These land with ready2air=False; review in the library and mark ready when approved.")

    try:
        send_mail(
            subject, "\n".join(lines),
            settings.DEFAULT_FROM_EMAIL, recipients,
            fail_silently=False,
        )
    except Exception as exc:
        print(f"  [notify] Contributor-upload email failed for {user.username}: {exc}")


@require_http_methods(["GET"])
def api_cd_detect(request):
    """Read whatever CD is in /dev/sr0 and try to identify it via
    MusicBrainz. Returns the disc info + editable album/track
    metadata for the frontend to render. A 404 means the tray is
    empty or unreadable; a 200 with mb_matched=False means the
    disc was read but MusicBrainz has no match (frontend falls
    back to manual entry). Never blocks the whipper subprocess --
    this is a read-only probe."""
    from library.cd_ripping import detect_disc, DiscNotFoundError
    from library.models import CDRipConfig
    try:
        return JsonResponse(detect_disc(CDRipConfig.load().device))
    except DiscNotFoundError as exc:
        return JsonResponse({"error": str(exc)}, status=404)
    except Exception as exc:
        # Log full traceback so we can see WHICH field the MB parser
        # tripped on -- the top-level `exc` message alone (e.g.
        # KeyError('name')) doesn't say where.
        import traceback
        traceback.print_exc()
        return JsonResponse(
            {"error": f"CD detect failed: {type(exc).__name__}: {exc}"},
            status=500,
        )


@require_http_methods(["POST"])
def api_cd_eject(request):
    """Open the CD tray. Idempotent -- calling it with the tray
    already open is a no-op success. Refuses if a rip is in flight
    so the operator can't yank the disc mid-rip."""
    from library.cd_ripping import eject
    from library.models import CDRipConfig, CDRipJob
    if CDRipJob.objects.filter(state__in=["pending", "running"]).exists():
        return JsonResponse(
            {"error": "A rip is in progress -- cancel it before ejecting."},
            status=409,
        )
    try:
        eject(CDRipConfig.load().device)
        return JsonResponse({"ok": True})
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


@require_http_methods(["POST"])
def api_cd_rip_start(request):
    """Kick off a rip. Body is JSON: {category_id, disc_id,
    mb_release_id (optional), album_meta}. Fails 409 if a rip is
    already running -- one drive, one at a time."""
    from library.cd_ripping import spawn_rip_child
    from library.models import CDRipJob
    if CDRipJob.objects.filter(state__in=["pending", "running"]).exists():
        return JsonResponse(
            {"error": "A rip is already in progress."},
            status=409,
        )
    try:
        body = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)
    category_id = body.get("category_id")
    if not category_id:
        return JsonResponse({"error": "category_id required"}, status=400)
    category = get_object_or_404(Category, pk=category_id)
    album_meta = body.get("album_meta") or {}
    tracks = album_meta.get("tracks") or []
    if not tracks:
        return JsonResponse({"error": "album_meta.tracks is empty"}, status=400)

    from library.models import CDRipConfig
    cd_cfg = CDRipConfig.load()
    staging_root = Path(cd_cfg.staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    job = CDRipJob.objects.create(
        state="pending",
        disc_id=body.get("disc_id", ""),
        mb_release_id=body.get("mb_release_id", ""),
        device=body.get("device", cd_cfg.device),
        category=category,
        album_meta=album_meta,
        progress_total_tracks=len(tracks),
        status_message="Queued.",
    )
    job.staging_dir = str(staging_root / f"job-{job.id}")
    job.save(update_fields=["staging_dir"])

    try:
        pid = spawn_rip_child(job.id)
    except Exception as exc:
        job.state = "error"
        job.error_message = f"Failed to spawn rip child: {exc}"
        job.finished_at = timezone.now()
        job.save()
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({"ok": True, "job_id": job.id, "pid": pid})


@require_http_methods(["GET"])
def api_cd_rip_status(request):
    """Return the current rip job (whichever is most recent, whether
    running or terminal), including progress + created track links.
    Returns 204 if no job has ever run so the frontend can render an
    idle state."""
    from library.models import CDRipJob
    job = CDRipJob.objects.order_by("-created_at").first()
    if job is None:
        return JsonResponse({"state": "idle"})
    payload = {
        "id": job.id,
        "state": job.state,
        "disc_id": job.disc_id,
        "category": {"id": job.category_id, "code": job.category.code},
        "album_title": (job.album_meta or {}).get("album_title", ""),
        "album_artist": (job.album_meta or {}).get("album_artist", ""),
        "progress_current_track": job.progress_current_track,
        "progress_total_tracks": job.progress_total_tracks,
        "status_message": job.status_message,
        "error_message": job.error_message,
        "created_track_ids": job.created_track_ids,
        "accurate_rip_matches": job.accurate_rip_matches,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    return JsonResponse(payload)


@require_http_methods(["POST"])
def api_cd_rip_cancel(request):
    """Cancel the currently-running rip by killing the whipper
    subprocess. Best-effort -- if whipper has moved on to
    post-processing, this may not stop cleanly."""
    from library.models import CDRipJob
    import os, signal
    job = CDRipJob.objects.filter(state__in=["pending", "running"]).first()
    if job is None:
        return JsonResponse({"ok": True, "message": "No active rip."})
    if job.whipper_pid:
        try:
            # Kill the whole process group (child was started with
            # start_new_session=True, so it has its own pgid).
            os.killpg(os.getpgid(job.whipper_pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception as exc:
            return JsonResponse({"error": f"Kill failed: {exc}"}, status=500)
    job.state = "cancelled"
    job.status_message = "Cancelled by operator."
    job.finished_at = timezone.now()
    job.save(update_fields=["state", "status_message", "finished_at"])
    return JsonResponse({"ok": True})


# Browser-native <audio> support varies by codec -- mp3/wav/ogg/m4a play
# in every modern browser, flac is now broadly supported too, but aiff,
# mp2, and alac may not play in some/most browsers even though the
# server serves them correctly (ALAC in particular has poor native
# browser support outside Safari, even though GStreamer's avdec_alac
# decodes it fine for real on-air playback -- confirmed live). Not
# something fixable without a much bigger on-the-fly transcoding
# feature, so this just serves the real file as-is.
_AUDIO_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "mp2": "audio/mpeg",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "alac": "audio/mp4",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
}


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def _open_track_source_file(track):
    """Open the authoritative Track source file or raise a path-safe 404.

    Playback and download must agree on what constitutes a usable source.
    Returning the already-open stream also avoids a validate-then-open race
    and lets FileResponse stream without loading the file into memory.
    """
    if not track.filepath:
        raise Http404("Track audio is unavailable.")

    fp = Path(track.filepath)
    source = None
    try:
        if not fp.is_file():
            raise Http404("Track audio is unavailable.")
        source = fp.open("rb")
        size = os.fstat(source.fileno()).st_size
    except Http404:
        raise
    except (OSError, ValueError):
        if source is not None:
            source.close()
        raise Http404("Track audio is unavailable.") from None
    return fp, source, size


def _leading_id3_size(source):
    """Some FLAC files in this library have a non-standard ID3v2 tag
    bolted onto the front (a common mistake from MP3-oriented tagging
    tools) -- a real FLAC file must start with the literal bytes "fLaC",
    no exceptions, so this breaks browsers' strict native FLAC decoders
    even though GStreamer's decodebin (confirmed live, same element
    engine.py uses for real on-air playback) and mutagen/ffprobe are all
    lenient enough to find the audio data anyway. Affects ~12,000 of the
    ~26,000 FLAC files in this library (confirmed via a real scan) --
    not an on-air problem, purely a browser-preview one, so this skips
    the bogus tag when SERVING the file rather than touching any real
    file on disk. Returns the number of bytes to skip (0 if the file
    already starts correctly)."""
    original_position = source.tell()
    try:
        source.seek(0)
        header = source.read(10)
    finally:
        source.seek(original_position)
    if header[:3] != b"ID3":
        return 0
    # ID3v2 size is "syncsafe": 4 bytes, each only using its low 7 bits.
    size = ((header[6] & 0x7F) << 21) | ((header[7] & 0x7F) << 14) | ((header[8] & 0x7F) << 7) | (header[9] & 0x7F)
    return 10 + size


@require_http_methods(["GET"])
def api_track_audio(request, pk):
    from django.http import HttpResponse

    track = get_object_or_404(Track, pk=pk)
    try:
        _fp, source, real_size = _open_track_source_file(track)
    except Http404:
        # Preserve this endpoint's established missing-file response shape;
        # the dedicated download endpoint may use Django's ordinary 404.
        return JsonResponse({"error": "File not found on disk"}, status=404)

    content_type = _AUDIO_CONTENT_TYPES.get(track.format, "application/octet-stream")
    # Everything below is relative to this offset, not the real file --
    # 0 for the ~14,000 FLAC files (and every non-FLAC format) that don't
    # have the bogus leading tag.
    try:
        skip = _leading_id3_size(source) if track.format == "flac" else 0
    except (OSError, ValueError):
        source.close()
        return JsonResponse({"error": "File not found on disk"}, status=404)
    file_size = real_size - skip

    # Django's own FileResponse does NOT implement HTTP Range support
    # (confirmed by reading django/http/response.py directly -- no Range
    # handling exists there) despite that being an easy assumption to
    # make. Without this, the browser's <audio> element can still play
    # from the start, but seeking/scrubbing doesn't work properly and
    # larger files take longer to become playable at all.
    range_match = _RANGE_RE.match(request.META.get("HTTP_RANGE", ""))
    if range_match:
        start_str, end_str = range_match.groups()
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        try:
            source.seek(skip + start)
            chunk = source.read(length)
        except (OSError, ValueError):
            return JsonResponse({"error": "File not found on disk"}, status=404)
        finally:
            source.close()

        response = HttpResponse(chunk, status=206, content_type=content_type)
        response["Content-Length"] = str(length)
        response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        response["Accept-Ranges"] = "bytes"
        return response

    try:
        source.seek(skip)
    except (OSError, ValueError):
        source.close()
        return JsonResponse({"error": "File not found on disk"}, status=404)
    response = FileResponse(source, content_type=content_type)
    response["Content-Length"] = str(file_size)
    response["Accept-Ranges"] = "bytes"
    return response


@require_http_methods(["GET"])
def api_track_download(request, pk):
    """Stream a Track's original source file as an authorized download."""
    from library.middleware import user_is_library_read_only

    if user_is_library_read_only(request.user):
        return JsonResponse({"error": "Read-only for this account."}, status=403)

    track = get_object_or_404(Track, pk=pk)
    fp, source, _size = _open_track_source_file(track)
    content_type = _AUDIO_CONTENT_TYPES.get(
        track.format, "application/octet-stream"
    )
    return FileResponse(
        source,
        as_attachment=True,
        filename=fp.name,
        content_type=content_type,
    )


@require_http_methods(["POST"])
def api_track_blocked_slot_toggle(request, pk):
    """Roadmap 2.5A: requires library.manage_tracks -- previously
    reachable (and, per the template, previously actually presented in
    the UI) to any Contributor/remote_dj account with zero server-side
    check. See PROJECT_NOTES.md's "Roadmap 2.5" section."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    track = get_object_or_404(Track, pk=pk)
    try:
        slot = int(json.loads(request.body)["slot"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid slot"}, status=400)
    if not (0 <= slot <= 167):
        return JsonResponse({"error": "slot out of range"}, status=400)

    blocked = set(track.blocked_slots)
    if slot in blocked:
        blocked.discard(slot)
        now_blocked = False
    else:
        blocked.add(slot)
        now_blocked = True
    track.blocked_slots = sorted(blocked)
    track.save(update_fields=["blocked_slots"])
    return JsonResponse({"slot": slot, "blocked": now_blocked})


@require_http_methods(["POST"])
def api_track_blocked_slot_toggle_row(request, pk):
    """Flips an entire day-of-week row as a unit -- same master-toggle
    convention as a "select all" checkbox header: if any hour in the row
    is currently blocked, the click clears the whole row; if the row is
    fully open, the click blocks the whole row. A single read-modify-
    write, not 24 individual toggle calls, so the row updates atomically
    in one request.

    Roadmap 2.5A: requires library.manage_tracks (see
    api_track_blocked_slot_toggle's comment)."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    track = get_object_or_404(Track, pk=pk)
    try:
        day_of_week = int(json.loads(request.body)["day_of_week"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid day_of_week"}, status=400)
    if not (0 <= day_of_week <= 6):
        return JsonResponse({"error": "day_of_week out of range"}, status=400)

    row_slots = [day_of_week * 24 + hour for hour in range(24)]
    blocked = set(track.blocked_slots)
    now_blocked = not any(s in blocked for s in row_slots)

    if now_blocked:
        blocked.update(row_slots)
    else:
        blocked.difference_update(row_slots)
    track.blocked_slots = sorted(blocked)
    track.save(update_fields=["blocked_slots"])
    return JsonResponse({"day_of_week": day_of_week, "slots": row_slots, "blocked": now_blocked})


@require_http_methods(["POST"])
def api_track_blocked_slot_toggle_column(request, pk):
    """Flips an entire hour-of-day column (all 7 days at that hour) as a
    unit -- same master-toggle convention as toggle_row above, just
    sliced the other way across the grid.

    Roadmap 2.5A: requires library.manage_tracks (see
    api_track_blocked_slot_toggle's comment)."""
    result = authorize(request.user, "library.manage_tracks")
    if not result:
        return forbidden_response(result, user=request.user, capability_slug="library.manage_tracks")

    track = get_object_or_404(Track, pk=pk)
    try:
        hour = int(json.loads(request.body)["hour"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "Invalid hour"}, status=400)
    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour out of range"}, status=400)

    column_slots = [dow * 24 + hour for dow in range(7)]
    blocked = set(track.blocked_slots)
    now_blocked = not any(s in blocked for s in column_slots)

    if now_blocked:
        blocked.update(column_slots)
    else:
        blocked.difference_update(column_slots)
    track.blocked_slots = sorted(blocked)
    track.save(update_fields=["blocked_slots"])
    return JsonResponse({"hour": hour, "slots": column_slots, "blocked": now_blocked})


# ---------------------------------------------------------------------
# Royalty reports (SoundExchange NCE etc.)
# ---------------------------------------------------------------------
def _reports_permission_check(request):
    """Reports carry statutory-license implications -- keep the surface
    tight. Staff or superuser only. If we later add a Treasurer group
    with its own GroupAccess row that gates /reports/, this check will
    still succeed for them (staff/superuser bypasses the group middleware
    but the group middleware ALSO gates /reports/ for non-privileged
    users). Returns HttpResponseForbidden or None."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_staff or user.is_superuser)):
        return HttpResponseForbidden("Reports are staff-only.")
    return None


@ensure_csrf_cookie
def reports_page(request):
    denied = _reports_permission_check(request)
    if denied:
        return denied

    from datetime import timedelta
    from library.models import IcecastSample, RoyaltyReport
    from library.services.royalty_reports import GENERATORS

    reports = RoyaltyReport.objects.select_related("generated_by").order_by(
        "-period_start", "-generated_at"
    )[:200]

    # Default the month picker to last month -- most reports are
    # generated 1-2 weeks into the following month.
    today = timezone.localdate()
    if today.month == 1:
        default_month = f"{today.year - 1}-12"
    else:
        default_month = f"{today.year}-{today.month - 1:02d}"

    # Sampler health: healthy if we've had a sample within the last
    # 5 minutes (timer fires every minute; 5 min is enough slack for
    # a missed fire without crying wolf). Stale = last 5-60 min.
    # Dead = >1h. Also counts samples in the last hour so the operator
    # sees the actual cadence, not just the last-time.
    now = timezone.now()
    latest = IcecastSample.objects.order_by("-sampled_at").first()
    last_hour_count = IcecastSample.objects.filter(
        sampled_at__gte=now - timedelta(hours=1)
    ).count()
    if latest is None:
        sampler_status = "dead"
        sampler_msg = "No samples ever. Is isadoraair-sample-icecast.timer enabled?"
    else:
        age_minutes = (now - latest.sampled_at).total_seconds() / 60.0
        if age_minutes <= 5:
            sampler_status = "healthy"
            sampler_msg = (
                f"{last_hour_count} sample(s) in the last hour, "
                f"last at {timezone.localtime(latest.sampled_at):%H:%M:%S} "
                f"(total listeners then: {latest.listeners_total})"
            )
        elif age_minutes <= 60:
            sampler_status = "stale"
            sampler_msg = (
                f"Last sample {age_minutes:.0f} min ago -- sampler timer may have "
                f"missed a fire. Check `systemctl status isadoraair-sample-icecast.timer`."
            )
        else:
            sampler_status = "dead"
            sampler_msg = (
                f"Last sample {age_minutes/60:.1f}h ago. "
                f"isadoraair-sample-icecast.timer is likely stopped."
            )

    from .forms import HiddenTrackDetectionForm

    return render(request, "library/reports.html", {
        "reports": reports,
        "default_month": default_month,
        "format_choices": [(k, GENERATORS[k].__doc__.strip().split(chr(10))[0] if GENERATORS[k].__doc__ else k)
                            for k in GENERATORS.keys()],
        "format_display": dict(RoyaltyReport.FORMAT_CHOICES),
        "sampler_status": sampler_status,
        "sampler_msg": sampler_msg,
        # Hidden Track Detection tab (see api_hidden_track_scan below).
        # Unbound form here just supplies field defaults/help text for
        # the initial page render -- the actual scan is a separate POST
        # the page's own JS fires and renders results from, matching
        # the royalty-report "Generate" button's existing pattern.
        "hidden_track_form": HiddenTrackDetectionForm(),
        "hidden_track_categories": Category.objects.order_by("name").values("code", "name"),
        # Listener Stats tab (see api_reports_listener_stats below) --
        # defaults to the current calendar month, station-local, same
        # "today" used for default_month above.
        "listener_stats_default_start": today.replace(day=1).isoformat(),
        "listener_stats_default_end": today.isoformat(),
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_reports_generate(request):
    denied = _reports_permission_check(request)
    if denied:
        return denied

    import calendar
    from django.core.files.base import ContentFile
    from django.http import HttpResponseForbidden
    from django.urls import reverse
    from library.models import RoyaltyReport
    from library.services.royalty_reports import GENERATORS, compute_stats, generate

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    month = body.get("month", "")
    fmt = body.get("format", "")
    if fmt not in GENERATORS:
        return JsonResponse({"error": f"Unknown format: {fmt!r}"}, status=400)
    try:
        year, mo = month.split("-")
        year, mo = int(year), int(mo)
        period_start = date_type(year, mo, 1)
        period_end = date_type(year, mo, calendar.monthrange(year, mo)[1])
    except (ValueError, IndexError):
        return JsonResponse({"error": "month must be YYYY-MM"}, status=400)

    # ATH override (optional). Only meaningful for the NCE format,
    # but pass through -- generate() ignores it for the others.
    ath_override = body.get("ath_override")
    if ath_override is not None:
        try:
            ath_override = float(ath_override)
            if ath_override < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            return JsonResponse({"error": "ath_override must be a non-negative number"}, status=400)

    content, ext = generate(period_start, period_end, fmt, ath_override=ath_override)
    stats = compute_stats(period_start, period_end)

    from library.services.royalty_reports import compute_ath
    ath_computed = compute_ath(period_start, period_end)
    ath_used = ath_override if ath_override is not None else ath_computed

    rr = RoyaltyReport(
        period_start=period_start,
        period_end=period_end,
        format=fmt,
        generated_by=request.user,
        total_plays=stats["total_plays"],
        unique_tracks=stats["unique_tracks"],
        unique_artists=stats["unique_artists"],
        plays_with_isrc=stats["plays_with_isrc"],
        ath_computed=ath_computed,
        ath_override=ath_override,
        ath_used=ath_used,
    )
    fname = f"{period_start:%Y-%m}-{fmt}.{ext}"
    rr.file.save(fname, ContentFile(content.encode("utf-8")), save=False)
    rr.save()

    return JsonResponse({
        "ok": True,
        "id": rr.id,
        "download_url": reverse("library:reports-download", args=[rr.id]),
        "total_plays": stats["total_plays"],
        "unique_tracks": stats["unique_tracks"],
        "plays_with_isrc": stats["plays_with_isrc"],
    })


def reports_download(request, pk):
    """Serves a persisted report file through Django (not directly by
    nginx) so the access check is enforced. Files live outside the
    web-served MEDIA_ROOT tree entirely (REPORTS_ROOT, default
    /var/lib/isadoraair/reports/) -- there is no direct static URL
    that could be leaked, so this view is the only path in."""
    denied = _reports_permission_check(request)
    if denied:
        return denied

    from django.http import FileResponse
    from library.models import RoyaltyReport

    rr = get_object_or_404(RoyaltyReport, pk=pk)
    if not rr.file:
        return HttpResponseNotFound("Report file no longer exists on disk.")

    fname = f"{rr.period_start:%Y-%m}-{rr.format}.{rr.file.name.rsplit('.', 1)[-1]}"
    return FileResponse(rr.file.open("rb"), as_attachment=True, filename=fname)


@require_http_methods(["GET"])
def api_reports_listener_stats(request):
    """Bucketed listener-count time series for the /reports/ Listener
    Stats tab -- per-stream lines plus an aggregate line, over an
    operator-chosen date range (defaults to the current calendar
    month, station-local, matching listener_stats_default_start/_end
    in reports_page above).

    Read-only, GET (unlike the other /api/reports/ endpoints, which
    mutate or generate a file) -- query params `start`/`end`
    (YYYY-MM-DD, inclusive), both optional. All aggregation happens in
    compute_listener_series; this view is just param parsing +
    permission check + JSON shaping."""
    denied = _reports_permission_check(request)
    if denied:
        return denied

    from library.services.royalty_reports import compute_listener_series

    today = timezone.localdate()
    start_raw = request.GET.get("start", "").strip()
    end_raw = request.GET.get("end", "").strip()
    try:
        period_start = date_type.fromisoformat(start_raw) if start_raw else today.replace(day=1)
        period_end = date_type.fromisoformat(end_raw) if end_raw else today
    except ValueError:
        return JsonResponse({"error": "start/end must be YYYY-MM-DD"}, status=400)
    if period_start > period_end:
        return JsonResponse({"error": "start must not be after end"}, status=400)

    series = compute_listener_series(period_start, period_end)
    return JsonResponse({
        "ok": True,
        "start": period_start.isoformat(),
        "end": period_end.isoformat(),
        **series,
    })


# ---------------------------------------------------------------------
# Hidden Track Detection -- Phase 1 (diagnostic only, no persistence).
# See library/services/hidden_track_detection.py's module docstring
# for the full algorithm/schema notes. This view is strictly a thin
# adapter: validate the form, build a filtered queryset FROM TRACK
# ROWS ONLY (never a caller-supplied filesystem path), call the
# service for ONE BOUNDED BATCH, return JSON. No DB writes, no audio
# decode, no subprocess.
#
# Batched, not a single synchronous full-library call: a real-library
# measurement (read-only, this station's actual ~29.6k-track
# ready2air/music-kind scope) took 49.1s wall-clock end to end --
# comfortably over gunicorn's own 30s default worker timeout (deploy/
# isadoraair-gunicorn.service sets no --timeout override), and by a
# wide enough margin that no reasonable per-request budget would make
# one request safe. The browser instead calls this endpoint repeatedly
# with an increasing `cursor` (see scan_for_hidden_tracks_batch) until
# `is_last_batch` comes back true, accumulating results client-side.
# Every batch call independently re-validates the form and re-checks
# permissions/CSRF -- there is no server-side scan state between
# calls, matching the "no persistence" contract this feature keeps
# throughout (no scan-run model, no candidate cache).
# ---------------------------------------------------------------------
@require_http_methods(["POST"])
def api_hidden_track_scan(request):
    """Same staff/superuser gate as every other /reports/ action (see
    _reports_permission_check) -- Track ID filtering below still only
    ever narrows within that same staff-only visibility, so a Track ID
    filter can't be used to see a track a staff user couldn't already
    view directly at /track/<pk>/. CSRF protection is NOT exempted
    here (unlike api_reports_generate above, an existing, unrelated
    choice this view deliberately does not copy) -- the frontend sends
    the standard X-CSRFToken header, same convention as
    autofillRelatedArtists() in library.html."""
    denied = _reports_permission_check(request)
    if denied:
        return denied

    from .forms import HiddenTrackDetectionForm
    from .services.hidden_track_detection import DetectionSettings, scan_for_hidden_tracks_batch

    form = HiddenTrackDetectionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors.get_json_data()}, status=400)

    # `cursor`: the highest track pk already processed by a PRIOR batch
    # in this same scan, or absent/blank for the first call. Not a
    # form field (it's protocol state driven by the browser's own
    # batch loop, not an operator-editable setting) -- validated by
    # hand instead. Never anything but a pk__gt boundary WITHIN the
    # filtered queryset built below, so it can't be used to select an
    # arbitrary/unauthorized track: a bogus value just yields a
    # different (still fully filtered, still permission-scoped) slice
    # of the SAME authorized queryset, never something outside it.
    cursor_raw = (request.POST.get("cursor") or "").strip()
    cursor = None
    if cursor_raw:
        try:
            cursor = int(cursor_raw)
        except ValueError:
            return JsonResponse(
                {"ok": False, "errors": {"cursor": [{"message": "cursor must be an integer.", "code": "invalid"}]}},
                status=400,
            )

    data = form.cleaned_data
    qs = Track.objects.all()

    track_id = data.get("track_id")
    if track_id:
        # Individual-track mode overrides the other filters entirely --
        # a focused single-row scan for debugging one suspect track.
        qs = qs.filter(id=track_id)
    else:
        ready2air = data.get("ready2air") or "yes"
        if ready2air == "yes":
            qs = qs.filter(ready2air=True)
        elif ready2air == "no":
            qs = qs.filter(ready2air=False)
        # "all" -> no ready2air filter.

        category_code = (data.get("category") or "").strip()
        if category_code:
            try:
                category = Category.objects.get(code=category_code)
            except Category.DoesNotExist:
                return JsonResponse(
                    {"ok": False, "errors": {"category": [{"message": f"No category with code {category_code!r}.", "code": "invalid"}]}},
                    status=400,
                )
            # Primary OR additional category, same convention as
            # track_filters.filter_tracks and log_builder's pool
            # queries -- .distinct() guards the M2M-join double-count.
            qs = qs.filter(Q(category=category) | Q(additional_categories=category)).distinct()
        else:
            # Default: "all music-appropriate categories" -- same
            # Category.kind.code == "music" gate log_builder's holiday
            # pool uses, not a hardcoded category list.
            qs = qs.filter(
                Q(category__kind__code="music") | Q(additional_categories__kind__code="music")
            ).distinct()

    # Cheap indexed COUNT -- lets the browser show "X of Y processed"
    # progress without a separate round trip. Computed on every batch
    # (not cached server-side, since nothing is persisted between
    # requests -- see the module's "No persistence" contract); this is
    # the one query in this view that isn't already bounded by the
    # batch size, but a COUNT is orders of magnitude cheaper than
    # actually reading the matching rows' waveform JSON.
    total_eligible = qs.count()

    settings_obj = DetectionSettings(**form.settings_kwargs())
    results, summary, next_cursor, is_last_batch = scan_for_hidden_tracks_batch(
        qs, settings_obj, cursor=cursor,
    )

    for r in results:
        r["track_url"] = reverse("library:track-detail", args=[r["track_id"]])

    return JsonResponse({
        "ok": True,
        "results": results,
        "summary": summary,
        "settings": form.settings_kwargs(),
        "total_eligible": total_eligible,
        "next_cursor": next_cursor,
        "is_last_batch": is_last_batch,
    })
