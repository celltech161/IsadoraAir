"""Roadmap 2.27 Phase B -- read-only, advisory hour-refill PROPOSALS.

evaluate_hour_refill(snapshot) answers, without writing anything: is there a
safe, real net gap now; which causes made us look; which allowed music
categories give the best-fitting one or two tracks (and, when explicitly
configured, one residual PSA); where could a later installer place them;
and when must we abstain.

Off the engine thread only: it reads the database (policy, causes,
boundary, request reservations, candidate pools) and stats a few media
files. It never inserts, updates or deletes a row, never touches the live
queue and never reserves a Track. A proposal may be stale the moment it is
returned; Phase C must revalidate everything in its own transaction.
See docs/roadmap/2_27_PHASE_B_PROPOSAL_CONTRACT.md.
"""
from __future__ import annotations

import bisect
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from types import SimpleNamespace

from django.db.models import Min
from django.utils import timezone

from library.services.hour_refill_causality import CauseRecord, _ms
from library.services.hour_refill_projection import (
    BOUNDARY_ORDINARY,
    BOUNDARY_PARTIAL,
    BOUNDARY_UNKNOWN,
    EVIDENCE_NOTE,
    OUTCOME_OFF,
    POSITION_SANITY_SECONDS,
    TRANSITION_ALLOWANCE_SECONDS,
    BoundaryProof,
    committed_sequence,
    element_span,
    next_hour_key,
    project_refill,
)

MAX_MUSIC_TRACKS = 2
MIN_MUSIC_CANDIDATE_SECONDS = 30.0     # shorter "music" is not a refill candidate
MAX_ROWS_PER_SOURCE = 5000             # bounded pool per source category
PAIR_NEIGHBOURS = 8                    # valid partners examined per first track
PAIR_SCAN_LIMIT = 64                   # partners scanned per first track, conflicts included
PAIR_EVALUATION_BUDGET = 50_000        # deterministic count budget (no wall-clock cut-off)
MEDIA_RETRY_LIMIT = 8                  # re-searches after a winner fails the file check
PSA_RESIDUAL_LIMIT_SECONDS = 60.0      # residual PSA only for 0 < residual <= 60
PSA_CATEGORY_KIND = "spot"             # v1: an operator-designated spot-kind category
PROPOSAL_TTL_SECONDS = 20.0
CAUSE_LOOKBACK_DAYS = 1
CAUSE_ROW_LIMIT = 500

STATUS_PROPOSED = "proposed"
STATUS_NO_SAFE_ANCHOR = "no_safe_anchor"
STATUS_NO_CATEGORY = "no_eligible_music_category"
STATUS_NO_FIT = "no_fit"

SOURCE_ORIGINAL = "original_cause"
SOURCE_LAST_MUSIC = "last_remaining_music"


@dataclass(frozen=True)
class PsaResidualPolicy:
    """Explicit, caller-supplied (dry-run) PSA residual configuration. Off
    by default; there is no persisted PSA setting in Phase B."""
    enabled: bool = False
    category_id: int | None = None


# ---------------------------------------------------------------------------
# Database reads (worker-owned connection; never on the engine thread)
# ---------------------------------------------------------------------------

def policy_enabled():
    """HourRefillPolicy.enabled, read without creating the row."""
    from library.models import HourRefillPolicy

    return bool(HourRefillPolicy.objects.filter(pk=1).values_list("enabled", flat=True).first())


def load_relevant_causes(snapshot):
    """Causes near the active log's date, in causal order (bounded)."""
    from library.models import HourRefillCause

    if snapshot.active_log_date is None:
        return []
    low = snapshot.active_log_date - timedelta(days=CAUSE_LOOKBACK_DAYS)
    high = snapshot.active_log_date + timedelta(days=CAUSE_LOOKBACK_DAYS)
    rows = (HourRefillCause.objects.filter(log_date__gte=low, log_date__lte=high)
            .order_by("occurred_at", "id")[:CAUSE_ROW_LIMIT])
    return [CauseRecord.from_model(row) for row in rows]


def _active_profile():
    from library.models import ScheduleProfileState

    state = ScheduleProfileState.objects.filter(pk=1).select_related("active_profile").first()
    return state.active_profile if state is not None else None


