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


def generate_soundexchange_nce(period_start, period_end):
    """Return (text, extension) for a SoundExchange NCE Report of Use.

    Real SoundExchange templates carry a header block identifying the
    service, then aggregated per-track rows -- one line per unique
    (title, artist, isrc-or-album+label) with a spin count. NCE
    reporters using the ATH shortcut don't need per-listener
    performances; a play frequency + total ATH is sufficient. ATH
    integration (Phase 4) will add the header ATH line; today's
    version emits '' in that slot so the operator can fill it in
    manually until the Icecast pull lands."""

    buf = io.StringIO()
    writer = csv.writer(buf)

    # SoundExchange templates use a specific header block. Their
    # column names aren't standardized in a public spec, so this
    # closely follows the sample templates on soundexchange.com and
    # what small NCE stations report in practice. Adjust to the exact
    # template SoundExchange provides at registration time.
    writer.writerow([
        "Service Provider",
        "Call Letters",
        "Transmission Category",
        "Period Start",
        "Period End",
        "Aggregate Tuning Hours",
        "Channel or Program Name",
    ])
    writer.writerow([
        getattr(settings, "STATION_LEGAL_NAME", ""),
        getattr(settings, "STATION_CALL_LETTERS", ""),
        "C",  # C = noncommercial simulcast under 17 CFR 380
        period_start.isoformat(),
        period_end.isoformat(),
        "",  # ATH -- filled by Phase 4 Icecast integration
        getattr(settings, "STATION_STREAM_NAME", ""),
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


def generate(period_start, period_end, fmt):
    """Dispatch entry point. Returns (content_text, file_extension)."""
    if fmt not in GENERATORS:
        raise ValueError(f"Unknown format: {fmt!r}. Choices: {list(GENERATORS)}")
    return GENERATORS[fmt](period_start, period_end)
