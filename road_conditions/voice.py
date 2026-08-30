"""Voice selection for KanDrive road-report speech.

resolve_voice() has two real modes, chosen by
RoadConditionsConfiguration.tts_use_weather_schedule (mirrors the same
precedence isadoraair.runtime_requirements.inspect_station_selection()
already established: schedule mode wins over a fixed tts_voice):

  SHARED WEATHER-SCHEDULE (tts_use_weather_schedule=True -- KOGR's
  current production state):

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

  LEGACY (tts_use_weather_schedule=False -- the rollback path):
  bypasses the shared TTS service and invokes Kokoro directly (see
  synthesis.py's _synthesize_segment_wav "else" branch), but resolves
  its slot and voice identity through the EXACT SAME Django-owned
  WeatherConfig -> WeatherVoicePersona -> StationTTSVoice chain shared
  mode uses -- there is no separate "provider dictionary" anywhere
  left to duplicate or drift from. Historically this path imported
  weather-ingest's own external lib/voices.py (both for schedule
  resolution AND for a hardcoded day/night Kokoro model-id table) --
  that coupling is retired as part of the shared-TTS migration:
  weather-ingest's own provider dictionary no longer exists, so
  nothing legitimate is left to import, and this module must not
  introduce a new hardcoded copy of a provider voice id either. Only
  the FINAL step differs from shared mode: this path pulls
  resolve_station_voice()'s own provider_voice out into voice["model"]
  for a direct Kokoro subprocess call, and never sets shared_tts=True.
  ROLLBACK-ONLY -- do not expand or modernize this path; KOGR's real
  production state is shared-schedule mode (proven prior to this
  migration), not legacy.

  WHAT THIS PATH DOES AND DOES NOT ISOLATE: because both modes now
  share the exact same WeatherVoicePersona -> StationTTSVoice ->
  resolve_station_voice() identity-resolution chain, switching
  tts_use_weather_schedule to False no longer isolates KanDrive from a
  broken/missing/misconfigured persona or logical voice row -- a
  VoiceResolutionError there fails BOTH modes identically (see
  test_legacy_mode_fails_clearly_when_persona_missing). What this path
  still does isolate KanDrive from is a regression in the shared TTS
  SERVICE/invocation layer itself (synthesize_station_voice() and
  everything under isadoraair.tts.service) -- legacy mode never calls
  that; it takes the already-resolved provider_voice and invokes
  Kokoro directly (see synthesis.py's _synthesize_segment_wav "else"
  branch, unchanged by this migration). This is a real, deliberate
  narrowing of what "rollback" covers, forced by this migration's own
  constraint that no second hardcoded provider-voice-id table may
  exist anywhere (see this workorder's own "never hard-code provider
  voice IDs" rule) -- there is no remaining place to source an
  identity independent of the DB-backed chain. An operator relying on
  this flag as a resolution-layer rollback, not just a service-layer
  one, needs to know that distinction before flipping it.

  A third state, a FIXED tts_voice (no weather schedule involved), is
  intentionally NOT implemented here -- RoadConditionsConfiguration.
  tts_voice has no associated listener-facing name source anywhere in
  the current model (WeatherVoicePersona is schedule-slot-keyed only),
  so completing it cleanly would require inventing new persona
  metadata or a new field, which this round of work was explicitly
  told not to do. Setting tts_voice alone, with
  tts_use_weather_schedule left off, currently has no effect --
  resolve_voice() still takes the legacy path.

Both modes raise the same VoiceResolutionError for any failure --
unknown slot, missing/incomplete persona, non-Kokoro engine, or a
disabled/invalid shared-TTS voice -- so generate_road_condition_audio.py's
existing `except VoiceResolutionError` handling covers both without
changes."""
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


def _resolve_legacy_voice(slot_override, now):
    """ROLLBACK-ONLY -- see this module's own docstring. Resolves
    through the same WeatherVoicePersona/StationTTSVoice chain shared
    mode uses, but pulls the RESOLVED provider voice id out into
    voice["model"] for synthesis.py's direct-Kokoro fallback, and never
    sets shared_tts=True."""
    slot = _resolve_schedule_slot(slot_override, now)
    if slot_override and slot_override not in available_slots():
        raise VoiceResolutionError(
            f"Unknown voice slot {slot_override!r} -- KanDrive currently defines {list(available_slots())}."
        )
    persona, resolved = _resolve_persona_for_slot(slot)
    name, full_name = _persona_listener_facing_name(persona, slot)
    voice = {
        "engine": resolved.engine.value,
        "model": resolved.provider_voice,
        "name": name,
        "full_name": full_name,
        "signoff": persona.signoff,
    }
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
    """Returns (slot_name, voice_dict). Which of two real modes this
    uses is decided by RoadConditionsConfiguration.tts_use_weather_
    schedule -- see this module's own docstring for the full picture;
    both modes raise the same VoiceResolutionError for every failure
    mode, so every existing caller (generate_road_condition_audio.py)
    needs no mode-awareness of its own.

    `now` is an optional aware datetime for tests; defaults to the
    current time. The hour used is LOCAL time (America/Chicago on this
    box), matching WeatherConfig.voice_schedule's own documented
    contract ("local time, 0-23") -- both modes share this via
    _resolve_schedule_slot() above."""
    from road_conditions.models import RoadConditionsConfiguration
    config = RoadConditionsConfiguration.load()

    if config.tts_use_weather_schedule:
        return _resolve_shared_schedule_voice(config, slot_override, now)
    return _resolve_legacy_voice(slot_override, now)
