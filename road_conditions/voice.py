"""Voice selection for KanDrive road-report speech.

resolve_voice() now has two real modes, chosen by
RoadConditionsConfiguration.tts_use_weather_schedule (mirrors the same
precedence isadoraair.runtime_requirements.inspect_station_selection()
already established: schedule mode wins over a fixed tts_voice):

  LEGACY (tts_use_weather_schedule=False -- the rollback path, and
  KOGR's state until an operator opts in):

    1. THE SCHEDULE (which hour maps to "day" vs "night") lives in
       Django: WeatherConfig.voice_schedule, admin-editable. Read
       directly via the ORM -- road_conditions runs in the same
       process/database as weather's own Django config.

    2. THE VOICE IDENTITIES (which literal Kokoro model "day"/"night"
       mean -- currently af_jessica/"Claira" and am_liam/"Max") exist
       ONLY in /home/jreed/weather-ingest/lib/voices.py -- an external,
       non-Django project loaded directly by file path with importlib.
       Synthesis for this mode still shells out directly to
       KOKORO_BINARY (see road_conditions/synthesis.py).

  SHARED WEATHER-SCHEDULE (tts_use_weather_schedule=True -- the KOGR
  cutover target):

    1. THE SCHEDULE is the exact same WeatherConfig.voice_schedule,
       resolved via the exact same weather-ingest voice_for_hour()
       function object as the legacy path -- schedule resolution is
       schedule-only logic with zero Kokoro/Piper knowledge, so reusing
       it here (rather than a second Django-native reimplementation)
       means a schedule change can never resolve a different slot in
       one mode than the other.

    2. THE VOICE IDENTITY for that slot comes from
       weather.models.WeatherVoicePersona (feature-level listener-
       facing metadata: display_name/full_name/signoff) -> its
       tts_voice FK (a logical isadoraair.tts.models.StationTTSVoice
       name) -> isadoraair.tts.station.resolve_station_voice() (the
       authoritative provider/engine resolution). Only the logical
       name ever reaches the shared TTS API -- see synthesis.py's
       _synthesize_segment_wav(). The returned voice dict's "model" is
       populated from the RESOLVED provider_voice (not the logical
       name) specifically so report.compute_report_fingerprint()'s
       existing, unchanged voice["model"] field keeps invalidating the
       fingerprint if an operator repoints the same logical voice at a
       different provider identity.

    A third state, a FIXED tts_voice (no weather schedule involved), is
    intentionally NOT implemented here -- RoadConditionsConfiguration.
    tts_voice has no associated listener-facing name source anywhere in
    the current model (WeatherVoicePersona is schedule-slot-keyed only),
    so completing it cleanly would require inventing new persona
    metadata or a new field, which this round of work was explicitly
    told not to do. Setting tts_voice alone, with
    tts_use_weather_schedule left off, currently has no effect --
    resolve_voice() still takes the legacy path. A follow-up round
    would need to decide where a fixed-voice announcer name should
    live (a new field on RoadConditionsConfiguration itself is the
    most likely shape, since a fixed voice has no per-slot persona to
    attach one to) before this third mode can be completed.

Both modes raise the same VoiceResolutionError for any failure --
missing weather-ingest file, unknown slot, non-Kokoro engine, missing/
incomplete persona, or a disabled/invalid shared-TTS voice -- so
generate_road_condition_audio.py's existing
`except VoiceResolutionError` handling covers both without changes.
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


def _resolve_schedule_slot(slot_override, now):
    """The schedule-only half of voice resolution -- identical for both
    modes (see this module's own docstring): reuses weather-ingest's own
    voice_for_hour(), which takes the schedule as a plain argument and
    has no knowledge of VOICES/Kokoro/Piper at all, so a schedule change
    can never resolve a different slot in one mode than the other."""
    if slot_override:
        return slot_override
    from weather.models import WeatherConfig
    module = _load_weather_voices_module()
    config = WeatherConfig.load()
    local_now = dj_timezone.localtime(now or dj_timezone.now())
    return module.voice_for_hour(local_now.hour, config.voice_schedule)


def _resolve_legacy_voice(slot_override, now):
    module = _load_weather_voices_module()

    if slot_override:
        if slot_override not in module.VOICES:
            raise VoiceResolutionError(
                f"Unknown voice slot {slot_override!r} -- weather currently defines {sorted(module.VOICES.keys())}."
            )
        slot = slot_override
    else:
        slot = _resolve_schedule_slot(None, now)

    voice = module.VOICES[slot]
    if voice.get("engine") != "kokoro":
        raise VoiceResolutionError(
            f"Weather's {slot!r} voice is currently configured for engine {voice.get('engine')!r}, not kokoro -- "
            "KanDrive audio generation only supports Kokoro (see road_conditions/synthesis.py). "
            "This is a real, correctly-reported condition, not a bug: fix by either switching that "
            "weather voice slot back to Kokoro, or (in a future round) adding Piper support here."
        )
    return slot, voice


def _resolve_shared_schedule_voice(config, slot_override, now):
    """The weather-schedule -> WeatherVoicePersona -> StationTTSVoice ->
    canonical shared TTS path (see this module's own docstring). Never
    silently falls back to a different announcer or the legacy Kokoro
    path on any failure -- every failure mode below is a clear,
    immediate VoiceResolutionError, raised before any text has been
    composed or any audio synthesized (resolve_voice() is always called
    first -- see generate_road_condition_audio.py), so a bad/incomplete
    persona can never overwrite the last-known-good report."""
    from isadoraair.tts.errors import TTSError
    from isadoraair.tts.station import resolve_station_voice
    from weather.models import WeatherVoicePersona

    slot = _resolve_schedule_slot(slot_override, now)

    try:
        persona = WeatherVoicePersona.objects.select_related("tts_voice").get(slot=slot)
    except WeatherVoicePersona.DoesNotExist:
        raise VoiceResolutionError(
            f"Shared weather-schedule TTS is enabled but no Weather Voice Persona is configured for "
            f"slot {slot!r} -- KanDrive cannot resolve an announcer for this report. "
            "Configure Weather Voice Personas for every slot in WeatherConfig.voice_schedule before "
            "enabling Road Conditions' shared weather-schedule TTS."
        )
    if persona.tts_voice_id is None:
        raise VoiceResolutionError(
            f"Weather Voice Persona {slot!r} has no logical station voice selected -- "
            "KanDrive cannot resolve an announcer for this report."
        )

    try:
        resolved = resolve_station_voice(persona.tts_voice.name)
    except TTSError as exc:
        raise VoiceResolutionError(
            f"Shared TTS voice resolution failed for Weather Voice Persona {slot!r} "
            f"(logical voice {persona.tts_voice.name!r}): {exc}"
        ) from exc

    if resolved.engine.value != "kokoro":
        raise VoiceResolutionError(
            f"Weather Voice Persona {slot!r} resolves to engine {resolved.engine.value!r}, not kokoro -- "
            "KanDrive audio generation only supports Kokoro (see road_conditions/synthesis.py)."
        )

    # Listener-facing name, never the logical StationTTSVoice.name (a
    # technical id such as "Claira_Sky") -- see WeatherVoicePersona's
    # own field docstrings ("never passed to a TTS provider" applies
    # equally to never being SPOKEN or shown as the announcer name).
    # `slot` (e.g. "day") is the last-resort fallback ONLY if an
    # operator leaves both name fields blank -- still never the
    # technical logical id.
    display_name = (persona.display_name or "").strip()
    full_name = (persona.full_name or "").strip()
    name = display_name or full_name or slot

    voice = {
        "engine": resolved.engine.value,
        # The RESOLVED provider voice identity (e.g. "af_jessica"), not
        # the logical name -- this is what makes
        # report.compute_report_fingerprint()'s existing, unchanged
        # voice["model"] field keep invalidating the fingerprint if an
        # operator repoints this same logical voice at a different
        # provider mapping. NEVER passed to a subprocess directly (see
        # synthesis.py's _synthesize_segment_wav) -- fingerprint
        # authenticity only.
        "model": resolved.provider_voice,
        "name": name,
        "full_name": full_name or name,
        "signoff": persona.signoff,
        # The ONLY voice identity synthesis.py's shared-TTS route is
        # allowed to pass to synthesize_station_voice() -- see this
        # module's own docstring and synthesis.py's
        # _synthesize_segment_wav().
        "logical_voice_name": persona.tts_voice.name,
        "shared_tts": True,
        "tts_timeout_seconds": config.tts_timeout_seconds,
    }
    return slot, voice


def resolve_voice(slot_override=None, now=None):
    """Returns (slot_name, voice_dict). Which of two real modes this
    uses is decided by RoadConditionsConfiguration.tts_use_weather_
    schedule -- see this module's own docstring for the full picture;
    both modes raise the same VoiceResolutionError for every failure
    mode, so every existing caller (generate_road_condition_audio.py)
    needs no mode-awareness of its own.

    LEGACY (tts_use_weather_schedule=False -- the rollback path):
    voice_dict is the SAME dict object weather's own VOICES[slot] would
    return (engine/model/name/full_name/signoff/piper_fallback), not a
    copy. `slot_override` (from the management command's --voice
    option) skips schedule resolution entirely and looks up that slot
    directly -- still from the real, shared VOICES dict, so even a
    manual override during development uses a real weather voice
    identity, never an invented one. Raises VoiceResolutionError if the
    resolved voice's engine isn't "kokoro" -- Piper fallback is a
    weather-only concern; KanDrive audio generation only knows how to
    drive Kokoro (see synthesis.py).

    SHARED WEATHER-SCHEDULE (tts_use_weather_schedule=True): voice_dict
    additionally carries logical_voice_name (the only identity ever
    passed to the shared TTS API) and shared_tts=True (the routing flag
    synthesis.py's _synthesize_segment_wav() checks) -- see
    _resolve_shared_schedule_voice()'s own docstring for the full
    failure-mode contract.

    `now` is an optional aware datetime for tests; defaults to the
    current time. The hour used is LOCAL time (America/Chicago on this
    box), matching WeatherConfig.voice_schedule's own documented
    contract ("local time, 0-23") and weather's own scripts (which use
    plain datetime.now().hour, naturally local since they run without
    TZ-aware Django datetimes at all) -- both modes share this via
    _resolve_schedule_slot() above."""
    from road_conditions.models import RoadConditionsConfiguration
    config = RoadConditionsConfiguration.load()

    if config.tts_use_weather_schedule:
        return _resolve_shared_schedule_voice(config, slot_override, now)
    return _resolve_legacy_voice(slot_override, now)
