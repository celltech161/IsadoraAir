"""Operator queue Swap: replace ONE upcoming occurrence of the live queue with another track, in place.

``POST /api/engine/queue/replace/`` (library.views.api_engine_queue_replace) is the only caller. This is a
live-safe mutation path distinct from ``api_log_item_swap`` (draft logs only, schedule.edit), which stays
unchanged. See docs/QUEUE_SWAP.md.

What is replaced: the LogItem's track and its title/artist snapshots. What is preserved: the LogItem id,
its position and scheduled_time, its category (the slot's scheduling provenance, which the builder, live
fill and Hour Refill read) and its origin. Nothing is inserted.

Authority. A stale browser display is never sufficient. The caller states the occurrence it saw (item id,
the track it showed, the engine instance it was talking to) and every part is re-checked here:

1. the engine's own published state must be fresh, from that same engine instance, list the item in its
   live upcoming queue and not hold it on a deck;
2. under row locks taken in the same order as Web Requests and live fill (PlaylistLog, then LogItem,
   bounded lock wait), the occurrence must be unplayed and unclaimed (``playback_claimed_at`` is the
   engine's one-way commitment taken before any deck opens), in an approved log, a music slot of an
   ordinary origin, not reserved for a listener's SongRequest, and still carry the expected track;
3. the replacement must be playable under the shared Web Requests definition (``track_is_available``:
   ready to air, music, file present).

The engine closes the remaining window itself: its claim re-reads ``track_id`` under the same row lock and,
if a replacement committed after its ~3 s queue reload, opens the replacement instead of the stale copy
(PlaybackEngine._claim_playback_occurrence / _start_next_track).

Idempotent: repeating a request whose replacement already committed answers ``changed: false``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from django.db import OperationalError, connection, transaction

from library.models import LogItem, PlaylistLog, Track

ENGINE_STATE_PATH = Path("/run/isadoraair/engine_state.json")
#: The engine rewrites its state every poll tick (~250 ms); older than this is not "live".
STATE_MAX_AGE_SECONDS = 5.0
LOCK_TIMEOUT = "1s"
_LOCK_NOT_AVAILABLE = "55P03"

#: Origins an operator may replace. Engine insertions (urgent alerts, native Active Messages, dedication
#: intros) and DST fall-back replays are never replaceable; a row without an origin fails closed.
REPLACEABLE_ORIGINS = frozenset({
    LogItem.ORIGIN_GENERATED_SCHEDULE,
    LogItem.ORIGIN_GENERATED_FILL,
    LogItem.ORIGIN_LIVE_FILL,
    LogItem.ORIGIN_OPERATOR_INSERTED,
    LogItem.ORIGIN_REFILL_INSTALLED,
})

#: reason -> (HTTP status, operator-facing message)
REASONS = {
    "engine_unavailable": (409, "The engine's live queue is not available right now. Try again in a moment."),
    "engine_changed": (409, "The engine restarted since this queue was shown. The queue has been refreshed."),
    "not_upcoming": (409, "That track is no longer in the upcoming queue."),
    "on_deck": (409, "That track is already loaded on a deck and can no longer be swapped."),
    "claimed": (409, "The engine has already started loading that track."),
    "played": (409, "That track has already played."),
    "log_not_live": (409, "That track is not part of an approved live log."),
    "item_changed": (409, "That queue row changed since it was shown. The queue has been refreshed."),
    "not_replaceable": (409, "Only music slots can be swapped; this row is an announcement, alert, "
                             "intro or other protected item."),
    "request_reserved": (409, "That slot is reserved for a listener's song request and cannot be swapped."),
    "contended": (409, "The queue is being updated by the engine right now. Try again."),
    "track_not_found": (404, "The chosen replacement track no longer exists."),
    "track_ineligible": (422, "The chosen replacement is not ready to air (not a ready, music track with "
                              "a media file)."),
}


@dataclass
class ReplaceOutcome:
    ok: bool
    reason: str | None = None
    changed: bool = False
    item: LogItem | None = None
    previous_track_id: int | None = None
    detail: dict = field(default_factory=dict)

    @property
    def status(self):
        return 200 if self.ok else REASONS[self.reason][0]

    @property
    def message(self):
        return None if self.ok else REASONS[self.reason][1]


def read_engine_state(path=None, now=None):
    """The engine's published state, or None if it is missing, unreadable or older than
    STATE_MAX_AGE_SECONDS."""
    try:
        state = json.loads(Path(path or ENGINE_STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = state.get("timestamp") if isinstance(state, dict) else None
    if not isinstance(stamp, (int, float)):
        return None
    if (now if now is not None else time.time()) - stamp > STATE_MAX_AGE_SECONDS:
        return None
    return state


def state_engine_instance(state):
    revision = state.get("state_revision") if isinstance(state, dict) else None
    instance = revision.get("instance") if isinstance(revision, dict) else None
    return instance or state.get("engine_session_id")


def assign_track_snapshot(item, track):
    """The LogItem fields a track substitution rewrites (the same three Web Requests' swap writes)."""
    item.track = track
    item.track_title = track.title
    item.track_artist = track.artist.name if track.artist_id else ""
    return ["track", "track_title", "track_artist"]


def _music_slot(item):
    category = item.category
    return bool(category and category.kind_id and category.kind.code == "music")


def replace_upcoming_item(*, item_id, expected_track_id, track_id, engine_instance, state):
    """Replace one upcoming occurrence. ``state`` is the engine state read by the caller (None if
    unavailable). Returns a ReplaceOutcome; never raises for an expected refusal."""
    from webrequests.models import SongRequest
    from webrequests.services import track_is_available

    if state is None:
        return ReplaceOutcome(False, "engine_unavailable")
    if not engine_instance or state_engine_instance(state) != engine_instance:
        return ReplaceOutcome(False, "engine_changed")
    if any(deck and deck.get("log_item_id") == item_id for deck in (state.get("decks") or {}).values()):
        return ReplaceOutcome(False, "on_deck")
    if not any(entry.get("item_id") == item_id for entry in state.get("queue") or []):
        return ReplaceOutcome(False, "not_upcoming")

    track = Track.objects.select_related("artist", "category", "category__kind").filter(pk=track_id).first()
    if track is None:
        return ReplaceOutcome(False, "track_not_found")

    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            parent_id = LogItem.objects.filter(pk=item_id).values_list("playlist_log_id", flat=True).first()
            if parent_id is None:
                return ReplaceOutcome(False, "not_upcoming")
            log = PlaylistLog.objects.select_for_update().filter(pk=parent_id).first()
            # of=("self",): category is a nullable FK (Postgres refuses FOR UPDATE on an outer join's
            # nullable side).
            item = (
                LogItem.objects.select_for_update(of=("self",))
                .select_related("category", "category__kind")
                .filter(pk=item_id, playlist_log_id=parent_id).first()
            )
            if item is None or log is None:
                return ReplaceOutcome(False, "not_upcoming")
            if item.played_at is not None:
                return ReplaceOutcome(False, "played")
            if item.playback_claimed_at is not None:
                return ReplaceOutcome(False, "claimed")
            if log.status != "approved":
                return ReplaceOutcome(False, "log_not_live")
            if item.track_id == track.id:
                # Already this track: a repeated request after success, or a no-op choice.
                return ReplaceOutcome(True, changed=False, item=item, previous_track_id=item.track_id)
            if item.track_id != expected_track_id:
                return ReplaceOutcome(False, "item_changed")
            if item.origin not in REPLACEABLE_ORIGINS or not _music_slot(item):
                return ReplaceOutcome(False, "not_replaceable")
            if SongRequest.objects.filter(log_item_id=item.id, status="scheduled").exists():
                return ReplaceOutcome(False, "request_reserved")
            if not track_is_available(track):
                return ReplaceOutcome(False, "track_ineligible")
            previous = item.track_id
            item.save(update_fields=assign_track_snapshot(item, track))
            return ReplaceOutcome(True, changed=True, item=item, previous_track_id=previous,
                                  detail={"log_id": log.id, "position": item.position})
    except OperationalError as exc:
        if getattr(getattr(exc, "__cause__", None), "pgcode", None) == _LOCK_NOT_AVAILABLE:
            return ReplaceOutcome(False, "contended")
        raise
