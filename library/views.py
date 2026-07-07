import json
import subprocess
import time as time_mod
from datetime import date as date_type, time
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from django.shortcuts import get_object_or_404

from .models import Artist, Album, Category, Genre, LogItem, Playlist, PlaylistItem, PlaylistLog, Rotation, RotationSlot, ScheduleBlock, Track
from .services.log_builder import _build_from_playlist, build_hour_log


@ensure_csrf_cookie
def dashboard_page(request):
    playlists = Playlist.objects.all().order_by("name")
    return render(request, "library/dashboard.html", {"playlists": playlists})


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
        "content_kind": content_kind,
        "content_id": content.id if content else None,
        "content_name": content.name if content else None,
    }


@require_http_methods(["GET", "POST"])
def api_schedule_list(request):
    if request.method == "GET":
        blocks = (
            ScheduleBlock.objects
            .filter(day_of_week__isnull=False)
            .select_related("rotation", "playlist")
            .order_by("day_of_week", "start_time")
        )
        return JsonResponse({"blocks": [_block_to_dict(b) for b in blocks]})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    day_of_week = body.get("day_of_week")
    hour = body.get("hour")
    # Accept either {"rotation_id": ...} or {"playlist_id": ...}. The
    # schedule grid UI only writes rotations right now; playlist-backed
    # blocks come from admin and are read-only via this endpoint.
    rotation_id = body.get("rotation_id")
    playlist_id = body.get("playlist_id")

    if day_of_week is None or hour is None:
        return JsonResponse({"error": "day_of_week and hour are required"}, status=400)
    if (rotation_id is None) == (playlist_id is None):
        return JsonResponse({"error": "exactly one of rotation_id or playlist_id is required"}, status=400)

    if not (0 <= day_of_week <= 6):
        return JsonResponse({"error": "day_of_week must be 0-6"}, status=400)
    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour must be 0-23"}, status=400)

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

    block, created = ScheduleBlock.objects.update_or_create(
        day_of_week=day_of_week,
        start_time=time(hour, 0),
        specific_date=None,
        defaults=defaults,
    )

    payload = _block_to_dict(block)
    payload["created"] = created
    return JsonResponse(payload)


@require_http_methods(["DELETE"])
def api_schedule_delete(request, pk):
    deleted, _ = ScheduleBlock.objects.filter(pk=pk).delete()
    return JsonResponse({"ok": True, "deleted": deleted > 0})


@require_http_methods(["GET", "POST"])
def api_rotation_list(request):
    if request.method == "GET":
        rotations = Rotation.objects.all().order_by("name").annotate(_slot_count=Count("slots"))
        data = [
            {"id": r.id, "name": r.name, "description": r.description, "slot_count": r._slot_count}
            for r in rotations
        ]
        return JsonResponse({"rotations": data})

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
    rotation = get_object_or_404(Rotation, pk=pk)

    if request.method == "GET":
        return JsonResponse(_rotation_to_dict(rotation))

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
    slot = get_object_or_404(RotationSlot.objects.select_related("rotation"), pk=slot_id)
    rotation = slot.rotation
    slot.delete()

    remaining = list(rotation.slots.order_by("position"))
    _reposition_items(remaining, model=RotationSlot)

    return JsonResponse(_rotation_to_dict(rotation))


@require_http_methods(["POST"])
def api_rotation_reorder(request, pk):
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
    playlist = get_object_or_404(Playlist, pk=pk)

    if request.method == "GET":
        return JsonResponse(_playlist_to_dict(playlist))

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
    item = get_object_or_404(PlaylistItem.objects.select_related("playlist"), pk=item_id)
    playlist = item.playlist
    item.delete()

    remaining = list(playlist.items.order_by("position"))
    _reposition_items(remaining, model=PlaylistItem)

    return JsonResponse(_playlist_to_dict(playlist))


@require_http_methods(["POST"])
def api_playlist_reorder(request, pk):
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


