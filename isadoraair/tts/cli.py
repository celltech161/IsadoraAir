"""Stable external IsadoraAir TTS command-line interface."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import redirect_stderr
from typing import TextIO

from isadoraair.tts.errors import TTSError, TTSExitCode
from isadoraair.tts.request import DEFAULT_TIMEOUT_SECONDS


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isadoraair-tts", allow_abbrev=False)
    parser.add_argument(
        "--voice",
        required=True,
        help="StationTTSVoice logical identifier",
    )
    parser.add_argument(
        "--output-file",
        required=True,
        help="Final WAV destination",
    )
    parser.add_argument("--speed", type=float)
    parser.add_argument("--language")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Timeout in seconds")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
    station_service=None,
) -> int:
    input_stream = stdin or sys.stdin
    error_stream = stderr or sys.stderr
    with redirect_stderr(error_stream):
        args = build_argument_parser().parse_args(argv)
    try:
        text = input_stream.read()
        if station_service is None:
            from isadoraair.tts.station import StationTTSService

            station_service = StationTTSService()
        station_service.synthesize(
            text,
            voice=args.voice,
            output_path=args.output_file,
            speed=args.speed,
            language=args.language,
            timeout_seconds=args.timeout,
        )
    except TTSError as exc:
        error_stream.write(f"isadoraair-tts: {exc.category}: {exc}\n")
        return int(exc.exit_code)
    return int(TTSExitCode.SUCCESS)
