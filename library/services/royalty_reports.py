"""Royalty report generation from PlayEvent evidence.

Three output formats:

  soundexchange_nce
    SoundExchange Noncommercial Educational Webcaster Report of Use
    format. Per unique sound recording within the period: featured
    artist, title, ISRC, album, marketing label, spin count. Header
    rows carry service-provider identification. Music-category plays
    only. Applies the 30-second SoundExchange threshold at query
    time (rows with duration_played_seconds < 30 are dropped).

  summary
    Human-readable text: totals, unique counts, missing-ISRC
    percentage, top-N artists and tracks. For the operator to eyeball
    before submitting an NCE report.

  raw_csv
    Every PlayEvent row in the period as-is, with all snapshot fields
    exposed. For archival and audit -- not to be submitted anywhere.
    No category filter, no threshold; that's the whole point.

Callers: library.management.commands.royalty_report (CLI) and
library.views.reports_generate (web POST).
"""
import csv
import io
from collections import Counter
from datetime import date

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from library.models import PlayEvent


# Below this duration a SoundExchange play doesn't count. Applied to
# soundexchange_nce and (implicitly) summary; raw_csv includes
# everything so an operator can inspect skips.
SOUNDEXCHANGE_MIN_SECONDS = 30.0


def _period_bounds(period_start, period_end):
    """Return timezone-aware datetimes bracketing the period in the
    station's active timezone. StationTimeActivateMiddleware sets
    that at request time; management-command context uses the
    settings default fallback."""
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(
        timezone.datetime.combine(period_start, timezone.datetime.min.time()),
        tz,
    )
    # inclusive-end: last-day 23:59:59.999999
    end = timezone.make_aware(
        timezone.datetime.combine(period_end, timezone.datetime.max.time()),
        tz,
    )
    return start, end


def _music_qs(period_start, period_end, apply_threshold=True):
    start, end = _period_bounds(period_start, period_end)
    qs = PlayEvent.objects.filter(
        started_at__gte=start,
        started_at__lte=end,
        category_kind="Music",
    )
    if apply_threshold:
        qs = qs.filter(duration_played_seconds__gte=SOUNDEXCHANGE_MIN_SECONDS)
    return qs


def compute_stats(period_start, period_end):
    """Small dict of period stats. Used by both the summary format
    and the persisted RoyaltyReport row for the /reports/ list page."""
    qs = _music_qs(period_start, period_end)
    total_plays = qs.count()
    unique_tracks = qs.values("track_title", "track_artist").distinct().count()
    unique_artists = qs.exclude(track_artist="").values("track_artist").distinct().count()
    plays_with_isrc = qs.exclude(isrc="").count()
    return {
        "total_plays": total_plays,
        "unique_tracks": unique_tracks,
        "unique_artists": unique_artists,
        "plays_with_isrc": plays_with_isrc,
    }


def _encoder_label_map():
    """Map each live-sample key ("host:port/mount", as constructed by
    sample_icecast_listeners.py) to its Encoder's display name --
    ENABLED encoders only. This is also the exclusion filter -- and,
    via _owned_listener_total below, the SAME exclusion filter every
    consumer of IcecastSample listener data uses: the Icecast/
    Shoutcast servers this station samples can carry OTHER stations'
    streams alongside this station's own on the same shared box
    (observed live on this install: SIDs 1 and 3 are this station's
    configured Encoders, SIDs 2 and 4 belong to a different station
    sharing the same physical Shoutcast server). IcecastSample itself
    has no concept of "ours" -- listeners_by_mount just mirrors
    whatever the server reports -- so every caller here treats any key
    with no entry in this map as not this station's and drops it
    entirely, rather than showing/counting it under its raw key.

    Reconstructs the key the exact same way the sampler does rather
    than duplicating that logic a second time: Icecast uses the
    Encoder's own `mount` verbatim (required non-blank by Encoder.
    clean() for icecast/shoutcast2), Shoutcast 1/2 use `shoutcast_sid`
    (the same normalized-SID property monitoring's own probe already
    relies on) as "/<sid>".

    A disabled Encoder is deliberately excluded, not just unlabeled --
    this map is rebuilt fresh on every call (not cached, not FK-
    backed against IcecastSample), so disabling a stream in admin
    removes it from every report using this map on the very next
    request/generation. That matches "configured and enabled on this
    box" as the live definition of "ours," at the cost of a disabled-
    then-re-enabled stream's in-between history simply not existing --
    acceptable for a listener-trend view and for ATH (there being no
    historical Encoder-ownership ledger is a deliberate scope
    boundary, not an oversight), not an audit trail."""
    from encoders.models import Encoder
    labels = {}
    for enc in Encoder.objects.filter(enabled=True):
        if enc.protocol == "icecast":
            mount = enc.mount if enc.mount.startswith("/") else f"/{enc.mount}"
        else:
            sid = enc.shoutcast_sid
            mount = f"/{sid}" if sid else None
        if not mount:
            continue
        labels[f"{enc.host}:{enc.port}{mount}"] = enc.name
    return labels


