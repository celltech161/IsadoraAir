"""Dependency-free public interface for IsadoraAir text-to-speech."""

from isadoraair.tts.errors import (
    TTSConfigurationError,
    TTSError,
    TTSOutputValidationError,
    TTSRuntimeUnavailable,
    TTSSynthesisError,
    TTSTimeout,
    TTSVoiceUnavailable,
)
from isadoraair.tts.request import SynthesisRequest, TTSEngine
from isadoraair.tts.service import TTSService, synthesize

__all__ = [
    "SynthesisRequest",
    "TTSConfigurationError",
    "TTSEngine",
    "TTSError",
    "TTSOutputValidationError",
    "TTSRuntimeUnavailable",
    "TTSService",
    "TTSSynthesisError",
    "TTSTimeout",
    "TTSVoiceUnavailable",
    "synthesize",
]
