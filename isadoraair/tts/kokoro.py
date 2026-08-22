"""IsadoraAir-owned Kokoro synthesis provider and Piper-compatible CLI.

This preserves the product-relevant behavior of the production helper without
its host-specific interpreter, home directory, CPU affinity, niceness, or
fixed ONNX thread count. Runtime and asset defaults come from the checked-in
component contract, and callers may override asset paths for staged validation.
"""

from __future__ import annotations

import argparse
import os
import sys
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from isadoraair.runtime_components import get_runtime_component
from isadoraair.tts.normalization import preprocess_text


RuntimeFactory = Callable[[str, str], Any]


class KokoroSynthesisError(RuntimeError):
    """Kokoro cannot synthesize because its invocation contract is invalid."""


def _default_runtime_factory(model_path: str, voices_path: str) -> Any:
    # Import only inside the separately provisioned TTS runtime. Importing this
    # module for management commands or unit tests must not require kokoro-onnx.
    from kokoro_onnx import Kokoro

    return Kokoro(model_path, voices_path)


def _encode_pcm_s16(audio: Any) -> bytes:
    """Apply the production NumPy clip/scale/int16 conversion."""

    # NumPy is owned by the dedicated Kokoro runtime and intentionally is not
    # a main-app venv dependency. Keeping this behind a small seam lets normal
    # Django unit tests mock the external TTS runtime completely.
    import numpy as np

    return (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


def canonical_kokoro_paths() -> tuple[Path, Path]:
    """Return model and voice-data paths from the component contract."""

    component = get_runtime_component("kokoro")
    return Path(component["assets"]["model"]["path"]), Path(component["assets"]["voices"]["path"])


class KokoroSynthesizer:
    """Synthesize one text payload to a mono signed-16-bit WAV file."""

    def __init__(
        self,
        *,
        model_path: str | os.PathLike[str] | None = None,
        voices_path: str | os.PathLike[str] | None = None,
        runtime_factory: RuntimeFactory | None = None,
    ) -> None:
        canonical_model, canonical_voices = canonical_kokoro_paths()
        self.model_path = Path(model_path) if model_path is not None else canonical_model
        self.voices_path = Path(voices_path) if voices_path is not None else canonical_voices
        self.runtime_factory = runtime_factory or _default_runtime_factory

    def _validate_required_paths(self) -> None:
        for label, path in (("model", self.model_path), ("voices database", self.voices_path)):
            if not path.is_file():
                raise KokoroSynthesisError(f"Kokoro {label} file is missing: {path}")

    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        output_path: str | os.PathLike[str],
        speed: float = 1.0,
        lang: str = "en-us",
    ) -> Path:
        """Normalize ``text`` and write production-compatible WAV output."""

        stripped_text = text.strip()
        if not stripped_text:
            raise KokoroSynthesisError("no text on stdin")
        self._validate_required_paths()

        runtime = self.runtime_factory(str(self.model_path), str(self.voices_path))
        audio, sample_rate = runtime.create(
            preprocess_text(stripped_text),
            voice=voice,
            speed=speed,
            lang=lang,
        )

        pcm = _encode_pcm_s16(audio)
        destination = Path(output_path).absolute()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm)
        return destination


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isadoraair-tts")
    parser.add_argument("--model", required=True, help="Kokoro voice name")
    parser.add_argument("--output_file", required=True, help="Path to write the WAV to")
    parser.add_argument("--speed", type=float, default=1.0, help="Speech rate multiplier; default 1.0")
    parser.add_argument("--lang", default="en-us")
    parser.add_argument("--model-path", help="Override the contract model path for staged validation")
    parser.add_argument("--voices-path", help="Override the contract voices path for staged validation")
    return parser


def main(argv: Sequence[str] | None = None, *, stdin: TextIO | None = None) -> int:
    """Run the Piper-compatible Kokoro command-line interface."""

    parser = build_argument_parser()
    args = parser.parse_args(argv)
    text = (stdin or sys.stdin).read().strip()
    if not text:
        raise SystemExit("kokoro_synth: no text on stdin")

    synthesizer = KokoroSynthesizer(model_path=args.model_path, voices_path=args.voices_path)
    synthesizer.synthesize(
        text,
        voice=args.model,
        output_path=args.output_file,
        speed=args.speed,
        lang=args.lang,
    )
    return 0
