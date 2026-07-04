import random
from datetime import datetime, time, timedelta

from django.db.models import Count
from django.utils import timezone

from library.models import (
    Category,
    LogFillConfig,
    LogItem,
    PlaylistLog,
    RecencyConfig,
    ScheduleBlock,
    Track,
)


def resolve_schedule_block(target_date, hour):
    """Find the ScheduleBlock that applies to a given date+hour. A
    specific_date match always beats a recurring day_of_week match."""
    t = time(hour, 0)

    block = (
        ScheduleBlock.objects
        .filter(specific_date=target_date, start_time=t)
        .select_related("rotation", "playlist")
        .first()
    )
    if block:
        return block

    dow = target_date.weekday()
    block = (
        ScheduleBlock.objects
        .filter(day_of_week=dow, start_time=t, specific_date__isnull=True)
        .select_related("rotation", "playlist")
        .first()
    )
    return block


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


MAX_FILL_TRACKS = 200  # safety cap against a runaway loop on bad data


def _fill_remaining_hour(picks, accumulated_seconds, target_datetime):
    """Top up `picks` with fallback-category tracks (admin-configured via
    LogFillConfig) until the hour is filled, or as tightly as duration-fit
    allows. Called after any build path in case it comes up short of a
    full hour (e.g. a playlist/rotation that doesn't sum to 3600s)."""
    remaining = 3600 - accumulated_seconds
    if remaining <= DURATION_FIT_MARGIN:
        return picks, accumulated_seconds

    cfg = LogFillConfig.load()
    if cfg.strategy == "fixed_category":
        category = cfg.fallback_category
    else:
        category = picks[-1]["category"] if picks else None
    if category is None:
        return picks, accumulated_seconds

    recency_cfg = RecencyConfig.load()
    picked_tracks = [p["track"] for p in picks]
    picked_artist_ids = [p["track"].artist_id for p in picks]

    for _ in range(MAX_FILL_TRACKS):
        if remaining <= DURATION_FIT_MARGIN:
            break
        artist_sep, title_sep = get_separation(category, recency_cfg)
        exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
            target_datetime, artist_sep, title_sep, picked_tracks, picked_artist_ids,
        )
        track = pick_track(
            category, exclude_track_ids, exclude_artist_ids,
            artist_sep, title_sep, target_datetime,
            remaining_seconds=remaining,
        )
        if track is None:
            break  # nothing eligible even after loosening — stop gracefully

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
        remaining = 3600 - accumulated_seconds
        if track_duration <= 0:
            break  # avoid infinite loop on zero-duration data

    return picks, accumulated_seconds


def _build_from_rotation(target_date, hour, rotation):
    slots = list(rotation.slots.select_related("category").order_by("position"))
    if not slots:
        return None, f"Rotation '{rotation.name}' has no slots."

    recency_cfg = RecencyConfig.load()
    target_datetime = timezone.make_aware(
        datetime.combine(target_date, time(hour, 0))
    )

    picks = []
    picked_tracks = []
    picked_artist_ids = []
    accumulated_seconds = 0.0

    for slot in slots:
        category = slot.category
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

    picks, accumulated_seconds = _fill_remaining_hour(picks, accumulated_seconds, target_datetime)
    return _persist_log(target_date, hour, picks)


def _build_from_playlist(target_date, hour, playlist):
    items = list(
        playlist.items
        .select_related("track", "track__category")
        .order_by("position")
    )
    if not items:
        return None, f"Playlist '{playlist.name}' has no items."

    target_datetime = timezone.make_aware(
        datetime.combine(target_date, time(hour, 0))
    )

    picks = []
    accumulated_seconds = 0.0
    for item in items:
        track = item.track
        track_duration = track.next_start_seconds or track.duration_seconds or 0
        scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
        picks.append({
            "position": len(picks),
            "scheduled_time": scheduled_time,
            "track": track,
            "category": track.category,
        })
        accumulated_seconds += track_duration

    picks, accumulated_seconds = _fill_remaining_hour(picks, accumulated_seconds, target_datetime)
    return _persist_log(target_date, hour, picks)


def _persist_log(target_date, hour, picks):
    PlaylistLog.objects.filter(date=target_date, hour=hour).delete()

    log = PlaylistLog.objects.create(
        date=target_date,
        hour=hour,
        status="draft",
    )

    log_items = [
        LogItem(
            playlist_log=log,
            position=pick["position"],
            scheduled_time=pick["scheduled_time"],
            track=pick["track"],
            category=pick["category"],
        )
        for pick in picks
    ]
    LogItem.objects.bulk_create(log_items)
    return log, None


def build_hour_log(target_date, hour):
    block = resolve_schedule_block(target_date, hour)
    if block is None:
        return None, "No schedule block for this hour."

    if block.playlist_id:
        return _build_from_playlist(target_date, hour, block.playlist)
    if block.rotation_id:
        return _build_from_rotation(target_date, hour, block.rotation)

    return None, "ScheduleBlock has neither rotation nor playlist."
