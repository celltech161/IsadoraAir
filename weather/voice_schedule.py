"""Pure announcer-persona schedule resolution.

WeatherConfig.voice_schedule stores arbitrary persona slot keys, not
day/night modes. Django-side consumers use this provider-free, DB-free
module directly; the in-tree weather_ingest runtime applies the same
algorithm to the JSON exported by dump_weather_config.

"""

class ScheduleError(ValueError):
    """A voice_schedule value that is not well-formed: wrong shape, an
    out-of-range hour, a gap (some hour covered by no entry), or an
    overlap (some hour covered by more than one entry). Raised by both
    directions of the r0028 admin grid's round trip
    (expand_to_hours()/compress_from_hours()) and by anything else that
    wants the SAME authoritative check malformed stored/POSTed data
    must fail -- never a UI-only concern (see this module's own
    provider-free/DB-free boundary: still no DB access here, callers
    that also need persona/voice validation do that separately)."""


def voice_for_hour(hour, voice_schedule):
    """Return the persona slot scheduled for local hour.

    The schedule is validated as a complete, non-overlapping 24-hour
    assignment before resolution. A malformed or incomplete schedule raises
    ScheduleError; no persona name is ever invented as a fallback.
    """
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ScheduleError(f"hour must be an integer from 0 through 23, got {hour!r}")
    return expand_to_hours(voice_schedule)[hour]


def expand_to_hours(voice_schedule):
    """The inverse of compress_from_hours(): a [voice, start, end]
    triple list -> {0: voice, 1: voice, ..., 23: voice}, one entry per
    local hour. Raises ScheduleError for malformed entries, a gap, or
    an overlap, since the admin grid must show operators an honest
    picture of a broken schedule rather than guessing at one (see
    weather/forms.py's own use of this for exactly that "fail clearly
    and safely" requirement)."""
    if not isinstance(voice_schedule, list):
        raise ScheduleError("voice_schedule must be a list")
    coverage = [0] * 24
    hour_to_voice = {}
    for entry in voice_schedule:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 3
            or not isinstance(entry[0], str)
            or not entry[0]
            or isinstance(entry[1], bool)
            or not isinstance(entry[1], int)
            or isinstance(entry[2], bool)
            or not isinstance(entry[2], int)
            or not 0 <= entry[1] <= 23
            or not 0 <= entry[2] <= 23
        ):
            raise ScheduleError(f"malformed schedule entry: {entry!r}")
        voice, start, end = entry
        hours = range(start, end + 1) if start <= end else (*range(start, 24), *range(0, end + 1))
        for hour in hours:
            coverage[hour] += 1
            hour_to_voice[hour] = voice
    missing = [hour for hour, count in enumerate(coverage) if count == 0]
    if missing:
        raise ScheduleError(f"schedule has no entry covering hour(s): {missing}")
    overlapping = [hour for hour, count in enumerate(coverage) if count > 1]
    if overlapping:
        raise ScheduleError(f"schedule has overlapping entries at hour(s): {overlapping}")
    return hour_to_voice


def compress_from_hours(hour_to_voice):
    """The inverse of expand_to_hours(): {0: voice, ..., 23: voice},
    exactly one entry per local hour 0-23 -- -> the minimal [voice,
    start, end] triple list, merging consecutive same-voice hours
    (including a run that wraps past midnight) into one entry each.
    Deterministic output order (by each run's own start hour, ascending)
    so a round trip through expand_to_hours() -> compress_from_hours()
    on an already-canonical schedule reproduces it byte-for-byte -- see
    test_voice_schedule.py's own round-trip-stability tests."""
    if set(hour_to_voice) != set(range(24)):
        raise ScheduleError("must have exactly one assignment for every hour 0-23")
    ordered = [hour_to_voice[hour] for hour in range(24)]
    for hour, voice in enumerate(ordered):
        if not isinstance(voice, str) or not voice:
            raise ScheduleError(f"hour {hour} has no persona assigned")
    if len(set(ordered)) == 1:
        return [[ordered[0], 0, 23]]
    boundaries = [hour for hour in range(24) if ordered[hour] != ordered[hour - 1]]
    runs = []
    for index, start in enumerate(boundaries):
        next_start = boundaries[(index + 1) % len(boundaries)]
        end = (next_start - 1) % 24
        runs.append([ordered[start], start, end])
    runs.sort(key=lambda run: run[1])
    return runs
