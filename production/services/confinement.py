"""OS-enforced confinement for production-media validation tools.

Every ffprobe / ffmpeg / GStreamer child that production.services.validation
runs on media bytes -- including bytes a browser just uploaded -- goes through
``run_confined``. Python-level checks (byte caps, duration policy) still apply
first; this module is the kernel-enforced boundary that keeps a hostile or
pathological file from exhausting, outliving or escaping into the station.

The boundary (2.22B corrective; no privilege, no sudo)
------------------------------------------------------
**A per-run cgroup v2 leaf** in the web service's own *delegated* cgroup
subtree (``deploy/isadoraair-gunicorn.service``: ``Delegate=`` +
``DelegateSubgroup=web``, so Gunicorn runs in ``<unit>/web`` and this module
creates ``<unit>/iportal-validation/run-<pid>-<id>``). The leaf carries the
AGGREGATE limits for the whole tool tree:

* ``memory.max``   (+ ``memory.swap.max`` 0, ``memory.oom.group`` 1): all
  processes together; an OOM kills the entire tree;
* ``pids.max``     processes AND threads together (fork/thread pressure);
* ``cpu.max``      at most ``cpu_percent`` of one CPU, when the cpu
  controller is delegated; and an aggregate CPU-time budget (``cpu.stat``
  ``usage_usec`` >= ``cpu_seconds``) enforced by the parent's monitor;
* a hard wall-clock timeout.

The trusted launcher (``confined_exec.py``) moves ITSELF into the leaf before
anything untrusted runs, then sets no_new_privs, a Landlock policy (read and
execute only: no filesystem writes anywhere, so ``cgroup.procs`` cannot be
written; ptrace/signals confined to the sandbox), a seccomp filter (``clone3``
-> ENOSYS, so ``CLONE_INTO_CGROUP`` is unavailable) and per-process rlimits
(RLIMIT_AS/CPU/FSIZE/NOFILE/CORE), and exec()s the tool.

Cgroup membership is inherited and is not changed by setsid(), setpgid(),
double forking or a parent exiting; with Landlock and seccomp the tool tree
has no way left to move a task out of the leaf. After EVERY run -- success,
failure, timeout, interruption -- the parent writes ``cgroup.kill`` (the
kernel SIGKILLs every task in the leaf, race-free against concurrent forks),
waits until the leaf reports ``populated 0`` and removes it. When this
function returns, no task of the validation scope is alive.

Fail closed: no delegated subtree, a missing controller, no ``cgroup.kill``,
Landlock or seccomp unavailable, a launcher that cannot verify any step, or a
leaf that cannot be emptied -> ``confinement_unavailable`` (a retryable
infrastructure error). A tool is never run outside the boundary.
"""
from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from . import confined_exec

LAUNCHER = str(Path(__file__).with_name("confined_exec.py"))
LAUNCHER_MARKER = confined_exec.MARKER
LAUNCHER_EXIT_LIMITS = confined_exec.EXIT_LIMITS
LAUNCHER_EXIT_EXEC = confined_exec.EXIT_EXEC
CGROUP_MOUNT = confined_exec.CGROUP_MOUNT
VALIDATION_CGROUP_NAME = "iportal-validation"
REQUIRED_CONTROLLERS = ("memory", "pids")
CLEANUP_TIMEOUT_SECONDS = 10.0
_LEAF_RE = re.compile(r"^run-(\d+)-[0-9a-f]{12}$")

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


class ConfinementUnavailable(Exception):
    """The kernel boundary could not be established or verified."""