@csrf_exempt
@require_http_methods(["POST"])
def api_playlist_play_now(request, pk):
    """Force the engine to play this playlist immediately, replacing
    whatever's assigned to the current hour rather than waiting for a
    scheduled slot."""
    playlist = get_object_or_404(Playlist, pk=pk)

    now = timezone.localtime()
    log, error = _build_from_playlist(now.date(), now.hour, playlist)
    if error:
        return JsonResponse({"error": error}, status=400)

    log.status = "approved"
    log.save(update_fields=["status"])

    cmd_path = Path("/run/isadoraair/engine_cmd.json")
    cmd_path.write_text(json.dumps({"command": "reload_current_log"}), encoding="utf-8")

    return JsonResponse({"ok": True, "log_id": log.id, "item_count": log.items.count()})


@ensure_csrf_cookie
def playlists_page(request):
    categories = Category.objects.order_by("name")
    return render(request, "library/playlists.html", {"categories": categories})


def library_page(request):
    categories = Category.objects.order_by("name")
    return render(request, "library/library.html", {"categories": categories})


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
    if q:
        # "+" joins multiple required terms, e.g. "Pink Floyd + If" ->
        # must match "Pink Floyd" (in any field) AND "If" (in any field).
        # Each .filter() call below ANDs onto the queryset, while the Q()
        # combination within one call ORs across fields for that term --
        # a plain query with no "+" is just one term, so this is fully
        # backward compatible with the existing single-phrase search.
        terms = [t.strip() for t in q.split("+") if t.strip()]
        for term in terms:
            qs = qs.filter(
                Q(title__icontains=term)
                | Q(artist__name__icontains=term)
                | Q(album__title__icontains=term)
            )

    cat_id = request.GET.get("category")
    if cat_id:
        # Matches either the track's primary category or a secondary one
        # (additional_categories) -- distinct() guards against the M2M
        # join duplicating a row for a track that somehow matches both.
        qs = qs.filter(Q(category_id=cat_id) | Q(additional_categories=cat_id)).distinct()

    ready = request.GET.get("ready2air")
    if ready == "true":
        qs = qs.filter(ready2air=True)
    elif ready == "false":
        qs = qs.filter(ready2air=False)

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
    else:
        return JsonResponse({"error": "Unknown action"}, status=400)

    return JsonResponse({"ok": True, "updated": updated})


@ensure_csrf_cookie
def track_detail_page(request, pk):
    track = get_object_or_404(
        Track.objects.select_related("artist", "album", "genre", "category"), pk=pk
    )
    categories = Category.objects.order_by("name")
    return render(request, "library/track_detail.html", {
        "track": track,
        "categories": categories,
        "energy_choices": Track.ENERGY_CHOICES,
        "vocal_type_choices": Track.VOCAL_TYPE_CHOICES,
        "end_type_choices": Track.END_TYPE_CHOICES,
    })


@require_http_methods(["GET", "PATCH"])
def api_track_detail(request, pk):
    track = get_object_or_404(
        Track.objects.select_related("artist", "album", "genre", "category"), pk=pk
    )

    if request.method == "GET":
        return JsonResponse(_track_to_dict(track))

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

    for field, value in body.items():
        if field in DIRECT_FIELDS:
            setattr(track, field, value)
        elif field == "artist":
            artist_obj, _ = Artist.objects.get_or_create(name=value)
            track.artist = artist_obj
        elif field == "album":
            if value:
                album_obj, _ = Album.objects.get_or_create(
                    title=value, defaults={"album_artist": ""}
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

    track.save()
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
    }


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
                "title": item.track.title,
                "artist": item.track.artist.name if item.track.artist else "",
                "category": item.category.code if item.category else "",
                "duration": item.track.next_start_seconds or item.track.duration_seconds or 0,
            }
            for item in items
        ],
    }


@require_http_methods(["POST"])
def api_log_build(request):
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

    log, error = build_hour_log(target_date, hour)
    if error:
        return JsonResponse({"error": error}, status=400)

    return JsonResponse(_log_to_dict(log))


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
    deleted, _ = PlaylistLog.objects.filter(pk=pk, status="draft").delete()
    return JsonResponse({"ok": True, "deleted": deleted > 0})


@require_http_methods(["PATCH"])
def api_log_item_swap(request, item_id):
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
    item.save(update_fields=["track"])

    return JsonResponse(_log_to_dict(item.playlist_log))


