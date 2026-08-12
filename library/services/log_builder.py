import bisect
import math
import random
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date, datetime, time, timedelta
from time import monotonic

from django.db import connection, transaction
from django.db.models import Count, Q
from django.db.models.expressions import RawSQL
from django.utils import timezone

from monitoring.models import emit_event

from library.models import (
    Category,
    Holiday,
    LogFillConfig,
    LogItem,
    PlaylistLog,
    RecencyConfig,
    ScheduleBlock,
    Track,
)
from library.services.related_artists import track_identity_keys


class TrackIdentityCache:
    """Per-build cache of track_id -> frozenset(identity keys) --
    normalized primary artist name plus every normalized related-artist
    entry (see related_artists.track_identity_keys). Exists so repeated
    pick_track() calls against the same pool within one build (multiple
    slots of the same category in one hour's rotation, or
    fill_remaining_hour's own loop) load that pool's artist/
    related_artists data ONCE instead of once per call -- the N+1
    pattern this whole cache exists to avoid.

    Keyed by an opaque `pool_key` the caller chooses -- a plain
    category pool uses ("category", category_id); a holiday-injection
    pool (see _music_holiday_pool) uses ("holiday", <sorted holiday
    codes tuple>), since the underlying queryset object's own identity
    changes on every call even for the same holiday-code combination.
    NOT keyed by the queryset itself for that reason.

    Deliberately build-scoped, never a module-global: one instance is
    created per build_hour_log/fill_remaining_hour/preview_hour_log
    call and threaded through every pick_track() call in that build.
    A module-global would leak stale pool data across builds and,
    under gunicorn's worker process reuse, across unrelated requests."""

    def __init__(self):
        self._pools = {}

    def _load_pool(self, pool_key, base_qs):
        if pool_key not in self._pools:
            rows = base_qs.values_list("id", "artist__name", "related_artists")
            self._pools[pool_key] = {
                track_id: track_identity_keys(artist_name, related)
                for track_id, artist_name, related in rows
            }
        return self._pools[pool_key]

    def conflicting_track_ids(self, pool_key, base_qs, banned_identity_keys):
        """Track ids within the pool (loaded/cached under `pool_key`
        from `base_qs`, the UNNARROWED category/holiday pool -- not
        whatever partially-excluded queryset a caller happens to be
        mid-build with) whose identity set intersects
        `banned_identity_keys`. Empty banned set short-circuits to
        avoid loading the pool at all when nothing is excluded."""
        if not banned_identity_keys:
            return set()
        pool = self._load_pool(pool_key, base_qs)
        return {tid for tid, keys in pool.items() if keys & banned_identity_keys}


def _active_holidays_at(target_datetime):
    """Return Holidays currently in their ramp window for target_datetime.

    Handles year wrap by checking last-year, this-year, and next-year
    candidate dates -- Christmas with ramp_out_days=7 is still active on
    Jan 1 (7 days after Dec 25 of the previous calendar year), and
    should be. New Year's on Jan 1 with ramp_in_days=7 is active on
    Dec 25 of the previous year (7 days before Jan 1 of the next).
    """
    target_date = target_datetime.date() if hasattr(target_datetime, 'date') else target_datetime
    active = []
    for h in Holiday.objects.all():
        for year_offset in (-1, 0, 1):
            try:
                cand = _date(target_date.year + year_offset, h.month, h.day)
            except ValueError:
                # Feb 29 on a non-leap year -- rare; skip. If a station
                # ever configures a leap-day holiday, they can set an
                # explicit occurrence per year via specific_date on a
                # ScheduleBlock instead, or bump to Mar 1 in the model.
                continue
            if cand - timedelta(days=h.ramp_in_days) <= target_date <= cand + timedelta(days=h.ramp_out_days):
                active.append(h)
                break
    return active


def _holiday_boost_expr(active_holiday_codes):
    """Return a (sql_snippet, params) pair that resolves to the MAX
    max_weight_boost across a track's active-holiday tags -- 0 if the
    track isn't tagged with any active holiday. Injected into
    _weighted_order's RawSQL so tracks tagged with a currently-ramping
    holiday sort proportionally more likely to come up. Empty active
    list returns ("0", []) so the caller can splice in a bare zero."""
    if not active_holiday_codes:
        return "0", []
    # Holiday.pk is `code` (CharField), so the M2M through-table's
    # holiday_id column stores code strings, not integer ids.
    subq = (
        "COALESCE(("
        "  SELECT MAX(h.max_weight_boost) "
        "  FROM library_holiday h "
        "  INNER JOIN library_track_holidays th ON th.holiday_id = h.code "
        "  WHERE th.track_id = library_track.id "
        "  AND h.code = ANY(%s)"
        "), 0)"
    )
    return subq, [list(active_holiday_codes)]


def _holiday_daily_share(holiday, target_date):
    """Linear-tent ramp share for a Holiday on `target_date`. 0 at the
    ramp-window edges, `Holiday.max_share` at peak, linearly
    interpolated between. Returns 0 outside the ramp window entirely.

    max_share is thus the PEAK-DAY station-wide fraction of music-kind
    picks that should come from this holiday. Setting max_share=0.6
    for Christmas with ramp_in_days=30 gives a smooth curve from ~2%
    at day 1 of the ramp to 60% at Dec 25 back down to 0 at
    ramp-end -- "occasional at the edges, mostly on peak day," which
    is the user's stated mental model.

    Same year-wrap handling as _active_holidays_at."""
    for year_offset in (-1, 0, 1):
        try:
            peak = _date(target_date.year + year_offset, holiday.month, holiday.day)
        except ValueError:
            continue
        ramp_start = peak - timedelta(days=holiday.ramp_in_days)
        ramp_end = peak + timedelta(days=holiday.ramp_out_days)
        if not (ramp_start <= target_date <= ramp_end):
            continue
        if target_date <= peak:
            distance = (peak - target_date).days
            denom = max(1, holiday.ramp_in_days)
        else:
            distance = (target_date - peak).days
            denom = max(1, holiday.ramp_out_days)
        fraction = 1.0 - distance / denom
        return float(holiday.max_share) * fraction
    return 0.0


def _music_holiday_pool(chosen_holiday_codes, target_datetime):
    """Station-wide pool for a "this-slot-is-a-holiday-slot" pick:
    ready2air tracks tagged with any of the given holiday codes AND
    filed under at least one music-kind Category (via primary or
    additional_categories). Deliberately doesn't respect the SLOT's
    specific category -- the whole point of the injection is that a
    50's Rock slot on Christmas Day is fine playing a Christmas song
    filed under 60's Rock or Country Classics.

    Music-kind gate prevents holiday-tagged imaging/spot/talk/
    syndicated content from being pulled into music slots -- a
    "Merry Christmas from KOGR" imaging cut tagged with Christmas
    stays in its own imaging slots, doesn't cross-inject into
    50's Rock."""
    if not chosen_holiday_codes:
        return Track.objects.none()
    qs = Track.objects.filter(
        holidays__code__in=list(chosen_holiday_codes),
        ready2air=True,
    ).filter(
        Q(category__kind__code="music") | Q(additional_categories__kind__code="music")
    ).distinct()
    if target_datetime is not None:
        slot = target_datetime.weekday() * 24 + target_datetime.hour
        qs = qs.exclude(blocked_slots__contains=[slot])
    return qs


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
                          already_picked_tracks, already_picked_identity_keys):
    """`already_picked_identity_keys` is a set of normalized identity
    keys (see related_artists.track_identity_keys) -- NOT artist ids.
    Using identity keys instead of Track.artist_id here (and
    throughout this module) is what makes a related-artist entry
    actually participate in separation: two tracks conflict whenever
    their identity sets intersect, which artist_id equality alone
    can't express (see log_builder's related_artists integration
    notes below pick_track)."""
    exclude_track_ids = set(t.id for t in already_picked_tracks)
    exclude_identity_keys = set(already_picked_identity_keys)

    max_lookback = max(artist_sep_hours, title_sep_hours)
    if max_lookback <= 0:
        return exclude_track_ids, exclude_identity_keys

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
            # cleanup) -- nothing left to exclude by track/identity.
            continue
        if item.scheduled_time >= title_cutoff:
            exclude_track_ids.add(item.track_id)
        if item.scheduled_time >= artist_cutoff:
            # track/track__artist are already select_related above --
            # no extra query per item.
            exclude_identity_keys |= track_identity_keys(
                item.track.artist.name if item.track.artist_id else None,
                item.track.related_artists,
            )

    return exclude_track_ids, exclude_identity_keys


# Historical single-tier fit threshold -- superseded by
# EXACT_FIT_THRESHOLD_SECONDS (see below pick_track's fit_mode
# computation) as of the 1.1 spec's three-mode split. Left defined
# (unreferenced) rather than removed, in case anything external still
# imports it.
DURATION_FIT_THRESHOLD = 480  # start fitting when < 8 minutes remain
DURATION_FIT_MARGIN = 30     # acceptable overshoot in seconds, both single-track and pair modes

# Additional dormancy bonus (1.1 spec): a track that has never aired, or
# hasn't aired in over a year, gets an extra flat multiplier on top of
# the existing log-dampened dormancy factor -- ADDITIONAL to, not a
# replacement for, the finite-365-day COALESCE treatment those tracks
# already get for the log-dampened factor itself (which they still need,
# so they sort among other long-idle tracks instead of as an unbounded
# special case). See _effective_weight_sql / compute_effective_weight.
DORMANT_WEIGHT_BONUS_DAYS = 365
DORMANT_WEIGHT_BONUS = 2.0

