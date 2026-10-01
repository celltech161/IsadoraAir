"""Minute-resolution schedule resolution for roadmap 3.1C.

Storage model
-------------
A schedule is a set of *transition rows*: ScheduleBlock rows whose exact
``start_time`` says when a Rotation/Playlist becomes effective. The 10:00 row
is the base of the hour; later rows in the same hour (10:30, 10:45 ...) are
explicit transitions layered on top of it. Nothing is materialized per minute.

Layering (always inside ONE ScheduleProfile -- there is no cross-profile
fallback):

1. the recurring weekly layer (``day_of_week`` rows);
2. the selected date's ``specific_date`` layer.

Within a layer the latest row starting at or before a minute wins. The date
layer takes precedence over the weekly layer for every minute it covers, i.e.
from its own first row onward; a LATER weekly transition does not punch
through an already-active date-specific row. Before a date layer's first row,
weekly inheritance stays visible. There are no blank/tombstone date rows:
reverting means deleting the dated row and exposing whatever lower layer is
then effective.

Base-hour invariant
-------------------
Minute detail refines a scheduled wall-clock hour. A buildable hour must have
an effective assignment at ``HH:00`` (a dated row at HH:00 or a weekly row at
HH:00). An hour with only later rows is NOT buildable and resolves to no
segments, exactly like the legacy exact-hour resolver
(``log_builder.resolve_schedule_block``), which this module deliberately does
not replace: the legacy helper keeps its exact ``HH:00`` meaning so blank
continuation hours stay blank.

Only rows whose ``start_time`` falls exactly on a minute (seconds == 0) take
part; a hand-entered row at 10:30:15 is ignored rather than guessed at.
"""
from __future__ import annotations

import dataclasses
from datetime import time

from library.models import ScheduleBlock

ORIGIN_DATE = "date_override"
ORIGIN_WEEKLY = "weekly"
ORIGIN_NONE = "none"


class ScheduleConflict(Exception):
    """A schedule write/delete that would leave minute transitions without a
    base assignment. Views turn it into HTTP 409."""

    status = 409

    def __init__(self, message, *, blockers=None):
        super().__init__(message)
        self.message = message
        self.blockers = blockers or []


@dataclasses.dataclass(frozen=True)
class ScheduleSegment:
    """One effective programming source inside a wall-clock hour."""
    start_minute: int
    block: ScheduleBlock
    origin: str

    @property
    def start_time(self):
        return time(self.block.start_time.hour, self.start_minute)

    @property
    def content_kind(self):
        return self.block.content_kind

    @property
    def content(self):
        return self.block.content


def _minute_of(row):
    """The row's minute-of-hour, or None if it does not sit exactly on a minute."""
    start = row.start_time
    if start.second or start.microsecond:
        return None
    return start.minute


def _index(rows):
    by_minute = {}
    for row in rows:
        minute = _minute_of(row)
        if minute is not None:
            by_minute[minute] = row
    return by_minute


def _effective_at(minute, weekly, dated):
    """(row, origin) effective at `minute` from two {minute: row} layers."""
    dated_starts = [m for m in dated if m <= minute]
    if dated_starts:
        return dated[max(dated_starts)], ORIGIN_DATE
    weekly_starts = [m for m in weekly if m <= minute]
    if weekly_starts:
        return weekly[max(weekly_starts)], ORIGIN_WEEKLY
    return None, ORIGIN_NONE


def effective_segments(weekly_rows, dated_rows):
    """Ordered effective segments for ONE hour from that hour's rows.

    Returns [] when there is no effective assignment at HH:00 (the hour is not
    buildable). A new segment begins whenever the effective ROW changes, so a
    weekly transition shadowed by an active dated row creates none.
    """
    weekly, dated = _index(weekly_rows), _index(dated_rows)
    if 0 not in weekly and 0 not in dated:
        return []
    segments = []
    previous = None
    for minute in sorted(set(weekly) | set(dated)):
        row, origin = _effective_at(minute, weekly, dated)
        if row is None or (previous is not None and row.pk == previous.pk):
            continue
        segments.append(ScheduleSegment(start_minute=minute, block=row, origin=origin))
        previous = row
    return segments


def minute_map(weekly_rows, dated_rows, *, layer):
    """Sixty server-derived entries describing the hour, for the Hour Detail UI.

    ``layer`` is ``"weekly"`` or ``"date"``: the layer being edited. Each entry:
    ``minute``, ``origin`` (date_override / weekly / none), ``effective_block``
    (ScheduleBlock or None), ``explicit_block`` (the row IN THE EDITED LAYER
    that starts exactly at this minute, or None), ``inherited_transition``
    (date layer only: a weekly row starts here AND is the effective source at
    this minute, i.e. no dated row at or before it shadows it),
    ``segment_start`` (an effective segment begins here) and ``has_base``. Rows that cannot take effect because the hour has no
    base are reported as ``orphan`` explicit rows and never as effective.
    """
    weekly, dated = _index(weekly_rows), _index(dated_rows)
    segments = effective_segments(weekly_rows, dated_rows)
    has_base = bool(segments)
    segment_starts = {segment.start_minute for segment in segments}
    edited = dated if layer == "date" else weekly
    entries = []
    for minute in range(60):
        row, origin = _effective_at(minute, weekly, dated) if has_base else (None, ORIGIN_NONE)
        explicit = edited.get(minute)
        entries.append({
            "minute": minute,
            "origin": origin,
            "effective_block": row,
            "explicit_block": explicit,
            # A weekly transition is "inherited" only when it actually becomes
            # effective here: a weekly row shadowed by an earlier dated row
            # (origin is the date layer) is not.
            "inherited_transition": bool(
                layer == "date" and minute in weekly and explicit is None
                and has_base and origin == ORIGIN_WEEKLY
            ),
            "segment_start": minute in segment_starts,
            "has_base": has_base,
            "orphan": bool(explicit is not None and not has_base),
        })
    return entries


