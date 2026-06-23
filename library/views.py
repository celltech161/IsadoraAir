import json
import time as time_mod
from datetime import date as date_type, time
from pathlib import Path

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from django.core.paginator import Paginator
from django.db.models import Q

from django.shortcuts import get_object_or_404

from .models import Artist, Album, Category, Clock, Genre, LogItem, PlaylistLog, ScheduleBlock, Track
from .services.log_builder import build_hour_log


def dashboard_page(request):
    return render(request, "library/dashboard.html")


@ensure_csrf_cookie
def schedule_page(request):
    clocks = Clock.objects.all().order_by("name")
    return render(request, "library/schedule.html", {"clocks": clocks})


@require_http_methods(["GET", "POST"])
def api_schedule_list(request):
    if request.method == "GET":
        blocks = (
            ScheduleBlock.objects
            .filter(day_of_week__isnull=False)
            .select_related("clock")
            .order_by("day_of_week", "start_time")
        )
        data = [
            {
                "id": b.id,
                "day_of_week": b.day_of_week,
                "start_hour": b.start_time.hour,
                "clock_id": b.clock_id,
                "clock_name": b.clock.name,
            }
            for b in blocks
        ]
        return JsonResponse({"blocks": data})

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    day_of_week = body.get("day_of_week")
    hour = body.get("hour")
    clock_id = body.get("clock_id")

    if day_of_week is None or hour is None or clock_id is None:
        return JsonResponse({"error": "day_of_week, hour, and clock_id are required"}, status=400)

    if not (0 <= day_of_week <= 6):
        return JsonResponse({"error": "day_of_week must be 0-6"}, status=400)
    if not (0 <= hour <= 23):
        return JsonResponse({"error": "hour must be 0-23"}, status=400)

    try:
        clock = Clock.objects.get(id=clock_id)
    except Clock.DoesNotExist:
        return JsonResponse({"error": "Clock not found"}, status=404)

    block, created = ScheduleBlock.objects.update_or_create(
        day_of_week=day_of_week,
        start_time=time(hour, 0),
        specific_date=None,
        defaults={
            "end_time": time((hour + 1) % 24, 0),
            "clock": clock,
        },
    )

    return JsonResponse({
        "id": block.id,
        "day_of_week": block.day_of_week,
        "start_hour": block.start_time.hour,
        "clock_id": block.clock_id,
        "clock_name": clock.name,
        "created": created,
    })


@require_http_methods(["DELETE"])
def api_schedule_delete(request, pk):
    deleted, _ = ScheduleBlock.objects.filter(pk=pk).delete()
    return JsonResponse({"ok": True, "deleted": deleted > 0})


@require_http_methods(["GET"])
def api_clock_list(request):
    clocks = Clock.objects.all().order_by("name")
    data = [{"id": c.id, "name": c.name} for c in clocks]
    return JsonResponse({"clocks": data})


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
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(artist__name__icontains=q)
            | Q(album__title__icontains=q)
        )

    cat_id = request.GET.get("category")
    if cat_id:
        qs = qs.filter(category_id=cat_id)

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
    if log.status != "draft":
        return JsonResponse({"error": "Cannot modify an approved log"}, status=400)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    order = body.get("order")
    if not order or not isinstance(order, list):
        return JsonResponse({"error": "order (list of item IDs) is required"}, status=400)

    items = {item.id: item for item in log.items.all()}
    for pos, item_id in enumerate(order):
        if item_id in items:
            items[item_id].position = pos
    LogItem.objects.bulk_update(items.values(), ["position"])

    return JsonResponse(_log_to_dict(log))


@require_http_methods(["GET"])
def api_engine_status(request):
    state_path = Path("/run/isadoraair/engine_state.json")
    if not state_path.is_file():
        return JsonResponse({"transport": "OFFLINE", "now_playing": None, "next_up": None})
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if time_mod.time() - data.get("timestamp", 0) > 10:
            data["transport"] = "STALE"
        return JsonResponse(data)
    except Exception:
        return JsonResponse({"transport": "ERROR", "now_playing": None, "next_up": None})


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