def resolve_boundary(snapshot):
    """The next programming takeover after the active log's hour. An
    approved next-hour log is the authority (its earliest persisted
    scheduled_time, the engine's own _takeover_eligibility rule);
    otherwise the live schedule resolution the engine uses before
    materialisation. A blank or empty next hour is unproven."""
    from library.models import PlaylistLog
    from library.services.engine import PlaybackEngine
    from library.services.schedule_resolution import resolve_schedule_segments

    if snapshot.active_log_date is None or snapshot.active_log_hour is None:
        return BoundaryProof(BOUNDARY_UNKNOWN, None, "none", reason="no_active_log")
    profile = _active_profile()
    if profile is None:
        return BoundaryProof(BOUNDARY_UNKNOWN, None, "none", reason="no_active_schedule_profile")

    # Phase B does not model an intra-log program handoff. A later segment
    # in the active hour is therefore an earlier protected boundary, not
    # spare runway to the next hour. Abstain rather than proposing material
    # that Phase C could place across that transition.
    active_start = timezone.make_aware(
        datetime.combine(snapshot.active_log_date, dtime(snapshot.active_log_hour)),
        timezone.get_current_timezone(),
    )
    future_transitions = [
        active_start + timedelta(minutes=segment.start_minute)
        for segment in resolve_schedule_segments(
            snapshot.active_log_date, snapshot.active_log_hour, profile,
        )
        if active_start + timedelta(minutes=segment.start_minute) > snapshot.captured_at
    ]
    if future_transitions:
        return BoundaryProof(
            BOUNDARY_UNKNOWN, None, "schedule_resolution",
            snapshot.active_log_date, snapshot.active_log_hour,
            reason="future_intra_log_transition_unmodelled",
        )

    n_date, n_hour = next_hour_key(snapshot.active_log_date, snapshot.active_log_hour)
    n_start = timezone.make_aware(datetime.combine(n_date, dtime(n_hour)), timezone.get_current_timezone())
    row = (PlaylistLog.objects.filter(date=n_date, hour=n_hour, status="approved")
           .annotate(first_scheduled=Min("items__scheduled_time"))
           .values_list("id", "first_scheduled").first())
    if row is not None:
        log_id, first_scheduled = row
        if first_scheduled is None:
            return BoundaryProof(BOUNDARY_UNKNOWN, None, "approved_next_log", n_date, n_hour, log_id,
                                 reason="next_log_has_no_items")
        _eligible, takeover = PlaybackEngine._takeover_eligibility(n_date, n_hour, first_scheduled,
                                                                   snapshot.captured_at)
        if takeover is None:
            return BoundaryProof(BOUNDARY_ORDINARY, n_start, "approved_next_log", n_date, n_hour, log_id)
        return BoundaryProof(BOUNDARY_PARTIAL, takeover, "approved_next_log", n_date, n_hour, log_id)
    segments = resolve_schedule_segments(n_date, n_hour, profile)
    if not segments:
        return BoundaryProof(BOUNDARY_UNKNOWN, None, "none", n_date, n_hour, reason="next_hour_unscheduled")
    minute = segments[0].start_minute
    kind = BOUNDARY_ORDINARY if minute == 0 else BOUNDARY_PARTIAL
    return BoundaryProof(kind, n_start + timedelta(minutes=minute), "schedule_resolution", n_date, n_hour)


@dataclass(frozen=True)
class RequestContext:
    active_track_ids: frozenset     # Tracks of pending/no_slot_soon/scheduled requests
    request_item_ids: frozenset     # LogItems reserved by an active request
    intro_item_ids: frozenset       # dedication intro LogItems

    def digest(self):
        raw = repr((sorted(self.active_track_ids), sorted(self.request_item_ids), sorted(self.intro_item_ids)))
        return hashlib.sha256(raw.encode()).hexdigest()


def load_request_context():
    """Internal ids only -- never requester names, messages or text."""
    from webrequests.models import SongRequest

    rows = (SongRequest.objects.filter(status__in=SongRequest.ACTIVE_STATUSES)
            .values_list("track_id", "log_item_id", "intro_log_item_id"))
    tracks, items, intros = set(), set(), set()
    for track_id, item_id, intro_id in rows:
        tracks.add(track_id)
        items.add(item_id)
        intros.add(intro_id)
    return RequestContext(*(frozenset(ids - {None}) for ids in (tracks, items, intros)))


# ---------------------------------------------------------------------------
# Placement (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    mode: str | None                 # tail_append / insert_before / None
    reason: str
    after_log_item_id: int | None = None
    anchor_log_item_id: int | None = None
    start_position: int | None = None
    requires: str = ""

    def as_dict(self):
        return {"mode": self.mode, "reason": self.reason, "after_log_item_id": self.after_log_item_id,
                "anchor_log_item_id": self.anchor_log_item_id, "start_position": self.start_position,
                "requires": self.requires, "advisory": True}