def _hour_bounds(hour):
    return time(hour, 0), time(hour, 59, 59, 999999)


def load_hour_rows(profile, target_date, hour):
    """(weekly_rows, dated_rows) for one profile, one date's weekday/date and
    one wall-clock hour -- two small queries."""
    low, high = _hour_bounds(hour)
    in_hour = (
        ScheduleBlock.objects
        .filter(profile=profile, start_time__gte=low, start_time__lte=high)
        .select_related("rotation", "playlist")
    )
    weekly = list(in_hour.filter(day_of_week=target_date.weekday(), specific_date__isnull=True))
    dated = list(in_hour.filter(specific_date=target_date, day_of_week__isnull=True))
    return weekly, dated


def load_weekly_hour_rows(profile, day_of_week, hour):
    low, high = _hour_bounds(hour)
    return list(
        ScheduleBlock.objects
        .filter(profile=profile, day_of_week=day_of_week, specific_date__isnull=True,
                start_time__gte=low, start_time__lte=high)
        .select_related("rotation", "playlist")
    )


def resolve_schedule_segments(target_date, hour, profile):
    """Effective ordered segments for one hour of one concrete profile.

    `profile` is required (callers capture the profile once; see
    log_builder.build_hour_log). The first segment, when present, is always the
    same row the legacy exact-hour resolver returns for HH:00.
    """
    weekly, dated = load_hour_rows(profile, target_date, hour)
    return effective_segments(weekly, dated)


def detail_counts_for_date(profile, target_date):
    """{hour: number of effective transitions after the base} for one date,
    computed from two queries -- the server-side input for the "detailed hour"
    overview indicator."""
    weekly_by_hour, dated_by_hour = {}, {}
    for row in ScheduleBlock.objects.filter(
        profile=profile, day_of_week=target_date.weekday(), specific_date__isnull=True,
    ):
        weekly_by_hour.setdefault(row.start_time.hour, []).append(row)
    for row in ScheduleBlock.objects.filter(
        profile=profile, specific_date=target_date, day_of_week__isnull=True,
    ):
        dated_by_hour.setdefault(row.start_time.hour, []).append(row)
    counts = {}
    for hour in set(weekly_by_hour) | set(dated_by_hour):
        segments = effective_segments(weekly_by_hour.get(hour, []), dated_by_hour.get(hour, []))
        if len(segments) > 1:
            counts[hour] = len(segments) - 1
    return counts


# ---------------------------------------------------------------
# Write-side safety (base-hour invariant)
# ---------------------------------------------------------------

def assert_base_for_write(profile, *, day_of_week, specific_date, hour, minute):
    """Reject a non-zero-minute transition that would have no base at HH:00."""
    if minute == 0:
        return
    at_base = ScheduleBlock.objects.filter(profile=profile, start_time=time(hour, 0))
    if specific_date is None:
        has_base = at_base.filter(day_of_week=day_of_week, specific_date__isnull=True).exists()
        needs = "a weekly assignment at that day's"
    else:
        has_base = (
            at_base.filter(specific_date=specific_date, day_of_week__isnull=True).exists()
            or at_base.filter(day_of_week=specific_date.weekday(), specific_date__isnull=True).exists()
        )
        needs = "a date override or weekly assignment at that date's"
    if not has_base:
        raise ScheduleConflict(
            f"A minute transition needs {needs} {hour:02d}:00 base assignment first.",
        )


def assert_can_delete(block):
    """Reject deleting an HH:00 base while later transitions depend on it."""
    minute = _minute_of(block)
    if minute != 0:
        return
    hour = block.start_time.hour
    low, high = _hour_bounds(hour)
    base_time = time(hour, 0)
    blockers = []
    in_hour = ScheduleBlock.objects.filter(profile=block.profile, start_time__gte=low, start_time__lte=high)
    if block.day_of_week is not None:
        weekly_dependents = (
            in_hour.filter(day_of_week=block.day_of_week, specific_date__isnull=True)
            .exclude(pk=block.pk).count()
        )
        if weekly_dependents:
            blockers.append(f"{weekly_dependents} later weekly transition(s) in this hour depend on this base")
        stranded = sorted({
            row.specific_date for row in in_hour.filter(specific_date__isnull=False).exclude(start_time=base_time)
            if row.specific_date.weekday() == block.day_of_week
            and not ScheduleBlock.objects.filter(
                profile=block.profile, specific_date=row.specific_date, start_time=base_time,
            ).exists()
        })
        if stranded:
            blockers.append(
                "dated transition(s) on " + ", ".join(day.isoformat() for day in stranded)
                + " depend on this weekly base"
            )
    else:
        dated_dependents = (
            in_hour.filter(specific_date=block.specific_date).exclude(pk=block.pk).count()
        )
        weekly_base = ScheduleBlock.objects.filter(
            profile=block.profile, day_of_week=block.specific_date.weekday(),
            specific_date__isnull=True, start_time=base_time,
        ).exists()
        if dated_dependents and not weekly_base:
            blockers.append(f"{dated_dependents} dated transition(s) in this hour would be left without a base")
    if blockers:
        raise ScheduleConflict(
            "This base assignment cannot be removed while later transitions depend on it.",
            blockers=blockers,
        )