@dataclass(frozen=True)
class Limits:
    """Per-run limits. Defaults are calibrated on the real validators with
    10-minute WAV / FLAC / Opus input (see docs/IPORTAL.md): ffmpeg, ffprobe
    and the GStreamer child peak at <= 140 MiB charged memory and <= 30 tasks
    for the whole tree, ~2.5 s CPU."""

    memory_bytes: int = 1024 * MIB          # per process: RLIMIT_AS (virtual)
    cpu_seconds: int = 180                  # per process RLIMIT_CPU AND the tree's aggregate budget
    file_size_bytes: int = 16 * MIB         # per process: RLIMIT_FSIZE (the tools write nothing)
    open_files: int = 256                   # per process: RLIMIT_NOFILE
    group_memory_bytes: int = 512 * MIB     # whole tree: cgroup memory.max (no swap)
    group_tasks: int = 64                   # whole tree: cgroup pids.max (processes + threads)
    cpu_percent: int = 100                  # whole tree: cgroup cpu.max (100 = one CPU)

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


def configured_limits() -> Limits:
    """``settings.PRODUCTION_VALIDATION_LIMITS`` (a dict of Limits fields) may
    tighten or loosen the defaults; anything malformed fails loudly."""
    overrides = getattr(settings, "PRODUCTION_VALIDATION_LIMITS", None) or {}
    return Limits(**overrides)


# -- the delegated subtree ----------------------------------------------------

def validation_root() -> Path:
    """Where per-run leaves are created: ``settings.PRODUCTION_VALIDATION_CGROUP``
    (an absolute cgroup-filesystem path) or, by default, ``iportal-validation``
    beside this process's own cgroup -- i.e. inside the unit cgroup that
    systemd delegated to the service (``DelegateSubgroup=web``)."""
    configured = getattr(settings, "PRODUCTION_VALIDATION_CGROUP", None)
    if configured:
        root = Path(configured)
    else:
        try:
            own = confined_exec.own_cgroup()
        except (OSError, confined_exec._Refused) as exc:
            raise ConfinementUnavailable(f"cannot read own cgroup: {exc}") from exc
        if own in ("", "/"):
            raise ConfinementUnavailable("this process is in the root cgroup; no delegated subtree")
        root = Path(CGROUP_MOUNT + own).parent / VALIDATION_CGROUP_NAME
    if not root.is_absolute() or os.path.realpath(root) != str(root) \
            or not str(root).startswith(CGROUP_MOUNT + "/"):
        raise ConfinementUnavailable(f"validation cgroup {str(root)!r} is not a canonical path under {CGROUP_MOUNT}")
    return root


def _read(path: Path) -> str:
    return path.read_text(encoding="ascii").strip()


def _write(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CLOEXEC)
    try:
        os.write(fd, value.encode("ascii"))
    finally:
        os.close(fd)


def _owned_and_writable(path: Path) -> bool:
    try:
        return path.stat().st_uid == os.geteuid() and os.access(path, os.W_OK)
    except OSError:
        return False


def _enable_controllers(cgroup: Path, wanted) -> set[str]:
    available = set(_read(cgroup / "cgroup.controllers").split())
    missing = [name for name in REQUIRED_CONTROLLERS if name not in available]
    if missing:
        raise ConfinementUnavailable(f"{cgroup}: controller(s) {', '.join(missing)} not delegated")
    enabled = set(_read(cgroup / "cgroup.subtree_control").split())
    want = [name for name in wanted if name in available and name not in enabled]
    if want:
        _write(cgroup / "cgroup.subtree_control", " ".join(f"+{name}" for name in want))
        enabled = set(_read(cgroup / "cgroup.subtree_control").split())
    if not set(REQUIRED_CONTROLLERS) <= enabled:
        raise ConfinementUnavailable(f"{cgroup}: cannot enable {', '.join(REQUIRED_CONTROLLERS)}")
    return enabled


