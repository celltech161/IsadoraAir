import json
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import LogItem
from webrequests.models import SongRequest, WebRequestConfig


class Command(BaseCommand):
    """Re-evaluates every non-terminal SongRequest on a timer (systemd),
    independent of both the polling scripts and the live engine:

      1. unavailable -- track disappeared or fell out of eligibility
         (deleted, ready2air flipped off, recategorized out of music)
         since the request was made.
      2. expired -- sitting past WebRequestConfig.expire_after_hours
         with no track-level problem, just never got a turn.
      3. Everything left over is FIFO-matched (oldest submitted_at
         first) against upcoming open, music-kind LogItems within
         lookahead_warning_minutes, capped per hour by
         max_fulfilled_per_hour (minus whatever's already fulfilled
         into that hour) -- giving each a pending/no_slot_soon status
         plus an advisory estimated_play_time.

    This is purely a read of already-built PlaylistLog/LogItem rows and
    a write to SongRequest -- it never touches the running engine, and
    the FIFO match here is only ever a preview. The actual
    slot-claiming happens at engine queue-advance time (a separate,
    not-yet-built piece) and can land a request in a different slot
    than this estimate guessed."""

    help = "Re-evaluate pending SongRequest statuses (unavailable/expired/pending/no_slot_soon) and estimated_play_time."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.load()
        now = timezone.now()
        open_slots = set(cfg.open_slots)

        pending = list(
            SongRequest.objects.filter(status__in=SongRequest.NON_TERMINAL_STATUSES)
            .select_related("track", "track__category", "track__category__kind")
            .order_by("submitted_at")
        )

        expire_cutoff = now - timedelta(hours=cfg.expire_after_hours)
        still_pending = []
        resolved_count = 0

        for req in pending:
            track = req.track
            ineligible = (
                track is None
                or not track.ready2air
                or track.category_id is None
                or track.category.kind.code != "music"
            )
            if ineligible:
                req.status = "unavailable"
                req.estimated_play_time = None
                req.resolved_at = now
                req.save(update_fields=["status", "estimated_play_time", "resolved_at"])
                resolved_count += 1
            elif req.submitted_at <= expire_cutoff:
                req.status = "expired"
                req.estimated_play_time = None
                req.resolved_at = now
                req.save(update_fields=["status", "estimated_play_time", "resolved_at"])
                resolved_count += 1
            else:
                still_pending.append(req)

        lookahead_cutoff = now + timedelta(minutes=cfg.lookahead_warning_minutes)
        candidates = (
            LogItem.objects.filter(
                scheduled_time__gte=now, scheduled_time__lt=lookahead_cutoff,
                played_at__isnull=True, category__kind__code="music",
            )
            .order_by("scheduled_time")
        )

        already_fulfilled_per_hour = defaultdict(int)
        fulfilled_log_ids = SongRequest.objects.filter(
            status="fulfilled", log_item__playlist_log_id__isnull=False,
            log_item__scheduled_time__gte=now, log_item__scheduled_time__lt=lookahead_cutoff,
        ).values_list("log_item__playlist_log_id", flat=True)
        for playlist_log_id in fulfilled_log_ids:
            already_fulfilled_per_hour[playlist_log_id] += 1

        remaining_capacity = defaultdict(lambda: cfg.max_fulfilled_per_hour)
        for playlist_log_id, count in already_fulfilled_per_hour.items():
            remaining_capacity[playlist_log_id] = max(0, cfg.max_fulfilled_per_hour - count)

        slot_pool = []
        used_per_hour = defaultdict(int)
        for item in candidates:
            local_dt = timezone.localtime(item.scheduled_time)
            slot = local_dt.weekday() * 24 + local_dt.hour
            if slot not in open_slots:
                continue
            if used_per_hour[item.playlist_log_id] >= remaining_capacity[item.playlist_log_id]:
                continue
            used_per_hour[item.playlist_log_id] += 1
            slot_pool.append(item.scheduled_time)

        slot_iter = iter(slot_pool)
        for req in still_pending:
            estimate = next(slot_iter, None)
            new_status = "pending" if estimate is not None else "no_slot_soon"
            if new_status != req.status or estimate != req.estimated_play_time:
                req.status = new_status
                req.estimated_play_time = estimate
                req.save(update_fields=["status", "estimated_play_time"])

        self.stdout.write(json.dumps({
            "checked": len(pending),
            "resolved": resolved_count,
            "still_pending": len(still_pending),
            "slots_available": len(slot_pool),
        }))
