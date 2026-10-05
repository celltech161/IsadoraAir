"""OS-enforced resource confinement for production-media validation tools.

Every ffprobe / ffmpeg / GStreamer child that production.services.validation
runs on media bytes -- including bytes a browser just uploaded -- goes through
``run_confined``. Python-level checks (byte caps, duration policy) still apply
first; this module is the kernel-enforced second line that keeps a hostile or
pathological file from exhausting the web process or the station.

Mechanism (no privilege, no sudo, no systemd dependency): the tool is started
through ``confined_exec.py``, a stdlib-only launcher that applies setrlimit()
limits to itself and then exec()s the tool, so the limits are inherited by the
tool and every process it creates:

* memory     RLIMIT_AS (virtual address space), default 1 GiB. Measured
             minimum for the real validators on 10-minute WAV/Opus/FLAC input:
             ffprobe < 256 MiB, ffmpeg full decode and the GStreamer child
             384-512 MiB (resident 40-56 MB) -- 1 GiB is ~2x headroom;
* CPU        RLIMIT_CPU, default 180 s (SIGXCPU, then SIGKILL 5 s later). A
             10-minute Opus decode costs ~2.5 s, i.e. ~60 s at the 4-hour
             platform maximum;
* files      RLIMIT_FSIZE, default 16 MiB (the tools write nothing; this
             bounds any attempt) and RLIMIT_NOFILE 256; no core dumps;
* wall time  the caller's timeout, enforced here;
* tree       the child starts in its OWN session/process group; on timeout,
             interruption AND after every normal exit the whole group is
             SIGKILLed, so a descendant that outlives its parent is reaped too.

Process COUNT is bounded by the group kill and by single-threaded decoding,
not by RLIMIT_NPROC: that limit counts every process of the real UID, and the
service account also runs Gunicorn and the engine, so any useful value would
either be meaningless or break unrelated station services.

Fail closed: if the limits cannot be applied (or the launcher cannot run), the
tool is never started unconfined -- the result is ``confinement_unavailable``,
which validation reports as a retryable infrastructure error.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

LAUNCHER = str(Path(__file__).with_name("confined_exec.py"))
LAUNCHER_MARKER = "confined-exec:"
LAUNCHER_EXIT_LIMITS = 125
LAUNCHER_EXIT_EXEC = 126

MIB = 1024 * 1024
# Signals that mean "a resource limit (or the kill) stopped the tool".
LIMIT_SIGNALS = frozenset({signal.SIGXCPU, signal.SIGXFSZ, signal.SIGKILL, signal.SIGSEGV, signal.SIGABRT,
                           signal.SIGBUS})
# A tool that exits >0 with one of these on stderr hit a limit WE imposed
# (memory, or the file-size limit when the tool handles EFBIG itself instead of
# dying of SIGXFSZ): a station limit, never a verdict on the bytes (see
# validation._run_failure). The marker list is deliberately narrow.
RESOURCE_LIMIT_MARKERS = ("Cannot allocate memory", "Out of memory", "MemoryError", "std::bad_alloc",
                          "File too large")


@dataclass(frozen=True)
class Limits:
    memory_bytes: int = 1024 * MIB
    cpu_seconds: int = 180
    file_size_bytes: int = 16 * MIB
    open_files: int = 256

    def __post_init__(self):
        for name in ("memory_bytes", "cpu_seconds", "file_size_bytes", "open_files"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def configured_limits() -> Limits:
    """``settings.PRODUCTION_VALIDATION_LIMITS`` (a dict of Limits fields) may
    tighten or loosen the defaults; anything malformed fails loudly."""
    overrides = getattr(settings, "PRODUCTION_VALIDATION_LIMITS", None) or {}
    return Limits(**overrides)


def _launcher_argv(executable: str, args, limits: Limits) -> list[str]:
    return [
        sys.executable, "-I", "-S", LAUNCHER,
        "--memory", str(limits.memory_bytes), "--cpu", str(limits.cpu_seconds),
        "--fsize", str(limits.file_size_bytes), "--nofile", str(limits.open_files),
        "--", executable, *args,
    ]


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_confined(args, *, timeout_seconds, stop_event=None, limits: Limits | None = None) -> dict:
    """Run argv (no shell) under ``limits``. Returns the same dict shape as
    library.services.media_health.run_bounded_command, with ``status`` one of
    ok / failed / timeout / stopped / unavailable / infrastructure_error /
    confinement_unavailable / resource_limit, plus ``confined: True``."""
    from library.services.media_health import OUTPUT_LIMIT_BYTES, _BoundedCollector, _terminate_process_group

    limits = limits or configured_limits()
    executable = args[0] if os.path.isabs(args[0]) else shutil.which(args[0])
    if not executable or not os.path.exists(executable):
        return {"status": "unavailable", "returncode": None, "stdout": "", "stderr": "", "confined": True}
    if not os.path.isfile(LAUNCHER):
        return {"status": "confinement_unavailable", "returncode": None, "stdout": "",
                "stderr": "confinement launcher missing", "confined": True}
    argv = _launcher_argv(executable, list(args[1:]), limits)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        return {"status": "confinement_unavailable", "returncode": None, "stdout": "",
                "stderr": str(exc)[:200], "confined": True}
    pgid = process.pid                     # start_new_session: the child leads its own group
    stdout = _BoundedCollector(process.stdout, OUTPUT_LIMIT_BYTES)
    stderr = _BoundedCollector(process.stderr, OUTPUT_LIMIT_BYTES)
    stdout.start()
    stderr.start()
    status = "ok"
    deadline = started + timeout_seconds
    try:
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                status = "stopped"
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                _terminate_process_group(process)
                break
            try:
                process.wait(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            _terminate_process_group(process)
    finally:
        # Reap the WHOLE tree, always: a descendant that outlived the tool
        # (or survived the leader's termination) is killed here.
        _kill_group(pgid)
    out = stdout.finish()
    err = stderr.finish()
    code = process.returncode
    if status == "ok" and code != 0:
        status = "failed"
        if code in (LAUNCHER_EXIT_LIMITS, LAUNCHER_EXIT_EXEC) and err.lstrip().startswith(LAUNCHER_MARKER):
            status = "confinement_unavailable" if code == LAUNCHER_EXIT_LIMITS else "unavailable"
        elif code < 0 and -code in LIMIT_SIGNALS:
            status = "resource_limit"
        elif code > 0 and any(marker in err for marker in RESOURCE_LIMIT_MARKERS):
            status = "resource_limit"
    return {
        "status": status, "returncode": code, "stdout": out, "stderr": err,
        "stdout_truncated": stdout.truncated, "stderr_truncated": stderr.truncated,
        "duration_seconds": round(time.monotonic() - started, 3), "confined": True,
    }