def assess_placement(snapshot, request_ctx):
    """Where a later installer could legally put new items, if provable.

    Tail append after a final MUSIC row. When the log ends in non-music
    rows (legal ID, PSA, imaging, underwriting -- treated as a protected
    closing block), only an insert-before anchor at the block's first row,
    and only when every row of that block is still in the future,
    unclaimed, unplayed, not on a deck, not forced and not tied to a
    request or dedication intro, and is not the very next item. Nothing is
    reordered; the closing block's rows keep their identity and order."""
    queue = snapshot.queue
    if not queue:
        return Placement(None, "empty_queue")
    last = queue[-1]
    if last.category_kind is None:
        return Placement(None, "tail_kind_unknown")
    if last.category_kind == "music":
        return Placement("tail_append", "final_row_is_music", after_log_item_id=last.log_item_id,
                         start_position=(last.position or 0) + 1,
                         requires="append_fill_items-style tail append after a fresh revalidation")
    index = len(queue) - 1
    while index >= 0 and queue[index].category_kind not in ("music", None):
        index -= 1
    if index >= 0 and queue[index].category_kind is None:
        return Placement(None, "closing_block_kind_unknown")
    anchor_index = index + 1
    anchor = queue[anchor_index]
    if anchor_index < snapshot.cursor:
        return Placement(None, "closing_block_already_dequeued", anchor_log_item_id=anchor.log_item_id)
    if anchor_index == snapshot.cursor:
        return Placement(None, "anchor_next_up", anchor_log_item_id=anchor.log_item_id)
    deck_ids = {d.item.log_item_id for d in snapshot.decks if d.item is not None}
    forced_ids = {it.log_item_id for it in snapshot.forced}
    for row in queue[anchor_index:]:
        problem = (
            "closer_claimed" if row.claimed else
            "closer_played" if row.played else
            "closer_on_deck" if row.log_item_id in deck_ids else
            "closer_forced" if row.log_item_id in forced_ids else
            "closer_request_slot" if row.log_item_id in request_ctx.request_item_ids else
            "closer_dedication_intro" if row.log_item_id in request_ctx.intro_item_ids else
            "closer_unplayable" if row.track_id is None else None
        )
        if problem:
            return Placement(None, problem, anchor_log_item_id=anchor.log_item_id)
    if anchor_index > 0 and queue[anchor_index - 1].log_item_id in request_ctx.intro_item_ids:
        return Placement(None, "intro_adjacency", anchor_log_item_id=anchor.log_item_id)
    return Placement("insert_before", "protected_closing_block", anchor_log_item_id=anchor.log_item_id,
                     start_position=anchor.position,
                     requires="a guarded insert-before splice (Phase C); append_fill_items cannot "
                              "place rows before an existing one")


# ---------------------------------------------------------------------------
# Fit frame and deterministic search (pure)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    track_id: int
    category_id: int
    rank: int                       # 0 original-cause category, 1 last-remaining-music category
    cue_in: float
    end: float
    effective: float
    identity_keys: frozenset
    filepath: str


@dataclass(frozen=True)
class FitFrame:
    """Committed timing split at the insertion point. total(seq) is the
    exact (upper-bound) runway with `seq` inserted, using the engine's
    trigger arithmetic at each new adjacency."""
    window: float
    prefix_fixed: float
    last_prefix: object
    suffix_first_cue: float | None
    suffix_total: float
    allowance: float
    # Number of prefix elements whose spans contribute `allowance` to
    # prefix_fixed + _last(). Fit arithmetic intentionally uses that
    # upper-bound allowance, but an adoption deadline needs the opposite
    # bound: the earliest possible trigger.
    prefix_element_count: int = 1
    # Phase C safety budget: seconds that must stay unused after the
    # inserted items (base + per inserted item). Zero in Phase B.
    reserve_base: float = 0.0
    reserve_per_item: float = 0.0

    def _last(self, successor_cue):
        last = self.last_prefix
        return element_span(last.cue_in, last.end, successor_cue, position=last.position,
                            allowance=self.allowance)

    def base_total(self):
        return self.prefix_fixed + self._last(self.suffix_first_cue) + self.suffix_total

    def reserve(self, count):
        return self.reserve_base + self.reserve_per_item * count if count else 0.0

    def total(self, seq):
        """Runway with `seq` inserted, plus the safety reserve for it."""
        total = self.prefix_fixed + self._last(seq[0].cue_in) + self.suffix_total
        for index, item in enumerate(seq):
            successor = seq[index + 1].cue_in if index + 1 < len(seq) else self.suffix_first_cue
            total += element_span(item.cue_in, item.end, successor, allowance=self.allowance)
        return total + self.reserve(len(seq))

    def insertion_lead(self, first):
        """Conservative seconds until `first` can become audible.

        The fit model adds one poll allowance per prefix transition because
        later playback is the safe bound for avoiding boundary overrun. An
        adoption deadline must not count those allowances: the engine can
        observe every trigger without any late poll at all.
        """
        upper = self.prefix_fixed + self._last(first.cue_in)
        return max(0.0, upper - self.allowance * self.prefix_element_count)


