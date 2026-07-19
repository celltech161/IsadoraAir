import random
from datetime import datetime, time, timedelta

from django.db.models import Count, Q
from django.db.models.expressions import RawSQL
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


def _tracks_for_category(category, target_datetime=None):
    """Tracks eligible for this category's rotation -- either filed here
    as their primary category, or tagged via additional_categories (e.g.
    a blues-rock song filed under Blues that should also play in Rock).
    distinct() guards against a track appearing twice if it somehow
    matches both sides of the OR for the same category.

    target_datetime, when given, also excludes tracks blocked for that
    exact hour x day-of-week slot (Track.blocked_slots). This is a hard
    constraint just like ready2air -- callers that just need overall
    category size (e.g. get_separation's proportional recency scaling)
    should leave target_datetime unset, since "how big is this category"
    shouldn't shrink depending on what hour it's evaluated at."""
    qs = Track.objects.filter(
        Q(category=category) | Q(additional_categories=category),
        ready2air=True,
    ).distinct()
    if target_datetime is not None:
        slot = target_datetime.weekday() * 24 + target_datetime.hour
        qs = qs.exclude(blocked_slots__contains=[slot])
    return qs


def get_separation(category, recency_cfg):
    artist_sep = category.artist_separation
    if artist_sep is None:
        artist_sep = recency_cfg.artist_separation

    title_sep = category.title_separation
    if title_sep is None:
        title_sep = recency_cfg.title_separation

    if category.recency_mode == "proportional":
        cat_count = _tracks_for_category(category).count()
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

    # played_at__isnull=False: only items that actually aired count as
    # "played" for recency-window purposes. LogItems that were picked
    # into an hour's log but never reached _create_deck (e.g. the hour
    # rolled over first) DON'T contribute to the exclusion set --
    # otherwise a track we NEVER PLAYED would still block itself from
    # being re-picked in the next hour or two, which is exactly the
    # opposite of what recency separation is supposed to do.
    # _create_deck writes played_at when it commits a track to a deck,
    # BEFORE the audio actually starts, so an in-preroll pick counts
    # normally.
    recent_items = (
        LogItem.objects
        .filter(
            scheduled_time__gte=cutoff, scheduled_time__lt=target_datetime,
            played_at__isnull=False,
        )
        .select_related("track", "track__artist")
    )

    artist_cutoff = target_datetime - timedelta(hours=artist_sep_hours)
    title_cutoff = target_datetime - timedelta(hours=title_sep_hours)

    for item in recent_items:
        if not item.track_id:
            # track was deleted after this LogItem aired (e.g. a library
            # cleanup) -- nothing left to exclude by track/artist id.
            continue
        if item.scheduled_time >= title_cutoff:
            exclude_track_ids.add(item.track_id)
        if item.scheduled_time >= artist_cutoff:
            exclude_artist_ids.add(item.track.artist_id)

    return exclude_track_ids, exclude_artist_ids


DURATION_FIT_THRESHOLD = 480  # start fitting when < 8 minutes remain
DURATION_FIT_MARGIN = 30     # acceptable overshoot in seconds


def _weighted_order(qs):
    """Random selection weighted by Track.rotation_weight (0-5, default 3)
    and by dormancy (hours since Track.last_played_at, updated live by
    engine.py on every real play) -- so among tracks that have already
    cleared their recency-separation window (the hard exclusion this
    queryset was already filtered by), ones that have sat idle longer are
    proportionally more likely to come up, without turning selection into
    a strict least-recently-played queue (which would sound mechanical).

    rotation_weight is shifted by +1 so weight 0 is never a hard
    zero-probability tier -- ready2air is what actually excludes a track,
    weight just makes 0 six times less likely to come up than 5, all else
    equal. Verified empirically (not just by the math) against real
    category data: 3000 draws over a weight-0 group vs a weight-5 group
    landed at a ~5.76x ratio, matching the expected 6x.

    Dormancy is folded in as a second multiplier, log-dampened
    (`1 + LN(1 + hours)`) so it grows with diminishing returns rather than
    linearly -- a track idle 30 days should be meaningfully favored over
    one idle 2 days, but not ~15x favored, which would let dormancy swamp
    rotation_weight entirely and just recreate the "sounds mechanical"
    problem from the other direction (an oldest-first queue instead of a
    flat random one). Never-played tracks (last_played_at IS NULL) are
    treated as idle 365 days -- a large but finite dormancy so genuinely
    ancient tracks (idle *longer* than a year) can still outrank a
    brand-new, never-aired addition.

    `-LN(RANDOM()) / weight` is the standard SQL-side trick for weighted
    sampling without materializing/shuffling anything -- same query cost
    as the plain `order_by("?")` it replaces."""
    return qs.order_by(RawSQL(
        "-LN(RANDOM()) / ((rotation_weight + 1) * (1 + LN(1 + "
        "EXTRACT(EPOCH FROM (NOW() - COALESCE(last_played_at, "
        "NOW() - INTERVAL '365 days'))) / 3600.0)))",
        [],
    ))


