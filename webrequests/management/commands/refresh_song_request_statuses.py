import json
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import LogItem, RecencyConfig
from monitoring.models import emit_event
from webrequests.models import SongRequest, WebRequestConfig
from webrequests.services import (
    _read_engine_state,
    classify_log_item,
    estimate_air_time,
    is_track_eligible_at,
    maybe_schedule_song_request,
    track_is_available,
)


class Command(BaseCommand):
    """Re-evaluates every SongRequest on a timer (systemd), independent
    of the public-site transport. Lifecycle: pending/no_slot_soon (waiting)
    -> scheduled (assigned to a specific LogItem) -> fulfilled (that
    LogItem actually aired -- set by the engine's _create_deck, not by
    this command). unavailable/expired are terminal failures.

      1. Self-heals scheduled requests the engine already played but
         may not have promoted to fulfilled (a DB-write race between
         LogItem.played_at succeeding and the engine's
         mark_song_requests_aired call failing/not running).
      2. Reconciles every other still-scheduled-but-unaired request
         against the running engine's live state
         (webrequests.services.classify_log_item): a request whose
         assigned LogItem got swept away by an hour-boundary rollover
         before it ever aired, or whose track no longer matches what's
         actually in that LogItem, is requeued back to pending rather
         than sitting stuck showing "scheduled" (or, under the OLDER
         version of this feature, "fulfilled") forever. A request whose
         track itself became unplayable (deleted, ready2air off, file
         gone) is marked unavailable instead.
      3. Keeps estimated_play_time live-accurate for what's left
         scheduled, as real playback drifts from the schedule it was
         built with.
      4. unavailable -- track ineligible (see track_is_available) since
         the request was made.
      5. expired -- sitting past WebRequestConfig.expire_after_hours
         with no track-level problem, just never got a turn.
      6. Proactively schedules: walks every upcoming open, music-kind,
         not-yet-played, approved-log LogItem in order (excluding any
         the running engine has already abandoned -- see
         classify_log_item/_is_dead_slot) and tries to assign the
         oldest eligible waiting request via
         webrequests.services.maybe_schedule_song_request. This is what
         makes a scheduled request show up in "Up Next" right away
         instead of only at the literal moment it airs -- the running
         engine's own _reload_queue_if_changed() (engine.py) picks up
         this DB-side swap within a few seconds. The engine calls the
         same function itself too, once, right before a slot actually
         plays -- a last-second safety net for anything that became
         eligible too recently for this pass to have caught it yet.
         Idempotent between the two callers and across repeated runs.
      7. Everything still left over (couldn't be scheduled above) is
         matched (oldest submitted_at first) against remaining open,
         music-kind, not-already-reserved LogItems within
         lookahead_warning_minutes, capped per hour by
         max_fulfilled_per_hour (counting distinct LogItems already
         scheduled/fulfilled that hour, not request rows) -- giving
         each a pending/no_slot_soon status plus an advisory
         estimated_play_time. Each candidate slot is checked with the
         SAME eligibility+recency test (is_track_eligible_at) real
         scheduling uses, evaluated at max(now, that slot's own
         scheduled_time) -- matching maybe_schedule_song_request's own
         eligibility_time exactly, so this advisory pass can't disagree
         with what real scheduling would actually decide for the same
         item right now. Still only ever a preview: since a real
         scheduling can happen for any request at any later cycle, an
         advisory estimate can end up not matching which slot a
         request actually lands in.

      Candidate queries (6 and 7) bound scheduled_time to
      [current_hour_start, lookahead_cutoff) -- current_hour_start
      (the floor of the current station-local hour), not `now`. A
      not-yet-played item's scheduled_time is a static build-time
      estimate that routinely drifts behind real playback over the
      course of an hour (ordinary variance, not a fault condition);
      bounding by `now` instead silently drops a drifted-but-still-
      upcoming item from every candidate query the moment its stale
      estimate slips into the past, often the very next thing about to
      air. current_hour_start avoids that without reintroducing
      long-abandoned historical LogItems as candidates -- those stay
      excluded by played_at__isnull=True combined with _is_dead_slot's
      own STRANDED/UNKNOWN-in-current-or-earlier-hour classification,
      not by a raw time filter."""

    help = "Re-evaluate SongRequest statuses (unavailable/expired/pending/no_slot_soon/scheduled) and estimated_play_time."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.load()
        now = timezone.now()
        local_now = timezone.localtime(now)
        # Station-local (not UTC) -- PlaylistLog.date/.hour and
        # engine_state.json's date/hour are station wall-clock values.
        # Comparing against a UTC `now` is wrong in the evening Central
        # Time, once UTC has already rolled to the next calendar date.
        wall_clock_key = (local_now.date(), local_now.hour)
        # Floor of the current station-local hour -- the correct lower
        # bound for "still worth considering" candidate queries below,
        # replacing a prior `scheduled_time__gte=now`. scheduled_time is
        # a STATIC estimate set once when the hour's log was built;
        # real playback routinely drifts behind (or ahead of) that
        # original plan over the course of an hour -- varying track
        # lengths, no anomaly required -- so a not-yet-played CURRENT-
        # HOUR item's own scheduled_time can already be in the past
        # relative to `now` well before it actually airs. Filtering on
        # `now` silently dropped exactly those drifted-but-still-
        # upcoming items from every candidate query below (confirmed
        # live 2026-08-20: a request submitted ~2 minutes before its
        # only eligible slot showed "no slot soon" the whole time,
        # because that slot's build-time scheduled_time had already
        # drifted ~4 minutes into the past). `played_at__isnull=True`
        # alone is NOT a safe substitute for a lower bound -- an
        # abandoned/stranded LogItem from an earlier hour can
        # legitimately sit unplayed forever, and must not be
        # reintroduced as a candidate just because the time filter was
        # removed; `current_hour_start` keeps the query scoped to "this
        # hour or later" the same way `now` was meant to, without being
        # defeated by ordinary intra-hour drift. Genuinely abandoned
        # CURRENT-hour rows (not on a deck, not in the live queue) are
        # still excluded -- that's _is_dead_slot's job, unchanged below.
        current_hour_start = local_now.replace(minute=0, second=0, microsecond=0)
        open_slots = set(cfg.open_slots)
        state = _read_engine_state()

        def _is_dead_slot(item):
            """True if this LogItem should be excluded from candidate
            selection -- either definitively stranded/invalid
            (classify_log_item), or unverifiable (engine state missing/
            stale) AND belonging to the current-or-earlier local hour,
            where a false-negative (schedule into what the engine
            already abandoned) is worse than a false-positive (skip a
            still-good slot for one cycle). A strictly future-hour item
            is never excluded on unverifiability alone -- its validity
            doesn't depend on live engine data."""
            classification = classify_log_item(item, state)
            if classification == "STRANDED":
                return True
            if classification == "UNKNOWN":
                item_key = (item.playlist_log.date, item.playlist_log.hour)
                return item_key <= wall_clock_key
            return False

        # --- Reconciliation pass 1: self-heal scheduled requests the
        # engine already played (state-independent -- runs regardless
        # of engine-state availability). ---
        aired_count = 0
        for req in (
            SongRequest.objects.filter(status="scheduled", log_item__played_at__isnull=False)
            .select_related("log_item")
        ):
            heal_now = timezone.now()
            if req.log_item.track_id == req.track_id:
                aired_count += SongRequest.objects.filter(
                    id=req.id, status="scheduled", log_item_id=req.log_item_id, track_id=req.track_id,
                ).update(
                    status="fulfilled", fulfilled_at=req.log_item.played_at,
                    resolved_at=req.log_item.played_at, estimated_play_time=req.log_item.played_at,
                    status_updated_at=heal_now,
                )
            else:
                # Something else aired from this slot -- don't credit it.
                SongRequest.objects.filter(id=req.id, status="scheduled", log_item_id=req.log_item_id).update(
                    status="pending", log_item=None, scheduled_at=None, fulfilled_at=None,
                    resolved_at=None, estimated_play_time=None, status_updated_at=heal_now,
                    intro_log_item=None,  # abandoning this assignment -- keep intro_track (reusable), clear the marker
                )

        # --- Reconciliation pass 2: track-availability + stranding
        # check for everything still scheduled and unaired. ---
        requeued_count = 0
        for req in (
            SongRequest.objects.filter(status="scheduled", log_item__played_at__isnull=True)
            .select_related("log_item", "log_item__playlist_log", "track", "track__category", "track__category__kind")
        ):
            recon_now = timezone.now()
            if not track_is_available(req.track):
                SongRequest.objects.filter(id=req.id, status="scheduled", log_item_id=req.log_item_id).update(
                    status="unavailable", log_item=None, scheduled_at=None, fulfilled_at=None,
                    estimated_play_time=None, resolved_at=recon_now, status_updated_at=recon_now,
                    intro_log_item=None,
                )
                continue
            classification = classify_log_item(req.log_item, state)
            mismatched = req.log_item is not None and req.log_item.track_id != req.track_id
            if classification == "STRANDED" or mismatched:
                updated = SongRequest.objects.filter(id=req.id, status="scheduled", log_item_id=req.log_item_id).update(
                    status="pending", log_item=None, scheduled_at=None, fulfilled_at=None,
                    resolved_at=None, estimated_play_time=None, status_updated_at=recon_now,
                    intro_log_item=None,
                )
                if updated:
                    requeued_count += 1
                    emit_event(
                        category="webrequests", level="warning",
                        title="Song request stranded by hour rollover, requeued",
                        detail={"request_id": req.external_request_id},
                        dedupe_key=f"webrequests|stranded|{req.id}",
                    )
            # AIRING/QUEUED/FUTURE, available, no mismatch -> left alone.

        # --- Reconciliation pass 3: ETA refresh for what's left
        # scheduled (compare-and-set -- a stale read here must not
        # overwrite a fulfilled transition that happened moments after
        # the read). ---
        for req in (
            SongRequest.objects.filter(status="scheduled", log_item__isnull=False, log_item__played_at__isnull=True)
            .select_related("log_item")
        ):
            live_estimate = estimate_air_time(req.log_item)
            if live_estimate != req.estimated_play_time:
                SongRequest.objects.filter(id=req.id, status="scheduled", log_item_id=req.log_item_id).update(
                    estimated_play_time=live_estimate, status_updated_at=timezone.now(),
                )

        # --- Waiting requests: unavailable/expired aging, same
        # compare-and-set discipline as above. ---
        pending = list(
            SongRequest.objects.filter(status__in=SongRequest.WAITING_STATUSES)
            .select_related("track", "track__category", "track__category__kind")
            .order_by("submitted_at")
        )

        expire_cutoff = now - timedelta(hours=cfg.expire_after_hours)
        still_pending = []
        resolved_count = 0

        for req in pending:
            age_now = timezone.now()
            if not track_is_available(req.track):
                resolved_count += SongRequest.objects.filter(id=req.id, status__in=SongRequest.WAITING_STATUSES).update(
                    status="unavailable", estimated_play_time=None, resolved_at=age_now, status_updated_at=age_now,
                )
            elif req.submitted_at <= expire_cutoff:
                resolved_count += SongRequest.objects.filter(id=req.id, status__in=SongRequest.WAITING_STATUSES).update(
                    status="expired", estimated_play_time=None, resolved_at=age_now, status_updated_at=age_now,
                )
            else:
                still_pending.append(req)

        lookahead_cutoff = now + timedelta(minutes=cfg.lookahead_warning_minutes)

        fulfillment_candidates = [
            item for item in (
                LogItem.objects.filter(
                    scheduled_time__gte=current_hour_start, scheduled_time__lt=lookahead_cutoff,
                    played_at__isnull=True, category__kind__code="music",
                    playlist_log__status="approved",
                )
                .select_related("playlist_log")
                .order_by("scheduled_time")
            )
            if not _is_dead_slot(item)
        ]
        for item in fulfillment_candidates:
            maybe_schedule_song_request(item)

        # Re-check: the proactive pass above may have just scheduled
        # some of these.
        still_pending_ids = [req.id for req in still_pending]
        still_pending = list(
            SongRequest.objects.filter(
                id__in=still_pending_ids, status__in=SongRequest.WAITING_STATUSES,
            )
            # track__artist: is_track_eligible_at (below, in the
            # per-request loop) now needs the artist name for
            # identity-set comparison -- without this, that's a lazy
            # per-request query.
            .select_related("track", "track__artist", "track__category", "track__category__kind")
            .order_by("submitted_at")
        )
        scheduled_now_count = len(still_pending_ids) - len(still_pending)

        candidates = [
            item for item in (
                LogItem.objects.filter(
                    scheduled_time__gte=current_hour_start, scheduled_time__lt=lookahead_cutoff,
                    played_at__isnull=True, category__kind__code="music",
                    playlist_log__status="approved",
                )
                .select_related("playlist_log")
                .order_by("scheduled_time")
            )
            if not _is_dead_slot(item)
        ]

        already_used_slots_per_hour = defaultdict(int)
        for row in (
            SongRequest.objects.filter(
                status__in=("scheduled", "fulfilled"), log_item__playlist_log_id__isnull=False,
                log_item__scheduled_time__gte=current_hour_start, log_item__scheduled_time__lt=lookahead_cutoff,
            )
            .values("log_item__playlist_log_id", "log_item_id").distinct()
        ):
            already_used_slots_per_hour[row["log_item__playlist_log_id"]] += 1

        remaining_capacity = defaultdict(lambda: cfg.max_fulfilled_per_hour)
        for playlist_log_id, count in already_used_slots_per_hour.items():
            remaining_capacity[playlist_log_id] = max(0, cfg.max_fulfilled_per_hour - count)

        # A LogItem already carrying a scheduled request will never be
        # handed to a DIFFERENT request by the real scheduler (see
        # maybe_schedule_song_request's already_reserved guard) --
        # don't advertise it as an estimate for one either.
        reserved_item_ids = set(
            SongRequest.objects.filter(status="scheduled", log_item_id__isnull=False)
            .values_list("log_item_id", flat=True)
        )

        open_candidates = []
        for item in candidates:
            if item.id in reserved_item_ids:
                continue
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
                # Same max(now, ...) real scheduling uses (see
                # maybe_schedule_song_request's own eligibility_time) --
                # a drifted current-hour item's scheduled_time can be
                # minutes in the past by the time this runs, and
                # recency exclusions are relative to the CHECK time, not
                # the slot's stale build-time estimate. Evaluating at
                # the stale value alone could show this advisory pass
                # disagreeing with what real scheduling would actually
                # decide for the exact same item right now.
                eligibility_time = max(now, item.scheduled_time)
                if req.track is None or not is_track_eligible_at(req.track, eligibility_time, recency_cfg):
                    continue
                estimate = estimate_air_time(item)
                claimed_item_ids.add(item.id)
                used_per_hour[item.playlist_log_id] += 1
                slots_offered += 1
                break

            new_status = "pending" if estimate is not None else "no_slot_soon"
            if new_status != req.status or estimate != req.estimated_play_time:
                SongRequest.objects.filter(id=req.id, status__in=SongRequest.WAITING_STATUSES).update(
                    status=new_status, estimated_play_time=estimate, status_updated_at=timezone.now(),
                )

        self.stdout.write(json.dumps({
            "checked": len(pending),
            "resolved": resolved_count,
            # Key name kept as "fulfilled_now" for backward compatibility
            # with the legacy helper during deployment cutover -- semantically
            # this now counts requests
            # that moved to "scheduled" this run (real fulfillment
            # happens later, at actual airtime), but renaming the wire
            # key would require a coordinated deploy on their side for
            # what's purely an informational log line on theirs.
            "fulfilled_now": scheduled_now_count,
            "still_pending": len(still_pending),
            "slots_available": slots_offered,
            "aired": aired_count,
            "requeued_stranded": requeued_count,
        }))