def build_fit_frame(snapshot, projection, placement, allowance=TRANSITION_ALLOWANCE_SECONDS, safety_budget=None):
    (deck,) = [d for d in snapshot.decks if d.live]
    elements, _skipped, _problems = committed_sequence(snapshot, deck)
    split = len(elements)
    if placement.mode == "insert_before":
        ids = [e.log_item_id for e in elements]
        if placement.anchor_log_item_id not in ids:
            return None
        split = ids.index(placement.anchor_log_item_id)
    prefix, suffix = elements[:split], elements[split:]
    if not prefix:
        return None
    prefix_spans = [element_span(e.cue_in, e.end, prefix[i + 1].cue_in, position=e.position, allowance=allowance)
                    for i, e in enumerate(prefix[:-1])]
    suffix_spans = [element_span(e.cue_in, e.end, suffix[i + 1].cue_in if i + 1 < len(suffix) else None,
                                 allowance=allowance)
                    for i, e in enumerate(suffix)]
    return FitFrame(
        window=projection.window_seconds,
        prefix_fixed=sum(prefix_spans),
        last_prefix=prefix[-1],
        suffix_first_cue=suffix[0].cue_in if suffix else None,
        suffix_total=sum(suffix_spans),
        allowance=allowance,
        prefix_element_count=len(prefix),
        reserve_base=safety_budget[0] if safety_budget else 0.0,
        reserve_per_item=safety_budget[1] if safety_budget else 0.0,
    )


@dataclass(frozen=True)
class FitResult:
    items: tuple
    total_seconds: float
    residual_seconds: float
    evaluations: int
    truncated: bool


def _rank_key(residual, seq):
    return (round(residual, 3), sum(c.rank for c in seq), len(seq), tuple(c.track_id for c in seq))


def search_best_fit(candidates, frame, *, max_tracks=MAX_MUSIC_TRACKS, budget=PAIR_EVALUATION_BUDGET):
    """Best one- or two-track sequence with zero overrun. Ranked by smallest
    non-negative residual, then original-cause category, then fewer tracks,
    then ascending Track ids. Deterministic: no randomness and a COUNT
    budget, not a clock. Pairs: for each first track, the largest valid
    partners at or below the remaining gap via bisect (O(N log N))."""
    best = None
    evaluations = 0
    truncated = False

    def consider(seq):
        nonlocal best, evaluations
        evaluations += 1
        total = frame.total(seq)
        residual = frame.window - total
        if residual < 0:
            return
        key = _rank_key(residual, seq)
        if best is None or key < best[0]:
            best = (key, tuple(seq), total, residual)

    ordered = sorted(candidates, key=lambda c: (c.effective, c.track_id))
    for candidate in ordered:
        consider([candidate])
    if max_tracks >= 2 and len(ordered) > 1:
        airtimes = [c.effective for c in ordered]
        available = frame.window - frame.base_total()
        for first in sorted(ordered, key=lambda c: c.track_id):
            if evaluations >= budget:
                truncated = True
                break
            index = bisect.bisect_right(airtimes, available - first.effective) - 1
            valid = scanned = 0
            while index >= 0 and valid < PAIR_NEIGHBOURS and scanned < PAIR_SCAN_LIMIT:
                second = ordered[index]
                index -= 1
                scanned += 1
                if second.track_id == first.track_id or first.identity_keys & second.identity_keys:
                    continue
                valid += 1
                consider([first, second])
                consider([second, first])
    if best is None:
        return FitResult((), 0.0, 0.0, evaluations, truncated)
    _key, seq, total, residual = best
    return FitResult(seq, _ms(total), _ms(residual), evaluations, truncated)


# ---------------------------------------------------------------------------
# Candidate pools (database reads, strict -- never loosened)
# ---------------------------------------------------------------------------

def _window_slots(start, end, tz):
    """Track.blocked_slots indices (weekday*24 + hour) touched by [start, end)."""
    slots, cursor = [], start.astimezone(tz).replace(minute=0, second=0, microsecond=0)
    while cursor < end:
        local = cursor.astimezone(tz)
        slot = local.weekday() * 24 + local.hour
        if slot not in slots:
            slots.append(slot)
        cursor += timedelta(hours=1)
    return slots


def _reserved_media(filepath):
    from active_messages import playout as active_playout
    from spoken import playout as spoken_playout

    probe = SimpleNamespace(filepath=filepath)
    return spoken_playout.is_candidate(probe) or active_playout.is_candidate(probe)


def _timing_from_row(cue_in, next_start, duration):
    """(cue_in, end, effective) for a candidate, or (None, reason)."""
    if duration is None or duration <= 0:
        return None, "unreliable_duration"
    if next_start is not None and (next_start <= 0 or next_start > duration + POSITION_SANITY_SECONDS):
        return None, "unreliable_next_start"
    cue_in = cue_in or 0.0
    end = next_start if next_start is not None else duration
    if cue_in < 0 or end - cue_in <= 0:
        return None, "nonpositive_airtime"
    return (cue_in, end, end - cue_in), None


@dataclass
class ExclusionContext:
    hard_track_ids: set
    hard_identity_keys: set
    holiday_codes: list
    slots: list
    now: datetime