def _pick_best_fit(qs, remaining_seconds):
    """From a queryset of eligible tracks, pick the one whose duration
    best fills the remaining time without overshooting too much."""
    candidates = list(
        _weighted_order(qs).values_list("id", "next_start_seconds", "duration_seconds")[:200]
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
               remaining_seconds=None, max_loosening=3,
               hard_exclude_track_ids=None, hard_exclude_artist_ids=None):
    """`exclude_*` are RECENCY-HISTORY exclusions -- they get progressively
    dropped by the loosening loop below if no candidate can be found.

    `hard_exclude_*` are "already-picked in THIS build" exclusions -- they
    MUST hold through every loosening pass, otherwise a category with few
    eligible tracks (or a fresh build whose picks aren't yet persisted as
    LogItems) can pick the same track for multiple slots in the same
    hour. Real bug caught live 2026-07-17 Floydian Slip 21:00: three
    Local Drops slots all landed on the same "Saturdays at Noon" promo
    because time-based sep excluded 18/19 candidates, the loose retry
    reset `exclude_track_ids` to LogItem-history only, and the current
    hour's picks weren't yet in LogItem so the accumulator was invisible.
    """
    fit_mode = remaining_seconds is not None and remaining_seconds < DURATION_FIT_THRESHOLD
    hard_exclude_track_ids = set(hard_exclude_track_ids or ())
    hard_exclude_artist_ids = set(hard_exclude_artist_ids or ())

    # Category-level "no separation" opt-outs. When the resolved
    # separation for this pick is 0 (either via the category's
    # explicit override or a global default of 0), that dimension is
    # OFF for both the picker side and the emitter side -- see the
    # callers, which also skip appending to picked_tracks /
    # picked_artist_ids when the same is true. Setting title_sep=0 on
    # e.g. WxTemp (single-track weather callout that legitimately
    # airs multiple times per hour) makes both slots pick the same
    # track without complaint; setting artist_sep=0 on WxObs, WxTemp,
    # WxForecast (all voiced by the "Oak Grove Radio" house artist,
    # same artist as Legal ID) makes them pickable in the same hour
    # as any KOGR-LP variant.
    if title_sep == 0:
        hard_exclude_track_ids = set()
    if artist_sep == 0:
        hard_exclude_artist_ids = set()

    def _build_qs():
        qs = _tracks_for_category(category, target_datetime=target_datetime)
        combined_tracks = set(exclude_track_ids) | hard_exclude_track_ids
        combined_artists = set(exclude_artist_ids) | hard_exclude_artist_ids
        if combined_tracks:
            qs = qs.exclude(id__in=combined_tracks)
        if combined_artists:
            qs = qs.exclude(artist_id__in=combined_artists)
        return qs

    for attempt in range(max_loosening + 1):
        qs = _build_qs()
        if fit_mode:
            track = _pick_best_fit(qs, remaining_seconds)
        else:
            track = _weighted_order(qs).first()

        if track:
            return track

        artist_sep = artist_sep / 2.0
        title_sep = title_sep / 2.0

        loosened_exclude_tracks = set()
        loosened_exclude_artists = set()

        if artist_sep > 0 or title_sep > 0:
            cutoff = target_datetime - timedelta(hours=max(artist_sep, title_sep))
            # See get_recent_exclusions for why played_at__isnull=False
            # is on this query too -- symmetry.
            recent = (
                LogItem.objects
                .filter(
                    scheduled_time__gte=cutoff, scheduled_time__lt=target_datetime,
                    played_at__isnull=False,
                )
                .select_related("track")
            )
            artist_cutoff = target_datetime - timedelta(hours=artist_sep)
            title_cutoff = target_datetime - timedelta(hours=title_sep)
            for item in recent:
                if not item.track_id:
                    continue
                if item.scheduled_time >= title_cutoff:
                    loosened_exclude_tracks.add(item.track_id)
                if item.scheduled_time >= artist_cutoff:
                    loosened_exclude_artists.add(item.track.artist_id)

        # Only the recency-history part gets rebuilt; hard exclusions
        # (caller's accumulator of already-picked-in-this-build tracks)
        # are preserved via _build_qs() unioning them back in.
        exclude_track_ids = loosened_exclude_tracks
        exclude_artist_ids = loosened_exclude_artists

    # Final pass: drop history exclusions AND hard artist separation,
    # keep only hard TRACK exclusion. Rationale: the two hard exclusions
    # have different priorities. "Don't play the same track twice in
    # one hour" is a strong invariant we should never violate. "Don't
    # play the same artist too close together" is a soft preference
    # that should yield rather than have a slot silently skipped.
    #
    # Real bug this covers: WxObs (and every other single-track
    # "Oak Grove Radio"-branded category -- Legal ID, WxTemp,
    # WxForecast, WxAlert, imaging tags...) has one track, and after
    # any earlier same-hour pick by the same house artist it would sit
    # blocked forever with no way to survive. Well-populated music
    # categories still see full artist separation on earlier attempts;
    # only pool-of-1 branded categories reach this fallback.
    exclude_track_ids = set()
    exclude_artist_ids = set()
    hard_exclude_artist_ids = set()
    qs = _build_qs()
    if fit_mode:
        return _pick_best_fit(qs, remaining_seconds)
    return _weighted_order(qs).first()


