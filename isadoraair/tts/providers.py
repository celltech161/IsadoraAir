"""Dependency-free provider adapters for the shared TTS service."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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

    def wav_requirements_for(self, request: SynthesisRequest) -> WavRequirements: ...


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

    def wav_requirements_for(self, request: SynthesisRequest) -> WavRequirements:
        return self.wav_requirements

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

    def wav_requirements_for(self, request: SynthesisRequest) -> WavRequirements:
        return self.wav_requirements

    def synthesize(self, request: SynthesisRequest, output_path: Path) -> None:
        raise TTSVoiceUnavailable(
            f"Piper logical voice '{request.voice}' is not configured for this station"
        )


@dataclass(frozen=True, slots=True)
class PiperVoiceSpec:
    """Path-free station metadata resolved to the canonical Piper asset root."""

    model_id: str
    model_filename: str
    config_filename: str
    model_sha256: str
    config_sha256: str
    language: str
    sample_rate_hz: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_language(value: str) -> str:
    return value.replace("_", "-").lower()


class PiperTTSProvider:
    """Invoke canonical Piper with a closed, checksum-pinned model registry."""

    wav_requirements = DEFAULT_WAV_REQUIREMENTS

    def __init__(
        self,
        *,
        executable: str,
        asset_root: str | os.PathLike[str],
        voices: Sequence[PiperVoiceSpec],
    ) -> None:
        self.executable = executable
        self.asset_root = Path(asset_root)
        self.voices = {voice.model_id: voice for voice in voices}

    def _spec_for(self, request: SynthesisRequest) -> PiperVoiceSpec:
        try:
            return self.voices[request.voice]
        except KeyError as exc:
            raise TTSVoiceUnavailable(
                f"Piper model identity '{request.voice}' is not configured for this station"
            ) from exc

    def wav_requirements_for(self, request: SynthesisRequest) -> WavRequirements:
        return WavRequirements(sample_rate_hz=self._spec_for(request).sample_rate_hz)

    def _asset_paths(self, spec: PiperVoiceSpec) -> tuple[Path, Path]:
        for filename, suffix in (
            (spec.model_filename, ".onnx"),
            (spec.config_filename, ".onnx.json"),
        ):
            if Path(filename).name != filename or not filename.endswith(suffix):
                raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' has invalid asset metadata")
        if spec.config_filename != f"{spec.model_filename}.json":
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' has an invalid model/config pair")
        asset_root = self.asset_root.resolve()
        model_path = (asset_root / spec.model_filename).resolve()
        config_path = (asset_root / spec.config_filename).resolve()
        if model_path.parent != asset_root or config_path.parent != asset_root:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' has invalid asset metadata")
        return model_path, config_path

    def _validate_assets(self, request: SynthesisRequest, spec: PiperVoiceSpec) -> tuple[Path, Path]:
        model_path, config_path = self._asset_paths(spec)
        if not model_path.is_file() or not config_path.is_file():
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' assets are unavailable")
        try:
            model_sha256 = _sha256(model_path)
            config_sha256 = _sha256(config_path)
        except OSError as exc:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' assets are unavailable") from exc
        if model_sha256 != spec.model_sha256:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' model checksum does not match")
        if config_sha256 != spec.config_sha256:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' config checksum does not match")
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            sample_rate = int(config["audio"]["sample_rate"])
            config_language = str(config["language"]["code"])
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' config is invalid") from exc
        if sample_rate != spec.sample_rate_hz:
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' sample-rate metadata does not match")
        if _normalized_language(config_language) != _normalized_language(spec.language):
            raise TTSVoiceUnavailable(f"Piper model '{spec.model_id}' language metadata does not match")
        if _normalized_language(request.language) != _normalized_language(spec.language):
            raise TTSVoiceUnavailable(
                f"Piper model '{spec.model_id}' does not support language '{request.language}'"
            )
        return model_path, config_path

    def synthesize(self, request: SynthesisRequest, output_path: Path) -> None:
        if not _command_exists(self.executable):
            raise TTSRuntimeUnavailable("piper runtime executable is unavailable")
        spec = self._spec_for(request)
        model_path, config_path = self._validate_assets(request, spec)
        # Piper length_scale is duration, the inverse of the public speed
        # multiplier: speed 2.0 means length_scale 0.5.
        command = (
            self.executable,
            "--model",
            str(model_path),
            "--config",
            str(config_path),
            "--output-file",
            str(output_path),
            "--length-scale",
            format(1.0 / request.speed, ".12g"),
        )
        SubprocessTTSProvider(
            engine=TTSEngine.PIPER,
            command_factory=lambda _request, _output: command,
        ).synthesize(request, output_path)


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