# Nominal hour length in seconds -- the default target_duration_seconds
# for every build path. Admin rebuild/preview always use this default;
# the engine's own auto-build can pass a shorter computed target when it
# knows (via clock-drift recovery, see engine.py) that the upcoming hour
# will actually start late. Never longer than this -- see
# MAX_CLOCK_RECOVERY_SECONDS in engine.py for the one-way-only rationale.
NOMINAL_HOUR_SECONDS = 3600


def _effective_weight_sql(active_holiday_codes=None):
    """The deterministic per-track effective-weight SQL expression --
    shared by _weighted_order's weighted-random draw formula and by any
    test/caller that wants the same NUMERIC weight without an actual
    randomized draw (used to prove SQL/Python equivalence against
    compute_effective_weight, the Python-side version pair/exact-fit
    mode uses when it materializes candidates outside SQL). Returns
    (sql_expression, params) for splicing into RawSQL/.annotate().

    effective_weight = (rotation_weight + 1 + holiday_boost)
                      * (1 + LN(1 + hours_since_last_played))
                      * dormancy_bonus

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
    treated as idle 365 days for THIS factor -- a large but finite
    dormancy so genuinely ancient tracks (idle *longer* than a year) can
    still outrank a brand-new, never-aired addition.

    `dormancy_bonus` is a THIRD, separate multiplier (DORMANT_WEIGHT_
    BONUS, additional to the log-dampened factor above, not a
    replacement for it): tracks that have never aired, or haven't aired
    in over DORMANT_WEIGHT_BONUS_DAYS, get an extra flat boost -- these
    are exactly the tracks the log-dampening otherwise under-favors
    relative to how rarely they actually come up in practice.

    `active_holiday_codes` (a list of Holiday.code strings currently in
    their ramp window) adds a per-track boost to `rotation_weight` --
    the MAX max_weight_boost across the track's active-holiday tags,
    or 0 if the track has no active-holiday tags. So during Halloween's
    ramp, a track tagged with Halloween (max_weight_boost=3) picks
    with effective weight (rotation_weight+1+3) instead of just
    (rotation_weight+1) -- ~2x more likely to come up per pick if it
    was already at the default weight=3. See _active_holidays_at for
    the ramp-window semantics."""
    boost_expr, boost_params = _holiday_boost_expr(active_holiday_codes)
    sql = (
        f"(rotation_weight + 1 + {boost_expr}) "
        "* (1 + LN(1 + EXTRACT(EPOCH FROM (NOW() - COALESCE(last_played_at, "
        "NOW() - INTERVAL '365 days'))) / 3600.0)) "
        "* (CASE WHEN last_played_at IS NULL OR last_played_at < NOW() - (%s * INTERVAL '1 day') "
        "THEN %s ELSE 1.0 END)"
    )
    params = boost_params + [DORMANT_WEIGHT_BONUS_DAYS, DORMANT_WEIGHT_BONUS]
    return sql, params


def compute_effective_weight(rotation_weight, holiday_boost, last_played_at, now=None):
    """Python-side equivalent of _effective_weight_sql's deterministic
    per-track weight -- used by pair/exact-fit mode (FitCandidate
    scoring), which materializes candidates in Python instead of doing a
    SQL-side weighted-random draw. Must stay mathematically identical to
    the SQL expression for the same inputs; see
    test_log_builder_selection.py's equivalence tests, which compute
    both for a range of last_played_at values against a real DB row and
    assert they match within floating-point tolerance.

    play_count is deliberately never used here or in the SQL version --
    this project weights by rotation_weight and recency of play only."""
    now = now or timezone.now()
    if last_played_at is None:
        reference = now - timedelta(days=DORMANT_WEIGHT_BONUS_DAYS)
        is_dormant = True
    else:
        reference = last_played_at
        is_dormant = last_played_at < now - timedelta(days=DORMANT_WEIGHT_BONUS_DAYS)
    hours_since = max(0.0, (now - reference).total_seconds() / 3600.0)
    dormancy_factor = 1 + math.log(1 + hours_since)
    bonus = DORMANT_WEIGHT_BONUS if is_dormant else 1.0
    return (rotation_weight + 1 + holiday_boost) * dormancy_factor * bonus


def _weighted_order(qs, active_holiday_codes=None):
    """Random selection weighted by _effective_weight_sql -- see that
    function's docstring for the full formula and rationale.

    `-LN(RANDOM()) / weight` is the standard SQL-side trick for weighted
    sampling without materializing/shuffling anything -- same query cost
    as the plain `order_by("?")` it replaces."""
    weight_sql, weight_params = _effective_weight_sql(active_holiday_codes)
    return qs.order_by(RawSQL(f"-LN(RANDOM()) / ({weight_sql})", weight_params))


def _effective_airtime(next_start, duration, cue_in):
    """Shared arithmetic core for effective_airtime_seconds -- pulled out
    as a pure function of raw scalar values (not a Track/FitCandidate
    object) so _extract_fit_candidates, which deliberately reads raw
    columns via .values_list() rather than materializing full Track
    rows (see its own docstring -- avoiding an N+1 there is the whole
    point), can compute the IDENTICAL number as every call site that
    does have a real track object, instead of a parallel, driftable
    copy of the same formula. effective_airtime_seconds() below is the
    one-object-argument convenience wrapper every other caller should
    use.

    1.1 second-pass correction (listener-audible-start semantics):
    confirmed by a full engine trace that a fresh deck is NEVER seeked
    to cue_in_seconds on a normal start -- _create_deck plays every
    track from real file position 0, and _get_deck_position measures
    elapsed time from that same position-0 reference, not from
    cue_in. A track's own audible content therefore only begins
    cue_in_seconds AFTER its deck was created. Stacking successive
    tracks' spans end-to-end (which is what every accumulation loop in
    this module does) means the correct per-track contribution is the
    span from THIS track's own audible start to the NEXT track's
    audible start -- and working through the crossfade trigger's own
    math (_poll_position: trigger_point = next_start_current -
    cue_in_next, engine.py) shows that span collapses to exactly
    `next_start_seconds - cue_in_seconds` of the SAME track, in the
    ordinary (unclamped) case. See effective_airtime_seconds's
    docstring for the one case this does NOT capture (the engine's
    10-second trigger-point clamp, which needs the NEXT track's own
    cue_in and is out of scope for a single-track helper).

    Explicit `is not None` checks throughout -- next_start_seconds is
    nullable with no default, so an explicit 0.0 (a deliberately
    near-instant crossfade point, e.g. a very short sweeper/ID) is a
    real, meaningful value and must not be silently replaced by
    duration_seconds the way a bare `or` chain would. cue_in_seconds is
    never None at the DB level (default=0, not nullable) but is
    defended the same way for robustness against any caller passing a
    lightweight stand-in that doesn't enforce that constraint (e.g. a
    test fixture)."""
    if next_start is not None:
        end_point = next_start
    elif duration is not None:
        end_point = duration
    else:
        return 0.0
    cue_in = cue_in if cue_in is not None else 0.0
    return max(0.0, end_point - cue_in)


def effective_airtime_seconds(track):
    """THE authoritative definition of a track's listener-facing
    broadcast-clock contribution -- how much wall-clock time separates
    this track's own audible start from the next track's audible
    start, in the ordinary (unclamped) crossfade case. Every place in
    this module that means "how much of the hour does this track
    consume" must route through this function (or _effective_airtime,
    its raw-values equivalent used only by _extract_fit_candidates)
    rather than reading next_start_seconds/duration_seconds directly,
    so scheduling math can never silently drift between build modes
    again. See _effective_airtime's docstring for the full derivation
    and the one known gap (the engine's 10-second clamp; see
    test_log_builder_selection.py's documented clamp-limitation test).

    Deliberately NOT used for engine.py's own position/trigger
    comparisons -- next_start_seconds alone remains correct there,
    since those compare two values already expressed in the same
    absolute-file-position coordinate frame. This function is only for
    scheduling/accounting math that represents elapsed broadcast
    timeline (log_builder's own accumulation, remaining-runway, and
    fit/landing calculations)."""
    return _effective_airtime(track.next_start_seconds, track.duration_seconds, track.cue_in_seconds)


@dataclass(frozen=True)
class FitCandidate:
    """Lightweight, DB-independent stand-in for a Track during exact-fit/
    pair-planning scoring -- so scoring a whole pool never touches a
    real Track/Artist row per candidate (no N+1), only after a winner is
    chosen. `identity_keys` is the same normalized frozenset
    related_artists.track_identity_keys returns. `duration_seconds` is
    actually effective airtime (see effective_airtime_seconds) despite
    the field's name -- kept as-is to avoid a churny rename across
    every pair/exact-fit scoring function that already reads it."""
    track_id: int
    identity_keys: frozenset
    duration_seconds: float
    effective_weight: float


def _extract_fit_candidates(qs, active_holiday_codes=None):
    """Full-pool extraction (1.1 spec) -- every eligible track in `qs`
    becomes a FitCandidate, in ONE query (annotated with the same
    holiday-boost subquery _weighted_order uses), no top-200
    truncation and no N+1s. Replaces the historical top-200 weighted-
    random pre-slice that used to feed _pick_best_fit; full-pool
    extraction is what lets exact-fit mode actually find the best
    duration match instead of only searching whatever 200 rows a
    random SQL-side draw happened to surface."""
    boost_expr, boost_params = _holiday_boost_expr(active_holiday_codes)
    rows = qs.annotate(_fit_holiday_boost=RawSQL(boost_expr, boost_params)).values_list(
        "id", "next_start_seconds", "duration_seconds", "cue_in_seconds", "rotation_weight",
        "last_played_at", "artist__name", "related_artists", "_fit_holiday_boost",
    )
    now = timezone.now()
    candidates = []
    for track_id, next_start, duration, cue_in, rotation_weight, last_played_at, artist_name, related_artists, holiday_boost in rows:
        candidates.append(FitCandidate(
            track_id=track_id,
            identity_keys=track_identity_keys(artist_name, related_artists),
            duration_seconds=_effective_airtime(next_start, duration, cue_in),
            effective_weight=compute_effective_weight(rotation_weight, holiday_boost or 0, last_played_at, now=now),
        ))
    return candidates


