"""Resolve product runtime policy plus read-only station configuration.

This module decides *what* must be validated.  It deliberately performs no
filesystem inspection, synthesis, subprocess execution, or database writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from isadoraair.runtime_components import load_runtime_components


COMPONENT_NAMES = ("fdkaac", "kokoro", "piper")


@dataclass(frozen=True, slots=True)
class PiperModelRequirement:
    model_id: str
    model_filename: str
    config_filename: str
    model_sha256: str
    config_sha256: str
    language: str
    sample_rate_hz: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_filename": self.config_filename,
            "config_sha256": self.config_sha256,
            "language": self.language,
            "model_filename": self.model_filename,
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "sample_rate_hz": self.sample_rate_hz,
        }


@dataclass(frozen=True, slots=True)
class VoiceRequirement:
    logical_name: str
    engine: str
    provider_voice: str
    language: str
    speed: float
    reasons: tuple[str, ...]
    piper_model: PiperModelRequirement | None = None


@dataclass(frozen=True, slots=True)
class ComponentRequirement:
    name: str
    required: bool = False
    reasons: tuple[str, ...] = ()
    voices: tuple[VoiceRequirement, ...] = ()
    piper_models: tuple[PiperModelRequirement, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeRequirements:
    components: dict[str, ComponentRequirement]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StationSelection:
    """Path-free station selection supplied to the pure resolver."""

    voices: tuple[VoiceRequirement, ...] = ()
    fdkaac_reasons: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _validation_error(label: str, exc: ValidationError) -> str:
    fields = sorted(getattr(exc, "message_dict", {}) or {})
    suffix = f" ({', '.join(fields)})" if fields else ""
    return f"{label} is invalid{suffix}"


def _weather_voice_references(
    errors: list[str], *, require_complete: bool = False
) -> list[tuple[int, str]]:
    from weather.models import WeatherConfig, WeatherVoicePersona

    config = WeatherConfig.objects.filter(pk=1).only("voice_schedule").first()
    if config is None:
        return []
    slots: set[str] = set()
    hour_coverage = [0] * 24
    if not isinstance(config.voice_schedule, list):
        errors.append("weather voice schedule is invalid")
        return []
    for entry in config.voice_schedule:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 3
            or not isinstance(entry[0], str)
            or not entry[0]
            or not isinstance(entry[1], int)
            or not isinstance(entry[2], int)
            or not 0 <= entry[1] <= 23
            or not 0 <= entry[2] <= 23
        ):
            errors.append("weather voice schedule is invalid")
            return []
        slots.add(entry[0])
        start, end = entry[1], entry[2]
        hours = range(start, end + 1) if start <= end else (*range(start, 24), *range(0, end + 1))
        for hour in hours:
            hour_coverage[hour] += 1
    if any(count != 1 for count in hour_coverage):
        errors.append("weather voice schedule must cover every hour exactly once")
        return []
    personas = {
        persona.slot: persona
        for persona in WeatherVoicePersona.objects.filter(slot__in=slots).only("slot", "tts_voice_id")
    }
    if require_complete:
        missing = sorted(
            slot
            for slot in slots
            if slot not in personas or personas[slot].tts_voice_id is None
        )
        if missing:
            errors.append("enabled road conditions weather TTS schedule is incomplete")
            return []
    return [
        (personas[slot].tts_voice_id, f"weather persona '{slot}'")
        for slot in sorted(slots)
        if slot in personas and personas[slot].tts_voice_id is not None
    ]


def inspect_station_selection() -> StationSelection:
    """Read authoritative Django configuration without creating any rows."""

    from aircheck.models import AircheckConfig
    from encoders.models import Encoder
    from encoders.services.encoder_manager import DEFAULT_INPUT_DEVICE
    from isadoraair.tts.models import StationTTSVoice
    from road_conditions.models import RoadConditionsConfiguration
    from webrequests.models import WebRequestConfig

    errors: list[str] = []
    references: list[tuple[int, str]] = _weather_voice_references(errors)

    web = WebRequestConfig.objects.filter(pk=1).only("enabled", "dedication_tts_voice_id").first()
    if web is not None and web.enabled and web.dedication_tts_voice_id is not None:
        references.append((web.dedication_tts_voice_id, "enabled web-request dedications"))

    road = RoadConditionsConfiguration.objects.filter(pk=1).only(
        "enabled", "tts_voice_id", "tts_use_weather_schedule"
    ).first()
    if road is not None and road.enabled:
        if road.tts_use_weather_schedule:
            scheduled = _weather_voice_references(errors, require_complete=True)
            if not scheduled:
                errors.append("enabled road conditions select an unconfigured weather TTS schedule")
            references.extend((voice_id, f"enabled road conditions via {reason}") for voice_id, reason in scheduled)
        elif road.tts_voice_id is not None:
            references.append((road.tts_voice_id, "enabled road conditions"))

    reasons_by_voice: dict[int, set[str]] = {}
    for voice_id, reason in references:
        reasons_by_voice.setdefault(voice_id, set()).add(reason)

    voices_by_id = {
        voice.pk: voice
        for voice in StationTTSVoice.objects.filter(pk__in=reasons_by_voice).select_related("piper_model")
    }
    selected_voices: list[VoiceRequirement] = []
    for voice_id in sorted(reasons_by_voice):
        voice = voices_by_id.get(voice_id)
        if voice is None:
            errors.append(f"selected logical voice id {voice_id} is missing")
            continue
        reasons = tuple(sorted(reasons_by_voice[voice_id]))
        if not voice.enabled:
            errors.append(f"selected logical voice '{voice.name}' is disabled")
        try:
            voice.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            errors.append(_validation_error(f"selected logical voice '{voice.name}'", exc))

        piper_requirement = None
        if voice.engine == StationTTSVoice.Engine.PIPER and voice.piper_model is not None:
            model = voice.piper_model
            try:
                model.full_clean(validate_unique=False, validate_constraints=False)
            except ValidationError as exc:
                errors.append(_validation_error(f"selected Piper model '{model.model_id}'", exc))
            piper_requirement = PiperModelRequirement(
                model_id=model.model_id,
                model_filename=model.model_filename,
                config_filename=model.config_filename,
                model_sha256=model.model_sha256,
                config_sha256=model.config_sha256,
                language=model.language,
                sample_rate_hz=model.sample_rate_hz,
            )
        selected_voices.append(
            VoiceRequirement(
                logical_name=voice.name,
                engine=voice.engine,
                provider_voice=(
                    piper_requirement.model_id if piper_requirement is not None else voice.provider_voice
                ),
                language=voice.language,
                speed=float(voice.speed),
                reasons=reasons,
                piper_model=piper_requirement,
            )
        )

    fdkaac_reasons: set[str] = set()
    enabled_encoders = list(
        Encoder.objects.filter(enabled=True).only("name", "format", "bitrate_kbps", "input_device")
    )
    for encoder in enabled_encoders:
        if encoder.format == "aac":
            profile = "he_aac_v2" if encoder.bitrate_kbps <= 64 else (
                "he_aac" if encoder.bitrate_kbps <= 96 else "aac_lc"
            )
            fdkaac_reasons.add(f"enabled encoder '{encoder.name}' selects {profile}")

    if any((encoder.input_device or DEFAULT_INPUT_DEVICE) == DEFAULT_INPUT_DEVICE for encoder in enabled_encoders):
        aircheck = AircheckConfig.objects.filter(pk=1).only("audio_format", "bitrate").first()
        if aircheck is not None and aircheck.audio_format == "he_aac":
            fdkaac_reasons.add("active aircheck output selects he_aac")

    return StationSelection(
        voices=tuple(sorted(selected_voices, key=lambda item: item.logical_name)),
        fdkaac_reasons=tuple(sorted(fdkaac_reasons)),
        errors=tuple(sorted(set(errors))),
    )


def resolve_runtime_requirements(
    selection: StationSelection,
    manifest: dict[str, Any] | None = None,
) -> RuntimeRequirements:
    """Pure product-policy + station-selection requirement resolution."""

    product = manifest or load_runtime_components()
    requirements: dict[str, ComponentRequirement] = {}
    for name in COMPONENT_NAMES:
        # Access the policy here so malformed/missing product authority cannot
        # silently become a station default.
        product["components"][name]["availability"]["policy"]
        requirements[name] = ComponentRequirement(name=name)

    for engine in ("kokoro", "piper"):
        voices = tuple(
            sorted(
                (voice for voice in selection.voices if voice.engine == engine),
                key=lambda item: item.logical_name,
            )
        )
        reasons = tuple(sorted({reason for voice in voices for reason in voice.reasons}))
        models = tuple(
            sorted(
                {voice.piper_model.model_id: voice.piper_model for voice in voices if voice.piper_model}.values(),
                key=lambda item: item.model_id,
            )
        )
        requirements[engine] = ComponentRequirement(
            name=engine,
            required=bool(voices),
            reasons=reasons,
            voices=voices,
            piper_models=models,
        )

    requirements["fdkaac"] = ComponentRequirement(
        name="fdkaac",
        required=bool(selection.fdkaac_reasons),
        reasons=selection.fdkaac_reasons,
    )
    unknown_engines = sorted({voice.engine for voice in selection.voices} - {"kokoro", "piper"})
    errors = list(selection.errors)
    errors.extend(f"selected logical voice uses unsupported engine '{engine}'" for engine in unknown_engines)
    return RuntimeRequirements(components=requirements, errors=tuple(sorted(set(errors))))


def resolve_current_runtime_requirements(
    manifest: dict[str, Any] | None = None,
) -> RuntimeRequirements:
    return resolve_runtime_requirements(inspect_station_selection(), manifest)


def unresolved_runtime_requirements(error: str) -> RuntimeRequirements:
    """Return an explicitly indeterminate, fail-closed station result."""

    return RuntimeRequirements(
        components={name: ComponentRequirement(name=name) for name in COMPONENT_NAMES},
        errors=(error,),
    )
