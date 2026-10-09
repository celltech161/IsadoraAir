"""The render subprocess boundary: the ONE place a render runs a tool.

* argv lists only -- never a shell, never a string command;
* the executable is resolved from a fixed PATH, not the caller's environment;
* a fixed minimal environment;
* kernel resource limits through ``prlimit`` (address space, CPU seconds,
  output file size, open files, no core dumps) -- if ``prlimit`` is missing
  the tool is NOT run (fail closed);
* its own process group, killed as a whole at the deadline;
* stdout/stderr captured with a hard byte cap;
* inherited descriptors only as explicitly passed (the media inputs).

Renders decode media that already passed the sandboxed validation service
(ffprobe + full decode + GStreamer). Running the render itself inside that
service's cgroup/Landlock boundary is a C2 decision (see docs/SPOKEN_CONTENT.md).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

TOOL_PATH = "/usr/bin:/bin"
OUTPUT_LIMIT_BYTES = 256 * 1024
ADDRESS_SPACE_BYTES = 2 * 1024 ** 3
OUTPUT_FILE_LIMIT_BYTES = 1024 ** 3
OPEN_FILES = 256


@dataclass(frozen=True)
class ToolResult:
    status: str                 # ok | timeout | unavailable | failed_to_start
    returncode: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.returncode == 0


def resolve_tool(name: str) -> str | None:
    return shutil.which(name, path=TOOL_PATH)


class _Collector(threading.Thread):
    def __init__(self, stream, limit):
        super().__init__(daemon=True)
        self.stream, self.limit, self.chunks, self.size, self.truncated = stream, limit, [], 0, False

    def run(self):
        for chunk in iter(lambda: self.stream.read(65536), b""):
            room = self.limit - self.size
            if room > 0:
                self.chunks.append(chunk[:room])
                self.size += min(len(chunk), room)
            if len(chunk) > room:
                self.truncated = True

    def value(self) -> bytes:
        return b"".join(self.chunks)


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(argv: list[str], *, timeout_seconds: float, pass_fds=(), cwd=None) -> ToolResult:
    if not argv or not all(isinstance(arg, str) and "\x00" not in arg for arg in argv):
        raise TypeError("argv must be a non-empty list of NUL-free strings")
    tool = resolve_tool(argv[0])
    prlimit = resolve_tool("prlimit")
    if tool is None or prlimit is None:
        return ToolResult("unavailable", None, b"", b"")
    cpu = int(timeout_seconds) + 5
    wrapped = [prlimit, f"--as={ADDRESS_SPACE_BYTES}", f"--cpu={cpu}", f"--fsize={OUTPUT_FILE_LIMIT_BYTES}",
               f"--nofile={OPEN_FILES}", "--core=0", "--", tool, *argv[1:]]
    try:
        process = subprocess.Popen(
            wrapped, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            cwd=cwd, env={"PATH": TOOL_PATH, "LC_ALL": "C"}, start_new_session=True,
            pass_fds=tuple(pass_fds), close_fds=True,
        )
    except OSError:
        return ToolResult("failed_to_start", None, b"", b"")
    out, err = _Collector(process.stdout, OUTPUT_LIMIT_BYTES), _Collector(process.stderr, OUTPUT_LIMIT_BYTES)
    out.start()
    err.start()
    deadline = time.monotonic() + timeout_seconds
    status = "ok"
    while process.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            status = "timeout"
            _kill_group(process)
            break
        try:
            process.wait(timeout=min(0.2, remaining))
        except subprocess.TimeoutExpired:
            pass
    process.wait()
    out.join(5)
    err.join(5)
    return ToolResult(status, process.returncode, out.value(), err.value(), out.truncated or err.truncated)