MAX_FILL_TRACKS = 200  # safety cap against a runaway loop on bad data


def fill_remaining_hour(picks, accumulated_seconds, target_datetime):
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
            hard_exclude_track_ids={t.id for t in picked_tracks},
            hard_exclude_artist_ids=set(picked_artist_ids),
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
        # sep==0 opts this pick out of contributing to subsequent slots'
        # exclusions on that dimension -- see pick_track's header for
        # the "WxTemp twice per hour" / "WxObs same-artist-as-Legal-ID"
        # semantics.
        if title_sep > 0:
            picked_tracks.append(track)
        if artist_sep > 0:
            picked_artist_ids.append(track.artist_id)
        accumulated_seconds += track_duration
        remaining = 3600 - accumulated_seconds
        if track_duration <= 0:
            break  # avoid infinite loop on zero-duration data

    return picks, accumulated_seconds


def _build_from_rotation(target_date, hour, rotation):
    slots = list(rotation.slots.select_related("category", "track", "track__category").order_by("position"))
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
        if slot.track_id:
            # Direct track insert — the hybrid rotation/playlist ask.
            # Skips recency separation entirely on the way in, even for a
            # music track that would otherwise violate it; the LogItem it
            # still produces below (with a real scheduled_time) is what
            # makes it count as "recently played" for any category slots
            # that come after it, in this build or future ones.
            track = slot.track
            category = track.category
        else:
            category = slot.category

        # Effective separation for THIS slot's category. Used both to
        # gate the pick (else branch below) AND to gate this slot's
        # contribution to subsequent slots' accumulator exclusions
        # (bottom of the loop). Computed once regardless of branch so
        # a direct-track slot backed by e.g. a WxTemp track with
        # title_sep=0 also plays correctly with later WxTemp/Legal ID
        # slots.
        artist_sep, title_sep = get_separation(category, recency_cfg)

        if not slot.track_id:
            exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
                target_datetime, artist_sep, title_sep,
                picked_tracks, picked_artist_ids,
            )

            remaining = 3600 - accumulated_seconds
            track = pick_track(
                category, exclude_track_ids, exclude_artist_ids,
                artist_sep, title_sep, target_datetime,
                remaining_seconds=remaining,
                hard_exclude_track_ids={t.id for t in picked_tracks},
                hard_exclude_artist_ids=set(picked_artist_ids),
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

        # sep==0 on this slot's category means it doesn't participate
        # in subsequent slots' exclusions on that dimension. See
        # pick_track's header for the semantics.
        if title_sep > 0:
            picked_tracks.append(track)
        if artist_sep > 0:
            picked_artist_ids.append(track.artist_id)
        accumulated_seconds += track_duration

        if accumulated_seconds >= 3600:
            break

    picks, accumulated_seconds = fill_remaining_hour(picks, accumulated_seconds, target_datetime)
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

    picks, accumulated_seconds = fill_remaining_hour(picks, accumulated_seconds, target_datetime)
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
            track_title=pick["track"].title,
            track_artist=pick["track"].artist.name if pick["track"].artist_id else "",
            category=pick["category"],
        )
        for pick in picks
    ]
    LogItem.objects.bulk_create(log_items)
    return log, None


