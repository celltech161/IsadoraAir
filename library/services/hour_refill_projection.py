"""Roadmap 2.27 Phase B -- read-only playback-window snapshot and net-deficit
projection. Advisory only: nothing here inserts, reorders, claims or plays
anything, and nothing here is called by the engine.

- capture_playback_window(engine) copies primitive values out of engine
  memory (plus one GStreamer position read per live deck). No database,
  lock, disk or print. A later worker may call it on the engine thread.
- project_refill(snapshot, causes=..., boundary=...) is pure: it decides
  whether a real, current, NET shortage exists before a proven boundary,
  using the engine's own trigger arithmetic, and which causes qualify.

Causes are hints, never proof: the shortage is recomputed from the decks,
queue and boundary. See docs/roadmap/2_27_PHASE_B_PROPOSAL_CONTRACT.md.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone as dt_timezone

from django.utils import timezone

from library.services.hour_refill_causality import (
    CAUSE_OPERATOR_EJECT,
    CAUSE_REQUEST_SUBSTITUTION,
    REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS,
    _MISSING,
    _cached,
    _loaded,
    _ms,
    build_causality_trace,
)

# Draft Phase B policy (not a roadmap requirement): an Eject-only cause set
# proposes music only for a net shortage strictly greater than 30 s, the
# ordinary fit margin. Web-request rule stays strictly > 60 s.
EJECT_MUSIC_TRIGGER_THRESHOLD_SECONDS = 30

# Engine timing facts (engine._poll_position), mirrored here as constants.
TRIGGER_CLAMP_SECONDS = 10.0       # trigger = next_start - next_cue_in, or next_start when < 10
POLL_MIN_POSITION_SECONDS = 5.0    # no trigger is evaluated at position <= 5 s
TRANSITION_ALLOWANCE_SECONDS = 0.25  # one 250 ms poll tick of trigger latency per transition
POSITION_SANITY_SECONDS = 10.0     # the engine distrusts a position beyond duration + 10 s

# Within this of the takeover the queue is about to swap; never propose.
MIN_WINDOW_SECONDS = 60.0
MAX_WINDOW_SECONDS = 2 * 3600.0

OUTCOME_OFF = "off"
OUTCOME_INVALID_SNAPSHOT = "invalid_snapshot"
OUTCOME_WRONG_HOUR = "wrong_hour"
OUTCOME_BLOCKED_TRANSPORT = "blocked_transport"
OUTCOME_UNSAFE_WINDOW = "unsafe_window"
OUTCOME_NO_SAFE_BOUNDARY = "no_safe_boundary"
OUTCOME_NO_QUALIFYING_CAUSE = "no_qualifying_cause"
OUTCOME_ALREADY_SATISFIED = "already_satisfied"
OUTCOME_BELOW_THRESHOLD = "below_threshold"
OUTCOME_ELIGIBLE = "eligible"

BOUNDARY_ORDINARY = "ordinary_next_hour"
BOUNDARY_PARTIAL = "partial_takeover"
BOUNDARY_UNKNOWN = "unknown"

EVIDENCE_NOTE = (
    "HourRefillCause recording is best effort and non-exhaustive: a missing "
    "cause does not prove that nothing shortened the hour"
)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ItemTiming:
    """Primitive copy of one queued or loaded occurrence. category_kind is
    the SLOT category's kind code; "" = no category, None = not loaded."""
    log_item_id: int
    playlist_log_id: int | None
    position: int | None
    track_id: int | None
    category_id: int | None
    category_kind: str | None
    origin: str | None
    cue_in: float | None
    next_start: float | None
    duration: float | None
    claimed: bool
    played: bool

    def end_point(self):
        return self.next_start if self.next_start is not None else self.duration

    def effective_seconds(self):
        end = self.end_point()
        if end is None:
            return None
        return max(0.0, end - (self.cue_in or 0.0))


@dataclass(frozen=True)
class DeckState:
    slot: str
    generation: int | None
    item: ItemTiming | None
    position_seconds: float | None
    live: bool
    paused: bool
    on_air: bool
    continuation: bool