def _owned_listener_total(listeners_by_mount, owned_map):
    """Sum of listener counts in one IcecastSample's listeners_by_mount
    dict, counting only keys present in `owned_map` (as returned by
    _encoder_label_map() -- i.e. belonging to a currently enabled
    Encoder). Any other key -- another station sharing the same
    physical Icecast/Shoutcast server, or a stream this box no longer
    has configured -- contributes nothing, and a missing/empty
    listeners_by_mount (the sampler couldn't reach the server that
    cycle) correctly sums to 0 rather than falling back to any raw
    server-wide total.

    This is the ONE definition of "this station's listeners" shared by
    compute_listener_series (per-bucket chart total) and compute_ath
    (SoundExchange ATH integration) -- the two are structurally unable
    to drift apart on what counts as "ours" as long as both call this
    (and _encoder_label_map for `owned_map`) instead of each computing
    it independently."""
    return sum(count for key, count in (listeners_by_mount or {}).items() if key in owned_map)


def compute_ath(period_start, period_end):
    """Aggregate Tuning Hours derived from IcecastSample rows in the
    period.

    Source of truth: `listeners_by_mount`, filtered through
    _owned_listener_total/_encoder_label_map to streams belonging to a
    CURRENTLY ENABLED IsadoraAir Encoder row -- never the sample's raw
    `listeners_total`, which is the whole server's listener count and
    can include another station's streams when this station shares a
    physical Icecast/Shoutcast server with one. A sample whose
    listeners_by_mount is empty/missing contributes 0 for the interval
    it represents, never a fallback to listeners_total (that fallback
    would silently reintroduce the exact bug this exists to prevent,
    precisely when structured per-stream data is unavailable).

    Integrates owned-listeners * dt across the period, where dt is the
    actual time between consecutive samples, capped at 1h so a long
    gap (sampler outage, systemd stopped) can't inflate ATH from one
    over-weighted sample. The final sample's dt runs to the period
    end. Negative dt (clock skew / out-of-order rows) is clamped to 0.
    Returns 0.0 if there are no samples at all in the period.

    Not called if the operator supplies ath_override on the report
    form -- the override wins, unconditionally (see
    generate_soundexchange_nce)."""
    from library.models import IcecastSample
    start, end = _period_bounds(period_start, period_end)
    samples = list(
        IcecastSample.objects
        .filter(sampled_at__gte=start, sampled_at__lte=end)
        .order_by("sampled_at")
        .only("sampled_at", "listeners_by_mount")
    )
    if not samples:
        return 0.0
    owned_map = _encoder_label_map()  # one query, reused across every sample below
    ath_seconds = 0.0
    for i, s in enumerate(samples):
        if i + 1 < len(samples):
            dt = (samples[i + 1].sampled_at - s.sampled_at).total_seconds()
        else:
            dt = (end - s.sampled_at).total_seconds()
        # Cap dt so a long gap (sampler outage, systemd stopped) can't
        # inflate ATH from one over-weighted sample.
        dt = min(max(dt, 0.0), 3600.0)
        ath_seconds += _owned_listener_total(s.listeners_by_mount, owned_map) * dt
    return ath_seconds / 3600.0