# How many of the closest-duration candidates compete via weighted-random
# choice in exact-fit mode -- mirrors the historical top-200-then-best-5
# behavior's "best 5" width, just now drawn from the full pool and
# weighted by effective_weight instead of picked uniformly.
FIT_NEAR_MATCH_COUNT = 5


def _weighted_choice_by(items, weight_fn):
    """Weighted-random choice over `items` using weight_fn(item) as each
    item's relative probability. Falls back to a uniform choice if every
    weight is non-positive (shouldn't happen given compute_effective_
    weight's >=1 floor, but never crash on it)."""
    weights = [max(0.0, weight_fn(item)) for item in items]
    if sum(weights) <= 0:
        return random.choice(items)
    return random.choices(items, weights=weights, k=1)[0]


def _pick_best_fit(qs, remaining_seconds, active_holiday_codes=None):
    """Exact-fit selection (1.1 spec): from the FULL pool of eligible
    tracks (no top-200 truncation), weighted-random choice among the
    FIT_NEAR_MATCH_COUNT closest-duration candidates that don't
    overshoot `remaining_seconds` by more than DURATION_FIT_MARGIN.
    Weighting by effective_weight here is an intentional improvement
    over the historical uniform-random-among-best-5 behavior: rotation_
    weight/dormancy now matter even in fit mode, not just in normal
    weighted-random mode.

    Second-pass correction: if NOTHING clears the overshoot margin,
    this now returns None (graceful stop) instead of the original
    draft's behavior of force-picking the closest-of-everything
    regardless of how badly it overshoots. That original "dead air is
    worse than a duration mismatch" reasoning doesn't hold for THIS
    mode: exact-fit only engages when remaining_seconds is already
    small (near a real wall-clock boundary, live-fill or the tail of a
    build), so forcing e.g. a 7-minute track into a 2-minute gap
    doesn't avoid dead air, it just knowingly creates a large,
    undisclosed top-of-hour overrun -- exactly what clock-drift
    recovery exists to avoid causing in the first place. Callers
    (pick_track's loosening ladder, _build_from_rotation,
    fill_remaining_hour) already treat None gracefully -- loosen
    further, skip the slot, or stop the fill loop -- so this is a safe,
    already-supported return value, not a new failure mode."""
    candidates = _extract_fit_candidates(qs, active_holiday_codes=active_holiday_codes)
    if not candidates:
        return None

    scored = [(abs(remaining_seconds - c.duration_seconds), c) for c in candidates]
    within_margin = [(diff, c) for diff, c in scored if remaining_seconds - c.duration_seconds >= -DURATION_FIT_MARGIN]
    if not within_margin:
        return None
    within_margin.sort(key=lambda pair: pair[0])
    finalists = [c for _, c in within_margin[:FIT_NEAR_MATCH_COUNT]]

    chosen = _weighted_choice_by(finalists, lambda c: c.effective_weight)
    return Track.objects.get(id=chosen.track_id)


# Selection-mode thresholds (1.1 spec) -- defaults, not sacred values;
# tune per station preference. Landing mode (two-slot matched-pair
# planning) engages when MORE than EXACT_FIT_THRESHOLD_SECONDS but no
# more than PAIR_LANDING_THRESHOLD_SECONDS remain; exact-fit (full-pool
# single-track fitting) engages at or below EXACT_FIT_THRESHOLD_SECONDS,
# or whenever only one slot remains (a pair is structurally impossible
# with one slot). Above PAIR_LANDING_THRESHOLD_SECONDS, normal
# (unmodified) sequential weighted-random picking continues.
PAIR_LANDING_THRESHOLD_SECONDS = 720  # ~12 minutes
EXACT_FIT_THRESHOLD_SECONDS = 360     # ~6 minutes

# How many duration-adjacent candidates from the SECOND pool get examined
# per candidate from the first pool during the bisect search below.
PAIR_DURATION_NEIGHBORS = 8
# Cap on retained scored pairs before the final weighted-random choice
# (spec's suggested range is 25-100 finalists; 50 sits in the middle).
PAIR_FINALIST_LIMIT = 50
# Soft wall-clock ceiling on the pair search itself (not a CI timing
# assertion -- see the benchmark command for that). Protects against a
# pathologically large pool without needing a hard candidate-count cap.
PAIR_SOLVER_BUDGET_MS = 500
# landing_quality = 1 / (1 + landing_error / this) -- larger values make
# landing_quality decay more gently with distance from a perfect fit.
PAIR_LANDING_QUALITY_DIVISOR = 15.0


@dataclass(frozen=True)
class PairResult:
    """A single scored two-track landing candidate. Exposed (not just
    used internally) so SelectionDiagnostics can report the winning
    pair's landing_error/pair_score for operator visibility."""
    candidate_a: FitCandidate
    candidate_b: FitCandidate
    pair_score: float
    landing_error: float


def _pair_valid(a, b):
    """A pair is invalid if it's literally the same track twice, or if
    the two tracks mutually conflict via related-artist identity keys.
    Both invariants matter here even though sequential per-slot hard-
    exclusion (which normally prevents these) doesn't apply -- the two
    tracks in a pair are chosen JOINTLY, neither one first."""
    if a.track_id == b.track_id:
        return False
    if a.identity_keys & b.identity_keys:
        return False
    return True


def _score_pair(a, b, remaining_seconds):
    """pair_score = effective_weight_a * effective_weight_b *
    landing_quality (the 1.1 spec's formula).

    Second-pass review corrected an earlier draft here: that draft
    multiplied in a fourth "remainder_quality" factor defined as a
    duration-BALANCE measure (penalizing a lopsided split like 7:00 +
    3:00 relative to 5:00 + 5:00 at the same combined duration). On
    review that wasn't the intended concept and had no basis --
    there's no requirement that matched songs be similar in length,
    and landing_quality already expresses everything presently
    available about whether a pair is a good choice (how close its
    combined duration lands to the remaining runway). A separate
    "is the remainder usefully fillable" signal would be redundant
    with landing_quality (a large landing_error already means a large,
    poorly-absorbed remainder either way), so it's removed rather than
    redefined -- three factors, matching the spec's own formula."""
    total_duration = a.duration_seconds + b.duration_seconds
    landing_error = abs(remaining_seconds - total_duration)
    landing_quality = 1.0 / (1.0 + landing_error / PAIR_LANDING_QUALITY_DIVISOR)
    pair_score = a.effective_weight * b.effective_weight * landing_quality
    return PairResult(candidate_a=a, candidate_b=b, pair_score=pair_score, landing_error=landing_error)


