import json
from datetime import time

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from django.core.paginator import Paginator
from django.db.models import Q

from .models import Category, Clock, ScheduleBlock, Track


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
