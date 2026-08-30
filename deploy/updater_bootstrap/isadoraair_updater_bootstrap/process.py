"""Fixed-argv, bounded-output, timeout/reap subprocess primitives.

An INDEPENDENT implementation of the same safety properties as
deploy/updater_runtime/isadoraair_updater/process.py (the worker's own
copy) -- deliberately not imported (Correction 1). This copy also adds
run_and_track() for launching a long-running child (the selected
worker) without waiting for it to exit -- the worker's own process.py
never needed that, since the worker only ever shells out to short-lived
commands (git, systemctl); the supervisor's defining job is launching
one long-running child and tracking it, so this is a genuinely
different, not merely duplicated, requirement."""
from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading


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


def _kill_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            pass


class CommandRunner:
    """No generic shell entry point exists; callers provide literal argv."""

    def run(self, argv: list[str] | tuple[str, ...], *, timeout: float,
            env: dict[str, str] | None = None, output_limit: int = DEFAULT_OUTPUT_LIMIT) -> ProcessResult:
        if not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
            raise TypeError("argv must be a non-empty sequence of NUL-free strings")
        controlled_env = dict(BASE_ENV)
        if env:
            controlled_env.update(env)
        process = subprocess.Popen(
            list(argv), env=controlled_env,
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


class TrackedChild:
    """A long-running child process this supervisor has launched and is
    tracking -- distinct from CommandRunner.run()'s wait-to-completion
    model, since the worker is meant to keep running. Owns the child's
    own process group so the supervisor can terminate the whole group
    (including anything the worker itself spawns) in one signal."""

    def __init__(self, process: subprocess.Popen):
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        """None while still running; the exit code once it has exited."""
        return self._process.poll()

    def terminate(self, *, grace_seconds: float = 3.0) -> None:
        if self._process.poll() is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._process.wait(timeout=grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        _kill_group(self._process)
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def launch_tracked(argv: list[str] | tuple[str, ...], *, cwd, env: dict[str, str] | None = None) -> TrackedChild:
    """Starts argv as a new process-group leader, own stdio pipes not
    read here (the readiness protocol, protocol.py, is how the
    supervisor learns the child's state -- stdout/stderr are inherited
    to the parent's own log destination, never buffered unbounded in
    this process, and never a source of a decode this supervisor would
    have to trust)."""
    if not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
        raise TypeError("argv must be a non-empty sequence of NUL-free strings")
    controlled_env = dict(BASE_ENV)
    if env:
        controlled_env.update(env)
    process = subprocess.Popen(
        list(argv), cwd=str(cwd), env=controlled_env,
        stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    return TrackedChild(process)