def compute_listener_series(period_start, period_end):
    """Bucketed listener time series from IcecastSample rows in the
    period, for the /reports/ Listener Stats tab.

    Bucket width adapts to the requested range so a chart never has to
    render more than a few hundred points per series regardless of
    whether the operator picked a day or a year:
      <= 3 days   -> 15-minute buckets
      <= 60 days  -> 1-hour buckets
      else        -> 1-day buckets
    Buckets are fixed-width in epoch time, anchored to the period's
    (station-local) start -- for the daily-bucket case this can drift
    by up to an hour across a DST transition inside the period; not
    worth the complexity of calendar-aware day-walking for a trend
    chart.

    Only streams belonging to a currently enabled Encoder on this box
    are included -- see _encoder_label_map. Any other key in a
    sample's listeners_by_mount (another station sharing the same
    physical Icecast/Shoutcast server, or a stream this box no longer
    has configured) is dropped entirely, not shown under its raw key.
    "total" is therefore NOT the sampler's raw listeners_total field
    (which counts every stream the server reports, foreign or not) --
    it's recomputed per sample as the sum of only this station's
    streams, so the aggregate line can never include another
    station's listeners.

    Each stream's value in a bucket is the mean of that stream's
    listener count across every sample IN that bucket that reported
    it at all -- a stream absent from every sample in a bucket (its
    Encoder was disabled, or the whole box was unreachable) is left
    out of that bucket's "streams" dict entirely, so the chart renders
    a gap rather than a fabricated zero. "total" is the mean, across
    the bucket's samples, of each sample's own owned-streams sum
    (including samples that owned-summed to zero, e.g. a fetch
    failure that reported no streams at all that cycle).

    Returns a dict with "bucket_seconds", "points" (chronological list
    of {"t": isoformat, "total": float, "streams": {label: float}}),
    "stream_labels" (sorted, stable ordering for the chart legend/
    colors), and "sample_count" (raw IcecastSample rows in range, so
    the UI can tell "no data" from "range not sampled yet")."""
    from library.models import IcecastSample
    start, end = _period_bounds(period_start, period_end)
    span_days = (end - start).total_seconds() / 86400.0
    if span_days <= 3:
        bucket_seconds = 900
    elif span_days <= 60:
        bucket_seconds = 3600
    else:
        bucket_seconds = 86400

    samples = list(
        IcecastSample.objects
        .filter(sampled_at__gte=start, sampled_at__lte=end)
        .order_by("sampled_at")
        .only("sampled_at", "listeners_by_mount")
    )

    labels = _encoder_label_map()
    epoch0 = start.timestamp()
    buckets = {}
    for s in samples:
        idx = int((s.sampled_at.timestamp() - epoch0) // bucket_seconds)
        b = buckets.setdefault(idx, {"total": [], "streams": {}})
        for key, count in (s.listeners_by_mount or {}).items():
            label = labels.get(key)
            if label is None:
                continue  # not a currently enabled Encoder on this box -- excluded
            b["streams"].setdefault(label, []).append(count)
        # Same ownership sum compute_ath uses (_owned_listener_total) --
        # not the loop above's per-label bookkeeping recomputed by hand,
        # so this can't silently drift from what ATH counts as "ours."
        b["total"].append(_owned_listener_total(s.listeners_by_mount, labels))

    points = []
    for idx in sorted(buckets):
        b = buckets[idx]
        bucket_dt = timezone.datetime.fromtimestamp(
            epoch0 + idx * bucket_seconds, tz=start.tzinfo,
        )
        points.append({
            "t": bucket_dt.isoformat(),
            "total": round(sum(b["total"]) / len(b["total"]), 2),
            "streams": {
                label: round(sum(vals) / len(vals), 2)
                for label, vals in b["streams"].items()
            },
        })

    stream_labels = sorted({label for p in points for label in p["streams"]})
    return {
        "bucket_seconds": bucket_seconds,
        "points": points,
        "stream_labels": stream_labels,
        "sample_count": len(samples),
    }


def generate_soundexchange_nce(period_start, period_end, ath_override=None):
    """Return (text, extension) for a SoundExchange NCE Report of Use.

    Header block: service provider, call letters, transmission
    category, period, ATH, channel/program name. Body: per-unique-
    track spin counts.

    ATH source of truth: if ath_override is passed (from the /reports/
    form's manual entry), that wins. Otherwise compute_ath() integrates
    IcecastSample rows over the period. Zero if no samples exist
    (fresh install pre-sampling, or a period before samples started).
    """

    buf = io.StringIO()
    writer = csv.writer(buf)

    # SoundExchange templates use a specific header block. Their
    # column names aren't standardized in a public spec, so this
    # closely follows the sample templates on soundexchange.com and
    # what small NCE stations report in practice. Adjust to the exact
    # template SoundExchange provides at registration time.
    if ath_override is not None:
        ath_value = float(ath_override)
    else:
        ath_value = compute_ath(period_start, period_end)

    writer.writerow([
        "Service Provider",
        "Call Letters",
        "Transmission Category",
        "Period Start",
        "Period End",
        "Aggregate Tuning Hours",
        "Channel or Program Name",
    ])
    from library.models import StationInfo
    info = StationInfo.load()

    writer.writerow([
        info.legal_name,
        info.call_letters,
        "C",  # C = noncommercial simulcast under 17 CFR 380
        period_start.isoformat(),
        period_end.isoformat(),
        f"{ath_value:.2f}" if ath_value else "",
        info.stream_name,
    ])
    writer.writerow([])  # blank separator row

    writer.writerow([
        "Featured Artist",
        "Sound Recording Title",
        "ISRC",
        "Album Title",
        "Marketing Label",
        "Play Frequency",
    ])

    qs = _music_qs(period_start, period_end)
    # Aggregate: one row per unique (title, artist, isrc-or-album+label).
    # SoundExchange treats ISRC as authoritative, so tracks with the
    # same ISRC collapse regardless of title/label spelling differences.
    # Tracks without ISRC collapse on the (title, artist, album, label)
    # tuple. Two rows for the same recording with different tag
    # spellings will end up as two lines -- that's a data-cleanup
    # opportunity, not a report bug.
    counts = Counter()
    key_meta = {}
    for pe in qs.iterator():
        if pe.isrc:
            key = ("isrc", pe.isrc)
        else:
            key = ("tuple", pe.track_title, pe.track_artist, pe.album_title, pe.record_label)
        counts[key] += 1
        # Latest write wins for the display meta (title, artist, etc)
        # since a rename would ideally show the current spelling.
        key_meta[key] = (
            pe.track_artist, pe.track_title, pe.isrc,
            pe.album_title, pe.record_label,
        )

    # Stable output: sort by spin count descending, then artist, then title.
    def sort_key(item):
        (key, cnt) = item
        artist, title, _isrc, _album, _label = key_meta[key]
        return (-cnt, artist.lower(), title.lower())

    for key, cnt in sorted(counts.items(), key=sort_key):
        artist, title, isrc, album, label = key_meta[key]
        writer.writerow([artist, title, isrc, album, label, cnt])

    return buf.getvalue(), "csv"


def generate_summary(period_start, period_end):
    """Human-readable stats block. Not for submission -- for the
    operator to eyeball before generating the SoundExchange CSV."""

    qs = _music_qs(period_start, period_end)
    stats = compute_stats(period_start, period_end)

    lines = []
    lines.append("IsadoraAir royalty report -- summary")
    lines.append("=" * 60)
    lines.append(f"Period: {period_start} to {period_end}")
    lines.append(f"Category filter: Music only")
    lines.append(f"Threshold: {SOUNDEXCHANGE_MIN_SECONDS:.0f}s minimum played per SoundExchange")
    lines.append("")
    lines.append(f"Total plays (post-threshold): {stats['total_plays']:,}")
    lines.append(f"Unique tracks (by title+artist): {stats['unique_tracks']:,}")
    lines.append(f"Unique artists: {stats['unique_artists']:,}")

    isrc_pct = (100.0 * stats["plays_with_isrc"] / stats["total_plays"]) if stats["total_plays"] else 0.0
    lines.append(
        f"Plays with ISRC: {stats['plays_with_isrc']:,} "
        f"({isrc_pct:.1f}%) -- rest use album + label fallback"
    )

    total_hours = qs.aggregate(
        total=Count("id"),
    )
    seconds_qs = qs.values_list("duration_played_seconds", flat=True)
    total_seconds = sum(s for s in seconds_qs if s)
    lines.append(f"Total music airtime: {total_seconds/3600:.1f} hours")

    ath = compute_ath(period_start, period_end)
    from library.models import IcecastSample
    sample_count = IcecastSample.objects.filter(
        sampled_at__gte=_period_bounds(period_start, period_end)[0],
        sampled_at__lte=_period_bounds(period_start, period_end)[1],
    ).count()
    lines.append(
        f"Aggregate Tuning Hours (Icecast, {sample_count} samples): {ath:.2f}"
        + (" -- no samples yet, sampler timer not running?" if not sample_count else "")
    )
    lines.append("")

    # Under-threshold rows (short plays, skips) -- surfaced separately
    # so the operator can see how much was excluded.
    short_qs = PlayEvent.objects.filter(
        started_at__gte=_period_bounds(period_start, period_end)[0],
        started_at__lte=_period_bounds(period_start, period_end)[1],
        category_kind="Music",
        duration_played_seconds__lt=SOUNDEXCHANGE_MIN_SECONDS,
    )
    lines.append(f"Music plays below {SOUNDEXCHANGE_MIN_SECONDS:.0f}s (excluded): {short_qs.count():,}")
    unclosed = PlayEvent.objects.filter(
        started_at__gte=_period_bounds(period_start, period_end)[0],
        started_at__lte=_period_bounds(period_start, period_end)[1],
        category_kind="Music",
        ended_at__isnull=True,
    ).count()
    if unclosed:
        lines.append(f"Music plays with no close-out (engine crash or in-flight): {unclosed:,}")
    lines.append("")

    top_artists = (
        qs.exclude(track_artist="")
          .values("track_artist")
          .annotate(spins=Count("id"))
          .order_by("-spins", "track_artist")[:20]
    )
    if top_artists:
        lines.append(f"Top {min(20, len(top_artists))} artists:")
        for r in top_artists:
            lines.append(f"  {r['spins']:>4}  {r['track_artist']}")
        lines.append("")

    top_tracks = (
        qs.values("track_artist", "track_title")
          .annotate(spins=Count("id"))
          .order_by("-spins", "track_artist", "track_title")[:20]
    )
    if top_tracks:
        lines.append(f"Top {min(20, len(top_tracks))} tracks:")
        for r in top_tracks:
            lines.append(f"  {r['spins']:>4}  {r['track_artist']} -- {r['track_title']}")

    return "\n".join(lines) + "\n", "txt"


def generate_raw_csv(period_start, period_end):
    """Every PlayEvent row in the period, all snapshot fields. No
    category filter, no threshold -- this is the audit source of
    truth for what the other formats derived from."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "started_at", "ended_at", "duration_played_seconds",
        "category_kind", "source",
        "track_artist", "track_title",
        "album_title", "record_label", "isrc",
        "track_id",
    ])
    start, end = _period_bounds(period_start, period_end)
    qs = PlayEvent.objects.filter(
        started_at__gte=start, started_at__lte=end,
    ).order_by("started_at")
    for pe in qs.iterator():
        writer.writerow([
            pe.started_at.isoformat(),
            pe.ended_at.isoformat() if pe.ended_at else "",
            f"{pe.duration_played_seconds:.2f}" if pe.duration_played_seconds is not None else "",
            pe.category_kind, pe.source,
            pe.track_artist, pe.track_title,
            pe.album_title, pe.record_label, pe.isrc,
            pe.track_id or "",
        ])
    return buf.getvalue(), "csv"


GENERATORS = {
    "soundexchange_nce": generate_soundexchange_nce,
    "summary": generate_summary,
    "raw_csv": generate_raw_csv,
}


def generate(period_start, period_end, fmt, ath_override=None):
    """Dispatch entry point. Returns (content_text, file_extension).
    ath_override is only consumed by the soundexchange_nce generator;
    other formats ignore it."""
    if fmt not in GENERATORS:
        raise ValueError(f"Unknown format: {fmt!r}. Choices: {list(GENERATORS)}")
    if fmt == "soundexchange_nce":
        return GENERATORS[fmt](period_start, period_end, ath_override=ath_override)
    return GENERATORS[fmt](period_start, period_end)
