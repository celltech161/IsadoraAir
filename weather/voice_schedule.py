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
