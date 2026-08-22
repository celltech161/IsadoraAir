"""Engine-neutral public synthesis request."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from os import PathLike
from pathlib import Path

from isadoraair.tts.errors import TTSConfigurationError


DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TEXT_CHARACTERS = 1_000_000
_LOGICAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,31}$")


class TTSEngine(StrEnum):
    KOKORO = "kokoro"
    PIPER = "piper"


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Everything a caller may specify for one TTS operation.

    Runtime interpreters, model paths, voice databases, process scheduling,
    and station service-account details deliberately do not appear here.
    """

    text: str
    engine: TTSEngine | str
    voice: str
    output_path: Path | PathLike[str] | str
    speed: float = 1.0
    language: str = "en-us"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise TTSConfigurationError("input text is empty")
        if len(self.text) > MAX_TEXT_CHARACTERS:
            raise TTSConfigurationError("input text exceeds the supported size limit")

        try:
            engine = self.engine if isinstance(self.engine, TTSEngine) else TTSEngine(str(self.engine).lower())
        except ValueError as exc:
            allowed = ", ".join(engine.value for engine in TTSEngine)
            raise TTSConfigurationError(f"unsupported TTS engine; expected one of: {allowed}") from exc
        object.__setattr__(self, "engine", engine)

        if not isinstance(self.voice, str) or not _LOGICAL_ID_RE.fullmatch(self.voice):
            raise TTSConfigurationError(
                "voice must be a non-empty logical identifier using letters, digits, dot, underscore, or hyphen"
            )
        if not isinstance(self.language, str) or not _LANGUAGE_RE.fullmatch(self.language):
            raise TTSConfigurationError("language must be a non-empty language identifier")

        try:
            speed = float(self.speed)
        except (TypeError, ValueError) as exc:
            raise TTSConfigurationError("speed must be a positive finite number") from exc
        if not math.isfinite(speed) or speed <= 0:
            raise TTSConfigurationError("speed must be a positive finite number")
        object.__setattr__(self, "speed", speed)

        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError) as exc:
            raise TTSConfigurationError("timeout must be a positive finite number of seconds") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise TTSConfigurationError("timeout must be a positive finite number of seconds")
        object.__setattr__(self, "timeout_seconds", timeout)

        if not isinstance(self.output_path, (str, PathLike)) or not str(self.output_path):
            raise TTSConfigurationError("output path is required")
        object.__setattr__(self, "output_path", Path(self.output_path).absolute())