def build_exclusion_context(snapshot, projection, request_ctx):
    """Every Track already committed, loaded, forced, recently caused,
    requested or opening the next hour, plus the identity keys (primary +
    related artists) of everything committed this hour. Hard: never
    relaxed, unlike pick_track's loosening ladder."""
    from library.models import LogItem, Track
    from library.services.log_builder import _active_holidays_at
    from library.services.related_artists import track_identity_keys

    committed = {it.track_id for it in snapshot.queue + snapshot.forced if it.track_id}
    committed |= {d.item.track_id for d in snapshot.decks if d.item is not None and d.item.track_id}
    keys = set()
    for _id, artist, related in Track.objects.filter(id__in=committed).values_list("id", "artist__name",
                                                                                   "related_artists"):
        keys |= track_identity_keys(artist, related)
    hard = set(committed)
    for cause in projection.qualified_causes:
        hard |= {t for t in (cause.original_track_id, cause.replacement_track_id) if t}
    hard |= set(request_ctx.active_track_ids)
    boundary = projection.boundary
    if boundary is not None and boundary.next_log_id:
        hard |= {t for t in LogItem.objects.filter(playlist_log_id=boundary.next_log_id)
                 .values_list("track_id", flat=True) if t}
    tz = timezone.get_current_timezone()
    now = snapshot.captured_at
    return ExclusionContext(
        hard_track_ids=hard,
        hard_identity_keys=keys,
        holiday_codes=[h.code for h in _active_holidays_at(now.astimezone(tz))],
        slots=_window_slots(now, boundary.instant, tz),
        now=now,
    )


def _recency_config():
    from library.models import RecencyConfig

    return RecencyConfig.objects.filter(pk=1).first() or RecencyConfig()  # never created here


def load_candidates(category, rank, ctx, counters, *, min_seconds, max_seconds):
    """Strict pool for one category: Ready-to-Air, category or additional
    category, not blocked in any slot of the window, not tagged with an
    active holiday, outside the category's full recency separation (actual
    airs), not hard-excluded, with reliable positive effective airtime in
    [min_seconds, max_seconds] and a non-reserved file path."""
    from library.services.log_builder import _tracks_for_category, get_recent_exclusions, get_separation
    from library.services.related_artists import track_identity_keys

    artist_sep, title_sep = get_separation(category, _recency_config())
    recent_ids, recent_keys = get_recent_exclusions(ctx.now, artist_sep, title_sep, [], set())
    banned_ids = ctx.hard_track_ids | recent_ids
    banned_keys = ctx.hard_identity_keys | recent_keys
    qs = _tracks_for_category(category)
    for slot in ctx.slots:
        qs = qs.exclude(blocked_slots__contains=[slot])
    if ctx.holiday_codes:
        qs = qs.exclude(holidays__code__in=ctx.holiday_codes)
    if banned_ids:
        qs = qs.exclude(id__in=banned_ids)
    rows = list(qs.order_by("id").values_list(
        "id", "cue_in_seconds", "next_start_seconds", "duration_seconds", "artist__name",
        "related_artists", "filepath",
    )[:MAX_ROWS_PER_SOURCE + 1])
    if len(rows) > MAX_ROWS_PER_SOURCE:
        counters["pool_truncated"] = counters.get("pool_truncated", 0) + 1
        rows = rows[:MAX_ROWS_PER_SOURCE]
    out = []
    for track_id, cue_in, next_start, duration, artist, related, filepath in rows:
        reason = None
        timing, problem = _timing_from_row(cue_in, next_start, duration)
        if timing is None:
            reason = problem
        elif track_identity_keys(artist, related) & banned_keys:
            reason = "artist_separation"
        elif not filepath:
            reason = "no_filepath"
        elif _reserved_media(filepath):
            reason = "reserved_media"
        elif timing[2] < min_seconds:
            reason = "too_short"
        elif timing[2] > max_seconds:
            reason = "longer_than_gap"
        if reason:
            counters[reason] = counters.get(reason, 0) + 1
            continue
        out.append(Candidate(track_id, category.id, rank, timing[0], timing[1], timing[2],
                             track_identity_keys(artist, related), filepath))
    counters["eligible"] = counters.get("eligible", 0) + len(out)
    return out


def _media_available(candidate, cache):
    if candidate.track_id not in cache:
        cache[candidate.track_id] = Path(candidate.filepath).is_file()
    return cache[candidate.track_id]


def search_with_media(candidates, frame, *, max_tracks, media_cache, unavailable):
    """search_best_fit, re-run (bounded) after dropping winners whose file
    is not on disk. Files are checked only for winners."""
    pool = list(candidates)
    result = FitResult((), 0.0, 0.0, 0, False)
    for _attempt in range(MEDIA_RETRY_LIMIT + 1):
        result = search_best_fit(pool, frame, max_tracks=max_tracks)
        missing = [c for c in result.items if not _media_available(c, media_cache)]
        if not missing:
            return result, False
        unavailable.extend(c.track_id for c in missing)
        drop = {c.track_id for c in missing}
        pool = [c for c in pool if c.track_id not in drop]
    return FitResult((), 0.0, 0.0, result.evaluations, result.truncated), True


