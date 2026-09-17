"""Bounded, atomic filesystem transport for playback-engine commands.

This module is deliberately pure stdlib so Django processes, management
commands, the GStreamer engine, and standalone in-tree ingest scripts can all
use the same writer without importing one another's runtime dependencies.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import time
import uuid
from pathlib import Path


ENGINE_COMMAND_QUEUE_DIR = Path("/run/isadoraair/engine_cmd.d")
ENGINE_COMMAND_LOCK_PATH = Path("/run/isadoraair/engine_cmd.lock")
ENGINE_COMMAND_MAX_DEPTH = 256
ENGINE_COMMAND_MAX_PAYLOAD_BYTES = 4096
ENGINE_COMMAND_BATCH_SIZE = 32

_COMMITTED_RE = re.compile(
    r"^cmd-(?P<sequence>[0-9]{20})-(?P<pid>[0-9]+)-(?P<nonce>[0-9a-f]{32})\.json$"
)
_TEMP_RE = re.compile(
    r"^\.cmd-(?P<sequence>[0-9]{20})-(?P<pid>[0-9]+)-(?P<nonce>[0-9a-f]{32})\.tmp$"
)


class EngineCommandError(RuntimeError):
    """Base class for observable command-transport failures."""


class EngineCommandValidationError(EngineCommandError):
    """The caller supplied a payload that cannot enter the queue."""


class EngineCommandQueueFull(EngineCommandError):
    """The bounded queue is at capacity; no existing command was evicted."""


class EngineCommandTransportError(EngineCommandError):
    """The runtime queue could not be accessed or published to safely."""


def _serialize_payload(payload, max_payload_bytes):
    if not isinstance(payload, dict):
        raise EngineCommandValidationError("engine command payload must be a JSON object")
    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        raise EngineCommandValidationError(
            "engine command payload requires a nonblank string 'command'"
        )
    try:
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EngineCommandValidationError(
            f"engine command payload is not JSON serializable: {exc}"
        ) from exc
    if len(encoded) > max_payload_bytes:
        raise EngineCommandValidationError(
            f"engine command payload is {len(encoded)} bytes; maximum is "
            f"{max_payload_bytes}"
        )
    return encoded


def _regular_file(entry):
    try:
        return stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def _scan_committed(queue_dir):
    entries = []
    for entry in queue_dir.iterdir():
        match = _COMMITTED_RE.fullmatch(entry.name)
        if match is not None and _regular_file(entry):
            entries.append((int(match.group("sequence")), entry.name, entry))
    entries.sort(key=lambda item: (item[0], item[1]))
    return entries


def _clean_stale_temps(queue_dir):
    """Remove only regular artifacts with our exact private temp syntax."""

    for entry in queue_dir.iterdir():
        if _TEMP_RE.fullmatch(entry.name) is not None and _regular_file(entry):
            try:
                entry.unlink()
            except FileNotFoundError:
                pass


class _QueueLock:
    def __init__(self, queue_dir, lock_path):
        self.queue_dir = Path(queue_dir)
        self.lock_path = Path(lock_path)
        self.fd = None

    def __enter__(self):
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            self.fd = os.open(self.lock_path, flags, 0o600)
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):
                raise OSError("engine command lock path is not a regular file")
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            raise EngineCommandTransportError(
                f"cannot lock engine command queue: {exc}"
            ) from exc

    def __exit__(self, exc_type, exc, traceback):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def enqueue_engine_command(
    payload,
    *,
    queue_dir=None,
    lock_path=None,
    max_depth=None,
    max_payload_bytes=None,
):
    """Atomically append one command and return its committed path.

    Sequence allocation, capacity checking, stale-temp cleanup, and publish
    all happen under one process-shared flock. Queue-full never evicts an
    older entry.
    """

    queue_dir = Path(queue_dir or ENGINE_COMMAND_QUEUE_DIR)
    lock_path = Path(lock_path or ENGINE_COMMAND_LOCK_PATH)
    max_depth = ENGINE_COMMAND_MAX_DEPTH if max_depth is None else max_depth
    max_payload_bytes = (
        ENGINE_COMMAND_MAX_PAYLOAD_BYTES
        if max_payload_bytes is None
        else max_payload_bytes
    )
    if not isinstance(max_depth, int) or max_depth < 1:
        raise EngineCommandValidationError("engine command maximum depth must be positive")
    if not isinstance(max_payload_bytes, int) or max_payload_bytes < 1:
        raise EngineCommandValidationError(
            "engine command maximum payload size must be positive"
        )
    encoded = _serialize_payload(payload, max_payload_bytes)

    temp_path = None
    try:
        with _QueueLock(queue_dir, lock_path):
            _clean_stale_temps(queue_dir)
            committed = _scan_committed(queue_dir)
            if len(committed) >= max_depth:
                raise EngineCommandQueueFull(
                    f"engine command queue is full ({len(committed)}/{max_depth})"
                )

            prior_sequence = committed[-1][0] if committed else 0
            sequence = max(time.time_ns(), prior_sequence + 1)
            nonce = uuid.uuid4().hex
            stem = f"cmd-{sequence:020d}-{os.getpid()}-{nonce}"
            temp_path = queue_dir / f".{stem}.tmp"
            committed_path = queue_dir / f"{stem}.json"

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp_path, flags, 0o600)
            try:
                handle = os.fdopen(fd, "wb")
                fd = None
                with handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if fd is not None:
                    os.close(fd)
            os.rename(temp_path, committed_path)
            temp_path = None
            return committed_path
    except EngineCommandError:
        raise
    except OSError as exc:
        raise EngineCommandTransportError(
            f"cannot publish engine command: {exc}"
        ) from exc
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass


def list_committed_engine_commands(
    *, queue_dir=None, lock_path=None, limit=None
):
    """Return a stable FIFO snapshot of known committed queue entries."""

    queue_dir = Path(queue_dir or ENGINE_COMMAND_QUEUE_DIR)
    lock_path = Path(lock_path or ENGINE_COMMAND_LOCK_PATH)
    if limit is not None and (not isinstance(limit, int) or limit < 0):
        raise EngineCommandValidationError("engine command list limit must be nonnegative")
    try:
        with _QueueLock(queue_dir, lock_path):
            _clean_stale_temps(queue_dir)
            paths = [item[2] for item in _scan_committed(queue_dir)]
            return paths if limit is None else paths[:limit]
    except EngineCommandError:
        raise
    except OSError as exc:
        raise EngineCommandTransportError(
            f"cannot scan engine command queue: {exc}"
        ) from exc


def consume_engine_command_file(path, *, max_payload_bytes=None):
    """Read and unlink one known committed entry before returning its payload.

    The unlink-before-dispatch contract is intentionally at-most-once. A
    malformed or oversized internally named file is also removed so it cannot
    poison every later poll. Arbitrary names are rejected without deletion.
    """

    path = Path(path)
    if _COMMITTED_RE.fullmatch(path.name) is None:
        raise EngineCommandValidationError(
            f"unrecognized engine command filename: {path.name!r}"
        )
    max_payload_bytes = (
        ENGINE_COMMAND_MAX_PAYLOAD_BYTES
        if max_payload_bytes is None
        else max_payload_bytes
    )
    raw = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise EngineCommandValidationError(
                    f"engine command entry is not a regular file: {path.name}"
                )
            handle = os.fdopen(fd, "rb")
            fd = None
            with handle:
                raw = handle.read(max_payload_bytes + 1)
        finally:
            if fd is not None:
                os.close(fd)
    except FileNotFoundError:
        return None
    except EngineCommandError:
        raise
    except OSError as exc:
        raise EngineCommandTransportError(
            f"cannot read engine command {path.name}: {exc}"
        ) from exc
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise EngineCommandTransportError(
                f"cannot remove consumed engine command {path.name}: {exc}"
            ) from exc

    if raw is None:
        return None
    if len(raw) > max_payload_bytes:
        raise EngineCommandValidationError(
            f"engine command file {path.name} exceeds {max_payload_bytes} bytes"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EngineCommandValidationError(
            f"engine command file {path.name} is not valid UTF-8 JSON: {exc}"
        ) from exc
    _serialize_payload(payload, max_payload_bytes)
    return payload
