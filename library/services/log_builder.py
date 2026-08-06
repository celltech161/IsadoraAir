import random
from contextlib import contextmanager
from datetime import date as _date, datetime, time, timedelta

from django.db import connection
from django.db.models import Count, Q
from django.db.models.expressions import RawSQL
from django.utils import timezone

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


DURATION_FIT_THRESHOLD = 480  # start fitting when < 8 minutes remain
DURATION_FIT_MARGIN = 30     # acceptable overshoot in seconds


def _weighted_order(qs, active_holiday_codes=None):
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
    as the plain `order_by("?")` it replaces.

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
    return qs.order_by(RawSQL(
        f"-LN(RANDOM()) / ((rotation_weight + 1 + {boost_expr}) * (1 + LN(1 + "
        "EXTRACT(EPOCH FROM (NOW() - COALESCE(last_played_at, "
        "NOW() - INTERVAL '365 days'))) / 3600.0)))",
        boost_params,
    ))


def _pick_best_fit(qs, remaining_seconds, active_holiday_codes=None):
    """From a queryset of eligible tracks, pick the one whose duration
    best fills the remaining time without overshooting too much."""
    candidates = list(
        _weighted_order(qs, active_holiday_codes=active_holiday_codes)
        .values_list("id", "next_start_seconds", "duration_seconds")[:200]
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


def pick_track(category, exclude_track_ids, exclude_identity_keys,
               artist_sep, title_sep, target_datetime,
               remaining_seconds=None, max_loosening=3,
               hard_exclude_track_ids=None, hard_exclude_identity_keys=None,
               active_holiday_codes=None,
               pool_override_qs=None, exclude_holiday_codes=None,
               identity_cache=None, pool_key=None):
    """`exclude_*` are RECENCY-HISTORY exclusions -- they get progressively
    dropped by the loosening loop below if no candidate can be found.

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
    fit_mode = remaining_seconds is not None and remaining_seconds < DURATION_FIT_THRESHOLD
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
                        identity_cache=None):
    """Top up `picks` with fallback-category tracks (admin-configured via
    LogFillConfig) until the hour is filled, or as tightly as duration-fit
    allows. Called after any build path in case it comes up short of a
    full hour (e.g. a playlist/rotation that doesn't sum to 3600s).

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

    for _ in range(MAX_FILL_TRACKS):
        if remaining <= DURATION_FIT_MARGIN:
            break
        artist_sep, title_sep = get_separation(category, recency_cfg)
        exclude_track_ids, exclude_identity_keys = get_recent_exclusions(
            target_datetime, artist_sep, title_sep, picked_tracks, picked_identity_keys,
        )

        # Same per-holiday dice roll as the main rotation loop.
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
            break  # nothing eligible even after loosening — stop gracefully

        track_duration = track.next_start_seconds or track.duration_seconds or 0
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
        picked_identity_keys |= track_identity_keys(
            track.artist.name if track.artist_id else None, track.related_artists,
        )
        accumulated_seconds += track_duration
        remaining = 3600 - accumulated_seconds
        if track_duration <= 0:
            break  # avoid infinite loop on zero-duration data

    return picks, accumulated_seconds


def _build_from_rotation(target_date, hour, rotation):
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
            exclude_track_ids, exclude_identity_keys = get_recent_exclusions(
                target_datetime, artist_sep, title_sep,
                picked_tracks, picked_identity_keys,
            )

            # Per-slot dice roll: independent draw per active holiday.
            # Only applies to music-kind slots -- Legal ID / imaging /
            # weather / talk slots never get holiday-tagged music
            # sprinkled in.
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
                # Non-holiday slot on a holiday day: keep the normal
                # category pool but strip out active-holiday-tagged
                # tracks so they can't leak in via their filed
                # category (would push actual share above target).
                exclude_holiday_codes = active_holiday_codes

            remaining = 3600 - accumulated_seconds
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
                continue

        track_duration = track.next_start_seconds or track.duration_seconds or 0
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
        picked_identity_keys |= track_identity_keys(
            track.artist.name if track.artist_id else None, track.related_artists,
        )
        accumulated_seconds += track_duration

        if accumulated_seconds >= 3600:
            break

    picks, accumulated_seconds = fill_remaining_hour(
        picks, accumulated_seconds, target_datetime,
        active_holiday_codes=active_holiday_codes,
        daily_shares=daily_shares,
        identity_cache=identity_cache,
    )
    return _persist_log(target_date, hour, picks)


def _build_from_playlist(target_date, hour, playlist):
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


def build_and_approve_hour_log_locked(target_date, hour):
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
    break."""
    with _advisory_lock_for_hour(target_date, hour) as acquired:
        if not acquired:
            return None, LOCK_CONTENDED
        existing = PlaylistLog.objects.filter(date=target_date, hour=hour, status="approved").first()
        if existing:
            return existing, None
        log, error = build_hour_log(target_date, hour)
        if error:
            return log, error
        log.status = "approved"
        log.save(update_fields=["status"])
        return log, None


def build_hour_log_for_admin(target_date, hour):
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
        return build_hour_log(target_date, hour)


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

                remaining = 3600 - accumulated_seconds
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

            track_duration = track.next_start_seconds or track.duration_seconds or 0
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
            if accumulated_seconds >= 3600:
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
            )
        else:
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