# ---------------------------------------------------------------------------
# Category sources
# ---------------------------------------------------------------------------

def last_remaining_music(snapshot):
    """(ItemTiming, None) for the active log's last MUSIC-slot occurrence
    still ahead or on the deck, in play order, or (None, reason). Rows of
    another log (a previous hour's song still on the deck, a forced item
    carried across a rollover) are never this hour's source. An unknown-kind
    row after it makes the answer ambiguous (fail closed). Never a PSA, ID,
    imaging or underwriting row, never the requested Track's own category."""
    (deck,) = [d for d in snapshot.decks if d.live]
    sequence = [deck.item]
    forced_ids = {it.log_item_id for it in snapshot.forced}
    sequence += list(snapshot.forced)
    sequence += [it for it in snapshot.queue[snapshot.cursor:] if it.log_item_id not in forced_ids]
    for item in reversed(sequence):
        if (item is None or item.track_id is None or item.playlist_log_id != snapshot.active_log_id
                or (item.playlist_log_id, item.log_item_id) in snapshot.poison_skip):
            continue
        if item.category_kind is None:
            return None, "remaining_kind_unknown"
        if item.category_kind == "music" and item.category_id is not None:
            return item, None
    return None, "no_remaining_music"


@dataclass(frozen=True)
class CategorySource:
    category_id: int
    category_code: str
    rank: int
    provenance: tuple
    status: str = "usable"


def resolve_category_sources(snapshot, projection):
    """Ordered, de-duplicated allowed sources: each qualified cause's
    original scheduled music category (causal order), then the last
    remaining music row's category. Re-checked against the database: a
    deleted or no-longer-music category is dropped. Never LogFillConfig."""
    from library.models import Category

    ordered = {}
    for cause in projection.qualified_causes:
        entry = ordered.setdefault(cause.scheduled_category_id, {"rank": 0, "provenance": []})
        entry["provenance"].append(cause.source_key)
    last, last_reason = last_remaining_music(snapshot)
    if last is not None:
        entry = ordered.setdefault(last.category_id, {"rank": 1, "provenance": []})
        entry["provenance"].append(f"last_music:li{last.log_item_id}")
    rows = {c.id: c for c in Category.objects.filter(id__in=list(ordered)).select_related("kind")}
    sources = []
    for category_id, entry in ordered.items():
        category = rows.get(category_id)
        status = ("category_missing" if category is None else
                  "category_not_music" if category.kind.code != "music" else "usable")
        sources.append((CategorySource(category_id, category.code if category else "", entry["rank"],
                                       tuple(entry["provenance"]), status), category))
    return sources, last_reason


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProposedItem:
    track_id: int
    category_id: int
    category_source: str
    effective_seconds: float

    def as_dict(self):
        return {"track_id": self.track_id, "category_id": self.category_id,
                "category_source": self.category_source, "effective_seconds": self.effective_seconds}


