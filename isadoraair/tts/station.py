"""Resolve station logical voices without exposing provider infrastructure."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError

from isadoraair.tts.errors import TTSVoiceUnavailable
from isadoraair.tts.providers import PiperVoiceSpec
from isadoraair.tts.request import DEFAULT_TIMEOUT_SECONDS, SynthesisRequest, TTSEngine
from isadoraair.tts.service import TTSService, build_default_service


@dataclass(frozen=True, slots=True)
class ResolvedStationVoice:
    logical_name: str
    engine: TTSEngine
    provider_voice: str
    language: str
    speed: float
    piper_spec: PiperVoiceSpec | None = None


def _ensure_django_ready() -> None:
    if apps.ready:
        return
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
    import django

    django.setup()


def resolve_station_voice(name: str) -> ResolvedStationVoice:
    """Resolve one enabled logical name or raise a stable voice error."""

    _ensure_django_ready()
    from isadoraair.tts.models import StationTTSVoice

    try:
        voice = StationTTSVoice.objects.select_related("piper_model").get(name=name)
    except StationTTSVoice.DoesNotExist as exc:
        raise TTSVoiceUnavailable(f"logical station voice '{name}' is not configured") from exc
    if not voice.enabled:
        raise TTSVoiceUnavailable(f"logical station voice '{name}' is disabled")
    try:
        voice.full_clean()
    except ValidationError as exc:
        raise TTSVoiceUnavailable(f"logical station voice '{name}' is invalid") from exc

    engine = TTSEngine(voice.engine)
    if engine is TTSEngine.KOKORO:
        return ResolvedStationVoice(
            logical_name=voice.name,
            engine=engine,
            provider_voice=voice.provider_voice,
            language=voice.language,
            speed=voice.speed,
        )

    model = voice.piper_model
    try:
        model.full_clean()
    except ValidationError as exc:
        raise TTSVoiceUnavailable(f"Piper model '{model.model_id}' is invalid") from exc
    spec = PiperVoiceSpec(
        model_id=model.model_id,
        model_filename=model.model_filename,
        config_filename=model.config_filename,
        model_sha256=model.model_sha256,
        config_sha256=model.config_sha256,
        language=model.language,
        sample_rate_hz=model.sample_rate_hz,
    )
    return ResolvedStationVoice(
        logical_name=voice.name,
        engine=engine,
        provider_voice=model.model_id,
        language=voice.language,
        speed=voice.speed,
        piper_spec=spec,
    )


class StationTTSService:
    """High-level service whose callers supply only a logical voice name."""

    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        output_path: str | os.PathLike[str],
        speed: float | None = None,
        language: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        service: TTSService | None = None,
    ) -> Path:
        resolved = resolve_station_voice(voice)
        request = SynthesisRequest(
            text=text,
            engine=resolved.engine,
            voice=resolved.provider_voice,
            output_path=output_path,
            speed=resolved.speed if speed is None else speed,
            language=resolved.language if language is None else language,
            timeout_seconds=timeout_seconds,
        )
        if service is None:
            piper_voices = (resolved.piper_spec,) if resolved.piper_spec is not None else None
            service = build_default_service(piper_voices=piper_voices)
        return service.synthesize(request)


def synthesize_station_voice(
    text: str,
    *,
    voice: str,
    output_path: str | os.PathLike[str],
    speed: float | None = None,
    language: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    service: TTSService | None = None,
) -> Path:
    return StationTTSService().synthesize(
        text,
        voice=voice,
        output_path=output_path,
        speed=speed,
        language=language,
        timeout_seconds=timeout_seconds,
        service=service,
    )
