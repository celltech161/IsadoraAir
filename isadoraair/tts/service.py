"""Engine-neutral TTS service with validated atomic output publication."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from isadoraair.runtime_components import load_runtime_components
from isadoraair.tts.errors import TTSError, TTSConfigurationError, TTSSynthesisError
from isadoraair.tts.providers import (
    ProviderMap,
    SubprocessTTSProvider,
    UnconfiguredPiperProvider,
    kokoro_provider_command,
)
from isadoraair.tts.request import DEFAULT_TIMEOUT_SECONDS, SynthesisRequest, TTSEngine
from isadoraair.tts.validation import KOKORO_WAV_REQUIREMENTS, validate_wav


PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class TTSService:
    def __init__(self, providers: ProviderMap) -> None:
        self.providers = dict(providers)

    def synthesize(self, request: SynthesisRequest) -> Path:
        provider = self.providers.get(request.engine)
        if provider is None:
            raise TTSConfigurationError(f"no provider is registered for engine '{request.engine.value}'")

        destination = request.output_path
        if destination.exists() and destination.is_dir():
            raise TTSConfigurationError("output path identifies a directory")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.",
                suffix=".tts.tmp.wav",
                dir=destination.parent,
            )
        except OSError as exc:
            raise TTSConfigurationError("output directory cannot be prepared") from exc

        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            provider.synthesize(request, temporary_path)
            validate_wav(temporary_path, provider.wav_requirements)
            os.replace(temporary_path, destination)
            return destination
        except TTSError:
            raise
        except OSError as exc:
            raise TTSSynthesisError("synthesized output could not be published") from exc
        except Exception as exc:
            raise TTSSynthesisError("TTS provider failed unexpectedly") from exc
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # Never hide the request's real provider/validation failure.
                logger.warning("failed to remove temporary TTS output")


def build_default_service() -> TTSService:
    manifest = load_runtime_components()
    kokoro = manifest["components"]["kokoro"]
    runtime = kokoro["runtime"]
    providers = {
        TTSEngine.KOKORO: SubprocessTTSProvider(
            engine=TTSEngine.KOKORO,
            command_factory=kokoro_provider_command(runtime["python"], runtime["provider_module"]),
            cwd=PROJECT_ROOT,
            module_root=PROJECT_ROOT,
            wav_requirements=KOKORO_WAV_REQUIREMENTS,
        ),
        TTSEngine.PIPER: UnconfiguredPiperProvider(),
    }
    return TTSService(providers)


def synthesize(
    text: str,
    *,
    engine: TTSEngine | str,
    voice: str,
    output_path: str | os.PathLike[str],
    speed: float = 1.0,
    language: str = "en-us",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    service: TTSService | None = None,
) -> Path:
    """Public process-isolated TTS API for IsadoraAir Python callers."""

    request = SynthesisRequest(
        text=text,
        engine=engine,
        voice=voice,
        output_path=output_path,
        speed=speed,
        language=language,
        timeout_seconds=timeout_seconds,
    )
    return (service or build_default_service()).synthesize(request)