def establish_root() -> tuple[Path, set[str]]:
    """Verify (and, first time, create) the validation subtree. Returns the
    root and the controllers its leaves get. Raises ConfinementUnavailable."""
    root = validation_root()
    delegated = root.parent
    try:
        if not (Path(CGROUP_MOUNT) / "cgroup.controllers").is_file():
            raise ConfinementUnavailable(f"no cgroup v2 hierarchy at {CGROUP_MOUNT}")
        for path in (delegated, delegated / "cgroup.procs", delegated / "cgroup.subtree_control"):
            if not _owned_and_writable(path):
                raise ConfinementUnavailable(f"{path} is not delegated to this service account")
        _enable_controllers(delegated, (*REQUIRED_CONTROLLERS, "cpu"))
        try:
            root.mkdir(mode=0o755)
        except FileExistsError:
            pass
        if not _owned_and_writable(root):
            raise ConfinementUnavailable(f"{root} is not owned by this service account")
        enabled = _enable_controllers(root, (*REQUIRED_CONTROLLERS, "cpu"))
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot establish {root}: {exc.strerror or exc}") from exc
    return root, enabled


_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def _prepare_leaf(limits: Limits) -> Path:
    root, enabled = establish_root()
    _sweep_abandoned(root)
    leaf = root / f"run-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        leaf.mkdir(mode=0o755)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot create {leaf}: {exc.strerror}") from exc
    with _ACTIVE_LOCK:
        _ACTIVE.add(leaf.name)
    try:
        if not (leaf / "cgroup.kill").exists():
            raise ConfinementUnavailable("kernel lacks cgroup.kill")
        _write(leaf / "memory.max", str(limits.group_memory_bytes))
        if (leaf / "memory.swap.max").exists():
            _write(leaf / "memory.swap.max", "0")
        _write(leaf / "memory.oom.group", "1")
        _write(leaf / "pids.max", str(limits.group_tasks))
        if "cpu" in enabled:
            _write(leaf / "cpu.max", f"{limits.cpu_percent * 1000} 100000")
        if (_read(leaf / "memory.max"), _read(leaf / "pids.max"), _read(leaf / "memory.oom.group")) != \
                (str(limits.group_memory_bytes), str(limits.group_tasks), "1"):
            raise ConfinementUnavailable("cgroup limits did not apply")
    except (OSError, ConfinementUnavailable) as exc:
        _destroy(leaf)
        if isinstance(exc, ConfinementUnavailable):
            raise
        raise ConfinementUnavailable(f"cannot limit {leaf}: {exc.strerror or exc}") from exc
    return leaf


def _populated(leaf: Path) -> bool:
    for line in _read(leaf / "cgroup.events").splitlines():
        key, _, value = line.partition(" ")
        if key == "populated":
            return value != "0"
    return True


def _counter(path: Path, key: str) -> int:
    try:
        for line in _read(path).splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value)
    except (OSError, ValueError):
        pass
    return 0


def _destroy(leaf: Path) -> dict:
    """Kill every task in ``leaf``, wait for it to empty, record what the
    kernel counted, remove it. ``cleaned`` is False only if a task survived."""
    stats = {"path": str(leaf), "cleaned": False}
    try:
        _write(leaf / "cgroup.kill", "1")
        deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        while _populated(leaf):
            if time.monotonic() >= deadline:
                return stats
            time.sleep(0.005)
        stats.update(
            oom_kill=_counter(leaf / "memory.events", "oom_kill"),
            memory_max_events=_counter(leaf / "memory.events", "max"),
            pids_max_events=_counter(leaf / "pids.events", "max"),
            cpu_usec=_counter(leaf / "cpu.stat", "usage_usec"),
        )
        for name in ("memory.peak", "pids.peak"):
            try:
                stats[name.replace(".", "_")] = int(_read(leaf / name))
            except (OSError, ValueError):
                pass
        for attempt in range(50):
            try:
                leaf.rmdir()
                break
            except OSError as exc:
                if exc.errno != errno.EBUSY or attempt == 49:
                    raise
                time.sleep(0.01)
        stats["cleaned"] = True
    except FileNotFoundError:
        stats["cleaned"] = True
    except OSError:
        pass
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(leaf.name)
    return stats


def _sweep_abandoned(root: Path) -> None:
    """Destroy leaves left by a worker that died mid-validation (its pid is
    gone). Leaves of live workers -- including this one's other threads -- are
    never touched."""
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        match = _LEAF_RE.match(entry.name)
        if not match or not entry.is_dir():
            continue
        pid = int(match.group(1))
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
            continue                     # owner alive
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        _destroy(entry)


