"""Pure day/night voice-schedule resolution -- the correct ownership
boundary for a fact every Django-side consumer of
WeatherConfig.voice_schedule needs (Road Conditions' KanDrive voice
resolution today; the external weather-ingest project reads this same
schedule via weather.management.commands.dump_weather_config's
exported "voice_schedule" field and applies the identical algorithm
independently -- see that project's own lib/voices.py).

Historically this exact algorithm lived ONLY in weather-ingest's own
lib/voices.py (an external, non-Django file IsadoraAir's Road
Conditions app imported directly by path). That coupling is retired as
part of the shared-TTS migration -- weather-ingest's own provider
dictionary (the reason Road Conditions imported that file in the first
place) no longer exists, so nothing legitimate is left to import.
voice_for_hour() below is the schedule-only half of what that file
used to provide, now owned where it belongs: alongside WeatherConfig
itself, provider-free and DB-free (takes the schedule as a plain
argument, exactly matching the retired function's own signature so
migrating every caller changes zero resolution behavior)."""

import logging

log = logging.getLogger(__name__)


def voice_for_hour(hour, voice_schedule):
    """voice_schedule is a list of [voice, start_hour, end_hour] triples
    (from WeatherConfig.voice_schedule), end inclusive, hours 0-23. A
    range may wrap past midnight (start > end, e.g. ["night", 21, 2])."""
    for voice, start, end in voice_schedule:
        if start <= end:
            if start <= hour <= end:
                return voice
        else:
            if hour >= start or hour <= end:
                return voice
    log.warning("No voice_schedule entry covers hour %d - defaulting to 'day'", hour)
    return "day"


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


def expand_to_hours(voice_schedule):
    """The inverse of compress_from_hours(): a [voice, start, end]
    triple list -> {0: voice, 1: voice, ..., 23: voice}, one entry per
    local hour. Raises ScheduleError for anything voice_for_hour()
    would have to silently paper over -- malformed entries, a gap, or
    an overlap -- since the admin grid must show operators an honest
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
