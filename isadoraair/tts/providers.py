"""Dependency-free provider adapters for the shared TTS service."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from isadoraair.tts.errors import (
    ERROR_TYPES_BY_CATEGORY,
    TTSRuntimeUnavailable,
    TTSSynthesisError,
    TTSTimeout,
    TTSVoiceUnavailable,
)
from isadoraair.tts.request import SynthesisRequest, TTSEngine
from isadoraair.tts.validation import DEFAULT_WAV_REQUIREMENTS, WavRequirements


MAX_CAPTURED_ERROR_BYTES = 4096
PROVIDER_ERROR_PREFIX = "ISADORAAIR_TTS_ERROR:"
_TERMINATE_GRACE_SECONDS = 1.0
CommandFactory = Callable[[SynthesisRequest, Path], Sequence[str]]


class TTSProvider(Protocol):
    wav_requirements: WavRequirements

    def synthesize(self, request: SynthesisRequest, output_path: Path) -> None: ...


class _BoundedCapture:
    def __init__(self, limit: int = MAX_CAPTURED_ERROR_BYTES) -> None:
        self.limit = limit
        self.total_bytes = 0
        self._chunks: list[bytes] = []
        self._stored_bytes = 0

    def drain(self, stream) -> None:
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    return
                self.total_bytes += len(chunk)
                remaining = self.limit - self._stored_bytes
                if remaining > 0:
                    kept = chunk[:remaining]
                    self._chunks.append(kept)
                    self._stored_bytes += len(kept)
        finally:
            stream.close()

    def bytes(self) -> bytes:
        return b"".join(self._chunks)


def _provider_environment(*, module_root: Path | None = None) -> dict[str, str]:
    """Pass only dispatcher-owned process/runtime settings to an engine worker."""

    allowed = (
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TMPDIR",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONUNBUFFERED"] = "1"
    if module_root is not None:
        # Provider source remains in one authoritative Git checkout. Do not
        # inherit PYTHONPATH from callers or require a second source install in
        # each engine runtime; the dispatcher owns this exact import root.
        environment["PYTHONPATH"] = str(module_root.resolve(strict=True))
    return environment


def _command_exists(executable: str) -> bool:
    path = Path(executable)
    if path.is_absolute():
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable) is not None


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _structured_provider_error(captured: bytes):
    text = captured.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith(PROVIDER_ERROR_PREFIX):
            continue
        try:
            payload = json.loads(line.removeprefix(PROVIDER_ERROR_PREFIX))
        except (json.JSONDecodeError, TypeError):
            return None
        category = payload.get("category")
        message = payload.get("message")
        error_type = ERROR_TYPES_BY_CATEGORY.get(category)
        if error_type is None or not isinstance(message, str) or not message:
            return None
        safe_message = " ".join(message.split())[:512]
        if not safe_message:
            return None
        return error_type(safe_message)
    return None


class SubprocessTTSProvider:
    """Invoke one engine runtime without importing it into Django."""

    def __init__(
        self,
        *,
        engine: TTSEngine,
        command_factory: CommandFactory,
        cwd: Path | None = None,
        module_root: Path | None = None,
        wav_requirements: WavRequirements = DEFAULT_WAV_REQUIREMENTS,
    ) -> None:
        self.engine = engine
        self.command_factory = command_factory
        self.cwd = cwd
        self.module_root = module_root
        self.wav_requirements = wav_requirements

    def synthesize(self, request: SynthesisRequest, output_path: Path) -> None:
        command = [str(argument) for argument in self.command_factory(request, output_path)]
        if not command:
            raise TTSRuntimeUnavailable(f"{self.engine.value} runtime command is not configured")
        if not _command_exists(command[0]):
            raise TTSRuntimeUnavailable(f"{self.engine.value} runtime executable is unavailable")

        capture = _BoundedCapture()
        with tempfile.TemporaryFile() as input_file:
            input_file.write(request.text.encode("utf-8"))
            input_file.seek(0)
            try:
                process = subprocess.Popen(
                    command,
                    stdin=input_file,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    cwd=self.cwd,
                    env=_provider_environment(module_root=self.module_root),
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                reason = exc.strerror or exc.__class__.__name__
                raise TTSRuntimeUnavailable(
                    f"{self.engine.value} runtime could not start: {reason}"
                ) from exc

            assert process.stderr is not None
            drain_thread = threading.Thread(target=capture.drain, args=(process.stderr,), daemon=True)
            drain_thread.start()
            try:
                return_code = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(process)
                drain_thread.join(timeout=_TERMINATE_GRACE_SECONDS)
                raise TTSTimeout(
                    f"{self.engine.value} synthesis exceeded {request.timeout_seconds:g} seconds"
                ) from exc
            drain_thread.join(timeout=_TERMINATE_GRACE_SECONDS)

        if return_code != 0:
            structured_error = _structured_provider_error(capture.bytes())
            if structured_error is not None:
                raise structured_error
            raise TTSSynthesisError(
                f"{self.engine.value} provider exited with status {return_code}; "
                f"diagnostic output suppressed ({capture.total_bytes} bytes)"
            )


class UnconfiguredPiperProvider:
    """Valid optional state until Foundation C supplies station voice mappings."""

    wav_requirements = DEFAULT_WAV_REQUIREMENTS

    def synthesize(self, request: SynthesisRequest, output_path: Path) -> None:
        raise TTSVoiceUnavailable(
            f"Piper logical voice '{request.voice}' is not configured for this station"
        )


def kokoro_provider_command(
    runtime_python: str,
    provider_module: str,
) -> CommandFactory:
    def build(request: SynthesisRequest, output_path: Path) -> Sequence[str]:
        return (
            runtime_python,
            "-m",
            provider_module,
            "--engine",
            TTSEngine.KOKORO.value,
            "--voice",
            request.voice,
            "--output-file",
            str(output_path),
            "--speed",
            str(request.speed),
            "--language",
            request.language,
        )

    return build


ProviderMap = Mapping[TTSEngine, TTSProvider]