def find_matched_pair(candidates_a, candidates_b, remaining_seconds, rng=None):
    """Duration-indexed bisect search (1.1 spec) for a two-track
    "landing" pair whose combined duration lands close to
    `remaining_seconds` -- explicitly NOT a Cartesian product (that
    would be O(M*N), and the spec calls this out by name as the thing
    to avoid). Sorts pool B once by duration (O(N log N)), then for
    each candidate in pool A (M items) bisects into pool B's sorted
    durations to find where a perfectly-complementary duration would
    fall and examines only the PAIR_DURATION_NEIGHBORS candidates
    around that point -- O(M log N) for the search itself. Overall
    O(M log N + N log N), matching the spec's target complexity
    (stated there as O(M log M + N log M); the two are the same shape).

    `candidates_a`/`candidates_b` may be the SAME pool object (typical
    case: two consecutive slots of the same category) -- self-pairing
    and mutual related-artist identity conflicts are filtered out
    explicitly (see _pair_valid), since sequential per-slot hard-
    exclusion doesn't apply when both tracks are chosen jointly rather
    than one after the other.

    Retains up to PAIR_FINALIST_LIMIT highest-pair_score valid pairs,
    then makes a WEIGHTED-RANDOM choice among them by pair_score --
    deliberately never deterministically the single closest-landing
    pair (see _score_pair). `rng` accepts a seeded random.Random for
    reproducible tests; defaults to the module-level `random`.

    Bounded by PAIR_SOLVER_BUDGET_MS: if pool sizes are large enough
    that scanning is taking unexpectedly long, stops examining further
    pool-A candidates and proceeds to select among whatever's already
    been collected -- never returns nothing purely because of the
    budget, only because no valid pair existed in what was scanned.

    Returns a PairResult, or None if no valid pair exists (e.g. one
    pool is empty, or degenerate down to nothing but self/identity
    conflicts)."""
    rng = rng if rng is not None else random

    if not candidates_a or not candidates_b:
        return None

    sorted_b = sorted(candidates_b, key=lambda c: c.duration_seconds)
    durations_b = [c.duration_seconds for c in sorted_b]

    scored = []
    deadline = monotonic() + (PAIR_SOLVER_BUDGET_MS / 1000.0)
    half_window = max(1, PAIR_DURATION_NEIGHBORS // 2)

    for i, a in enumerate(candidates_a):
        if i % 200 == 0 and i > 0 and monotonic() > deadline:
            break
        target_b_duration = remaining_seconds - a.duration_seconds
        idx = bisect.bisect_left(durations_b, target_b_duration)
        lo = max(0, idx - half_window)
        hi = min(len(sorted_b), idx + half_window)
        for b in sorted_b[lo:hi]:
            if not _pair_valid(a, b):
                continue
            scored.append(_score_pair(a, b, remaining_seconds))

    if not scored:
        return None

    scored.sort(key=lambda pr: pr.pair_score, reverse=True)
    finalists = scored[:PAIR_FINALIST_LIMIT]

    weights = [max(0.0, pr.pair_score) for pr in finalists]
    if sum(weights) <= 0:
        return rng.choice(finalists)
    return rng.choices(finalists, weights=weights, k=1)[0]


@dataclass
class SelectionDiagnostics:
    """Forward-compatible structured summary of how a build's category-
    random slots were filled (1.1 spec) -- NOT full advanced-rule
    support, just enough for an operator to see (via the SystemEvent
    _build_from_rotation emits) which selection mode ran where and how
    well landing pairs landed. Extend with more fields as future rule
    work needs them; this is deliberately a plain, mutable, JSON-
    serializable-via-as_detail() summary, not a persisted model."""
    normal_picks: int = 0
    landing_pairs: int = 0
    # Landing zone reached (EXACT_FIT_THRESHOLD_SECONDS < remaining <=
    # PAIR_LANDING_THRESHOLD_SECONDS) but no pair was actually used --
    # either structurally blocked (direct-track slot next, or no next
    # slot at all) or find_matched_pair found no valid pair. Each
    # fallback still produces exactly one exact_fit_picks entry.
    landing_pair_fallbacks: int = 0
    exact_fit_picks: int = 0
    direct_track_inserts: int = 0
    pool_exhausted_picks: int = 0  # pick_track returned None (slot skipped)
    landing_errors: list = field(default_factory=list)  # seconds, one per successful landing pair
    target_duration_seconds: float = NOMINAL_HOUR_SECONDS
    # How much shorter than a nominal hour this build's target was --
    # derived, not independently supplied; a genuine 0 and "no clock-
    # drift recovery attempted" are indistinguishable here by design,
    # since log_builder.py has no live-engine state of its own. The
    # engine emits its OWN separate diagnostic event for the clock-drift
    # PROJECTION itself (offset computation, MAX_CLOCK_RECOVERY_SECONDS
    # clamping) -- see engine.py.
    late_offset_seconds: float = 0.0

    def as_detail(self):
        return {
            "normal_picks": self.normal_picks,
            "landing_pairs": self.landing_pairs,
            "landing_pair_fallbacks": self.landing_pair_fallbacks,
            "exact_fit_picks": self.exact_fit_picks,
            "direct_track_inserts": self.direct_track_inserts,
            "pool_exhausted_picks": self.pool_exhausted_picks,
            "landing_errors": [round(e, 1) for e in self.landing_errors],
            "target_duration_seconds": round(self.target_duration_seconds, 1),
            "late_offset_seconds": round(self.late_offset_seconds, 1),
        }

    def needs_operator_attention(self):
        """Monitoring philosophy (1.1 follow-up): Monitoring should tell
        the operator when the scheduler needs attention; ordinary logs
        explain what it did. A normal successful hour build -- one or
        more landing-pair fallbacks that resolved via exact-fit,
        clock-drift recovery shortening the target, a small/acceptable
        landing error, direct tracks inserted normally -- is NOT by
        itself abnormal and must not occupy the Monitoring Recent
        Events feed.

        Deliberately narrow, explicit predicate rather than "emit
        whenever any field is nonzero": a slot that came up completely
        empty (pool_exhausted_picks) or a landing pair that missed its
        target by more than the station's own configured fit tolerance
        (DURATION_FIT_MARGIN -- the same tolerance single-track exact-
        fit already uses, reused here rather than inventing a second,
        unrelated threshold) are the two conditions this module can
        currently detect that mean the intended hour plan genuinely
        could not be achieved.

        Two other conditions from the review are intentionally NOT
        checked here, because they already have their own, separate,
        pre-existing visibility and duplicating them here would need a
        backwards import from engine.py (a real circular-import risk --
        engine.py imports extensively FROM this module):
          - clock recovery hitting MAX_CLOCK_RECOVERY_SECONDS: already
            reported by engine.py's own "Clock-drift target clamped"
            event at the point the clamp is actually applied, before
            target_duration_seconds ever reaches this module.
          - build/persistence failure: already reported by engine.py's
            "Async hour-log build failed"/"...crashed" events, and a
            failure during selection itself never reaches this point at
            all (the exception propagates before this function's
            caller can call it).

        Pair-solver-budget exhaustion (find_matched_pair's
        PAIR_SOLVER_BUDGET_MS deadline) is NOT currently instrumented
        anywhere -- deliberately left uninstrumented in this pass
        rather than threading a new out-parameter through
        find_matched_pair's signature and every caller, which would be
        exactly the "large schema redesign" this pass is meant to
        avoid. A worthwhile small follow-up, not done here."""
        if self.pool_exhausted_picks > 0:
            return True
        if self.landing_errors and max(self.landing_errors) > DURATION_FIT_MARGIN:
            return True
        return False


def _resolve_slot_pool_context(category, target_datetime, recency_cfg, active_holiday_codes, daily_shares):
    """Per-slot setup shared by sequential single-track picking and
    landing-mode pair candidate extraction: effective recency
    separation, plus an INDEPENDENT per-slot holiday dice roll -- each
    slot's roll must never be merged with another slot's (1.1 spec's
    per-slot-independent-holiday-resolution invariant). Mirrors the
    holiday-injection logic _build_from_rotation has always used
    inline; factored out here so landing mode can resolve TWO slots'
    contexts independently without duplicating the dice-roll logic.

    Returns (artist_sep, title_sep, pool_override_qs, pool_key,
    exclude_holiday_codes) -- pool_key always resolves to a usable
    TrackIdentityCache key (holiday tuple, category id, or a fresh
    unique object as a last resort), matching pick_track's own
    effective_pool_key semantics exactly."""
    artist_sep, title_sep = get_separation(category, recency_cfg)

    is_music_slot = category is not None and category.kind.code == "music"
    chosen_holiday_codes = []
    if is_music_slot and active_holiday_codes:
        for code, share in daily_shares.items():
            if random.random() < share:
                chosen_holiday_codes.append(code)

    pool_override_qs = None
    pool_key = None
    exclude_holiday_codes = None
    if chosen_holiday_codes:
        pool_override_qs = _music_holiday_pool(chosen_holiday_codes, target_datetime)
        pool_key = ("holiday", tuple(sorted(chosen_holiday_codes)))
    elif is_music_slot and active_holiday_codes:
        exclude_holiday_codes = active_holiday_codes

    if pool_key is None:
        pool_key = ("category", category.id) if category is not None else object()

    return artist_sep, title_sep, pool_override_qs, pool_key, exclude_holiday_codes


def _landing_slot_candidates(category, target_datetime, pool_override_qs, exclude_holiday_codes,
                              exclude_track_ids, exclude_identity_keys,
                              hard_exclude_track_ids, hard_exclude_identity_keys,
                              identity_cache, pool_key, active_holiday_codes):
    """Builds the eligible-track pool for ONE slot's landing-mode
    candidate extraction and returns it as FitCandidates. Mirrors pick_
    track's own private pool-building (base pool -> exclude hard+
    history+identity-conflicting track ids -> exclude active-holiday-
    tagged tracks if requested) intentionally -- kept as a parallel,
    clearly-documented implementation rather than a shared refactor of
    pick_track's closures, since landing mode needs BOTH slots' full
    candidate pools up front (to hand to find_matched_pair) rather than
    picking one Track at a time the way pick_track's loosening loop
    does."""
    base_qs = pool_override_qs if pool_override_qs is not None else _tracks_for_category(category, target_datetime=target_datetime)
    combined_tracks = set(exclude_track_ids) | set(hard_exclude_track_ids)
    combined_identity_keys = set(exclude_identity_keys) | set(hard_exclude_identity_keys)
    identity_conflict_ids = identity_cache.conflicting_track_ids(pool_key, base_qs, combined_identity_keys)
    combined_tracks = combined_tracks | identity_conflict_ids

    qs = base_qs
    if combined_tracks:
        qs = qs.exclude(id__in=combined_tracks)
    if exclude_holiday_codes:
        qs = qs.exclude(holidays__code__in=list(exclude_holiday_codes))

    return _extract_fit_candidates(qs, active_holiday_codes=active_holiday_codes)


def pick_track(category, exclude_track_ids, exclude_identity_keys,
               artist_sep, title_sep, target_datetime,
               remaining_seconds=None, max_loosening=3,
               hard_exclude_track_ids=None, hard_exclude_identity_keys=None,
               active_holiday_codes=None,
               pool_override_qs=None, exclude_holiday_codes=None,
               identity_cache=None, pool_key=None, force_fit_mode=False):
    """`exclude_*` are RECENCY-HISTORY exclusions -- they get progressively
    dropped by the loosening loop below if no candidate can be found.

    `force_fit_mode` (1.1 spec, second-pass correction): engages
    duration-aware exact-fit selection regardless of remaining_seconds'
    own relationship to EXACT_FIT_THRESHOLD_SECONDS. Used specifically
    by the landing-mode-pair-search-failed fallback in
    _build_from_rotation/fill_remaining_hour: once a build has already
    decided it's trying to land against a nearby wall-clock boundary
    (remaining_seconds is in the landing zone, well above
    EXACT_FIT_THRESHOLD_SECONDS) and a matched pair couldn't be found,
    the single-track fallback must STILL be duration-aware -- degrading
    to ordinary unconstrained weighted selection at that point would
    silently abandon the landing attempt entirely, picking essentially
    any track regardless of fit. Ordinary unconstrained selection
    remains correct and intentional everywhere remaining_seconds
    reflects genuine plentiful runway.

    `hard_exclude_*` are "already-picked in THIS build" exclusions -- they
    MUST hold through every loosening pass, otherwise a category with few
    eligible tracks (or a fresh build whose picks aren't yet persisted as
    LogItems) can pick the same track for multiple slots in the same
    hour.

    `exclude_identity_keys`/`hard_exclude_identity_keys` are sets of
    NORMALIZED IDENTITY KEYS (related_artists.track_identity_keys),
    not artist ids -- a candidate conflicts if its own identity set
    (primary artist + related artists) intersects the banned set. This
    is what makes related_artists actually participate in separation,
    fully mutually: if track B's primary artist appears in track A's
    related_artists, A's identity set contains B's key, so A is
    excluded once B has been picked/played recently -- and because set
    intersection is symmetric, the reverse (B excluded once A has been
    picked) also holds with no separate code path. Resolving "which
    pool track ids conflict with a banned key set" is delegated to
    `identity_cache` (a TrackIdentityCache) so the pool's identity data
    is loaded once per build, not once per pick_track call -- see that
    class's docstring. A caller that doesn't pass one gets a
    throwaway per-call cache (correct, just without the cross-call
    reuse benefit).

    `active_holiday_codes` drives the SQL boost inside _weighted_order:
    a track tagged with any currently-ramping Holiday sorts more likely
    within whichever pool it competes in. Used for BOTH pool modes
    below.

    `pool_override_qs` overrides the default `_tracks_for_category`
    pool. Used by _build_from_rotation when a music-kind slot's
    per-holiday dice roll came up "yes" -- the pool becomes a
    station-wide music-kind holiday queryset instead of THIS slot's
    normal category. See _music_holiday_pool. `pool_key` should be
    passed alongside it (see TrackIdentityCache) so the holiday pool's
    identity data is cached correctly.

    `exclude_holiday_codes` excludes tracks tagged with any of those
    codes from whichever pool is in play. Used when the dice roll came
    up "no" for a music-kind slot on a holiday day -- prevents holiday-
    tagged tracks from leaking through the SLOT'S normal category
    filing and inflating the effective share beyond the target.
    """
    # EXACT_FIT_THRESHOLD_SECONDS (1.1 spec) supersedes the historical
    # single-tier DURATION_FIT_THRESHOLD as the exact-fit engagement
    # point -- DURATION_FIT_THRESHOLD is kept defined (DURATION_FIT_
    # MARGIN, its sibling, is still used by _pick_best_fit's overshoot
    # tolerance) but no longer drives this decision. force_fit_mode
    # (see docstring above) engages it unconditionally.
    fit_mode = force_fit_mode or (remaining_seconds is not None and remaining_seconds < EXACT_FIT_THRESHOLD_SECONDS)
    hard_exclude_track_ids = set(hard_exclude_track_ids or ())
    hard_exclude_identity_keys = set(hard_exclude_identity_keys or ())
    identity_cache = identity_cache if identity_cache is not None else TrackIdentityCache()

    # Effective cache key for THIS pool -- stable across every
    # _build_qs() call within this one pick_track invocation (the
    # loosening loop never changes category/pool_override_qs), so
    # computed once here rather than inside _build_qs(). Falls back to
    # a guaranteed-unique key (no cross-call cache reuse, but still
    # correct) when the caller didn't pass one for a holiday pool --
    # every real call site in this module does pass one.
    if pool_key is not None:
        effective_pool_key = pool_key
    elif pool_override_qs is None and category is not None:
        effective_pool_key = ("category", category.id)
    else:
        effective_pool_key = object()

    # Note: sep=0 opts a category out of the RECENCY-window exclusion
    # (via get_separation returning 0 and the loosening loop treating
    # the window as empty), but NOT out of within-build hard exclusion.
    # Hard exclusion is orthogonal to recency -- it's "did I already
    # pick this track/artist in THIS hour's build" -- and it still
    # applies here so that a category with a pool of N > 1 (e.g. Local
    # Legends Tag with 9 tracks, sep=0) cycles through its pool within
    # the hour instead of picking the same track repeatedly. Pool-of-1
    # categories like WxTemp still work: their single track survives
    # via the pool-exhaustion fallback at the bottom of this function,
    # which drops hard_exclude_track_ids as a last resort.

    def _base_pool_qs():
        # Base pool: either the caller's override (holiday-slot
        # injection) or the slot's normal category pool. Kept
        # UNNARROWED (no excludes applied) -- this is exactly what
        # TrackIdentityCache should load/cache for `effective_pool_key`.
        if pool_override_qs is not None:
            return pool_override_qs
        return _tracks_for_category(category, target_datetime=target_datetime)

    def _build_qs():
        base_qs = _base_pool_qs()
        qs = base_qs
        combined_tracks = set(exclude_track_ids) | hard_exclude_track_ids
        combined_identity_keys = set(exclude_identity_keys) | hard_exclude_identity_keys
        identity_conflict_ids = identity_cache.conflicting_track_ids(
            effective_pool_key, base_qs, combined_identity_keys,
        )
        combined_tracks = combined_tracks | identity_conflict_ids
        if combined_tracks:
            qs = qs.exclude(id__in=combined_tracks)
        if exclude_holiday_codes:
            # Non-holiday-slot mode: pull holiday-tagged tracks OUT so
            # they can't leak into an ordinary music slot through their
            # normal category filing. Excluded via M2M through-join;
            # .distinct() from _tracks_for_category handles the
            # multi-tag edge case.
            qs = qs.exclude(holidays__code__in=list(exclude_holiday_codes))
        return qs

    for attempt in range(max_loosening + 1):
        qs = _build_qs()
        if fit_mode:
            track = _pick_best_fit(qs, remaining_seconds, active_holiday_codes=active_holiday_codes)
        else:
            track = _weighted_order(qs, active_holiday_codes=active_holiday_codes).first()

        if track:
            return track

        artist_sep = artist_sep / 2.0
        title_sep = title_sep / 2.0

        loosened_exclude_tracks = set()
        loosened_exclude_identity_keys = set()

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
                .select_related("track", "track__artist")
            )
            artist_cutoff = target_datetime - timedelta(hours=artist_sep)
            title_cutoff = target_datetime - timedelta(hours=title_sep)
            for item in recent:
                if not item.track_id:
                    continue
                if item.scheduled_time >= title_cutoff:
                    loosened_exclude_tracks.add(item.track_id)
                if item.scheduled_time >= artist_cutoff:
                    # Related-artist history loosens at the same point
                    # and by the same amount as ordinary artist
                    # history -- it's folded into the same
                    # artist_sep-gated branch, not a separate schedule.
                    loosened_exclude_identity_keys |= track_identity_keys(
                        item.track.artist.name if item.track.artist_id else None,
                        item.track.related_artists,
                    )

        # Only the recency-history part gets rebuilt; hard exclusions
        # (caller's accumulator of already-picked-in-this-build tracks)
        # are preserved via _build_qs() unioning them back in.
        exclude_track_ids = loosened_exclude_tracks
        exclude_identity_keys = loosened_exclude_identity_keys

    # Final pass: drop history exclusions AND hard artist separation,
    # keep only hard TRACK exclusion. Rationale: "don't play the same
    # track twice in one hour" is a stronger invariant than "don't play
    # the same artist too close together" -- yield the soft one first.
    exclude_track_ids = set()
    exclude_identity_keys = set()
    hard_exclude_identity_keys = set()
    qs = _build_qs()
    if fit_mode:
        track = _pick_best_fit(qs, remaining_seconds, active_holiday_codes=active_holiday_codes)
    else:
        track = _weighted_order(qs, active_holiday_codes=active_holiday_codes).first()
    if track is not None:
        return track

    # Pool-exhaustion fallback: drop the hard TRACK exclusion too. This
    # is what lets a pool-of-1 category (e.g. WxTemp, Legal ID, all the
    # imaging tag categories) still pick its single track on a second
    # slot in the same hour -- the hard-exclude accumulator holds it
    # otherwise. Also what lets a small-pool category (e.g. Local
    # Legends Tag with 9 tracks) re-cycle once every track has been
    # picked in the current build. Reaching this fallback for a
    # well-populated music category would mean the entire category is
    # empty or otherwise unpickable -- returning None below is then the
    # right answer and the caller emits a "no track survived" warning.
    hard_exclude_track_ids = set()
    qs = _build_qs()
    if fit_mode:
        return _pick_best_fit(qs, remaining_seconds, active_holiday_codes=active_holiday_codes)
    return _weighted_order(qs, active_holiday_codes=active_holiday_codes).first()


