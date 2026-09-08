"""Shared day/night announcer voice resolution -- used by
current_temp.py, wx_forecast.py, wx_alert.py, and amber_alert.py.

Routes ALL synthesis through the canonical, station-wide shared TTS
surface:

    /usr/local/bin/isadoraair-tts --voice <logical-name> \
        --output-file <path> --timeout <seconds>

using ONLY a logical StationTTSVoice name resolved via Django-owned
configuration (WeatherConfig.voice_schedule + WeatherVoicePersona,
exported by lib/wxconfig.py's load_weather_config() -- see that
module's own docstring for the cross-venv mechanism). This file knows
nothing about any TTS provider or engine, model files, or provider
voice identifiers -- see this project's own README.md for the current
architecture. The historical provider dictionary this file used to own
(fixed binary paths and hardcoded per-slot provider model ids) is
retired; nothing here duplicates that authority.

Resolution chain:
    WeatherConfig.voice_schedule (exported "voice_schedule")
        -> voice_for_hour() (pure schedule-only logic, "auto" only)
        -> WeatherConfig.voice_personas[slot] (exported persona: logical_voice/
           display_name/full_name/signoff)
        -> /usr/local/bin/isadoraair-tts --voice <logical_voice>
        -> provider/runtime (opaque to this file and every caller)
"""

import logging
import os
import subprocess

log = logging.getLogger(__name__)

# The one and only executable this module ever invokes. See
# docs/TTS_RUNTIME.md (IsadoraAir project) for what this surface does
# internally -- opaque to this project by design.
ISADORAAIR_TTS_BINARY = "/usr/local/bin/isadoraair-tts"

# Matches isadoraair.tts.request.DEFAULT_TIMEOUT_SECONDS (the canonical
# CLI's own public default) -- used unless a caller has a specific
# reason to pass something else. A little headroom is added on top of
# this for OUR OWN subprocess.run() timeout, below, so a synthesis that
# finishes right at the CLI's own --timeout has time to exit cleanly
# and be reported as a timeout by the CLI itself (exit code), not
# killed by us first and reported as a generic launch failure.
DEFAULT_TIMEOUT_SECONDS = 120
_SUBPROCESS_TIMEOUT_MARGIN_SECONDS = 10

# Distinguishable failure reasons -- see SynthesisResult below. Every
# caller's existing `if not voices.synthesize(...):` check keeps
# working unchanged (SynthesisResult.__bool__); tests (and any future
# caller that wants more detail) can inspect .reason.
REASON_MISSING_EXECUTABLE = "missing_executable"
REASON_TIMEOUT = "timeout"
REASON_LAUNCH_FAILED = "launch_failed"
REASON_NONZERO_EXIT = "nonzero_exit"
REASON_OUTPUT_MISSING = "output_missing"
REASON_NO_LOGICAL_VOICE = "no_logical_voice"


class VoiceResolutionError(Exception):
    """Raised when a requested slot/persona/logical voice cannot be
    resolved from Django-owned weather configuration -- missing
    voice_schedule, missing/incomplete persona for a slot, or no
    logical voice selected for that persona. Distinct from a
    SynthesisResult failure (which covers the CLI invocation itself,
    below): this is a configuration problem, raised immediately rather
    than discovered only after attempting synthesis."""


class SynthesisResult:
    """Boolean-compatible result of one synthesize() call -- every
    existing caller's `if not voices.synthesize(...):` check keeps
    working unchanged (via __bool__), while `.reason` lets tests (and
    any future caller) distinguish exactly why a failure happened, per
    this project's own observability requirement:
      - REASON_MISSING_EXECUTABLE: /usr/local/bin/isadoraair-tts absent
      - REASON_TIMEOUT: the canonical CLI did not finish in time
      - REASON_LAUNCH_FAILED: the subprocess could not be started at all
      - REASON_NONZERO_EXIT: the canonical CLI ran and reported failure
      - REASON_OUTPUT_MISSING: exit 0 but no output file was produced
      - REASON_NO_LOGICAL_VOICE: caller-supplied voice has no logical id
    """

    __slots__ = ("ok", "reason", "detail")

    def __init__(self, ok, reason="", detail=""):
        self.ok = ok
        self.reason = reason
        self.detail = detail

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return f"SynthesisResult(ok={self.ok!r}, reason={self.reason!r})"


def voice_for_hour(hour, voice_schedule):
    """voice_schedule is a list of [voice, start_hour, end_hour] triples
    (from WeatherConfig.voice_schedule, exported by
    lib/wxconfig.load_weather_config()), end inclusive, hours 0-23. A
    range may wrap past midnight (start > end, e.g. ["night", 21, 2]).
    Pure schedule-only logic -- must match IsadoraAir's own
    weather.voice_schedule.voice_for_hour() exactly (same algorithm,
    intentionally kept as an independent implementation here since this
    project has no Django/shared-code dependency on the companion
    application beyond the JSON config export)."""
    for voice, start, end in voice_schedule:
        if start <= end:
            if start <= hour <= end:
                return voice
        else:
            if hour >= start or hour <= end:
                return voice
    log.warning("No voice_schedule entry covers hour %d - defaulting to 'day'", hour)
    return "day"


