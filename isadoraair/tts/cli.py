"""Stable external IsadoraAir TTS command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr
from typing import TextIO

from isadoraair.tts.errors import TTSError, TTSExitCode
from isadoraair.tts.request import DEFAULT_TIMEOUT_SECONDS, SynthesisRequest, TTSEngine
from isadoraair.tts.service import TTSService, build_default_service


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isadoraair-tts")
    parser.add_argument("--engine", required=True, choices=[engine.value for engine in TTSEngine])
    parser.add_argument("--voice", "--model", dest="voice", required=True, help="Logical station voice ID")
    parser.add_argument(
        "--output-file",
        "--output_file",
        dest="output_file",
        required=True,
        help="Final WAV destination",
    )
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--language", "--lang", dest="language", default="en-us")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    service: TTSService | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    error_stream = stderr or sys.stderr
    with redirect_stderr(error_stream):
        args = build_argument_parser().parse_args(argv)
    try:
        request = SynthesisRequest(
            text=input_stream.read(),
            engine=args.engine,
            voice=args.voice,
            output_path=args.output_file,
            speed=args.speed,
            language=args.language,
            timeout_seconds=args.timeout,
        )
        (service or build_default_service()).synthesize(request)
    except TTSError as exc:
        error_stream.write(f"isadoraair-tts: {exc.category}: {exc}\n")
        return int(exc.exit_code)
    return int(TTSExitCode.SUCCESS)
