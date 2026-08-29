"""Internal worker executed by a dedicated engine runtime interpreter."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import TextIO

from isadoraair.tts.errors import TTSExitCode
from isadoraair.tts.kokoro import KokoroSynthesisError, KokoroSynthesizer
from isadoraair.tts.providers import PROVIDER_ERROR_PREFIX
from isadoraair.tts.request import TTSEngine


def _emit_error(category: str, message: str, stderr: TextIO) -> None:
    payload = json.dumps({"category": category, "message": message}, separators=(",", ":"))
    stderr.write(f"{PROVIDER_ERROR_PREFIX}{payload}\n")


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isadoraair-tts-provider")
    parser.add_argument("--engine", required=True, choices=[engine.value for engine in TTSEngine])
    parser.add_argument("--voice", required=True)
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--language", default="en-us")
    parser.add_argument("--model-path", help="Internal staged Kokoro model override")
    parser.add_argument("--voices-path", help="Internal staged Kokoro voices override")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_argument_parser().parse_args(argv)
    input_stream = stdin or sys.stdin
    error_stream = stderr or sys.stderr
    text = input_stream.read().strip()
    if not text:
        _emit_error("configuration", "input text is empty", error_stream)
        return int(TTSExitCode.CONFIGURATION)
    if args.engine != TTSEngine.KOKORO.value:
        _emit_error("runtime_unavailable", "Piper provider is not productized yet", error_stream)
        return int(TTSExitCode.RUNTIME_UNAVAILABLE)
    if (args.model_path is None) != (args.voices_path is None):
        _emit_error(
            "configuration",
            "Kokoro staged model and voices paths must be supplied together",
            error_stream,
        )
        return int(TTSExitCode.CONFIGURATION)

    try:
        KokoroSynthesizer(
            model_path=args.model_path,
            voices_path=args.voices_path,
        ).synthesize(
            text,
            voice=args.voice,
            output_path=args.output_file,
            speed=args.speed,
            lang=args.language,
        )
    except ModuleNotFoundError:
        _emit_error("runtime_unavailable", "Kokoro runtime package is unavailable", error_stream)
        return int(TTSExitCode.RUNTIME_UNAVAILABLE)
    except KokoroSynthesisError:
        _emit_error("runtime_unavailable", "Kokoro runtime assets are unavailable", error_stream)
        return int(TTSExitCode.RUNTIME_UNAVAILABLE)
    except Exception:
        _emit_error("synthesis_failed", "Kokoro synthesis failed", error_stream)
        return int(TTSExitCode.SYNTHESIS_FAILED)
    return int(TTSExitCode.SUCCESS)


if __name__ == "__main__":
    raise SystemExit(main())
