import random
from datetime import datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

from library.models import (
    Category,
    LogItem,
    PlaylistLog,
    RecencyConfig,
    RotationSlot,
    ScheduleBlock,
    Track,
)


def resolve_clock(target_date, hour):
    t = time(hour, 0)

    block = (
        ScheduleBlock.objects
        .filter(specific_date=target_date, start_time=t)
        .select_related("clock")
        .first()
    )
    if block:
        return block.clock

    dow = target_date.weekday()
    block = (
        ScheduleBlock.objects
        .filter(day_of_week=dow, start_time=t, specific_date__isnull=True)
        .select_related("clock")
        .first()
    )
    if block:
        return block.clock

    return None


def weighted_pick(slots):
    if not slots:
        return None
    total = sum(s.weight for s in slots)
    if total <= 0:
        return random.choice(slots)
    r = random.uniform(0, total)
    cumulative = 0
    for slot in slots:
        cumulative += slot.weight
        if r <= cumulative:
            return slot
    return slots[-1]


def get_separation(category, recency_cfg):
    artist_sep = category.artist_separation
    if artist_sep is None:
        artist_sep = recency_cfg.artist_separation

    title_sep = category.title_separation
    if title_sep is None:
        title_sep = recency_cfg.title_separation

    if category.recency_mode == "proportional":
        cat_count = Track.objects.filter(
            category=category, ready2air=True
        ).count()
        if cat_count > 0:
            scale = min(cat_count / 500.0, 1.0)
            artist_sep = artist_sep * scale
            title_sep = title_sep * scale

    return artist_sep, title_sep


def get_recent_exclusions(target_datetime, artist_sep_hours, title_sep_hours,
                          already_picked_tracks, already_picked_artists):
    exclude_track_ids = set(t.id for t in already_picked_tracks)
    exclude_artist_ids = set(a_id for a_id in already_picked_artists)

    max_lookback = max(artist_sep_hours, title_sep_hours)
    if max_lookback <= 0:
        return exclude_track_ids, exclude_artist_ids

    cutoff = target_datetime - timedelta(hours=max_lookback)

    recent_items = (
        LogItem.objects
        .filter(scheduled_time__gte=cutoff, scheduled_time__lt=target_datetime)
        .select_related("track", "track__artist")
    )

    artist_cutoff = target_datetime - timedelta(hours=artist_sep_hours)
    title_cutoff = target_datetime - timedelta(hours=title_sep_hours)

    for item in recent_items:
        if item.scheduled_time >= title_cutoff:
            exclude_track_ids.add(item.track_id)
        if item.scheduled_time >= artist_cutoff:
            exclude_artist_ids.add(item.track.artist_id)

    return exclude_track_ids, exclude_artist_ids


DURATION_FIT_THRESHOLD = 480  # start fitting when < 8 minutes remain
DURATION_FIT_MARGIN = 30     # acceptable overshoot in seconds


def _pick_best_fit(qs, remaining_seconds):
    """From a queryset of eligible tracks, pick the one whose duration
    best fills the remaining time without overshooting too much."""
    candidates = list(
        qs.values_list("id", "next_start_seconds", "duration_seconds")[:200]
    )
    if not candidates:
        return None

    def track_dur(c):
        return c[1] or c[2] or 0

    scored = []
    for c in candidates:
        dur = track_dur(c)
        diff = remaining_seconds - dur
        if diff >= -DURATION_FIT_MARGIN:
            scored.append((abs(diff), c[0]))

    if scored:
        scored.sort(key=lambda x: x[0])
        best_ids = [s[1] for s in scored[:5]]
        pick_id = random.choice(best_ids)
    else:
        pick_id = random.choice(candidates)[0]

    return Track.objects.get(id=pick_id)