def append_fill_items(log, picks, start_position):
    """Persist new LogItems appended to an *already-existing* PlaylistLog,
    starting at `start_position` — unlike _persist_log, this does not
    touch any existing items. Used to extend a log that's already
    approved and live/currently-playing (see engine.py's
    _extend_current_log_live), where deleting and recreating everything
    would discard real played_at history driving recency avoidance."""
    log_items = [
        LogItem(
            playlist_log=log,
            position=start_position + i,
            scheduled_time=pick["scheduled_time"],
            track=pick["track"],
            track_title=pick["track"].title,
            track_artist=pick["track"].artist.name if pick["track"].artist_id else "",
            category=pick["category"],
        )
        for i, pick in enumerate(picks)
    ]
    LogItem.objects.bulk_create(log_items)
    return log_items


def build_hour_log(target_date, hour):
    block = resolve_schedule_block(target_date, hour)
    if block is None:
        return None, "No schedule block for this hour."

    if block.playlist_id:
        return _build_from_playlist(target_date, hour, block.playlist)
    if block.rotation_id:
        return _build_from_rotation(target_date, hour, block.rotation)

    return None, "ScheduleBlock has neither rotation nor playlist."


def _describe_track_issues(track, prefix, issues):
    label = f"{track.artist.name if track.artist_id else 'Unknown'} - {track.title}"
    if not track.duration_seconds:
        issues.append({"severity": "error", "message": f"{prefix}: '{label}' has no duration set — likely never analyzed."})
    elif track.next_start_seconds is None:
        issues.append({"severity": "warning", "message": f"{prefix}: '{label}' has no next-start (cue) point — will play full length with no auto-mix."})
    if not track.waveform_path:
        issues.append({"severity": "warning", "message": f"{prefix}: '{label}' has no waveform on file."})
    if not track.ready2air:
        issues.append({"severity": "warning", "message": f"{prefix}: '{label}' is not marked ready2air."})