MAX_FILL_TRACKS = 200  # safety cap against a runaway loop on bad data


def _identity_keys_for_picks(picks):
    """Bulk-loaded (single query, regardless of how many picks) union
    of identity keys for every track already in `picks` -- avoids
    relying on `track.artist` being preloaded on whatever produced
    these Track objects (pick_track's own Track.objects.get(), a
    playlist item, or a rotation slot's direct-track insert may not
    have select_related('artist')), which would otherwise be one lazy
    query per pick for a fully-populated hour (~15-20 items)."""
    track_ids = [p["track"].id for p in picks]
    if not track_ids:
        return set()
    rows = Track.objects.filter(id__in=track_ids).values_list("id", "artist__name", "related_artists")
    by_id = {tid: track_identity_keys(name, related) for tid, name, related in rows}
    keys = set()
    for tid in track_ids:
        keys |= by_id.get(tid, frozenset())
    return keys


def fill_remaining_hour(picks, accumulated_seconds, target_datetime,
                        active_holiday_codes=None, daily_shares=None,
                        identity_cache=None, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    """Top up `picks` with fallback-category tracks (admin-configured via
    LogFillConfig) until `target_duration_seconds` is filled, or as
    tightly as duration-fit allows. Called after any build path in case
    it comes up short (e.g. a playlist/rotation that doesn't sum to the
    target on its own). `target_duration_seconds` defaults to a full
    nominal hour (3600s); the engine's own auto-build passes a shorter
    computed target when clock-drift recovery says the upcoming hour will
    start late -- see engine.py.

    `picks` may already contain explicit playlist items or rotation
    picks -- their identity sets (primary + related artists) are
    seeded into this function's own hard-exclusion accumulator below,
    so an explicit playlist/rotation item still blocks a same-identity
    fill pick even though it was never itself chosen via pick_track().

    Holiday injection: caller passes `active_holiday_codes` (list of
    codes currently in ramp) and `daily_shares` (dict of code -> [0..1]
    linear-tent share for today). Each fill pick rolls per-holiday dice
    the same way _build_from_rotation does. Callers that don't want
    holiday behavior pass both as None.

    `identity_cache`: pass the same TrackIdentityCache the caller's
    other pick_track() calls in this build are using, so the fallback
    category's pool data is reused rather than reloaded. Defaults to a
    fresh one if not given."""
    remaining = target_duration_seconds - accumulated_seconds
    if remaining <= DURATION_FIT_MARGIN:
        return picks, accumulated_seconds

    cfg = LogFillConfig.load()
    if cfg.strategy == "fixed_category":
        category = cfg.fallback_category
    else:
        category = picks[-1]["category"] if picks else None
    if category is None:
        return picks, accumulated_seconds

    identity_cache = identity_cache if identity_cache is not None else TrackIdentityCache()
    recency_cfg = RecencyConfig.load()
    picked_tracks = [p["track"] for p in picks]
    picked_identity_keys = _identity_keys_for_picks(picks)

    # Playlist branch and other callers may not thread state through --
    # compute a best-effort default here.
    if active_holiday_codes is None:
        active_holidays = _active_holidays_at(target_datetime)
        active_holiday_codes = [h.code for h in active_holidays]
        daily_shares = {
            h.code: _holiday_daily_share(h, target_datetime.date())
            for h in active_holidays
        }
    if daily_shares is None:
        daily_shares = {}

    is_music_fill = category is not None and category.kind.code == "music"

    def _append_fill_pick(track):
        nonlocal accumulated_seconds, remaining
        track_duration = effective_airtime_seconds(track)
        scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
        picks.append({
            "position": len(picks),
            "scheduled_time": scheduled_time,
            "track": track,
            "category": category,
        })
        # Always accumulate for within-build hard exclusion; see the
        # parallel comment in _build_from_rotation and pick_track's
        # pool-exhaustion fallback.
        picked_tracks.append(track)
        picked_identity_keys.update(track_identity_keys(
            track.artist.name if track.artist_id else None, track.related_artists,
        ))
        accumulated_seconds += track_duration
        remaining = target_duration_seconds - accumulated_seconds
        return track_duration

    for _ in range(MAX_FILL_TRACKS):
        if remaining <= DURATION_FIT_MARGIN:
            break
        artist_sep, title_sep = get_separation(category, recency_cfg)
        exclude_track_ids, exclude_identity_keys = get_recent_exclusions(
            target_datetime, artist_sep, title_sep, picked_tracks, picked_identity_keys,
        )

        # Same per-holiday dice roll as the main rotation loop -- only
        # ONE roll per iteration here (not two independent ones like
        # _build_from_rotation's landing-mode pairing), since a fill
        # pair always draws both tracks from this same repeating
        # category, unlike two distinct rotation slots that could
        # differ in category.
        chosen_holiday_codes = []
        if is_music_fill and active_holiday_codes:
            for code, share in daily_shares.items():
                if random.random() < share:
                    chosen_holiday_codes.append(code)
        pool_override_qs = None
        pool_key = None
        exclude_holiday_codes = None
        if chosen_holiday_codes:
            pool_override_qs = _music_holiday_pool(chosen_holiday_codes, target_datetime)
            pool_key = ("holiday", tuple(sorted(chosen_holiday_codes)))
        elif is_music_fill and active_holiday_codes:
            exclude_holiday_codes = active_holiday_codes

        # Landing zone (1.1 spec problem #3/#5: fill_remaining_hour
        # didn't jointly optimize fill pairs) -- try pairing two tracks
        # from this SAME category pool before falling back to a single
        # exact-fit pick. Mirrors _build_from_rotation's landing-mode
        # branch, adapted for one repeating pool instead of two
        # independently-resolved rotation slots.
        if EXACT_FIT_THRESHOLD_SECONDS < remaining <= PAIR_LANDING_THRESHOLD_SECONDS:
            hard_exclude_track_ids = {t.id for t in picked_tracks}
            hard_exclude_identity_keys = set(picked_identity_keys)
            candidates = _landing_slot_candidates(
                category, target_datetime, pool_override_qs, exclude_holiday_codes,
                exclude_track_ids, exclude_identity_keys,
                hard_exclude_track_ids, hard_exclude_identity_keys,
                identity_cache, pool_key, active_holiday_codes,
            )
            pair_result = find_matched_pair(candidates, candidates, remaining_seconds=remaining)
            if pair_result is not None:
                resolved = {
                    t.id: t for t in Track.objects.select_related("artist", "category").filter(
                        id__in=[pair_result.candidate_a.track_id, pair_result.candidate_b.track_id],
                    )
                }
                track_a = resolved.get(pair_result.candidate_a.track_id)
                track_b = resolved.get(pair_result.candidate_b.track_id)
                if track_a is not None and track_b is not None:
                    _append_fill_pick(track_a)
                    _append_fill_pick(track_b)
                    continue
                # Resolved ids vanished between selection and lookup --
                # fall through to the single-track fallback below.

        # force_fit_mode whenever remaining is at or below the landing
        # threshold -- covers both the direct exact-fit case (remaining
        # <= EXACT_FIT_THRESHOLD_SECONDS, where pick_track's own
        # internal check would already trigger fit_mode; forcing it
        # here too is redundant but harmless) AND, critically, the
        # landing-zone-pair-search-just-failed fallback reached from
        # above: `remaining` there is still > EXACT_FIT_THRESHOLD_
        # SECONDS, so without forcing, this call would silently use
        # ordinary unconstrained weighted selection instead of staying
        # duration-aware (second-pass correction).
        track = pick_track(
            category, exclude_track_ids, exclude_identity_keys,
            artist_sep, title_sep, target_datetime,
            remaining_seconds=remaining,
            hard_exclude_track_ids={t.id for t in picked_tracks},
            hard_exclude_identity_keys=set(picked_identity_keys),
            active_holiday_codes=active_holiday_codes,
            pool_override_qs=pool_override_qs,
            exclude_holiday_codes=exclude_holiday_codes,
            identity_cache=identity_cache,
            pool_key=pool_key,
            force_fit_mode=remaining <= PAIR_LANDING_THRESHOLD_SECONDS,
        )
        if track is None:
            break  # nothing eligible even after loosening — stop gracefully

        track_duration = _append_fill_pick(track)
        if track_duration <= 0:
            break  # avoid infinite loop on zero-duration data

    return picks, accumulated_seconds


def _build_from_rotation(target_date, hour, rotation, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    slots = list(
        rotation.slots
        .select_related("category__kind", "track", "track__category__kind", "track__artist")
        .order_by("position")
    )
    if not slots:
        return None, f"Rotation '{rotation.name}' has no slots."

    recency_cfg = RecencyConfig.load()
    target_datetime = timezone.make_aware(
        datetime.combine(target_date, time(hour, 0))
    )

    # Station-wide holiday injection state, computed once at build
    # start. `active_holidays` is the set of Holidays in their ramp
    # window; `daily_shares` maps each active holiday's code to the
    # linear-tent share for today (0 at ramp edges, max_share at peak).
    # On each music-kind category-random slot we roll a per-holiday die
    # (independent draws): if `random() < daily_shares[code]`, that
    # slot becomes a "holiday slot" for that holiday and its pool is
    # replaced with a station-wide music-kind holiday pool via
    # _music_holiday_pool. Slots that DIDN'T roll yes on any holiday
    # get their normal pool with active-holiday-tagged tracks excluded
    # (so they can't sneak in through their filed category and inflate
    # the effective share). Non-music-kind slots (Legal ID, imaging,
    # weather, etc.) are unaffected either way.
    active_holidays = _active_holidays_at(target_datetime)
    active_holiday_codes = [h.code for h in active_holidays]
    daily_shares = {
        h.code: _holiday_daily_share(h, target_datetime.date())
        for h in active_holidays
    }

    identity_cache = TrackIdentityCache()
    picks = []
    picked_tracks = []
    picked_identity_keys = set()
    accumulated_seconds = 0.0
    diagnostics = SelectionDiagnostics(
        target_duration_seconds=target_duration_seconds,
        late_offset_seconds=max(0.0, NOMINAL_HOUR_SECONDS - target_duration_seconds),
    )

    def _append_pick(track, category):
        nonlocal accumulated_seconds
        track_duration = effective_airtime_seconds(track)
        scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
        picks.append({
            "position": len(picks),
            "scheduled_time": scheduled_time,
            "track": track,
            "category": category,
        })
        # Always accumulate for within-build hard exclusion, regardless
        # of sep values. sep=0 opts out of the recency-window exclusion
        # (get_separation returns 0 -> no cross-hour recency block) but
        # NOT out of "did I already pick this in THIS build" cycling.
        # See pick_track's header + pool-exhaustion fallback.
        picked_tracks.append(track)
        picked_identity_keys.update(track_identity_keys(
            track.artist.name if track.artist_id else None, track.related_artists,
        ))
        accumulated_seconds += track_duration

    def _sequential_pick(category, artist_sep, title_sep, pool_override_qs, pool_key,
                          exclude_holiday_codes, remaining, force_fit_mode=False):
        """Single-track pick via pick_track -- used for NORMAL mode
        (remaining_seconds simply reported for logging; pick_track's own
        fit_mode only engages below EXACT_FIT_THRESHOLD_SECONDS) and for
        EXACT-FIT mode (direct, or as landing mode's fallback when a
        pair can't be formed -- that call site passes force_fit_mode=
        True, since remaining is still in the landing zone, well above
        EXACT_FIT_THRESHOLD_SECONDS, and must stay duration-aware rather
        than falling through to pick_track's own ordinary unconstrained
        dispatch for that range)."""
        exclude_track_ids, exclude_identity_keys = get_recent_exclusions(
            target_datetime, artist_sep, title_sep, picked_tracks, picked_identity_keys,
        )
        return pick_track(
            category, exclude_track_ids, exclude_identity_keys,
            artist_sep, title_sep, target_datetime,
            remaining_seconds=remaining,
            hard_exclude_track_ids={t.id for t in picked_tracks},
            hard_exclude_identity_keys=set(picked_identity_keys),
            active_holiday_codes=active_holiday_codes,
            pool_override_qs=pool_override_qs,
            exclude_holiday_codes=exclude_holiday_codes,
            identity_cache=identity_cache,
            pool_key=pool_key,
            force_fit_mode=force_fit_mode,
        )

    idx = 0
    while idx < len(slots):
        slot = slots[idx]

        if slot.track_id:
            # Direct track insert — the hybrid rotation/playlist ask.
            # Skips recency separation entirely on the way in, even for a
            # music track that would otherwise violate it; the LogItem it
            # still produces below (with a real scheduled_time) is what
            # makes it count as "recently played" for any category slots
            # that come after it, in this build or future ones. NEVER
            # replaced/reordered/skipped/treated-as-a-candidate-pool by
            # pair planning -- it is never eligible to be slot A or slot
            # B of a landing pair (see the landing-zone branch below,
            # which only ever looks at slots[idx+1] to decide whether a
            # pair is even attemptable, and always falls back to a plain
            # single pick if that neighbor is direct-track).
            _append_pick(slot.track, slot.track.category)
            diagnostics.direct_track_inserts += 1
            idx += 1
            if accumulated_seconds >= target_duration_seconds:
                break
            continue

        category = slot.category
        remaining = target_duration_seconds - accumulated_seconds

        if remaining > PAIR_LANDING_THRESHOLD_SECONDS:
            # Normal mode: unchanged sequential weighted-random picking.
            mode = "normal"
        elif remaining <= EXACT_FIT_THRESHOLD_SECONDS:
            mode = "exact_fit"
        else:
            mode = "landing"

        if mode in ("normal", "exact_fit"):
            artist_sep, title_sep, pool_override_qs, pool_key, exclude_holiday_codes = (
                _resolve_slot_pool_context(category, target_datetime, recency_cfg, active_holiday_codes, daily_shares)
            )
            track = _sequential_pick(category, artist_sep, title_sep, pool_override_qs, pool_key,
                                      exclude_holiday_codes, remaining)
            if track is None:
                diagnostics.pool_exhausted_picks += 1
                idx += 1
                continue
            _append_pick(track, category)
            if mode == "normal":
                diagnostics.normal_picks += 1
            else:
                diagnostics.exact_fit_picks += 1
            idx += 1

        else:
            # Landing zone: EXACT_FIT_THRESHOLD_SECONDS < remaining <=
            # PAIR_LANDING_THRESHOLD_SECONDS. Attempt a two-slot matched
            # pair with the IMMEDIATELY next slot -- never skip past a
            # direct-track slot to reach a category-random one further
            # ahead (that slot's own turn comes next iteration,
            # untouched by this decision).
            ctx_a = _resolve_slot_pool_context(category, target_datetime, recency_cfg, active_holiday_codes, daily_shares)
            artist_sep_a, title_sep_a, pool_override_qs_a, pool_key_a, exclude_holiday_codes_a = ctx_a

            next_slot = slots[idx + 1] if idx + 1 < len(slots) else None
            pair_result = None

            if next_slot is not None and not next_slot.track_id:
                category_b = next_slot.category
                ctx_b = _resolve_slot_pool_context(category_b, target_datetime, recency_cfg, active_holiday_codes, daily_shares)
                artist_sep_b, title_sep_b, pool_override_qs_b, pool_key_b, exclude_holiday_codes_b = ctx_b

                hard_exclude_track_ids = {t.id for t in picked_tracks}
                hard_exclude_identity_keys = set(picked_identity_keys)

                exclude_track_ids_a, exclude_identity_keys_a = get_recent_exclusions(
                    target_datetime, artist_sep_a, title_sep_a, picked_tracks, picked_identity_keys,
                )
                exclude_track_ids_b, exclude_identity_keys_b = get_recent_exclusions(
                    target_datetime, artist_sep_b, title_sep_b, picked_tracks, picked_identity_keys,
                )

                candidates_a = _landing_slot_candidates(
                    category, target_datetime, pool_override_qs_a, exclude_holiday_codes_a,
                    exclude_track_ids_a, exclude_identity_keys_a,
                    hard_exclude_track_ids, hard_exclude_identity_keys,
                    identity_cache, pool_key_a, active_holiday_codes,
                )
                candidates_b = _landing_slot_candidates(
                    category_b, target_datetime, pool_override_qs_b, exclude_holiday_codes_b,
                    exclude_track_ids_b, exclude_identity_keys_b,
                    hard_exclude_track_ids, hard_exclude_identity_keys,
                    identity_cache, pool_key_b, active_holiday_codes,
                )

                pair_result = find_matched_pair(candidates_a, candidates_b, remaining_seconds=remaining)

            if pair_result is not None:
                # Batch-resolve BOTH chosen ids to real Track objects in
                # one query -- never one .get() per candidate scored.
                resolved = {
                    t.id: t for t in Track.objects.select_related("artist", "category").filter(
                        id__in=[pair_result.candidate_a.track_id, pair_result.candidate_b.track_id],
                    )
                }
                track_a = resolved.get(pair_result.candidate_a.track_id)
                track_b = resolved.get(pair_result.candidate_b.track_id)
                if track_a is not None and track_b is not None:
                    _append_pick(track_a, category)
                    _append_pick(track_b, category_b)
                    diagnostics.landing_pairs += 1
                    diagnostics.landing_errors.append(pair_result.landing_error)
                    idx += 2
                    if accumulated_seconds >= target_duration_seconds:
                        break
                    continue
                # Resolved ids vanished between selection and lookup
                # (e.g. deleted mid-build) -- fall through to the
                # single-track fallback below rather than silently
                # dropping the slot.

            # No pair formed (structurally blocked, or find_matched_pair
            # found nothing, or the rare resolve-race above) -- fall
            # back to a single DURATION-AWARE (force_fit_mode=True) pick
            # for slot A alone, reusing its already-resolved context (no
            # re-rolling its holiday dice a second time). Second-pass
            # correction: `remaining` here is still in the landing zone
            # (> EXACT_FIT_THRESHOLD_SECONDS), so without forcing fit
            # mode this would silently fall through to pick_track's
            # ordinary unconstrained weighted selection -- exactly the
            # "immediately degrade to unconstrained normal selection"
            # behavior the pair-window fallback hierarchy must avoid.
            diagnostics.landing_pair_fallbacks += 1
            track = _sequential_pick(category, artist_sep_a, title_sep_a, pool_override_qs_a, pool_key_a,
                                      exclude_holiday_codes_a, remaining, force_fit_mode=True)
            if track is None:
                diagnostics.pool_exhausted_picks += 1
                idx += 1
                continue
            _append_pick(track, category)
            diagnostics.exact_fit_picks += 1
            idx += 1

        if accumulated_seconds >= target_duration_seconds:
            break

    picks, accumulated_seconds = fill_remaining_hour(
        picks, accumulated_seconds, target_datetime,
        active_holiday_codes=active_holiday_codes,
        daily_shares=daily_shares,
        identity_cache=identity_cache,
        target_duration_seconds=target_duration_seconds,
    )

    # Monitoring philosophy (1.1 follow-up): Monitoring tells the
    # operator when the scheduler needs attention; ordinary application
    # logs explain what it did. The full diagnostic payload is always
    # printed (visible in the systemd journal for engineering/debug
    # purposes) -- only an ABNORMAL build additionally raises a
    # Monitoring/SystemEvent. See SelectionDiagnostics.needs_operator_
    # attention's docstring for the exact predicate and why the other
    # candidate conditions from the review are deliberately not
    # duplicated here (they already have their own separate visibility,
    # or would require a large schema change to instrument).
    diagnostics_detail = {"date": target_date.isoformat(), "hour": hour, **diagnostics.as_detail()}
    if diagnostics.needs_operator_attention():
        reasons = []
        if diagnostics.pool_exhausted_picks > 0:
            reasons.append(f"{diagnostics.pool_exhausted_picks} slot(s) had no eligible track survive selection")
        if diagnostics.landing_errors and max(diagnostics.landing_errors) > DURATION_FIT_MARGIN:
            reasons.append(
                f"worst landing error {max(diagnostics.landing_errors):.1f}s exceeds "
                f"the {DURATION_FIT_MARGIN}s fit tolerance"
            )
        print(f"  Hour log selection needs attention for {target_date} {hour:02d}:00:", diagnostics_detail)
        emit_event(
            category="library", level="warning", title="Hour log selection needs attention",
            detail={**diagnostics_detail, "reasons": reasons},
            dedupe_key=f"library|selection-diagnostics|{target_date.isoformat()}|{hour}",
        )
    else:
        print(f"  Hour log selection diagnostics for {target_date} {hour:02d}:00 (healthy, no monitoring event needed):", diagnostics_detail)

    return _persist_log(target_date, hour, picks)


def _build_from_playlist(target_date, hour, playlist, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    items = list(
        playlist.items
        .select_related("track", "track__category", "track__artist")
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
        track_duration = effective_airtime_seconds(track)
        scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
        picks.append({
            "position": len(picks),
            "scheduled_time": scheduled_time,
            "track": track,
            "category": track.category,
        })
        accumulated_seconds += track_duration

    picks, accumulated_seconds = fill_remaining_hour(
        picks, accumulated_seconds, target_datetime,
        target_duration_seconds=target_duration_seconds,
    )
    return _persist_log(target_date, hour, picks)


def _persist_log(target_date, hour, picks):
    """Delete-then-recreate is only safe as one atomic unit -- an
    exception (or a concurrent process crash) between the delete and the
    bulk_create used to be able to leave an hour with NO PlaylistLog row
    at all, a state _install_built_hour/_ensure_log_building have no
    recovery path for short of the next AUTO_BUILD_CHECK_SECONDS tick
    rebuilding from scratch. Wrapping in transaction.atomic() makes the
    whole delete+create+bulk_create sequence all-or-nothing."""
    with transaction.atomic():
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
    approved and live/currently-playing (see engine.py's async live-fill
    worker), where deleting and recreating everything would discard real
    played_at history driving recency avoidance.

    Wrapped in transaction.atomic() so a caller can rely on "either all
    of `picks` landed in the DB, or none did" -- the engine's install
    callback must not extend self.log_items in memory unless this
    fully succeeded."""
    with transaction.atomic():
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


def build_hour_log(target_date, hour, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    block = resolve_schedule_block(target_date, hour)
    if block is None:
        return None, "No schedule block for this hour."

    if block.playlist_id:
        return _build_from_playlist(target_date, hour, block.playlist, target_duration_seconds=target_duration_seconds)
    if block.rotation_id:
        return _build_from_rotation(target_date, hour, block.rotation, target_duration_seconds=target_duration_seconds)

    return None, "ScheduleBlock has neither rotation nor playlist."


@contextmanager
def _advisory_lock_for_hour(target_date, hour):
    """Non-blocking Postgres advisory lock keyed by (target_date, hour),
    serializing every LIVE build_hour_log call across all processes --
    the engine's async worker, force_next_hour, api_log_build. Uses the
    native two-integer form (pg_try_advisory_lock(key1, key2)) rather
    than hashing a string key, avoiding even the theoretical 32-bit
    hash-collision false-contention risk of hashtext(). Session-scoped
    to this DB connection -- acquire/release happen on the same
    connection since both run inside one `with` block. Yields True if
    acquired, False if contended (caller must check and bail out
    without proceeding)."""
    key1, key2 = target_date.toordinal(), hour
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [key1, key2])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [key1, key2])


# Distinct sentinel: "someone else is handling it, retry later" -- not
# a genuine build failure. Callers must not log/alert on this the same
# way as a real error string from build_hour_log itself.
LOCK_CONTENDED = "lock_contention"


def build_and_approve_hour_log_locked(target_date, hour, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    """Engine auto-build semantics: skip rebuilding if an approved log
    already exists; otherwise build + approve, holding the advisory
    lock for the WHOLE persist-then-approve sequence so a concurrent
    builder (a different process) can't delete this row between
    persist and approve -- releasing the lock right after persisting
    and approving separately as a caller-side step would reopen the
    exact same race just one line later. For "ensure this hour is on
    air" callers only: the engine's async build worker and the manual
    force_next_hour command. NOT for admin rebuild-on-demand behavior
    (see build_hour_log_for_admin) -- that has different semantics
    (always rebuilds, never auto-approves) that this would silently
    break.

    `target_duration_seconds` defaults to a full nominal hour; the
    engine's async build worker passes a shorter computed target when
    clock-drift recovery projects the upcoming hour will actually start
    late (see engine.py's _project_upcoming_hour_start). Only applied
    when this call actually builds -- the "existing approved log" fast
    path returns whatever was already built/approved, target unchanged,
    since re-targeting an already-approved log isn't this function's job."""
    with _advisory_lock_for_hour(target_date, hour) as acquired:
        if not acquired:
            return None, LOCK_CONTENDED
        existing = PlaylistLog.objects.filter(date=target_date, hour=hour, status="approved").first()
        if existing:
            return existing, None
        log, error = build_hour_log(target_date, hour, target_duration_seconds=target_duration_seconds)
        if error:
            return log, error
        log.status = "approved"
        log.save(update_fields=["status"])
        return log, None


def build_hour_log_for_admin(target_date, hour, target_duration_seconds=NOMINAL_HOUR_SECONDS):
    """api_log_build's semantics: ALWAYS rebuilds -- matches the
    existing admin behavior exactly (_persist_log's delete-then-
    recreate replaces whatever was there, draft or approved) -- and
    does NOT auto-approve, leaving the result as a draft for human
    review, same as today. Only adds cross-process serialization on
    top of the existing behavior, so a concurrent engine auto-build for
    the same hour can't race it.

    Known, pre-existing caveat (not introduced by this lock): if this
    creates a draft for an hour the engine is also trying to
    auto-build, and the draft is never approved, the engine's own
    auto-build will find no *approved* row and will delete-and-rebuild
    this hour on its own the next time it checks -- same "approved
    existence only" semantics as before, just lock-serialized instead
    of racing. "Draft for human review" is not itself protection from
    the engine's auto-build."""
    with _advisory_lock_for_hour(target_date, hour) as acquired:
        if not acquired:
            return None, LOCK_CONTENDED
        return build_hour_log(target_date, hour, target_duration_seconds=target_duration_seconds)


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


def preview_hour_log(target_date, hour, target_duration_seconds=NOMINAL_HOUR_SECONDS):
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
            track_duration = effective_airtime_seconds(track)
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
            .select_related("category__kind", "track", "track__category__kind", "track__artist")
            .order_by("position")
        )
        if not slots:
            issues.append({"severity": "error", "message": f"Rotation '{block.rotation.name}' has no slots."})
        else:
            walked = True

        recency_cfg = RecencyConfig.load()
        identity_cache = TrackIdentityCache()
        picked_tracks = []
        picked_identity_keys = set()
        active_holidays = _active_holidays_at(target_datetime)
        active_holiday_codes = [h.code for h in active_holidays]
        daily_shares = {
            h.code: _holiday_daily_share(h, target_datetime.date())
            for h in active_holidays
        }

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
                exclude_track_ids, exclude_identity_keys = get_recent_exclusions(
                    target_datetime, artist_sep, title_sep, picked_tracks, picked_identity_keys,
                )

                is_music_slot = category is not None and category.kind.code == "music"
                chosen_holiday_codes = []
                if is_music_slot and active_holiday_codes:
                    for code, share in daily_shares.items():
                        if random.random() < share:
                            chosen_holiday_codes.append(code)
                pool_override_qs = None
                pool_key = None
                exclude_holiday_codes = None
                if chosen_holiday_codes:
                    pool_override_qs = _music_holiday_pool(chosen_holiday_codes, target_datetime)
                    pool_key = ("holiday", tuple(sorted(chosen_holiday_codes)))
                elif is_music_slot and active_holiday_codes:
                    exclude_holiday_codes = active_holiday_codes

                remaining = target_duration_seconds - accumulated_seconds
                track = pick_track(
                    category, exclude_track_ids, exclude_identity_keys,
                    artist_sep, title_sep, target_datetime,
                    remaining_seconds=remaining,
                    hard_exclude_track_ids={t.id for t in picked_tracks},
                    hard_exclude_identity_keys=set(picked_identity_keys),
                    active_holiday_codes=active_holiday_codes,
                    pool_override_qs=pool_override_qs,
                    exclude_holiday_codes=exclude_holiday_codes,
                    identity_cache=identity_cache,
                    pool_key=pool_key,
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

            track_duration = effective_airtime_seconds(track)
            scheduled_time = target_datetime + timedelta(seconds=accumulated_seconds)
            picks.append({
                "position": len(picks), "scheduled_time": scheduled_time,
                "track": track, "category": category,
            })
            # Always accumulate; see _build_from_rotation for rationale.
            picked_tracks.append(track)
            picked_identity_keys |= track_identity_keys(
                track.artist.name if track.artist_id else None, track.related_artists,
            )
            accumulated_seconds += track_duration
            if accumulated_seconds >= target_duration_seconds:
                break

    else:
        issues.append({"severity": "error", "message": "Schedule block has neither rotation nor playlist configured."})

    if walked:
        # Rotation branch has the holiday state computed above; the
        # playlist branch doesn't need it (tracks are fixed).
        if source == "rotation":
            picks, accumulated_seconds = fill_remaining_hour(
                picks, accumulated_seconds, target_datetime,
                active_holiday_codes=active_holiday_codes,
                daily_shares=daily_shares,
                identity_cache=identity_cache,
                target_duration_seconds=target_duration_seconds,
            )
        else:
            picks, accumulated_seconds = fill_remaining_hour(
                picks, accumulated_seconds, target_datetime,
                target_duration_seconds=target_duration_seconds,
            )

        shortfall = target_duration_seconds - accumulated_seconds
        if shortfall > DURATION_FIT_MARGIN:
            issues.append({
                "severity": "warning",
                "message": f"Hour only {int(accumulated_seconds)}s of {int(target_duration_seconds)}s filled ({int(shortfall)}s short) — filler category may also be exhausted.",
            })
        elif accumulated_seconds - target_duration_seconds > 300:
            issues.append({
                "severity": "warning",
                "message": f"Hour overshoots by {int(accumulated_seconds - target_duration_seconds)}s.",
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
                "duration": effective_airtime_seconds(p["track"]),
            }
            for p in picks
        ],
    }
    return result, None