def pick_track(category, exclude_track_ids, exclude_artist_ids,
               artist_sep, title_sep, target_datetime,
               remaining_seconds=None, max_loosening=3):
    fit_mode = remaining_seconds is not None and remaining_seconds < DURATION_FIT_THRESHOLD

    for attempt in range(max_loosening + 1):
        qs = Track.objects.filter(category=category, ready2air=True)

        if exclude_track_ids:
            qs = qs.exclude(id__in=exclude_track_ids)
        if exclude_artist_ids:
            qs = qs.exclude(artist_id__in=exclude_artist_ids)

        if fit_mode:
            track = _pick_best_fit(qs, remaining_seconds)
        else:
            track = qs.order_by("?").first()

        if track:
            return track

        artist_sep = artist_sep / 2.0
        title_sep = title_sep / 2.0

        loosened_exclude_tracks = set()
        loosened_exclude_artists = set()

        if artist_sep > 0 or title_sep > 0:
            cutoff = target_datetime - timedelta(hours=max(artist_sep, title_sep))
            recent = (
                LogItem.objects
                .filter(scheduled_time__gte=cutoff, scheduled_time__lt=target_datetime)
                .select_related("track")
            )
            artist_cutoff = target_datetime - timedelta(hours=artist_sep)
            title_cutoff = target_datetime - timedelta(hours=title_sep)
            for item in recent:
                if item.scheduled_time >= title_cutoff:
                    loosened_exclude_tracks.add(item.track_id)
                if item.scheduled_time >= artist_cutoff:
                    loosened_exclude_artists.add(item.track.artist_id)

        exclude_track_ids = loosened_exclude_tracks
        exclude_artist_ids = loosened_exclude_artists

    qs = Track.objects.filter(category=category, ready2air=True)
    if fit_mode:
        return _pick_best_fit(qs, remaining_seconds)
    return qs.order_by("?").first()


def build_hour_log(target_date, hour):
    clock = resolve_clock(target_date, hour)
    if clock is None:
        return None, "No clock assigned for this hour."

    clock_slots = list(
        clock.slots.select_related("rotation").order_by("position")
    )
    if not clock_slots:
        return None, f"Clock '{clock.name}' has no slots."

    recency_cfg = RecencyConfig.load()
    target_datetime = timezone.make_aware(
        datetime.combine(target_date, time(hour, 0))
    )

    picks = []
    picked_tracks = []
    picked_artist_ids = []
    accumulated_seconds = 0.0

    for clock_slot in clock_slots:
        rotation = clock_slot.rotation
        active_slots = list(
            rotation.slots.filter(active=True).select_related("category")
        )
        if not active_slots:
            continue

        rot_slot = weighted_pick(active_slots)
        category = rot_slot.category

        artist_sep, title_sep = get_separation(category, recency_cfg)

        exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
            target_datetime, artist_sep, title_sep,
            picked_tracks, picked_artist_ids,
        )

        remaining = 3600 - accumulated_seconds
        track = pick_track(
            category, exclude_track_ids, exclude_artist_ids,
            artist_sep, title_sep, target_datetime,
            remaining_seconds=remaining,
        )

        if track is None:
            continue

        track_duration = track.next_start_seconds or track.duration_seconds or 0
        scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)

        picks.append({
            "position": len(picks),
            "scheduled_time": scheduled_time,
            "track": track,
            "category": category,
        })

        picked_tracks.append(track)
        picked_artist_ids.append(track.artist_id)
        accumulated_seconds += track_duration

        if accumulated_seconds >= 3600:
            break

    PlaylistLog.objects.filter(date=target_date, hour=hour).delete()

    log = PlaylistLog.objects.create(
        date=target_date,
        hour=hour,
        status="draft",
    )

    log_items = []
    for pick in picks:
        log_items.append(LogItem(
            playlist_log=log,
            position=pick["position"],
            scheduled_time=pick["scheduled_time"],
            track=pick["track"],
            category=pick["category"],
        ))
    LogItem.objects.bulk_create(log_items)

    return log, None
