import json
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import LogItem, RecencyConfig
from webrequests.models import SongRequest, WebRequestConfig
from webrequests.services import estimate_air_time, is_track_eligible_at, maybe_fulfill_song_request


class Command(BaseCommand):
    """Re-evaluates every non-terminal SongRequest on a timer (systemd),
    independent of the polling scripts:

      1. unavailable -- track disappeared or fell out of eligibility
         (deleted, ready2air flipped off, recategorized out of music)
         since the request was made.
      2. expired -- sitting past WebRequestConfig.expire_after_hours
         with no track-level problem, just never got a turn.
      3. Proactively fulfills: walks every upcoming open, music-kind,
         not-yet-played LogItem in order and tries to swap in the
         oldest eligible request via webrequests.services.
         maybe_fulfill_song_request. This is what makes a fulfilled
         request show up in "Up Next" right away instead of only at
         the literal moment it airs -- the running engine's own
         _reload_queue_if_changed() (engine.py) picks up this DB-side
         swap within a few seconds. The engine calls the same function
         itself too, once, right before a slot actually plays -- a
         last-second safety net for anything that became eligible too
         recently (recency window just cleared, a slot just opened)
         for this pass to have caught it yet. Idempotent between the
         two callers and across repeated runs of this command.
      4. Everything still left over (couldn't be fulfilled above) is
         matched (oldest submitted_at first) against remaining open,
         music-kind LogItems within lookahead_warning_minutes, capped
         per hour by max_fulfilled_per_hour (minus whatever's now
         actually fulfilled into that hour) -- giving each a pending/
         no_slot_soon status plus an advisory estimated_play_time.
         Each candidate slot is checked with the SAME eligibility+
         recency test (is_track_eligible_at) real fulfillment uses,
         evaluated against that slot's own scheduled_time rather than
         "now" -- a request whose track is recency-blocked right now
         (e.g. the same artist just aired) is skipped past to a later
         slot instead of being handed a near-term estimate recency
         would never actually let it keep. Still only ever a preview:
         since a real fulfillment can happen for any request at any
         later cycle, an advisory estimate can end up not matching
         which slot a request actually lands in."""

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

        fulfillment_candidates = (
            LogItem.objects.filter(
                scheduled_time__gte=now, scheduled_time__lt=lookahead_cutoff,
                played_at__isnull=True, category__kind__code="music",
            )
            .order_by("scheduled_time")
        )
        for item in fulfillment_candidates:
            maybe_fulfill_song_request(item)

        # Re-check: the proactive pass above may have just fulfilled
        # some of these.
        still_pending_ids = [req.id for req in still_pending]
        still_pending = list(
            SongRequest.objects.filter(
                id__in=still_pending_ids, status__in=SongRequest.NON_TERMINAL_STATUSES,
            )
            .select_related("track", "track__category", "track__category__kind")
            .order_by("submitted_at")
        )
        fulfilled_now_count = len(still_pending_ids) - len(still_pending)

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

        open_candidates = []
        for item in candidates:
            local_dt = timezone.localtime(item.scheduled_time)
            slot = local_dt.weekday() * 24 + local_dt.hour
            if slot in open_slots:
                open_candidates.append(item)

        recency_cfg = RecencyConfig.load()
        claimed_item_ids = set()
        used_per_hour = defaultdict(int)
        slots_offered = 0

        for req in still_pending:
            estimate = None
            for item in open_candidates:
                if item.id in claimed_item_ids:
                    continue
                if used_per_hour[item.playlist_log_id] >= remaining_capacity[item.playlist_log_id]:
                    continue
                if req.track is None or not is_track_eligible_at(req.track, item.scheduled_time, recency_cfg):
                    continue
                estimate = estimate_air_time(item)
                claimed_item_ids.add(item.id)
                used_per_hour[item.playlist_log_id] += 1
                slots_offered += 1
                break

            new_status = "pending" if estimate is not None else "no_slot_soon"
            if new_status != req.status or estimate != req.estimated_play_time:
                req.status = new_status
                req.estimated_play_time = estimate
                req.save(update_fields=["status", "estimated_play_time"])

        # Fulfilled-but-not-yet-aired requests: keep estimated_play_time
        # live-accurate as real playback drifts from the schedule it was
        # built with, instead of freezing it at whatever the engine's
        # ETA happened to be the moment fulfillment occurred.
        still_airing = (
            SongRequest.objects.filter(status="fulfilled", log_item__played_at__isnull=True)
            .select_related("log_item")
        )
        for req in still_airing:
            live_estimate = estimate_air_time(req.log_item)
            if live_estimate != req.estimated_play_time:
                req.estimated_play_time = live_estimate
                req.save(update_fields=["estimated_play_time"])

        self.stdout.write(json.dumps({
            "checked": len(pending),
            "resolved": resolved_count,
            "fulfilled_now": fulfilled_now_count,
            "still_pending": len(still_pending),
            "slots_available": slots_offered,
        }))