def resolve_voice(cfg, requested_slot, *, now=None):
    """Returns (slot, voice) where voice is a dict with "slot",
    "logical_voice" (the ONLY identity ever passed to synthesize()),
    "name" (short, e.g. "Claira" -- listener-facing, matches the
    historical VOICES[slot]["name"] shape so ID3-title callers need no
    changes), "full_name", and "signoff" -- sourced from
    cfg["voice_personas"][slot], never hardcoded here. Raises
    VoiceResolutionError for any missing/malformed configuration --
    fails closed rather than guessing an announcer.

    requested_slot is "day", "night", or "auto" (resolves the current
    slot from cfg["voice_schedule"] via voice_for_hour()). `now` is an
    optional datetime for tests; defaults to the current local time
    (this project runs without TZ-aware datetimes, same as its own
    historical current_temp.py/wx_forecast.py callers)."""
    if not isinstance(cfg, dict):
        raise VoiceResolutionError("weather configuration is missing or malformed")

    if requested_slot == "auto":
        schedule = cfg.get("voice_schedule")
        if not schedule:
            raise VoiceResolutionError(
                "weather configuration has no voice_schedule -- cannot resolve 'auto'"
            )
        from datetime import datetime
        hour = (now or datetime.now()).hour
        slot = voice_for_hour(hour, schedule)
    else:
        slot = requested_slot

    personas = cfg.get("voice_personas")
    if not isinstance(personas, dict):
        raise VoiceResolutionError("weather configuration has no voice_personas")
    persona = personas.get(slot)
    if not isinstance(persona, dict):
        raise VoiceResolutionError(f"no Weather Voice Persona configured for slot {slot!r}")

    logical_voice = persona.get("logical_voice")
    if not logical_voice:
        raise VoiceResolutionError(
            f"Weather Voice Persona {slot!r} has no logical station voice selected"
        )

    display_name = (persona.get("display_name") or "").strip()
    full_name = (persona.get("full_name") or "").strip()
    name = display_name or full_name or slot

    voice = {
        "slot": slot,
        "logical_voice": logical_voice,
        "name": name,
        "full_name": full_name or name,
        "signoff": persona.get("signoff") or "",
    }
    return slot, voice


def synthesize(text, wav_path, voice, *, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """Render `text` to a WAV at `wav_path` via the canonical shared TTS
    CLI, passing ONLY voice["logical_voice"] -- never a provider id.
    Returns a SynthesisResult (bool-compatible: every existing
    `if not voices.synthesize(...):` caller keeps working unchanged).

    Deliberately does NOT remove/truncate any pre-existing file at
    wav_path before invoking the canonical CLI (the historical
    "os.remove(wav_path)" pre-clear this function used to do was a
    workaround for the old direct-provider dispatch not being atomic;
    the canonical CLI publishes its own output atomically -- see
    isadoraair.tts.service.TTSService.synthesize -- so wav_path is
    either fully replaced with the NEW audio on success, or completely
    untouched on any failure. Callers that need a guaranteed-absent
    path on failure already rely on that atomicity, not on this
    function clearing anything itself)."""
    logical_voice = voice.get("logical_voice") if isinstance(voice, dict) else None
    if not logical_voice:
        log.error("No logical voice on this persona; cannot synthesize.")
        return SynthesisResult(False, REASON_NO_LOGICAL_VOICE, "voice has no logical_voice")

    if not os.path.exists(ISADORAAIR_TTS_BINARY):
        log.error("Canonical TTS executable missing: %s", ISADORAAIR_TTS_BINARY)
        return SynthesisResult(False, REASON_MISSING_EXECUTABLE, ISADORAAIR_TTS_BINARY)

    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)

    argv = [
        ISADORAAIR_TTS_BINARY,
        "--voice", logical_voice,
        "--output-file", wav_path,
        "--timeout", str(timeout_seconds),
    ]
    try:
        result = subprocess.run(
            argv,
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=timeout_seconds + _SUBPROCESS_TIMEOUT_MARGIN_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.error(
            "Canonical TTS timed out after %ss for voice=%s -> %s",
            timeout_seconds, logical_voice, wav_path,
        )
        return SynthesisResult(False, REASON_TIMEOUT, f"timeout={timeout_seconds}s")
    except OSError as e:
        log.error("Canonical TTS could not launch: %s", e)
        return SynthesisResult(False, REASON_LAUNCH_FAILED, str(e))

    if result.returncode != 0:
        # The canonical CLI's own documented, stable stderr format is
        # "isadoraair-tts: <category>: <message>" (see
        # isadoraair.tts.cli.main) -- logged for operators, but never
        # parsed/branched on here. Only the exit code itself is a
        # contract; stderr text is not scraped as an API.
        stderr_text = result.stderr.decode("utf-8", "replace").strip()
        log.error(
            "Canonical TTS failed (exit %d) for voice=%s: %s",
            result.returncode, logical_voice, stderr_text,
        )
        return SynthesisResult(False, REASON_NONZERO_EXIT, f"exit={result.returncode}: {stderr_text}")

    if not os.path.exists(wav_path):
        log.error(
            "Canonical TTS reported success (exit 0) but no output file was produced at %s",
            wav_path,
        )
        return SynthesisResult(False, REASON_OUTPUT_MISSING, wav_path)

    return SynthesisResult(True)