def preview_hour_log(target_date, hour):
    """Read-only dry run of the same schedule-block/rotation/playlist walk
    as build_hour_log, for health-checking a rotation or playlist without
    touching PlaylistLog/LogItem at all -- never persists anything, so it
    can never disturb a real (possibly already-on-air) log for this date
    and hour. Category slots still use weighted random selection, so the
    exact tracks picked here won't necessarily match a real build (or
    what actually aired) -- this is for surfacing structural problems
    (empty categories, missing analysis, an under-filled hour), not for
    previewing an exact future log."""
    issues = []
    block = resolve_schedule_block(target_date, hour)
    if block is None:
        issues.append({"severity": "error", "message": f"No schedule block covers {target_date} hour {hour}."})
        return {
            "date": target_date.isoformat(), "hour": hour, "source": None,
            "source_name": None, "items": [], "issues": issues, "total_seconds": 0,
        }, None

    target_datetime = timezone.make_aware(datetime.combine(target_date, time(hour, 0)))
    picks = []
    accumulated_seconds = 0.0
    source = None
    source_name = None
    walked = False

    if block.playlist_id:
        source = "playlist"
        source_name = block.playlist.name
        items = list(
            block.playlist.items
            .select_related("track", "track__artist", "track__category")
            .order_by("position")
        )
        if not items:
            issues.append({"severity": "error", "message": f"Playlist '{block.playlist.name}' has no items."})
        else:
            walked = True
        for item in items:
            track = item.track
            _describe_track_issues(track, f"Playlist position {item.position + 1}", issues)
            track_duration = track.next_start_seconds or track.duration_seconds or 0
            scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
            picks.append({
                "position": len(picks), "scheduled_time": scheduled_time,
                "track": track, "category": track.category,
            })
            accumulated_seconds += track_duration

    elif block.rotation_id:
        source = "rotation"
        source_name = block.rotation.name
        slots = list(
            block.rotation.slots
            .select_related("category", "track", "track__category", "track__artist")
            .order_by("position")
        )
        if not slots:
            issues.append({"severity": "error", "message": f"Rotation '{block.rotation.name}' has no slots."})
        else:
            walked = True

        recency_cfg = RecencyConfig.load()
        picked_tracks = []
        picked_artist_ids = []

        for idx, slot in enumerate(slots):
            if slot.track_id:
                track = slot.track
                category = track.category
            else:
                category = slot.category
            # See _build_from_rotation -- effective separation for THIS
            # slot's category, computed once per iteration and used for
            # both the pick and the emit gate below.
            artist_sep, title_sep = get_separation(category, recency_cfg)

            if not slot.track_id:
                exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
                    target_datetime, artist_sep, title_sep, picked_tracks, picked_artist_ids,
                )
                remaining = 3600 - accumulated_seconds
                track = pick_track(
                    category, exclude_track_ids, exclude_artist_ids,
                    artist_sep, title_sep, target_datetime,
                    remaining_seconds=remaining,
                    hard_exclude_track_ids={t.id for t in picked_tracks},
                    hard_exclude_artist_ids=set(picked_artist_ids),
                )
                if track is None:
                    pool_size = _tracks_for_category(category, target_datetime=target_datetime).count()
                    if pool_size == 0:
                        issues.append({
                            "severity": "error",
                            "message": f"Slot {idx + 1}: category '{category.name}' has zero eligible tracks (empty, or none marked ready2air).",
                        })
                    else:
                        issues.append({
                            "severity": "warning",
                            "message": f"Slot {idx + 1}: category '{category.name}' had no track survive recency separation (pool of {pool_size}).",
                        })
                    continue

            _describe_track_issues(track, f"Slot {idx + 1} ({category.name if category else '?'})", issues)

            track_duration = track.next_start_seconds or track.duration_seconds or 0
            scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
            picks.append({
                "position": len(picks), "scheduled_time": scheduled_time,
                "track": track, "category": category,
            })
            if title_sep > 0:
                picked_tracks.append(track)
            if artist_sep > 0:
                picked_artist_ids.append(track.artist_id)
            accumulated_seconds += track_duration
            if accumulated_seconds >= 3600:
                break

    else:
        issues.append({"severity": "error", "message": "Schedule block has neither rotation nor playlist configured."})

    if walked:
        picks, accumulated_seconds = fill_remaining_hour(picks, accumulated_seconds, target_datetime)

        shortfall = 3600 - accumulated_seconds
        if shortfall > DURATION_FIT_MARGIN:
            issues.append({
                "severity": "warning",
                "message": f"Hour only {int(accumulated_seconds)}s of 3600s filled ({int(shortfall)}s short) — filler category may also be exhausted.",
            })
        elif accumulated_seconds - 3600 > 300:
            issues.append({
                "severity": "warning",
                "message": f"Hour overshoots by {int(accumulated_seconds - 3600)}s.",
            })

    seen_counts = {}
    for p in picks:
        seen_counts[p["track"].id] = seen_counts.get(p["track"].id, 0) + 1
    flagged_dupes = set()
    for p in picks:
        tid = p["track"].id
        if seen_counts[tid] > 1 and tid not in flagged_dupes:
            flagged_dupes.add(tid)
            t = p["track"]
            issues.append({
                "severity": "warning",
                "message": f"'{t.artist.name if t.artist_id else 'Unknown'} - {t.title}' is scheduled {seen_counts[tid]} times in this hour.",
            })

    result = {
        "date": target_date.isoformat(),
        "hour": hour,
        "source": source,
        "source_name": source_name,
        "total_seconds": accumulated_seconds,
        "issues": issues,
        "items": [
            {
                "position": p["position"],
                "scheduled_time": p["scheduled_time"].isoformat(),
                "track_id": p["track"].id,
                "title": p["track"].title,
                "artist": p["track"].artist.name if p["track"].artist_id else "",
                "category": p["category"].code if p["category"] else "",
                "duration": p["track"].next_start_seconds or p["track"].duration_seconds or 0,
            }
            for p in picks
        ],
    }
    return result, None
