from pathlib import Path

from django.db import close_old_connections
from django.utils import timezone

from library.models import RecencyConfig
from library.services.log_builder import get_recent_exclusions, get_separation
from monitoring.models import emit_event

from .models import SongRequest, WebRequestConfig


def is_track_eligible_at(track, target_datetime, recency_cfg):
    """Would `track` actually be allowed to air at `target_datetime`
    right now -- ready2air, still filed under a music-kind category, its
    file still on disk, and not recency-blocked (get_separation/
    get_recent_exclusions, the same functions log_builder itself uses
    for a normal rotation pick, evaluated relative to target_datetime
    rather than always "now" -- a track blocked at the current moment
    can still be legitimately eligible at a slot far enough in the
    future). Shared between maybe_fulfill_song_request (real
    fulfillment, always checked against "now") and
    refresh_song_request_statuses' advisory estimate pass (checked
    against each candidate future slot's own time, so the estimate it
    shows a requester doesn't promise a slot recency would actually
    block)."""
    if not track.ready2air:
        return False
    if track.category_id is None or track.category.kind.code != "music":
        return False
    if not track.filepath or not Path(track.filepath).is_file():
        return False

    artist_sep, title_sep = get_separation(track.category, recency_cfg)
    exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
        target_datetime, artist_sep, title_sep, set(), set(),
    )
    return track.id not in exclude_track_ids and track.artist_id not in exclude_artist_ids


def maybe_fulfill_song_request(log_item):
    """Web Requests fulfillment. If `log_item` is a music-kind slot in
    an open request hour and there's an eligible pending request, swap
    the track in-place (mutating log_item.track / track_title /
    track_artist, both via a DB save) so whatever ends up airing this
    slot is the requested song, not whatever rotation had picked.

    Called from two places:
      1. refresh_song_request_statuses (every ~20s, on every upcoming
         open/eligible LogItem within the lookahead window) -- this is
         the one that makes a fulfilled request show up in "Up Next"
         well before it airs, since the running engine's own
         _reload_queue_if_changed() picks up the DB-side swap within a
         few seconds and refreshes its in-memory queue from it.
      2. engine.py's _start_next_track, right before _create_deck, as a
         last-second safety net for a request that only became
         eligible in the last few seconds (recency just cleared, a
         slot just opened) -- too late for the periodic refresh to
         have caught it yet. Idempotent with #1: a LogItem already
         swapped, or a request already fulfilled, is simply not a
         candidate anymore by the time this runs again.

    Never preempts an _insert_urgent_next alert (AMBER/weather): those
    always land at the front of the queue as a brand-new, non-music-
    kind LogItem, so the music-kind gate below skips them regardless of
    which caller reaches them first.

    Recency rules are honored exactly as they are for a normal rotation
    pick (get_separation/get_recent_exclusions, the same functions
    log_builder itself uses) -- a request for a title or artist that's
    currently too recent just keeps waiting. "Collapse to one play":
    regardless of whether a swap happens here, every OTHER pending/
    no_slot_soon request for whatever track ends up in this slot rides
    along on this one play instead of separately waiting for its own
    turn -- covers both "we swapped this song in" and "rotation (or an
    earlier call to this function) already queued a song someone
    requested".

    played_at/last_played_at/play_count/PlayEvent are all still written
    by engine.py's _create_deck at actual airtime as normal -- nothing
    here duplicates that; this only ever touches the LogItem/
    SongRequest rows themselves.

    Wrapped in a blanket try/except: a bug in this feature must never
    be able to stop a track from starting (engine caller) or wedge the
    periodic refresh cycle (management-command caller)."""
    try:
        if log_item is None or log_item.track_id is None:
            return
        if log_item.category_id is None or not log_item.category.kind_id:
            return
        if log_item.category.kind.code != "music":
            return

        close_old_connections()
        cfg = WebRequestConfig.load()
        if not cfg.enabled:
            return

        local_now = timezone.localtime()
        slot_index = local_now.weekday() * 24 + local_now.hour
        if slot_index not in cfg.open_slots:
            return

        fulfilled_this_hour = SongRequest.objects.filter(
            status="fulfilled", log_item__playlist_log_id=log_item.playlist_log_id,
        ).count()

        if fulfilled_this_hour < cfg.max_fulfilled_per_hour:
            recency_cfg = RecencyConfig.load()
            candidates = (
                SongRequest.objects
                .filter(status__in=SongRequest.NON_TERMINAL_STATUSES)
                .exclude(track__isnull=True)
                .select_related("track", "track__artist", "track__category", "track__category__kind")
                .order_by("submitted_at")
            )
            for candidate in candidates:
                track = candidate.track
                if not is_track_eligible_at(track, local_now, recency_cfg):
                    continue  # ineligible or recency-blocked right now -- leave pending for a later slot

                log_item.track = track
                log_item.track_title = track.title
                log_item.track_artist = track.artist.name if track.artist_id else ""
                log_item.save(update_fields=["track", "track_title", "track_artist"])

                now = timezone.now()
                candidate.status = "fulfilled"
                candidate.log_item = log_item
                candidate.fulfilled_at = now
                candidate.resolved_at = now
                candidate.save(update_fields=["status", "log_item", "fulfilled_at", "resolved_at"])
                print(f"  Web request fulfilled: {track.artist.name if track.artist_id else '?'} - {track.title} (request id={candidate.external_request_id})")
                break

        now = timezone.now()
        SongRequest.objects.filter(
            status__in=SongRequest.NON_TERMINAL_STATUSES, track_id=log_item.track_id,
        ).update(status="fulfilled", log_item=log_item, fulfilled_at=now, resolved_at=now)
    except Exception as exc:
        print(f"  Web request fulfillment check failed (non-fatal): {exc}")
        emit_event(
            category="engine", level="warning",
            title="Web request fulfillment check failed",
            detail={"error": str(exc), "log_item_id": getattr(log_item, "id", None)},
            dedupe_key="engine|webrequest-fulfill-error",
        )
