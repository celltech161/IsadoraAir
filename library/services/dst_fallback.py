"""[P0] 1.21: playout continuity through a daylight-saving fall-back repeated hour.

The November 1, 2026 outage (America/Chicago, CDT -> CST at 07:00 UTC):

1. At 01:59:30 CDT the engine built and approved the 02:00 log and, by its established early-rollover policy,
   installed it as the active queue.
2. The clock then returned to 01:00 CST.  The approved 02:00 programming kept playing through the repeated hour,
   so its occurrences aired -- ``played_at`` set, PlayEvents written -- before the real 02:00 CST hour began.
3. From 02:00 CST that same approved log is the current hour's authority, but every occurrence is already
   played: the queue cursor (first unplayed item) is past its end and nothing schedules the next hour until the
   03:00 lookahead.  About an hour of silence followed.

The approved programming may be reused for the real hour; it must not be rebuilt.  A LogItem is one logical
occurrence -- ``played_at`` is its one-way actual-air gate and ``PlayEvent.log_item_id_snapshot`` is unique -- so
a second, truthfully counted airing needs occurrences of its own.  This module provides exactly that and nothing
broader:

* ``fallback_successor`` -- the transition identity, from the station time zone and actual instants only.  A log
  ``(date, hour)`` is a *fall-back successor* when the whole preceding local wall hour is repeated (its two
  instances have UTC offsets exactly one hour apart, the earlier one larger) and the log's own ``hour:00`` is
  unambiguous.  For America/Chicago that is only ``(2026-11-01, 2)``: repeated 01:00 CDT (06:00 UTC) and 01:00 CST
  (07:00 UTC), real hour start 02:00 CST (08:00 UTC).  Recognition never relies on ``now.fold``: 02:00 CST itself
  is not ambiguous.  Spring-forward gaps, ordinary days, other hours and zones whose shift is not one whole hour
  never qualify.
* The **replay window** is ``[hour_start - lead, hour_start + 1 hour)`` in actual instants (``lead`` = the engine's
  next-hour lookahead, 30 s): the same moment an ordinary next hour's queue would be installed early, through the
  end of the real hour.  Before it, during the repeated hour, the first pass plays as before; after it, ordinary
  forward progression owns the queue.
* ``ensure_replay`` -- under the approved PlaylistLog's row lock (bounded wait), and only if no pass exists for this
  log and successor hour, materializes the single replay pass in ONE transaction: a ``DstFallbackReplayPass`` record
  (unique per ``(playlist_log, successor_hour_start)`` in the database) and, for every ordinary programming
  occurrence of that log that actually aired *before* the real hour began (``played_at < hour_start``), a new
  LogItem in the same approved log -- same Track, Category, title/artist snapshot and ``scheduled_time``, appended
  after every existing row in original order -- with ``origin = dst_replay``, ``replay_pass`` = the pass and
  ``replay_source_id_snapshot`` = the id of the original occurrence it replays.  The originals and their history
  are never modified.  The new rows are ordinary unplayed occurrences: the engine's existing claim /
  first-real-buffer / PlayEvent path gives each its own ``played_at`` and PlayEvent exactly once (``source =
  scheduled`` whatever its position: it is scheduled programming), and the ~3 s queue reload and every restart
  position the cursor on them (first unplayed).

**Durable authority.**  Whether a pass was authorized is decided by the ``DstFallbackReplayPass`` record alone, never
by inspecting LogItem fields: an unrelated engine insert identical to an original can never count as a replay, and
editing (Track, Category, time) or deleting any or all replay occurrences never makes the hour eligible again -- no
replacement or third pass, in any process, after any restart.  The pass records which originals it replayed
(``source_log_item_ids``, ``replay_count``), so duplicate originals with identical fields stay distinct (one replay
per original id, unique in the database), and a pass whose occurrences were later deleted or changed is reported as
*damaged* (deduplicated SystemEvent) and never silently re-created: an operator's deletion of replay rows keeps them
from playing.  Creation is all-or-nothing: a lock timeout (``busy``) or any failure (``failed``) commits nothing and
a later tick may retry; a concurrent creator that slipped past the lock would hit the unique constraint and find the
committed pass.

Not replayed (they keep their single, first-pass airing): rows that never aired or aired in the real hour,
operator/engine inserts (urgent alerts, native Active Messages, dedication intros, Play Now: any origin other than
generated schedule / build-time fill / live fill), Hour Refill installs, positions >= 9999, the Dedications
category, Active Messages aggregate Tracks, and rows a web request or its dedication intro is attached to -- so no
one-shot fulfilment is credited twice.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import date as date_type, datetime, time, timedelta, timezone as dt_timezone

from django.db import IntegrityError, OperationalError, connection, transaction
from django.utils import timezone

UTC = dt_timezone.utc
ONE_HOUR = timedelta(hours=1)
#: Bounded wait for the approved log's row lock; this runs on the engine's single GLib thread.
LOCK_TIMEOUT = "250ms"
INSERT_POSITION_FLOOR = 9999
DEDICATIONS_CATEGORY_CODE = "Dedications"

AUTHORIZED = "authorized"            # a replay pass was materialized now
ALREADY_HANDLED = "already_handled"  # a replay pass exists (this or an earlier process made it)
NOT_ELIGIBLE = "not_eligible"        # nothing of this hour aired before its real start
WINDOW_CLOSED = "window_closed"      # not inside the bounded replay window
BUSY = "busy"                        # the approved log was locked; nothing committed; retried on a later tick
DAMAGED = "damaged"                  # a pass exists, but occurrences it authorized were deleted or changed
FAILED = "failed"                    # creation failed before commit; nothing committed; retried on a later tick
HANDLED = (AUTHORIZED, ALREADY_HANDLED, DAMAGED)


def _replay_source_origins():
    from library.models import LogItem
    return (LogItem.ORIGIN_GENERATED_SCHEDULE, LogItem.ORIGIN_GENERATED_FILL, LogItem.ORIGIN_LIVE_FILL)


def _format_offset(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


@dataclass(frozen=True)
class FallbackTransition:
    """The identity of one fall-back repeated hour and the log that follows it (all instants in UTC)."""
    zone: str
    log_date: date_type
    log_hour: int
    repeated_date: date_type
    repeated_hour: int
    first_instance_start: datetime     # the repeated wall hour's first instance (before the change)
    transition_at: datetime            # its second instance: the instant the clock went back
    hour_start: datetime               # the log's real hour start (unambiguous)
    offset_before: timedelta
    offset_after: timedelta
    lead: timedelta = field(default=timedelta(seconds=30))

    @property
    def window_start(self) -> datetime:
        return self.hour_start - self.lead

    @property
    def window_end(self) -> datetime:
        return self.hour_start + ONE_HOUR

    def window_open(self, now: datetime) -> bool:
        instant = now.astimezone(UTC)          # compare instants, never same-tzinfo wall times
        return self.window_start <= instant < self.window_end

    @property
    def identity(self) -> str:
        return (f"{self.zone} fall-back {self.repeated_date.isoformat()} {self.repeated_hour:02d}:00 "
                f"{_format_offset(self.offset_before)}->{_format_offset(self.offset_after)} at "
                f"{self.transition_at.isoformat()}; log {self.log_date.isoformat()} {self.log_hour:02d}:00 "
                f"starts {self.hour_start.isoformat()}")

    def as_detail(self) -> dict:
        return {
            "transition": self.identity, "time_zone": self.zone,
            "log_date": self.log_date.isoformat(), "log_hour": self.log_hour,
            "repeated_local_hour": f"{self.repeated_date.isoformat()} {self.repeated_hour:02d}:00",
            "repeated_first_instance_utc": self.first_instance_start.isoformat(),
            "transition_utc": self.transition_at.isoformat(),
            "log_hour_start_utc": self.hour_start.isoformat(),
            "utc_offset_before": _format_offset(self.offset_before),
            "utc_offset_after": _format_offset(self.offset_after),
            "replay_window_utc": [self.window_start.isoformat(), self.window_end.isoformat()],
        }


def _offsets(naive: datetime, tz):
    return naive.replace(tzinfo=tz, fold=0).utcoffset(), naive.replace(tzinfo=tz, fold=1).utcoffset()


@functools.lru_cache(maxsize=256)
def _successor(log_date, log_hour, tz, lead_seconds):
    naive_start = datetime.combine(log_date, time(log_hour))
    previous = naive_start - ONE_HOUR
    before, after = _offsets(previous, tz)
    # The whole preceding wall hour is repeated: ambiguous at its start and its last instant, with the earlier
    # instance exactly one hour ahead of the later (a fall-back; a spring-forward gap has the opposite sign).
    if before is None or after is None or before - after != ONE_HOUR:
        return None
    end_before, end_after = _offsets(naive_start - timedelta(microseconds=1), tz)
    if end_before - end_after != ONE_HOUR:
        return None
    start_before, start_after = _offsets(naive_start, tz)
    if start_before != start_after:                    # the log's own hour start must be unambiguous
        return None
    hour_start = naive_start.replace(tzinfo=tz).astimezone(UTC)
    transition_at = previous.replace(tzinfo=tz, fold=1).astimezone(UTC)
    first_instance = previous.replace(tzinfo=tz, fold=0).astimezone(UTC)
    if hour_start - transition_at != ONE_HOUR or transition_at - first_instance != ONE_HOUR:
        return None
    return FallbackTransition(
        zone=getattr(tz, "key", str(tz)), log_date=log_date, log_hour=log_hour,
        repeated_date=previous.date(), repeated_hour=previous.hour, first_instance_start=first_instance,
        transition_at=transition_at, hour_start=hour_start, offset_before=before, offset_after=after,
        lead=timedelta(seconds=lead_seconds),
    )


def fallback_successor(log_date, log_hour, *, tz=None, lead_seconds=30) -> FallbackTransition | None:
    """The fall-back transition whose repeated hour immediately precedes the log ``(log_date, log_hour)``, or None
    for every ordinary, spring-forward or partially repeated hour.  Pure time-zone arithmetic (memoized)."""
    if not isinstance(log_hour, int) or not 0 <= log_hour <= 23:
        return None
    return _successor(log_date, log_hour, tz or timezone.get_current_timezone(), int(lead_seconds))


# --------------------------------------------------------------------------- the replay pass

def is_replay(row) -> bool:
    """A replay occurrence: authorized by a DstFallbackReplayPass (never inferred from Track, Category or time)."""
    return getattr(row, "replay_pass_id", None) is not None


def _active_message_track(track) -> bool:
    try:
        from active_messages import playout
        return bool(playout.is_candidate(track))
    except Exception:                                  # undecidable: treat as protected, never replay it
        return True


def replay_sources(rows, transition, *, linked_ids=frozenset()) -> list:
    """The ordinary programming occurrences of the log that aired before its real hour began, in order."""
    origins = _replay_source_origins()
    sources = []
    for row in rows:
        if is_replay(row) or row.origin not in origins or row.position >= INSERT_POSITION_FLOOR \
                or row.track_id is None:
            continue
        if row.played_at is None or row.played_at >= transition.hour_start:
            continue
        if row.category_id and row.category is not None and row.category.code == DEDICATIONS_CATEGORY_CODE:
            continue
        if row.id in linked_ids or _active_message_track(row.track):
            continue
        sources.append(row)
    return sources


@dataclass
class ReplayResult:
    outcome: str
    reason: str = ""
    pass_id: int | None = None
    replay_ids: list = field(default_factory=list)
    source_ids: list = field(default_factory=list)
    unplayed: int = 0
    missing_source_ids: list = field(default_factory=list)
    changed_replay_ids: list = field(default_factory=list)


def pass_state(replay_pass) -> ReplayResult:
    """A committed pass as it is now: ``already_handled``, or ``damaged`` when occurrences it authorized were deleted
    (their original's id no longer has a replay row) or changed (a replay row's Track or Category differs from its
    original's).  Read only; nothing is ever re-created."""
    from library.models import LogItem
    rows = list(LogItem.objects.filter(replay_pass=replay_pass).order_by("position")
                .only("id", "track_id", "category_id", "played_at", "replay_source_id_snapshot"))
    authorized = [int(i) for i in replay_pass.source_log_item_ids]
    present = {r.replay_source_id_snapshot for r in rows}
    missing = [i for i in authorized if i not in present]
    originals = {o.id: o for o in LogItem.objects.filter(id__in=authorized).only("id", "track_id", "category_id")}
    changed = [r.id for r in rows if r.replay_source_id_snapshot in originals
               and (r.track_id, r.category_id) != (originals[r.replay_source_id_snapshot].track_id,
                                                   originals[r.replay_source_id_snapshot].category_id)]
    damaged = bool(missing or changed or len(rows) != replay_pass.replay_count)
    return ReplayResult(
        DAMAGED if damaged else ALREADY_HANDLED,
        ("the authorized pass exists but " + "; ".join(filter(None, [
            f"{len(missing)} of its {replay_pass.replay_count} occurrence(s) no longer exist" if missing else "",
            f"{len(changed)} occurrence(s) were changed" if changed else ""])) + "; nothing is re-created")
        if damaged else "a replay pass already exists for this log and hour",
        pass_id=replay_pass.id, replay_ids=[r.id for r in rows], source_ids=authorized,
        unplayed=sum(1 for r in rows if r.played_at is None), missing_source_ids=missing, changed_replay_ids=changed,
    )


def ensure_replay(log_id, transition, *, now, reason="") -> ReplayResult:
    """The log's single replay pass for this transition: found (``already_handled`` / ``damaged``) or, if due and
    absent, created atomically with its occurrences (``authorized``).  See the module docstring."""
    from library.models import DstFallbackReplayPass, LogItem, PlaylistLog
    from webrequests.models import SongRequest

    if not transition.window_open(now):
        return ReplayResult(WINDOW_CLOSED, "outside the bounded fall-back replay window")
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            log = PlaylistLog.objects.select_for_update().filter(pk=log_id, status="approved").first()
            if log is None or (log.date, log.hour) != (transition.log_date, transition.log_hour):
                return ReplayResult(NOT_ELIGIBLE, "the approved log is not this transition's log")
            existing = DstFallbackReplayPass.objects.filter(
                playlist_log=log, successor_hour_start=transition.hour_start).first()
            if existing is not None:
                return pass_state(existing)
            rows = list(LogItem.objects.filter(playlist_log=log).select_related("category", "track")
                        .order_by("position"))
            ids = [r.id for r in rows]
            linked = set(SongRequest.objects.filter(log_item_id__in=ids).values_list("log_item_id", flat=True))
            linked |= set(SongRequest.objects.filter(intro_log_item_id__in=ids)
                          .values_list("intro_log_item_id", flat=True))
            try:
                from active_messages.models import ActiveBroadcastInsertion
                linked |= set(ActiveBroadcastInsertion.objects.filter(log_item_id__in=ids)
                              .values_list("log_item_id", flat=True))
            except (ImportError, LookupError):
                pass
            sources = replay_sources(rows, transition, linked_ids=frozenset(linked))
            if not sources:
                return ReplayResult(NOT_ELIGIBLE, "none of this log's programming aired before its real hour")
            replay_pass = DstFallbackReplayPass.objects.create(
                playlist_log=log, successor_hour_start=transition.hour_start, transition_at=transition.transition_at,
                time_zone=transition.zone, authorized_at=now, reason=reason[:32],
                source_log_item_ids=[r.id for r in sources], replay_count=len(sources),
            )
            position = max(r.position for r in rows) + 1
            created = []
            for offset, source in enumerate(sources):
                created.append(LogItem.objects.create(
                    playlist_log=log, position=position + offset, scheduled_time=source.scheduled_time,
                    track_id=source.track_id, track_title=source.track_title, track_artist=source.track_artist,
                    category_id=source.category_id, origin=LogItem.ORIGIN_DST_REPLAY,
                    replay_pass=replay_pass, replay_source_id_snapshot=source.id,
                ))
            return ReplayResult(AUTHORIZED, "the approved programming aired during the repeated hour",
                                pass_id=replay_pass.id, replay_ids=[r.id for r in created],
                                source_ids=[r.id for r in sources], unplayed=len(created))
    except OperationalError as exc:
        return ReplayResult(BUSY, f"the approved log is busy ({type(exc).__name__}); nothing committed, retried later")
    except IntegrityError:
        # A creator that slipped past the row lock met the database's uniqueness: the committed pass is the authority.
        existing = DstFallbackReplayPass.objects.filter(
            playlist_log_id=log_id, successor_hour_start=transition.hour_start).first()
        if existing is not None:
            return pass_state(existing)
        return ReplayResult(FAILED, "a database constraint refused the pass; nothing committed")
    except Exception as exc:  # noqa: BLE001 -- the whole attempt rolled back; playout continues without it
        return ReplayResult(FAILED, f"creation failed before commit ({type(exc).__name__}: {exc}); nothing committed")