@dataclass(frozen=True)
class PlaybackWindowSnapshot:
    captured_at: datetime
    engine_instance: str
    active_log_id: int | None
    active_log_date: object
    active_log_hour: int | None
    cursor: int
    queue: tuple            # every in-memory log_items entry, in order
    forced: tuple           # _forced_next_items, in order
    decks: tuple            # DeckState for each occupied slot
    poison_skip: frozenset  # (playlist_log_id, log_item_id)
    manual_mode: bool = False
    remote_dj: bool = False
    mic_live: bool = False
    vt_phase: str = "idle"
    start_next_active: bool = False
    next_triggered: bool = False
    resume_pending: bool = False
    live_fill_in_progress: bool = False
    capture_errors: tuple = ()

    def digest(self):
        """Stable token over ordered identity/Track/claim content, cursor,
        forced items and deck occupancy. Playheads are excluded. A proposal
        token only -- never a lock."""
        def item(it):
            return [it.log_item_id, it.playlist_log_id, it.position, it.track_id, it.category_id,
                    it.claimed, it.played]
        payload = {
            "engine_instance": self.engine_instance,
            "log": [self.active_log_id, str(self.active_log_date), self.active_log_hour],
            "cursor": self.cursor,
            "queue": [item(it) for it in self.queue],
            "forced": [item(it) for it in self.forced],
            "decks": [[d.slot, d.generation, d.item.log_item_id if d.item else None,
                       d.item.track_id if d.item else None] for d in self.decks],
            "poison": sorted(list(pair) for pair in self.poison_skip),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()


def _flag(value):
    return value not in (None, _MISSING) and bool(value)


def _item_timing(item, errors, where):
    """ItemTiming from already-loaded fields only (never a lazy query)."""
    item_id = _loaded(item, "id")
    if item_id is _MISSING or item_id is None:
        errors.append(f"{where}:identity_not_loaded")
        return None
    values = {name: _loaded(item, name) for name in (
        "playlist_log_id", "position", "track_id", "category_id", "origin",
        "playback_claimed_at", "played_at",
    )}
    for name in ("playlist_log_id", "position", "track_id", "category_id", "playback_claimed_at", "played_at"):
        if values[name] is _MISSING:
            errors.append(f"{where}:li{item_id}:{name}_not_loaded")
            return None
    track = _cached(item, "track")
    cue_in = next_start = duration = None
    if values["track_id"] is not None:
        if track is None:
            errors.append(f"{where}:li{item_id}:track_not_loaded")
            return None
        timing = [_loaded(track, n) for n in ("cue_in_seconds", "next_start_seconds", "duration_seconds")]
        if any(v is _MISSING for v in timing):
            errors.append(f"{where}:li{item_id}:track_timing_not_loaded")
            return None
        cue_in, next_start, duration = timing
    kind = ""
    if values["category_id"] is not None:
        code = _loaded(_cached(_cached(item, "category"), "kind"), "code")
        kind = None if code is _MISSING else code
    origin = values["origin"]
    return ItemTiming(
        log_item_id=item_id,
        playlist_log_id=values["playlist_log_id"],
        position=values["position"],
        track_id=values["track_id"],
        category_id=values["category_id"],
        category_kind=kind,
        origin=None if origin is _MISSING else origin,
        cue_in=cue_in,
        next_start=next_start,
        duration=duration,
        claimed=values["playback_claimed_at"] is not None,
        played=values["played_at"] is not None,
    )


def capture_playback_window(engine, *, now=None):
    """Bounded copy of engine memory. Reads attributes and already-loaded
    ORM fields only, plus engine._get_deck_position() for each live deck (a
    GStreamer query). No database, lock, disk I/O or printing. Anything not
    already in memory is recorded in capture_errors (=> invalid snapshot)."""
    errors = []
    captured_at = now if now is not None else timezone.localtime()
    log = getattr(engine, "current_log", None)
    log_values = {n: _loaded(log, n) for n in ("id", "date", "hour")} if log is not None else {}
    if log is not None and any(v is _MISSING for v in log_values.values()):
        errors.append("active_log_not_loaded")
    queue = tuple(
        t for t in (_item_timing(it, errors, f"queue[{i}]")
                    for i, it in enumerate(list(getattr(engine, "log_items", None) or [])))
        if t is not None
    )
    forced = tuple(
        t for t in (_item_timing(it, errors, f"forced[{i}]")
                    for i, it in enumerate(list(getattr(engine, "_forced_next_items", None) or [])))
        if t is not None
    )
    decks = []
    for slot, deck in sorted((getattr(engine, "decks", None) or {}).items()):
        if deck is None:
            continue
        log_item = getattr(deck, "log_item", None)
        item = _item_timing(log_item, errors, f"deck[{slot}]") if log_item is not None else None
        if log_item is None:
            errors.append(f"deck[{slot}]:no_log_item")
        paused = bool(getattr(deck, "paused", False))
        live = not paused and not getattr(deck, "finished", False) and not getattr(deck, "retirement_started", False)
        position = None
        if live:
            try:
                position = float(engine._get_deck_position(deck))
            except Exception:  # noqa: BLE001 -- recorded, never raised
                errors.append(f"deck[{slot}]:position_unavailable")
        decks.append(DeckState(
            slot=slot,
            generation=getattr(deck, "generation", None),
            item=item,
            position_seconds=position,
            live=live,
            paused=paused,
            on_air=bool(getattr(deck, "air_start_recorded", False)),
            continuation=bool(getattr(deck, "continuation_reason", None)),
        ))
    try:
        mic_live = bool(engine._any_mic_live())
    except Exception:  # noqa: BLE001
        errors.append("mic_state_unavailable")
        mic_live = True
    return PlaybackWindowSnapshot(
        captured_at=captured_at,
        engine_instance=getattr(engine, "_engine_session_id", "") or "",
        active_log_id=log_values.get("id") if log is not None else None,
        active_log_date=log_values.get("date") if log is not None else None,
        active_log_hour=log_values.get("hour") if log is not None else None,
        cursor=int(getattr(engine, "_queue_cursor", 0) or 0),
        queue=queue,
        forced=forced,
        decks=tuple(decks),
        poison_skip=frozenset(getattr(engine, "_poison_skip_identities", None) or ()),
        manual_mode=bool(getattr(engine, "manual_mode", False)),
        remote_dj=getattr(engine, "remote_dj_session", None) is not None,
        mic_live=mic_live,
        vt_phase=(getattr(engine, "_vt", None) or {}).get("phase", "idle"),
        start_next_active=getattr(engine, "_start_next_active", None) is not None,
        next_triggered=bool(getattr(engine, "_next_triggered", False)),
        resume_pending=getattr(engine, "_resume_item", None) is not None,
        live_fill_in_progress=bool(getattr(engine, "_live_fill_in_progress", False)),
        capture_errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Timing (the engine's own trigger arithmetic)
# ---------------------------------------------------------------------------

def element_span(cue_in, end, successor_cue_in, *, position=None, allowance=TRANSITION_ALLOWANCE_SECONDS):
    """Wall-clock seconds from this element's reference point to the moment
    its successor becomes audible, mirroring engine._poll_position.

    A queued element's reference is its own audible start (file position
    cue_in); a live deck's is its current position. An unknown successor
    (None) is taken as cue-in 0. The trigger is end - successor_cue_in,
    clamped to `end` below 10 s, never before position 5 s (the poll guard)
    or before a live deck's current position; the successor is audible
    successor_cue_in after it. In the ordinary case a queued element's span
    is exactly effective_airtime_seconds(). `allowance` adds one poll tick
    of trigger latency, so the result is an upper bound."""
    cue_in = cue_in or 0.0
    successor_cue_in = successor_cue_in or 0.0
    trigger = end - successor_cue_in
    if trigger < TRIGGER_CLAMP_SECONDS:
        trigger = end
    trigger = max(trigger, POLL_MIN_POSITION_SECONDS)
    if position is not None:
        trigger = max(trigger, position)
    next_audible = trigger + successor_cue_in
    start = position if position is not None else cue_in
    return max(0.0, next_audible - start) + allowance


def _timing_problem(item):
    end = item.end_point()
    if end is None:
        return "no_duration"
    if end <= 0 or (item.cue_in or 0.0) < 0:
        return "nonpositive_or_negative_timing"
    if item.duration is not None and item.duration <= 0:
        return "nonpositive_duration"
    if end - (item.cue_in or 0.0) <= 0:
        return "nonpositive_effective_airtime"
    return None


@dataclass(frozen=True)
class TimingElement:
    log_item_id: int
    cue_in: float
    end: float
    position: float | None = None   # set only for the live deck element
    role: str = "queue"             # deck / forced / queue


@dataclass(frozen=True)
class RunwayBreakdown:
    elements: tuple                 # (log_item_id, role, span_seconds)
    total_seconds: float
    deck_remaining_seconds: float
    allowance_seconds: float
    skipped: tuple                  # (log_item_id, reason) counted as zero


def sequence_runway(elements, *, tail_successor_cue_in=None, allowance=TRANSITION_ALLOWANCE_SECONDS):
    """Upper-bound wall-clock runway of an ordered element sequence: each
    element's span depends on its successor's cue-in (the clamp)."""
    spans = []
    for index, element in enumerate(elements):
        successor = elements[index + 1].cue_in if index + 1 < len(elements) else tail_successor_cue_in
        spans.append(element_span(element.cue_in, element.end, successor,
                                  position=element.position, allowance=allowance))
    return spans


def committed_sequence(snapshot, live_deck):
    """(elements, skipped, problems): the live deck element, then forced
    items, then the queue from the cursor -- each LogItem at most once, the
    deck's item never again, poison-guarded and deleted-Track ghosts
    skipped as the engine skips them."""
    elements, skipped, problems = [], [], []
    seen = set()
    item = live_deck.item
    elements.append(TimingElement(item.log_item_id, item.cue_in or 0.0, item.end_point(),
                                  position=live_deck.position_seconds, role="deck"))
    seen.add(item.log_item_id)
    future = [(it, "forced") for it in snapshot.forced]
    forced_ids = {it.log_item_id for it in snapshot.forced}
    future += [(it, "queue") for it in snapshot.queue[snapshot.cursor:] if it.log_item_id not in forced_ids]
    for it, role in future:
        if it.log_item_id in seen:
            continue
        seen.add(it.log_item_id)
        if (it.playlist_log_id, it.log_item_id) in snapshot.poison_skip:
            skipped.append((it.log_item_id, "poison_guarded"))
            continue
        if it.track_id is None:
            skipped.append((it.log_item_id, "track_deleted"))
            continue
        problem = _timing_problem(it)
        if problem:
            problems.append(f"li{it.log_item_id}:{problem}")
            continue
        elements.append(TimingElement(it.log_item_id, it.cue_in or 0.0, it.end_point(), role=role))
    return elements, skipped, problems


# ---------------------------------------------------------------------------
# Boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoundaryProof:
    """The next programming takeover after the active log, and what proves
    it. Every v1 boundary is a NATURAL handoff (the playing deck crosses;
    docs/SCHEDULE_PROFILES.md) -- Phase B still allows zero overrun."""
    kind: str
    instant: datetime | None
    source: str                     # approved_next_log / schedule_resolution / none
    target_date: object = None
    target_hour: int | None = None
    next_log_id: int | None = None
    handoff: str = "natural"
    reason: str = ""

    def as_dict(self):
        return {
            "kind": self.kind, "instant": timezone.localtime(self.instant).isoformat() if self.instant else None,
            "source": self.source, "target_date": str(self.target_date) if self.target_date else None,
            "target_hour": self.target_hour, "next_log_id": self.next_log_id,
            "handoff": self.handoff, "reason": self.reason,
        }


def next_hour_key(log_date, log_hour):
    nxt = datetime.combine(log_date, dtime(log_hour)) + timedelta(hours=1)
    return nxt.date(), nxt.hour


def _hour_start(log_date, log_hour, tz):
    return timezone.make_aware(datetime.combine(log_date, dtime(log_hour)), tz)


def dst_problem(snapshot, boundary, tz=None):
    """Non-None when the active hour, the next hour, now or the boundary
    straddle a UTC-offset change, or an hour key does not exist on the
    wall clock. Never fabricate a window across DST."""
    tz = tz or timezone.get_current_timezone()
    a_date, a_hour = snapshot.active_log_date, snapshot.active_log_hour
    n_date, n_hour = next_hour_key(a_date, a_hour)
    starts = [_hour_start(a_date, a_hour, tz), _hour_start(n_date, n_hour, tz)]
    for start, (d, h) in zip(starts, [(a_date, a_hour), (n_date, n_hour)]):
        local = start.astimezone(dt_timezone.utc).astimezone(tz)  # a real round trip
        if (local.date(), local.hour) != (d, h):
            return "hour_does_not_exist"
    offsets = {s.utcoffset() for s in starts}
    offsets.add(snapshot.captured_at.astimezone(tz).utcoffset())
    if boundary.instant is not None:
        offsets.add(boundary.instant.astimezone(tz).utcoffset())
    if len(offsets) != 1:
        return "utc_offset_change"
    return None


# ---------------------------------------------------------------------------
# Causes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class QualifiedCause:
    cause_id: int | None
    source_key: str
    cause_type: str
    occurred_at: datetime
    log_item_id: int
    scheduled_category_id: int | None
    scheduled_category_code: str
    observed_shortening_seconds: float
    original_track_id: int | None
    replacement_track_id: int | None


def partition_causes(snapshot, records):
    """(qualified, excluded): Phase A's trace ordering/dedup, then only
    causes for THIS active log whose occurrence is still provably what the
    cause describes. Excluded causes are listed, never deleted."""
    trace = build_causality_trace(
        records, active_log_date=snapshot.active_log_date, active_log_hour=snapshot.active_log_hour,
    )
    excluded = [(key, reason) for key, reason in trace.duplicates]
    excluded += [(r.source_key, "other_hour") for r in trace.other_hour_causes]
    deck_ids = {d.item.log_item_id for d in snapshot.decks if d.item is not None}
    pending_ids = {it.log_item_id for it in snapshot.forced}
    pending_ids |= {it.log_item_id for it in snapshot.queue[snapshot.cursor:]}
    by_id = {it.log_item_id: it for it in snapshot.queue}
    by_id.update({it.log_item_id: it for it in snapshot.forced})
    qualified = []
    for record in trace.causes:
        if record.log_id != snapshot.active_log_id:
            excluded.append((record.source_key, "stale_log"))
            continue
        if record.scheduled_category_kind_code != "music" or record.scheduled_category_id is None:
            excluded.append((record.source_key, "not_music_slot"))
            continue
        occurrence = by_id.get(record.log_item_id)
        if occurrence is None:
            excluded.append((record.source_key, "missing_occurrence"))
            continue
        if record.cause_type == CAUSE_REQUEST_SUBSTITUTION:
            if occurrence.track_id != record.replacement_track_id:
                excluded.append((record.source_key, "superseded"))
                continue
        elif record.cause_type == CAUSE_OPERATOR_EJECT:
            if record.log_item_id in pending_ids or record.log_item_id in deck_ids:
                excluded.append((record.source_key, "occurrence_still_pending"))
                continue
        else:
            excluded.append((record.source_key, "unknown_cause_type"))
            continue
        qualified.append(QualifiedCause(
            cause_id=record.id, source_key=record.source_key, cause_type=record.cause_type,
            occurred_at=record.occurred_at, log_item_id=record.log_item_id,
            scheduled_category_id=record.scheduled_category_id,
            scheduled_category_code=record.scheduled_category_code,
            observed_shortening_seconds=record.observed_shortening_seconds,
            original_track_id=record.original_track_id,
            replacement_track_id=record.replacement_track_id,
        ))
    return tuple(qualified), tuple(excluded)


def threshold_for(qualified):
    """(seconds, basis). Any web substitution => the strict 60 s web rule
    for the combined net deficit; Eject-only => the provisional 30 s gate."""
    if any(c.cause_type == CAUSE_REQUEST_SUBSTITUTION for c in qualified):
        return float(REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS), "request_substitution_present"
    return float(EJECT_MUSIC_TRIGGER_THRESHOLD_SECONDS), "operator_eject_only_provisional"


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RefillProjection:
    outcome: str
    reasons: tuple
    snapshot_digest: str
    captured_at: datetime
    engine_instance: str
    active_log_id: int | None
    active_log_date: object
    active_log_hour: int | None
    boundary: BoundaryProof | None = None
    window_seconds: float | None = None
    runway: RunwayBreakdown | None = None
    net_shortage_seconds: float | None = None
    overfill_seconds: float | None = None
    threshold_seconds: float | None = None
    threshold_basis: str | None = None
    qualified_causes: tuple = ()
    excluded_causes: tuple = ()
    observed_shortening_seconds: float = 0.0   # diagnostic only, never the deficit
    authoritative_for_install: bool = False
    notes: tuple = field(default=(EVIDENCE_NOTE, "advisory projection; nothing was installed"))

    @property
    def eligible(self):
        return self.outcome == OUTCOME_ELIGIBLE


def transport_block(snapshot):
    """(outcome, reason) when on-air ownership or the deck state makes the
    window unsafe to project, else None."""
    checks = (
        (snapshot.manual_mode, OUTCOME_BLOCKED_TRANSPORT, "manual_mode"),
        (snapshot.remote_dj, OUTCOME_BLOCKED_TRANSPORT, "remote_dj"),
        (snapshot.mic_live, OUTCOME_BLOCKED_TRANSPORT, "mic_live"),
        (snapshot.vt_phase != "idle", OUTCOME_BLOCKED_TRANSPORT, "voice_track"),
        (snapshot.start_next_active, OUTCOME_BLOCKED_TRANSPORT, "start_next"),
        (snapshot.next_triggered, OUTCOME_BLOCKED_TRANSPORT, "transition_pending"),
        (snapshot.resume_pending, OUTCOME_BLOCKED_TRANSPORT, "resume_pending"),
        (snapshot.live_fill_in_progress, OUTCOME_BLOCKED_TRANSPORT, "live_fill_pending"),
        (any(d.paused for d in snapshot.decks), OUTCOME_BLOCKED_TRANSPORT, "paused_deck"),
    )
    for condition, outcome, reason in checks:
        if condition:
            return outcome, reason
    live = [d for d in snapshot.decks if d.live]
    if len(live) > 1:
        return OUTCOME_BLOCKED_TRANSPORT, "overlap"
    if not live:
        return OUTCOME_UNSAFE_WINDOW, "no_live_deck"
    return None


def _hour_relation(snapshot, boundary):
    """None if the active log owns the window to `boundary`, else a reason.
    Owned: wall hour == active hour, or the wall hour is the next hour and
    it is a partial hour whose takeover is still ahead (continuation)."""
    now = snapshot.captured_at.astimezone(timezone.get_current_timezone())
    wall = (now.date(), now.hour)
    active = (snapshot.active_log_date, snapshot.active_log_hour)
    if wall == active:
        return None
    if (wall == next_hour_key(*active) and boundary is not None and boundary.kind == BOUNDARY_PARTIAL
            and boundary.instant is not None and boundary.instant > snapshot.captured_at):
        return None
    return "active_log_not_current_hour"


def project_refill(snapshot, *, causes, boundary, allowance=TRANSITION_ALLOWANCE_SECONDS):
    """Pure, deterministic net-deficit projection. Never writes."""
    base = dict(
        snapshot_digest=snapshot.digest(),
        captured_at=snapshot.captured_at,
        engine_instance=snapshot.engine_instance,
        active_log_id=snapshot.active_log_id,
        active_log_date=snapshot.active_log_date,
        active_log_hour=snapshot.active_log_hour,
        boundary=boundary,
    )

    def result(outcome, *reasons, **extra):
        return RefillProjection(outcome=outcome, reasons=tuple(reasons), **{**base, **extra})

    if snapshot.capture_errors:
        return result(OUTCOME_INVALID_SNAPSHOT, *snapshot.capture_errors)
    if snapshot.active_log_id is None or snapshot.active_log_date is None or snapshot.active_log_hour is None:
        return result(OUTCOME_INVALID_SNAPSHOT, "no_active_log")
    if timezone.is_naive(snapshot.captured_at):
        return result(OUTCOME_INVALID_SNAPSHOT, "naive_clock")
    if not 0 <= snapshot.cursor <= len(snapshot.queue):
        return result(OUTCOME_INVALID_SNAPSHOT, "cursor_out_of_range")
    if any(it.playlist_log_id != snapshot.active_log_id for it in snapshot.queue):
        return result(OUTCOME_INVALID_SNAPSHOT, "queue_mixes_logs")

    relation = _hour_relation(snapshot, boundary)
    if relation:
        return result(OUTCOME_WRONG_HOUR, relation)
    blocked = transport_block(snapshot)
    if blocked:
        return result(blocked[0], blocked[1])
    (deck,) = [d for d in snapshot.decks if d.live]
    if deck.item is None or deck.item.track_id is None:
        return result(OUTCOME_UNSAFE_WINDOW, "deck_occurrence_unknown")
    if deck.position_seconds is None:
        return result(OUTCOME_UNSAFE_WINDOW, "position_unknown")
    problem = _timing_problem(deck.item)
    if problem:
        return result(OUTCOME_UNSAFE_WINDOW, f"deck_{problem}")
    ceiling = max(deck.item.duration or 0.0, deck.item.end_point()) + POSITION_SANITY_SECONDS
    if deck.position_seconds < 0 or deck.position_seconds > ceiling:
        return result(OUTCOME_UNSAFE_WINDOW, "position_implausible")

    if boundary is None or boundary.kind == BOUNDARY_UNKNOWN or boundary.instant is None:
        return result(OUTCOME_NO_SAFE_BOUNDARY, (boundary.reason if boundary else "") or "boundary_unproven")
    dst = dst_problem(snapshot, boundary)
    if dst:
        return result(OUTCOME_UNSAFE_WINDOW, f"dst_{dst}")
    window = (boundary.instant - snapshot.captured_at).total_seconds()
    if window <= MIN_WINDOW_SECONDS:
        return result(OUTCOME_UNSAFE_WINDOW, "imminent_takeover", window_seconds=_ms(window))
    if window > MAX_WINDOW_SECONDS:
        return result(OUTCOME_UNSAFE_WINDOW, "window_implausible", window_seconds=_ms(window))

    elements, skipped, problems = committed_sequence(snapshot, deck)
    if problems:
        return result(OUTCOME_UNSAFE_WINDOW, "unreliable_duration", *problems, window_seconds=_ms(window))
    spans = sequence_runway(elements, allowance=allowance)
    runway = RunwayBreakdown(
        elements=tuple((e.log_item_id, e.role, _ms(s)) for e, s in zip(elements, spans)),
        total_seconds=_ms(sum(spans)),
        deck_remaining_seconds=_ms(spans[0]),
        allowance_seconds=_ms(allowance * len(spans)),
        skipped=tuple(skipped),
    )
    net = _ms(max(0.0, window - runway.total_seconds))
    overfill = _ms(max(0.0, runway.total_seconds - window))
    qualified, excluded = partition_causes(snapshot, causes)
    threshold, basis = threshold_for(qualified)
    measured = dict(
        window_seconds=_ms(window), runway=runway, net_shortage_seconds=net, overfill_seconds=overfill,
        qualified_causes=qualified, excluded_causes=excluded,
        observed_shortening_seconds=_ms(sum(c.observed_shortening_seconds for c in qualified)),
    )
    if not qualified:
        return result(OUTCOME_NO_QUALIFYING_CAUSE, "no_current_hour_cause", **measured)
    measured.update(threshold_seconds=threshold, threshold_basis=basis)
    if net <= 0:
        return result(OUTCOME_ALREADY_SATISFIED, "committed_runway_reaches_boundary", **measured)
    if not net > threshold:
        return result(OUTCOME_BELOW_THRESHOLD, f"net_not_greater_than_{threshold:g}s", **measured)
    return result(OUTCOME_ELIGIBLE, "net_shortage_exceeds_threshold", **measured)
