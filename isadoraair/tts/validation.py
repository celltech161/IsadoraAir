"""Reusable validation for native TTS WAV output."""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

from isadoraair.tts.errors import TTSOutputValidationError


@dataclass(frozen=True, slots=True)
class WavRequirements:
    channels: int = 1
    sample_width_bytes: int = 2
    sample_rate_hz: int | None = None
    minimum_sample_rate_hz: int = 8000
    maximum_sample_rate_hz: int = 192000


DEFAULT_WAV_REQUIREMENTS = WavRequirements()
KOKORO_WAV_REQUIREMENTS = WavRequirements(sample_rate_hz=24000)


def validate_wav(path: Path, requirements: WavRequirements = DEFAULT_WAV_REQUIREMENTS) -> None:
    """Reject empty, malformed, compressed, or contract-incompatible WAVs."""

    try:
        if not path.is_file() or path.stat().st_size <= 44:
            raise TTSOutputValidationError("provider output is missing or empty")
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            compression = wav_file.getcomptype()
    except TTSOutputValidationError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise TTSOutputValidationError("provider output is not a valid WAV file") from exc

    if compression != "NONE":
        raise TTSOutputValidationError("provider output must be uncompressed PCM WAV")
    if frame_count <= 0:
        raise TTSOutputValidationError("provider output contains no audio frames")
    if channels != requirements.channels:
        raise TTSOutputValidationError(
            f"provider output has {channels} channel(s); expected {requirements.channels}"
        )
    if sample_width != requirements.sample_width_bytes:
        raise TTSOutputValidationError(
            f"provider output sample width is {sample_width} byte(s); expected {requirements.sample_width_bytes}"
        )
    if not requirements.minimum_sample_rate_hz <= sample_rate <= requirements.maximum_sample_rate_hz:
        raise TTSOutputValidationError("provider output sample rate is outside the supported range")
    if requirements.sample_rate_hz is not None and sample_rate != requirements.sample_rate_hz:
        raise TTSOutputValidationError(
            f"provider output sample rate is {sample_rate} Hz; expected {requirements.sample_rate_hz} Hz"
        )