# -- running a tool -----------------------------------------------------------

def _launcher_argv(leaf: Path, executable: str, args, limits: Limits) -> list[str]:
    return [
        sys.executable, "-I", "-S", LAUNCHER, "--cgroup", str(leaf),
        "--memory", str(limits.memory_bytes), "--cpu", str(limits.cpu_seconds),
        "--fsize", str(limits.file_size_bytes), "--nofile", str(limits.open_files),
        "--", executable, *args,
    ]


def _unavailable(reason: str) -> dict:
    return {"status": "confinement_unavailable", "returncode": None, "stdout": "",
            "stderr": reason[:300], "confined": True}


def run_confined(args, *, timeout_seconds, stop_event=None, limits: Limits | None = None) -> dict:
    """Run argv (no shell) inside a fresh kernel boundary. Returns the same
    dict shape as library.services.media_health.run_bounded_command, with
    ``status`` one of ok / failed / timeout / stopped / unavailable /
    confinement_unavailable / resource_limit, plus ``confined: True`` and the
    kernel's accounting for the run under ``cgroup``."""
    from library.services.media_health import OUTPUT_LIMIT_BYTES, _BoundedCollector

    limits = limits or configured_limits()
    executable = args[0] if os.path.isabs(args[0]) else shutil.which(args[0])
    if not executable or not os.path.exists(executable):
        return {"status": "unavailable", "returncode": None, "stdout": "", "stderr": "", "confined": True}
    if not os.path.isfile(LAUNCHER):
        return _unavailable("confinement launcher missing")
    try:
        leaf = _prepare_leaf(limits)
    except ConfinementUnavailable as exc:
        return _unavailable(str(exc))
    argv = _launcher_argv(leaf, executable, list(args[1:]), limits)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, start_new_session=True, close_fds=True,
        )
    except OSError as exc:
        _destroy(leaf)
        return _unavailable(str(exc))
    stdout = _BoundedCollector(process.stdout, OUTPUT_LIMIT_BYTES)
    stderr = _BoundedCollector(process.stderr, OUTPUT_LIMIT_BYTES)
    stdout.start()
    stderr.start()
    status = "ok"
    deadline = started + timeout_seconds
    cpu_budget_usec = limits.cpu_seconds * 1_000_000
    try:
        while process.poll() is None:
            if stop_event is not None and stop_event.is_set():
                status = "stopped"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            if _counter(leaf / "cpu.stat", "usage_usec") >= cpu_budget_usec:
                status = "resource_limit"
                break
            try:
                process.wait(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
    finally:
        # The WHOLE tree, always -- whatever session, process group or parent
        # its members have by now: they are all still in the leaf.
        kernel = _destroy(leaf)
        try:
            process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    out = stdout.finish()
    err = stderr.finish()
    code = process.returncode
    if not kernel["cleaned"]:
        result = _unavailable(f"validation cgroup {leaf} could not be emptied")
        result.update(returncode=code, cgroup=kernel)
        return result
    if status == "ok" and code != 0:
        status = "failed"
        if code in (LAUNCHER_EXIT_LIMITS, LAUNCHER_EXIT_EXEC) and err.lstrip().startswith(LAUNCHER_MARKER):
            status = "confinement_unavailable" if code == LAUNCHER_EXIT_LIMITS else "unavailable"
        elif kernel.get("oom_kill") or kernel.get("pids_max_events"):
            status = "resource_limit"
        elif code < 0 and -code in LIMIT_SIGNALS:
            status = "resource_limit"
        elif code > 0 and any(marker in err for marker in RESOURCE_LIMIT_MARKERS):
            status = "resource_limit"
    return {
        "status": status, "returncode": code, "stdout": out, "stderr": err,
        "stdout_truncated": stdout.truncated, "stderr_truncated": stderr.truncated,
        "duration_seconds": round(time.monotonic() - started, 3), "confined": True, "cgroup": kernel,
    }