@dataclass(frozen=True)
class RefillProposal:
    status: str
    reasons: tuple
    projection: object = None
    placement: Placement | None = None
    music: tuple = ()
    psa: ProposedItem | None = None
    psa_status: str = "not_evaluated"
    net_before_seconds: float | None = None
    residual_after_seconds: float | None = None
    total_runway_after_seconds: float | None = None
    category_sources: tuple = ()
    selection: dict = field(default_factory=dict)
    request_digest: str = ""
    expires_at: datetime | None = None
    uncertainty: tuple = ()
    advisory: bool = True
    installed: bool = False
    # The chosen Candidate objects (music, then PSA) for a Phase C
    # revalidation; never serialised.
    chosen: tuple = ()
    notes: tuple = (
        "ADVISORY ONLY: nothing was inserted, reordered, reserved or played",
        "may be stale immediately; Phase C must revalidate everything in its own transaction",
        EVIDENCE_NOTE,
    )

    def as_dict(self):
        """JSON-safe, internal ids and numbers only -- no requester names,
        messages, request text, titles or queue objects."""
        p = self.projection
        projection = None
        if p is not None:
            projection = {
                "outcome": p.outcome, "reasons": list(p.reasons), "snapshot_digest": p.snapshot_digest,
                "captured_at": timezone.localtime(p.captured_at).isoformat(),
                "engine_instance": p.engine_instance, "active_log_id": p.active_log_id,
                "active_log_date": str(p.active_log_date) if p.active_log_date else None,
                "active_log_hour": p.active_log_hour,
                "boundary": p.boundary.as_dict() if p.boundary else None,
                "window_seconds": p.window_seconds,
                "committed_runway_seconds": p.runway.total_seconds if p.runway else None,
                "deck_remaining_seconds": p.runway.deck_remaining_seconds if p.runway else None,
                "net_shortage_seconds": p.net_shortage_seconds, "overfill_seconds": p.overfill_seconds,
                "threshold_seconds": p.threshold_seconds, "threshold_basis": p.threshold_basis,
                "observed_shortening_seconds": p.observed_shortening_seconds,
                "qualified_causes": [{"cause_id": c.cause_id, "cause_type": c.cause_type,
                                      "log_item_id": c.log_item_id,
                                      "scheduled_category_id": c.scheduled_category_id,
                                      "observed_shortening_seconds": c.observed_shortening_seconds}
                                     for c in p.qualified_causes],
                "excluded_causes": [list(e) for e in p.excluded_causes],
                "authoritative_for_install": False,
            }
        return {
            "status": self.status, "reasons": list(self.reasons), "advisory": True, "installed": False,
            "projection": projection,
            "placement": self.placement.as_dict() if self.placement else None,
            "music": [m.as_dict() for m in self.music],
            "psa": self.psa.as_dict() if self.psa else None, "psa_status": self.psa_status,
            "net_before_seconds": self.net_before_seconds,
            "residual_after_seconds": self.residual_after_seconds,
            "total_runway_after_seconds": self.total_runway_after_seconds,
            "category_sources": [{"category_id": s.category_id, "category_code": s.category_code,
                                  "source": SOURCE_ORIGINAL if s.rank == 0 else SOURCE_LAST_MUSIC,
                                  "provenance": list(s.provenance), "status": s.status}
                                 for s in self.category_sources],
            "selection": dict(self.selection), "request_digest": self.request_digest,
            "expires_at": timezone.localtime(self.expires_at).isoformat() if self.expires_at else None,
            "uncertainty": list(self.uncertainty), "notes": list(self.notes),
        }


UNCERTAINTY = (
    "next-hour first item's cue-in is not modelled for the final transition (assumed 0)",
    "deck preroll/pipeline start latency is not modelled beyond one poll tick per transition",
    "queued items whose media file is missing are counted as runway (biases toward less fill)",
    "intra-log program segment handoffs are not re-timed; only the hour's end is projected",
)


def _psa_stage(policy, frame, music, ctx, residual, media_cache, unavailable, counters):
    """(psa_item | None, status, total, residual)."""
    from library.models import Category

    if residual <= 0:
        return None, "not_applicable_no_residual", None, residual
    if residual > PSA_RESIDUAL_LIMIT_SECONDS:
        return None, "not_applicable_residual_above_60s", None, residual
    if policy is None or not policy.enabled:
        return None, "psa_disabled", None, residual
    if policy.category_id is None:
        return None, "psa_not_configured", None, residual
    category = Category.objects.filter(id=policy.category_id).select_related("kind").first()
    if category is None:
        return None, "psa_category_missing", None, residual
    if category.kind.code != PSA_CATEGORY_KIND:
        return None, "psa_category_not_psa_kind", None, residual
    music_ids = {m.track_id for m in music}
    music_keys = frozenset().union(*(m.identity_keys for m in music))
    best = None
    for candidate in load_candidates(category, 2, ctx, counters, min_seconds=0.001, max_seconds=residual):
        if candidate.track_id in music_ids or candidate.identity_keys & music_keys:
            continue
        total = frame.total(list(music) + [candidate])
        left = frame.window - total
        key = (round(left, 3), candidate.track_id)
        if left < 0 or (best is not None and key >= best[0]):
            continue
        if not _media_available(candidate, media_cache):
            unavailable.append(candidate.track_id)
            continue
        best = (key, candidate, total, left)
    if best is None:
        return None, "no_psa_fit", None, residual
    _key, candidate, total, left = best
    return candidate, "psa_proposed", _ms(total), _ms(left)


