"""Stable, log-safe error taxonomy for the shared TTS interface."""

from __future__ import annotations

from enum import IntEnum


class TTSExitCode(IntEnum):
    SUCCESS = 0
    USAGE = 2
    CONFIGURATION = 10
    RUNTIME_UNAVAILABLE = 11
    VOICE_UNAVAILABLE = 12
    SYNTHESIS_FAILED = 13
    TIMEOUT = 14


class TTSError(RuntimeError):
    """Base class for safe, actionable TTS failures."""

    category = "tts_error"
    exit_code = TTSExitCode.SYNTHESIS_FAILED


class TTSConfigurationError(TTSError):
    category = "configuration"
    exit_code = TTSExitCode.CONFIGURATION


class TTSRuntimeUnavailable(TTSError):
    category = "runtime_unavailable"
    exit_code = TTSExitCode.RUNTIME_UNAVAILABLE


class TTSVoiceUnavailable(TTSError):
    category = "voice_unavailable"
    exit_code = TTSExitCode.VOICE_UNAVAILABLE


class TTSSynthesisError(TTSError):
    category = "synthesis_failed"
    exit_code = TTSExitCode.SYNTHESIS_FAILED


class TTSOutputValidationError(TTSSynthesisError):
    category = "invalid_output"


class TTSTimeout(TTSError):
    category = "timeout"
    exit_code = TTSExitCode.TIMEOUT


ERROR_TYPES_BY_CATEGORY = {
    error_type.category: error_type
    for error_type in (
        TTSConfigurationError,
        TTSRuntimeUnavailable,
        TTSVoiceUnavailable,
        TTSSynthesisError,
        TTSOutputValidationError,
        TTSTimeout,
    )
}