@require_http_methods(["POST"])
def api_log_reorder(request, pk):
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
        category=track.category,
    )

    insert_idx = cursor if position == "next" else len(all_items)
    all_items.insert(insert_idx, new_item)
    _reposition_items(all_items)

    return JsonResponse({"ok": True, "item_id": new_item.id})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_seek(request):
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

    cmd_path = Path("/run/isadoraair/engine_cmd.json")
    cmd_path.write_text(json.dumps(cmd), encoding="utf-8")
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_deck_command(request, slot):
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

    cmd_path = Path("/run/isadoraair/engine_cmd.json")
    cmd_path.write_text(
        json.dumps({"command": f"deck_{action}", "slot": slot}),
        encoding="utf-8",
    )
    return JsonResponse({"ok": True})


@csrf_exempt
@require_http_methods(["POST"])
def api_engine_restart(request):
    """Manual recovery button for a hung engine — restarts the
    isadoraair-engine systemd unit. Fire-and-forget (Popen, not run):
    if the engine is genuinely deadlocked, systemd may need the full
    stop-timeout before SIGKILLing it, and this request shouldn't block
    a gunicorn worker for that long."""
    subprocess.Popen(["sudo", "systemctl", "restart", "isadoraair-engine"])
    return JsonResponse({"ok": True})


@require_http_methods(["GET"])
def api_engine_status(request):
    state_path = Path("/run/isadoraair/engine_state.json")
    if not state_path.is_file():
        return JsonResponse({"transport": "OFFLINE", "decks": {"A": None, "B": None}, "queue": []})
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if time_mod.time() - data.get("timestamp", 0) > 10:
            data["transport"] = "STALE"
        return JsonResponse(data)
    except Exception:
        return JsonResponse({"transport": "ERROR", "decks": {"A": None, "B": None}, "queue": []})


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
    categories = Category.objects.select_related("kind").order_by("kind__sort_order", "name")
    return render(request, "library/import.html", {"categories": categories})


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

    from library.management.commands.analyze_tracks import analyze_one_track, get_waveforms_dir
    from library.management.commands.import_songs import SUPPORTED_EXT, parse_tags
    from library.models import AnalysisConfig

    category_id = request.POST.get("category_id")
    if not category_id:
        return JsonResponse({"error": "category_id required"}, status=400)
    category = get_object_or_404(Category, pk=category_id)

    uploaded_files = request.FILES.getlist("files")
    if not uploaded_files:
        return JsonResponse({"error": "No files uploaded"}, status=400)

    library_root = Path(getattr(django_settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))
    dest_dir = library_root / category.code
    dest_dir.mkdir(parents=True, exist_ok=True)

    cfg = AnalysisConfig.load()
    cfg_values = (
        cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
        cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
        cfg.waveform_floor_db,
    )
    wave_dir = get_waveforms_dir()

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

        title = clean(tags.get("title")) or dest_path.stem
        artist_name = clean(tags.get("artist")) or "Unknown Artist"
        album_title = clean(tags.get("album")) or ""
        album_artist_name = clean(tags.get("album_artist")) or ""
        genre_name = clean(tags.get("genre")) or ""

        artist_obj, _ = Artist.objects.get_or_create(name=artist_name)

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
            title=clean(title),
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
        )

        # Analyze immediately (waveform + cue points) rather than leaving
        # a freshly-uploaded track without them until someone remembers to
        # run analyze_tracks separately -- calling the shared
        # analyze_one_track() directly (not the bulk command) targets
        # exactly this track, regardless of whether older unanalyzed
        # tracks exist elsewhere in the library.
        row = (track.id, track.filepath, track.filename, track.duration_seconds,
               track.title, artist_obj.name, "")
        analyzed = analyze_one_track(row, cfg_values, wave_dir, force=False)

        results.append({
            "filename": uploaded.name,
            "ok": True,
            "track_id": track.id,
            "title": track.title,
            "artist": artist_obj.name,
            "saved_as": dest_path.name,
            "analyzed": analyzed,
        })

    return JsonResponse({"results": results})