def evaluate_hour_refill(snapshot, *, offline_dry_run=False, psa_policy=None, safety_budget=None):
    """The Phase B entry point. Never writes. With the policy Off (the
    default) and no explicit offline_dry_run it reads one flag and returns
    `off`. offline_dry_run never changes the persisted policy."""
    if not offline_dry_run and not policy_enabled():
        return RefillProposal(OUTCOME_OFF, ("hour_refill_policy_off",))
    boundary = resolve_boundary(snapshot)
    projection = project_refill(snapshot, causes=load_relevant_causes(snapshot), boundary=boundary)
    if not projection.eligible:
        return RefillProposal(projection.outcome, projection.reasons, projection=projection,
                              net_before_seconds=projection.net_shortage_seconds)
    common = dict(projection=projection, net_before_seconds=projection.net_shortage_seconds,
                  uncertainty=UNCERTAINTY)
    request_ctx = load_request_context()
    common["request_digest"] = request_ctx.digest()
    common["expires_at"] = min(snapshot.captured_at + timedelta(seconds=PROPOSAL_TTL_SECONDS),
                               boundary.instant - timedelta(seconds=60))
    sources, last_reason = resolve_category_sources(snapshot, projection)
    common["category_sources"] = tuple(s for s, _c in sources)
    usable = [(s, c) for s, c in sources if s.status == "usable"]
    if not usable:
        return RefillProposal(STATUS_NO_CATEGORY, tuple(s.status for s, _c in sources) + (last_reason or "",),
                              **common)
    placement = assess_placement(snapshot, request_ctx)
    common["placement"] = placement
    if placement.mode is None:
        return RefillProposal(STATUS_NO_SAFE_ANCHOR, (placement.reason,), **common)
    frame = build_fit_frame(snapshot, projection, placement, safety_budget=safety_budget)
    if frame is None:
        return RefillProposal(STATUS_NO_SAFE_ANCHOR, ("anchor_not_in_committed_sequence",), **common)
    ctx = build_exclusion_context(snapshot, projection, request_ctx)
    counters = {}
    by_track = {}
    for source, category in usable:
        for candidate in load_candidates(category, source.rank, ctx, counters,
                                         min_seconds=MIN_MUSIC_CANDIDATE_SECONDS,
                                         max_seconds=projection.net_shortage_seconds):
            kept = by_track.get(candidate.track_id)
            if kept is None or candidate.rank < kept.rank:
                by_track[candidate.track_id] = candidate
    media_cache, unavailable = {}, []
    fit, exhausted = search_with_media(list(by_track.values()), frame, max_tracks=MAX_MUSIC_TRACKS,
                                       media_cache=media_cache, unavailable=unavailable)
    selection = {"pool_by_reason": dict(sorted(counters.items())), "candidates": len(by_track),
                 "evaluations": fit.evaluations, "search_truncated": fit.truncated,
                 "media_unavailable_track_ids": sorted(unavailable), "media_checks": len(media_cache),
                 "media_retry_exhausted": exhausted,
                 "freshness": {"recency_basis_at": timezone.localtime(snapshot.captured_at).isoformat(),
                               "media_checked": "winners only, at proposal time",
                               "snapshot_digest": projection.snapshot_digest},
                 "ranking": "zero overrun; smallest residual; original-cause category; fewer tracks; "
                            "ascending track ids"}
    if not fit.items:
        return RefillProposal(STATUS_NO_FIT, ("no_candidate_fits_without_overrun",), selection=selection,
                              **common)
    music = tuple(fit.items)
    psa, psa_status, psa_total, psa_residual = _psa_stage(
        psa_policy, frame, music, ctx, fit.residual_seconds, media_cache, unavailable, counters,
    )
    selection["media_unavailable_track_ids"] = sorted(unavailable)
    return RefillProposal(
        STATUS_PROPOSED, ("best_fit_selected",), selection=selection,
        music=tuple(ProposedItem(c.track_id, c.category_id,
                                 SOURCE_ORIGINAL if c.rank == 0 else SOURCE_LAST_MUSIC, _ms(c.effective))
                    for c in music),
        psa=(ProposedItem(psa.track_id, psa.category_id, "psa_residual", _ms(psa.effective))
             if psa is not None else None),
        psa_status=psa_status,
        chosen=tuple(music) + ((psa,) if psa is not None else ()),
        residual_after_seconds=psa_residual if psa is not None else fit.residual_seconds,
        total_runway_after_seconds=psa_total if psa is not None else fit.total_seconds,
        **common,
    )


def proposal_staleness(proposal, *, fresh_snapshot, now, active_request_track_ids):
    """Reasons an advisory proposal no longer matches fresh state (empty
    tuple = not obviously stale). A cheap pre-check only; Phase C must
    still revalidate everything under its own locks."""
    reasons = []
    projection = proposal.projection
    if proposal.expires_at is None or now >= proposal.expires_at:
        reasons.append("expired")
    if projection is None or fresh_snapshot.active_log_id != projection.active_log_id:
        reasons.append("active_log_changed")
    engine_changed = projection is None or fresh_snapshot.engine_instance != projection.engine_instance
    if engine_changed:
        reasons.append("engine_instance_changed")
    elif fresh_snapshot.digest() != projection.snapshot_digest:
        reasons.append("queue_or_deck_changed")
    proposed = {m.track_id for m in proposal.music} | ({proposal.psa.track_id} if proposal.psa else set())
    if proposed & set(active_request_track_ids):
        reasons.append("proposed_track_requested")
    committed = {it.track_id for it in fresh_snapshot.queue + fresh_snapshot.forced}
    committed |= {d.item.track_id for d in fresh_snapshot.decks if d.item is not None}
    if proposed & committed:
        reasons.append("proposed_track_now_committed")
    return tuple(reasons)
