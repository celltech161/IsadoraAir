"""Voice selection for KanDrive road-report speech.

resolve_voice() has exactly one real mode: the SHARED WEATHER-SCHEDULE
path, gated on RoadConditionsConfiguration.tts_use_weather_schedule
being True (KOGR's real production state):

    1. THE SCHEDULE is WeatherConfig.voice_schedule, resolved via
       weather.voice_schedule.voice_for_hour() -- pure, provider-free
       schedule logic, Django-owned (see that module's own docstring).

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

  LEGACY (tts_use_weather_schedule=False) is RETIRED as of r0029. It
  used to bypass the shared TTS service and invoke Kokoro directly via
  a hardcoded `/home/jreed/kokoro/bin/kokoro_synth` path -- that
  runtime is gone (retired alongside the direct-Kokoro fallback; see
  docs/DISASTER_RECOVERY.md). tts_use_weather_schedule=False (still
  this field's default, e.g. on a freshly reset/never-configured row)
  is now simply an invalid configuration state for KanDrive: resolve_
  voice() raises VoiceResolutionError immediately, before any text is
  composed or persona/voice lookups are attempted, telling the
  operator to enable tts_use_weather_schedule rather than attempting a
  deleted binary. This is a genuine narrowing of behavior, not a
  fallback -- there is no other way for KanDrive to produce a voice.

  A FIXED tts_voice (no weather schedule involved) is intentionally
  NOT implemented here -- RoadConditionsConfiguration.tts_voice has no
  associated listener-facing name source anywhere in the current model
  (WeatherVoicePersona is schedule-slot-keyed only), so completing it
  cleanly would require inventing new persona metadata or a new field,
  which this round of work was explicitly told not to do. Setting
  tts_voice alone, with tts_use_weather_schedule left off, still has no
  effect -- resolve_voice() raises the same "not enabled" error.

Every failure -- schedule disabled, unknown slot, missing/incomplete
persona, non-Kokoro engine, or a disabled/invalid shared-TTS voice --
raises the same VoiceResolutionError, so generate_road_condition_audio.py's
existing `except VoiceResolutionError` handling covers all of them
without changes."""
from django.utils import timezone as dj_timezone

from weather.voice_schedule import voice_for_hour

# Real slot names this station currently schedules -- kept here (not
# derived from a live query) purely for --voice argument validation
# and help text; see available_slots() below. Matches
# WeatherConfig.voice_schedule's own documented contract ("day" or
# "night").
KNOWN_SLOTS = ("day", "night")


class VoiceResolutionError(Exception):
    """Raised when a slot/persona/logical voice can't be resolved, or
    when a resolved voice isn't usable (e.g. its engine isn't Kokoro --
    Piper support is explicitly out of scope for KanDrive)."""


def available_slots():
    """The slot names this station currently schedules -- used to
    validate --voice and to build admin/command help text without
    hard-coding the list at each call site."""
    return sorted(KNOWN_SLOTS)


def _resolve_schedule_slot(slot_override, now):
    """The schedule-only half of voice resolution -- identical for both
    modes (see this module's own docstring): WeatherConfig.voice_schedule
    resolved via weather.voice_schedule.voice_for_hour(), which has no
    knowledge of personas/Kokoro/Piper at all, so a schedule change can
    never resolve a different slot in one mode than the other."""
    if slot_override:
        return slot_override
    from weather.models import WeatherConfig
    config = WeatherConfig.load()
    local_now = dj_timezone.localtime(now or dj_timezone.now())
    return voice_for_hour(local_now.hour, config.voice_schedule)


def _resolve_persona_for_slot(slot):
    """Shared by both modes: WeatherVoicePersona -> its tts_voice FK ->
    isadoraair.tts.station.resolve_station_voice(). Raises
    VoiceResolutionError for any missing/incomplete persona or
    disabled/invalid/non-Kokoro voice -- never silently falls back to a
    different announcer."""
    from isadoraair.tts.errors import TTSError
    from isadoraair.tts.station import resolve_station_voice
    from weather.models import WeatherVoicePersona

    try:
        persona = WeatherVoicePersona.objects.select_related("tts_voice").get(slot=slot)
    except WeatherVoicePersona.DoesNotExist:
        raise VoiceResolutionError(
            f"No Weather Voice Persona is configured for slot {slot!r} -- "
            "KanDrive cannot resolve an announcer for this report. "
            "Configure Weather Voice Personas for every slot in WeatherConfig.voice_schedule."
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
    return persona, resolved


def _persona_listener_facing_name(persona, slot):
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
    return name, (full_name or name)


def _resolve_shared_schedule_voice(config, slot_override, now):
    """The weather-schedule -> WeatherVoicePersona -> StationTTSVoice ->
    canonical shared TTS path (see this module's own docstring). Never
    silently falls back to a different announcer on any failure --
    every failure mode below is a clear, immediate VoiceResolutionError,
    raised before any text has been composed or any audio synthesized
    (resolve_voice() is always called first -- see
    generate_road_condition_audio.py), so a bad/incomplete persona can
    never overwrite the last-known-good report."""
    slot = _resolve_schedule_slot(slot_override, now)
    persona, resolved = _resolve_persona_for_slot(slot)
    name, full_name = _persona_listener_facing_name(persona, slot)

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
        "full_name": full_name,
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
    """Returns (slot_name, voice_dict) via the shared weather-schedule
    path -- see this module's own docstring for the full picture.
    Raises VoiceResolutionError, before any text is composed or audio
    synthesized, when RoadConditionsConfiguration.tts_use_weather_
    schedule is not enabled (the direct-Kokoro rollback path this used
    to fall back to was retired in r0029; that runtime no longer
    exists) as well as for every other resolution failure (unknown
    slot, missing/incomplete persona, non-Kokoro engine, disabled/
    invalid shared-TTS voice) -- so every existing caller
    (generate_road_condition_audio.py) needs no mode-awareness of its
    own.

    `now` is an optional aware datetime for tests; defaults to the
    current time. The hour used is LOCAL time (America/Chicago on this
    box), matching WeatherConfig.voice_schedule's own documented
    contract ("local time, 0-23") -- see _resolve_schedule_slot()
    above."""
    from road_conditions.models import RoadConditionsConfiguration
    config = RoadConditionsConfiguration.load()

    if not config.tts_use_weather_schedule:
        raise VoiceResolutionError(
            "RoadConditionsConfiguration.tts_use_weather_schedule is not enabled -- "
            "KanDrive has no other way to resolve a voice. The legacy direct-Kokoro "
            "path this used to fall back to was retired in r0029 and no longer exists. "
            "Enable tts_use_weather_schedule (Road Conditions configuration) to resolve "
            "an announcer through the shared weather schedule."
        )
    return _resolve_shared_schedule_voice(config, slot_override, now)
