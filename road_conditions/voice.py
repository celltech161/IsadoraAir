"""Voice selection for KanDrive road-report speech -- reuses weather's
OWN voice-selection function and schedule, rather than a road_conditions
copy that could silently drift from it.

Two genuinely different sources of truth are combined here, on purpose:

  1. THE SCHEDULE (which hour maps to "day" vs "night") already lives in
     Django: WeatherConfig.voice_schedule, admin-editable. Read directly
     via the ORM -- no bridge needed, road_conditions runs in the same
     process/database as weather's own Django config. This alone means
     the schedule itself can never be duplicated or drift; there is
     only ever one copy, in one table.

  2. THE VOICE IDENTITIES (which literal Kokoro model "day"/"night" mean --
     currently af_jessica/"Claira" and am_liam/"Max") exist ONLY in
     /home/jreed/weather-ingest/lib/voices.py -- an external, non-Django
     project with its own venv. That module has zero third-party
     dependencies (stdlib only: os, subprocess, logging), so it's loaded
     directly by file path with importlib rather than hand-copied --
     this is the most literal way to satisfy "use the same voice-
     selection function weather uses": the exact same VOICES dict and
     voice_for_hour() function object, not a reimplementation that could
     drift the moment someone changes one copy and not the other.

If /home/jreed/weather-ingest ever moves or is removed, resolve_voice()
raises a clear VoiceResolutionError rather than a bare ImportError deep
in a stack trace -- this dependency is real and worth a legible failure.
"""
import importlib.util
from pathlib import Path

from django.utils import timezone as dj_timezone

WEATHER_INGEST_VOICES_PATH = Path("/home/jreed/weather-ingest/lib/voices.py")

_cached_module = None


class VoiceResolutionError(Exception):
    """Raised when the shared weather voice module can't be loaded, or
    when a resolved voice isn't usable (e.g. its engine isn't Kokoro --
    Piper support is explicitly out of scope for KanDrive)."""


def _load_weather_voices_module():
    """Imports weather-ingest/lib/voices.py by file path -- NOT a
    package install, just direct source loading, which works
    regardless of which venv is active since the module itself has no
    third-party dependencies. Cached after first successful load."""
    global _cached_module
    if _cached_module is not None:
        return _cached_module
    if not WEATHER_INGEST_VOICES_PATH.is_file():
        raise VoiceResolutionError(
            f"Shared weather voice module not found at {WEATHER_INGEST_VOICES_PATH} -- "
            "KanDrive voice selection depends on this file existing (see road_conditions/voice.py's "
            "module docstring). If weather-ingest has moved, update WEATHER_INGEST_VOICES_PATH."
        )
    spec = importlib.util.spec_from_file_location("_weather_ingest_voices", WEATHER_INGEST_VOICES_PATH)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise VoiceResolutionError(f"Failed to load shared weather voice module: {exc!r}") from exc
    _cached_module = module
    return module


def available_slots():
    """The real slot names weather currently defines (e.g. ["day", "night"]) --
    used to validate --voice and to build admin/command help text without
    hard-coding the list here either."""
    return sorted(_load_weather_voices_module().VOICES.keys())


def resolve_voice(slot_override=None, now=None):
    """Returns (slot_name, voice_dict) -- voice_dict is the SAME dict
    object weather's own VOICES[slot] would return (engine/model/name/
    signoff), not a copy. `slot_override` (from the management command's
    --voice option) skips schedule resolution entirely and looks up
    that slot directly -- still from the real, shared VOICES dict, so
    even a manual override during development uses a real weather voice
    identity, never an invented one.

    `now` is an optional aware datetime for tests; defaults to the
    current time. The hour used is LOCAL time (America/Chicago on this
    box), matching WeatherConfig.voice_schedule's own documented
    contract ("local time, 0-23") and weather's own scripts (which use
    plain datetime.now().hour, naturally local since they run without
    TZ-aware Django datetimes at all).

    Raises VoiceResolutionError if the resolved voice's engine isn't
    "kokoro" -- Piper fallback is a weather-only concern; KanDrive audio
    generation only knows how to drive Kokoro (see synthesis.py)."""
    module = _load_weather_voices_module()

    if slot_override:
        if slot_override not in module.VOICES:
            raise VoiceResolutionError(
                f"Unknown voice slot {slot_override!r} -- weather currently defines {sorted(module.VOICES.keys())}."
            )
        slot = slot_override
    else:
        from weather.models import WeatherConfig
        config = WeatherConfig.load()
        local_now = dj_timezone.localtime(now or dj_timezone.now())
        slot = module.voice_for_hour(local_now.hour, config.voice_schedule)

    voice = module.VOICES[slot]
    if voice.get("engine") != "kokoro":
        raise VoiceResolutionError(
            f"Weather's {slot!r} voice is currently configured for engine {voice.get('engine')!r}, not kokoro -- "
            "KanDrive audio generation only supports Kokoro (see road_conditions/synthesis.py). "
            "This is a real, correctly-reported condition, not a bug: fix by either switching that "
            "weather voice slot back to Kokoro, or (in a future round) adding Piper support here."
        )
    return slot, voice
