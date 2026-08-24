"""Fixed-argv, bounded-output, timeout/reap subprocess primitives."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import pwd
import signal
import subprocess
import threading
import time


BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/nonexistent",
}
DEFAULT_OUTPUT_LIMIT = 1024 * 1024


@dataclasses.dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.output_truncated


class _Collector:
    def __init__(self, stream, limit: int):
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self):
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    break
                room = self.limit - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if len(chunk) > room:
                    self.truncated = True
        finally:
            self.stream.close()


def _kill_group(process: subprocess.Popen):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class CommandRunner:
    """No generic shell entry point exists; callers provide literal argv."""

    def __init__(self, *, runuser_path: str = "/usr/sbin/runuser"):
        self.runuser_path = runuser_path

    def run(self, argv: list[str] | tuple[str, ...], *, timeout: float,
            cwd: Path | None = None, env: dict[str, str] | None = None,
            output_limit: int = DEFAULT_OUTPUT_LIMIT) -> ProcessResult:
        if not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise TypeError("argv must be a non-empty sequence of NUL-free strings")
        controlled_env = dict(BASE_ENV)
        if env:
            controlled_env.update(env)
        process = subprocess.Popen(
            list(argv), cwd=str(cwd) if cwd else None, env=controlled_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
        stdout = _Collector(process.stdout, output_limit)
        stderr = _Collector(process.stderr, output_limit)
        stdout.thread.start()
        stderr.thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(process)
            process.wait(timeout=2)
        stdout.thread.join(timeout=2)
        stderr.thread.join(timeout=2)
        return ProcessResult(
            tuple(argv), process.returncode, bytes(stdout.data), bytes(stderr.data),
            timed_out, stdout.truncated or stderr.truncated,
        )

    def run_as_user(self, user: str, argv: list[str] | tuple[str, ...], **kwargs) -> ProcessResult:
        return self.run(self.argv_as_user(user, argv), **kwargs)

    def argv_as_user(self, user: str, argv: list[str] | tuple[str, ...]) -> list[str]:
        """Root drops to ISA_USER; unprivileged same-user test runs stay direct."""
        if os.geteuid() == pwd.getpwnam(user).pw_uid:
            return list(argv)
        return [self.runuser_path, "--user", user, "--", *argv]

    def run_to_file(self, argv: list[str] | tuple[str, ...], destination: Path, *,
                    timeout: float, max_bytes: int, cwd: Path | None = None,
                    env: dict[str, str] | None = None) -> ProcessResult:
        """Stream stdout to an internally-selected file without buffering it."""
        if not argv or max_bytes <= 0:
            raise ValueError("argv and a positive max_bytes are required")
        controlled_env = dict(BASE_ENV)
        if env:
            controlled_env.update(env)
        process = subprocess.Popen(
            list(argv), cwd=str(cwd) if cwd else None, env=controlled_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
        stderr = _Collector(process.stderr, DEFAULT_OUTPUT_LIMIT)
        stderr.thread.start()
        overflow = threading.Event()

        def _copy():
            written = 0
            with open(destination, "xb", buffering=0) as output:
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        overflow.set()
                        break
                    output.write(chunk)
            process.stdout.close()

        copy_thread = threading.Thread(target=_copy, daemon=True)
        copy_thread.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        while process.poll() is None:
            if overflow.is_set():
                _kill_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(process)
                break
            time.sleep(0.01)
        process.wait(timeout=2)
        copy_thread.join(timeout=2)
        stderr.thread.join(timeout=2)
        return ProcessResult(
            tuple(argv), process.returncode, b"", bytes(stderr.data), timed_out,
            overflow.is_set() or stderr.truncated,
        )
