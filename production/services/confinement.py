"""OS-enforced confinement for production-media validation tools.

Every ffprobe / ffmpeg / GStreamer child that production.services.validation
runs on media bytes -- including bytes a browser just uploaded -- runs inside a
kernel boundary whose LIFETIME is owned by a dedicated systemd service, never
by the web process that asked for it.

Who owns what (2.22B lifecycle correction)
------------------------------------------
* ``isadoraair-validation.service`` (deploy/isadoraair-validation.service;
  production.services.validation_service) owns the delegated cgroup subtree
  ``<unit>/iportal-validation/`` exclusively. For each run it creates a fresh
  leaf ``run-<128-bit random id>``, starts the tool there with ``execute()``,
  enforces the hard wall deadline and the CPU-time budget itself, and destroys
  the tree when the run ends -- or as soon as the requesting client goes away.
* The web process is only a CLIENT: ``run_confined()`` sends one fixed-shape
  request over the service's Unix socket (the media as an open descriptor) and
  waits. If the web worker dies, the service sees the connection close and
  kills the run at once; if the client merely stalls, the deadline still fires.
* If the service itself dies, systemd (``KillMode=control-group``) kills every
  process left in its cgroup -- the validation leaves included -- and restarts
  it; the new service kills and removes every leftover ``run-*`` leaf before it
  accepts work. Nothing waits for a future upload to clean up.

The boundary of one run
-----------------------
The leaf carries the AGGREGATE limits for the whole tool tree (all REQUIRED --
any that cannot be configured and read back fails the run closed):

* ``memory.max``, ``memory.swap.max`` 0 and ``memory.oom.group`` 1: all
  processes together, no swap; an OOM kills the entire tree;
* ``pids.max``: processes AND threads together (fork/thread pressure);
* ``cpu.max``: at most ``cpu_percent`` of one CPU (kernel-enforced rate);
* the service's monitor: the hard wall deadline (the per-tool Phase-A timeout,
  capped by the service) and an aggregate CPU-time budget (``cpu.stat``,
  read STRICTLY: accounting that cannot be read or parsed, or that goes
  backwards, ends the run as ``confinement_unavailable`` -- never as zero).

The trusted launcher (``confined_exec.py``) moves ITSELF into the leaf before
anything untrusted runs, then sets no_new_privs, a Landlock policy (read and
execute only: no filesystem writes anywhere, so ``cgroup.procs`` cannot be
written; ptrace/signals confined to the sandbox), a seccomp filter (``clone3``
-> ENOSYS, so ``CLONE_INTO_CGROUP`` is unavailable) and per-process rlimits
(RLIMIT_AS/CPU/FSIZE/NOFILE/CORE), and exec()s the tool. Cgroup membership
survives setsid(), setpgid(), double forking and a parent exiting, so
``cgroup.kill`` -- written after EVERY run -- kills the whole tree.

Fail closed: no validation service, no delegated subtree, a missing cpu /
memory / pids controller, no ``memory.swap.max`` or ``cgroup.kill``, Landlock
or seccomp unavailable, a launcher step that cannot be verified, CPU accounting
that cannot be read, or a leaf that cannot be emptied ->
``confinement_unavailable`` (a retryable infrastructure error). A tool is never
run outside the boundary. A service at capacity answers ``busy`` (also
retryable) without reading the request.
"""
from __future__ import annotations

import array
import errno
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
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
REQUIRED_CONTROLLERS = ("cpu", "memory", "pids")
CLEANUP_TIMEOUT_SECONDS = 10.0
LEAF_PREFIX = "run-"
_LEAF_RE = re.compile(r"^run-[0-9a-f]{32}$")
DEFAULT_SOCKET = "/run/isadoraair-validation/validator.sock"

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
STATUSES = frozenset({"ok", "failed", "timeout", "stopped", "unavailable", "infrastructure_error",
                      "confinement_unavailable", "resource_limit", "busy"})
# cpu.stat is a handful of short lines; anything larger is not what we expect.
CPU_STAT_LIMIT_BYTES = 4096
_UINT_RE = re.compile(r"^[0-9]{1,20}$")


class ConfinementUnavailable(Exception):
    """The kernel boundary could not be established or verified."""


@dataclass(frozen=True)
class Limits:
    """Per-run limits (service configuration, never part of a request).
    Defaults are calibrated on the real validators with 10-minute WAV / FLAC /
    Opus input (docs/IPORTAL.md): the whole tree peaks at <= 22 MiB charged
    memory, <= 18 tasks and ~3 s CPU."""

    memory_bytes: int = 1024 * MIB          # per process: RLIMIT_AS (virtual)
    cpu_seconds: int = 180                  # per process RLIMIT_CPU AND the tree's aggregate CPU-time budget
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


# =============================================================================
# The executor -- runs ONLY inside the validation service (or a test standing
# in for it), in that service's delegated cgroup subtree.
# =============================================================================

def validation_root() -> Path:
    """Where per-run leaves are created: ``settings.PRODUCTION_VALIDATION_CGROUP``
    (an absolute cgroup-filesystem path) or, by default, ``iportal-validation``
    beside this process's own cgroup -- i.e. inside the unit cgroup systemd
    delegated to the validation service (``DelegateSubgroup=supervisor``)."""
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


def _enable_controllers(cgroup: Path) -> None:
    available = set(_read(cgroup / "cgroup.controllers").split())
    missing = [name for name in REQUIRED_CONTROLLERS if name not in available]
    if missing:
        raise ConfinementUnavailable(f"{cgroup}: controller(s) {', '.join(missing)} not delegated")
    enabled = set(_read(cgroup / "cgroup.subtree_control").split())
    want = [name for name in REQUIRED_CONTROLLERS if name not in enabled]
    if want:
        _write(cgroup / "cgroup.subtree_control", " ".join(f"+{name}" for name in want))
        enabled = set(_read(cgroup / "cgroup.subtree_control").split())
    if not set(REQUIRED_CONTROLLERS) <= enabled:
        raise ConfinementUnavailable(f"{cgroup}: cannot enable {', '.join(REQUIRED_CONTROLLERS)}")


def establish_root() -> Path:
    """Verify (and, first time, create) the validation subtree with every
    REQUIRED controller enabled for its leaves. Raises ConfinementUnavailable."""
    root = validation_root()
    delegated = root.parent
    try:
        if not (Path(CGROUP_MOUNT) / "cgroup.controllers").is_file():
            raise ConfinementUnavailable(f"no cgroup v2 hierarchy at {CGROUP_MOUNT}")
        for path in (delegated, delegated / "cgroup.procs", delegated / "cgroup.subtree_control"):
            if not _owned_and_writable(path):
                raise ConfinementUnavailable(f"{path} is not delegated to this service account")
        _enable_controllers(delegated)
        try:
            root.mkdir(mode=0o755)
        except FileExistsError:
            pass
        if not _owned_and_writable(root):
            raise ConfinementUnavailable(f"{root} is not owned by this service account")
        _enable_controllers(root)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot establish {root}: {exc.strerror or exc}") from exc
    return root


def _prepare_leaf(limits: Limits) -> Path:
    root = establish_root()
    leaf = root / f"{LEAF_PREFIX}{secrets.token_hex(16)}"
    try:
        leaf.mkdir(mode=0o755)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot create {leaf}: {exc.strerror}") from exc
    wanted = {
        "memory.max": str(limits.group_memory_bytes),
        "memory.swap.max": "0",
        "memory.oom.group": "1",
        "pids.max": str(limits.group_tasks),
        "cpu.max": f"{limits.cpu_percent * 1000} 100000",
    }
    try:
        for name in ("cgroup.kill", *wanted):
            if not (leaf / name).exists():
                raise ConfinementUnavailable(f"kernel facility {name} is unavailable")
        for name, value in wanted.items():
            _write(leaf / name, value)
        applied = {name: _read(leaf / name) for name in wanted}
        if applied != wanted:
            raise ConfinementUnavailable(f"cgroup limits did not apply: {applied}")
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


def _counter(path: Path, key: str) -> int | None:
    """Post-mortem accounting for the result's ``cgroup`` record only (None if
    unknown -- never a made-up zero). Enforcement uses cpu_usage_usec()."""
    try:
        for line in _read(path).splitlines():
            name, _, value = line.partition(" ")
            if name == key:
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def _read_cpu_stat(leaf: Path) -> bytes:
    """The raw ``cpu.stat`` of ``leaf``, read to end of file."""
    fd = os.open(leaf / "cpu.stat", os.O_RDONLY | os.O_CLOEXEC)
    try:
        data = b""
        while True:
            chunk = os.read(fd, CPU_STAT_LIMIT_BYTES + 1 - len(data))
            if not chunk:
                return data
            data += chunk
            if len(data) > CPU_STAT_LIMIT_BYTES:
                raise OSError(errno.EFBIG, "cpu.stat is larger than expected")
    finally:
        os.close(fd)


def cpu_usage_usec(leaf: Path) -> int:
    """The tree's cumulative CPU time (``usage_usec`` in the leaf's cpu.stat),
    read STRICTLY for enforcement. A missing or unreadable file, an I/O error,
    an incomplete read (no final newline), or a ``usage_usec`` field that is
    absent, repeated, signed or not a plain decimal raises
    ConfinementUnavailable: a broken counter is never mistaken for zero use."""
    try:
        raw = _read_cpu_stat(leaf)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot read {leaf.name}/cpu.stat: {exc.strerror or exc}") from exc
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise ConfinementUnavailable(f"{leaf.name}/cpu.stat is not ASCII") from None
    if not text.endswith("\n"):
        raise ConfinementUnavailable(f"{leaf.name}/cpu.stat read is incomplete")
    values = [value for name, _, value in (line.partition(" ") for line in text[:-1].split("\n"))
              if name == "usage_usec"]
    if len(values) != 1 or not _UINT_RE.match(values[0]):
        raise ConfinementUnavailable(f"{leaf.name}/cpu.stat has no usable usage_usec: {values!r}"[:200])
    return int(values[0])


def _remove(leaf: Path) -> None:
    for attempt in range(50):
        try:
            leaf.rmdir()
            return
        except OSError as exc:
            if exc.errno != errno.EBUSY or attempt == 49:
                raise
            time.sleep(0.01)


def _destroy(leaf: Path) -> dict:
    """Kill every task in ``leaf``, wait for it to empty, record what the
    kernel counted, remove it. ``cleaned`` is True only once the leaf is gone:
    a kill that fails, a task that survives or a removal that fails is False."""
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
        _remove(leaf)
        stats["cleaned"] = True
    except FileNotFoundError:
        stats["cleaned"] = not os.path.lexists(leaf)       # gone already -- not merely lacking cgroup.kill
    except OSError:
        pass
    return stats


def _children(root: Path) -> list[Path]:
    return sorted(root.iterdir())


def reap_all(root: Path | None = None) -> list[dict]:
    """Kill and remove EVERY run leaf under the validation root. Only the
    validation service calls this -- at start-up (before it accepts work) and
    when it stops: the subtree is exclusively its own, so any ``run-*`` leaf
    there is a run of a previous instance (or of this one, shutting down).
    No PID is consulted: nothing else ever lives in that subtree.

    All or nothing: raises ConfinementUnavailable unless the subtree could be
    scanned, every leaf was killed, emptied and removed, and the subtree is
    then verifiably unpopulated. A failure is never "nothing to clean"."""
    root = root or establish_root()
    try:
        entries = _children(root)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot scan {root}: {exc.strerror or exc}") from exc
    reaped = []
    for entry in entries:
        if not _LEAF_RE.match(entry.name):
            continue
        try:
            is_leaf = stat.S_ISDIR(entry.lstat().st_mode)
        except FileNotFoundError:
            continue                                   # removed meanwhile: nothing left to reap
        except OSError as exc:
            raise ConfinementUnavailable(f"cannot inspect {entry}: {exc.strerror or exc}") from exc
        if is_leaf:
            reaped.append(_destroy(entry))
    failed = [os.path.basename(r["path"]) for r in reaped if not r["cleaned"]]
    if failed:
        raise ConfinementUnavailable(f"could not destroy {len(failed)} leftover run(s): {', '.join(failed)}")
    try:
        populated = _populated(root)
    except OSError as exc:
        raise ConfinementUnavailable(f"cannot verify {root} is empty: {exc.strerror or exc}") from exc
    if populated:
        raise ConfinementUnavailable(f"{root} still holds live tasks after reaping")
    return reaped


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


def execute(args, *, timeout_seconds, stop_event=None, limits: Limits | None = None, pass_fds=(),
            cancelled=None) -> dict:
    """Run argv (no shell) inside a fresh leaf, in THIS process's delegated
    subtree. The hard deadline, the CPU-time budget and the final destruction
    of the tree are enforced here -- the validation service is the only
    production caller. ``cancelled()`` (e.g. "the client went away") stops the
    run early. Returns the run_bounded_command-shaped result plus the kernel's
    accounting under ``cgroup``."""
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
            shell=False, start_new_session=True, close_fds=True, pass_fds=tuple(pass_fds),
        )
    except OSError as exc:
        _destroy(leaf)
        return _unavailable(str(exc))
    stdout = _BoundedCollector(process.stdout, OUTPUT_LIMIT_BYTES)
    stderr = _BoundedCollector(process.stderr, OUTPUT_LIMIT_BYTES)
    stdout.start()
    stderr.start()
    status, failure = "ok", None
    deadline = started + timeout_seconds
    cpu_budget_usec = limits.cpu_seconds * 1_000_000
    cpu_used_usec = 0
    try:
        while process.poll() is None:
            if (stop_event is not None and stop_event.is_set()) or (cancelled is not None and cancelled()):
                status = "stopped"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
            try:
                used = cpu_usage_usec(leaf)
            except ConfinementUnavailable as exc:
                status, failure = "confinement_unavailable", f"CPU accounting failed: {exc}"
                break
            if used < cpu_used_usec:
                status, failure = "confinement_unavailable", \
                    f"CPU accounting went backwards ({cpu_used_usec} -> {used} usec)"
                break
            cpu_used_usec = used
            if used >= cpu_budget_usec:
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
    if not kernel["cleaned"] or failure:
        result = _unavailable(failure or f"validation cgroup {leaf} could not be emptied")
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


# =============================================================================
# The client -- what the web process (and every other validation caller) uses.
# =============================================================================

REQUEST_LIMIT_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 5.0
# How long beyond the requested deadline the client waits for the service's
# answer before giving up (the service's own deadline has fired long before).
RESPONSE_GRACE_SECONDS = 30.0
_PEERCRED = struct.Struct("3i")


def socket_path() -> str:
    return getattr(settings, "PRODUCTION_VALIDATION_SOCKET", None) or DEFAULT_SOCKET


def peer_uid(sock) -> int:
    _pid, uid, _gid = _PEERCRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size))
    return uid


def run_confined(args, *, timeout_seconds, stop_event=None, media=None) -> dict:
    """Have the validation service run one validator command. ``media`` -- the
    path that appears in ``args`` -- is opened here (regular file, no symlink)
    and sent as a descriptor; the request carries the command with a
    placeholder in its place. Same result shape as ``execute``. Any failure to
    reach a healthy service is ``confinement_unavailable`` (retryable); a
    service at capacity is ``busy`` (retryable); a command the service does not
    recognise is ``unavailable``."""
    from . import validator_commands

    argv = [str(item) for item in args]
    media_fd = None
    if media is not None:
        if argv.count(str(media)) != 1:
            return {"status": "unavailable", "returncode": None, "stdout": "",
                    "stderr": "media must appear exactly once in the command", "confined": True}
        argv[argv.index(str(media))] = validator_commands.MEDIA
        try:
            media_fd = os.open(media, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            return {"status": "infrastructure_error", "returncode": None, "stdout": "",
                    "stderr": f"cannot open media: {exc.strerror}", "confined": True}
        if not stat.S_ISREG(os.fstat(media_fd).st_mode):
            os.close(media_fd)
            return {"status": "infrastructure_error", "returncode": None, "stdout": "",
                    "stderr": "media is not a regular file", "confined": True}
    request = (json.dumps({"v": 1, "argv": argv, "timeout": float(timeout_seconds)}) + "\n").encode()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    try:
        try:
            sock.settimeout(CONNECT_TIMEOUT_SECONDS)
            try:
                sock.connect(socket_path())
            except BlockingIOError:         # EAGAIN: the service's accept queue is full
                return {"status": "busy", "returncode": None, "stdout": "",
                        "stderr": "the validation service's accept queue is full; retry later", "confined": True}
            if peer_uid(sock) != os.geteuid():
                return _unavailable("the validation socket belongs to another account")
            fds = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [media_fd]))] if media_fd is not None else []
            sock.sendmsg([request], fds)    # the connection then stays open: closing it cancels the run
        except OSError as exc:
            # A service at capacity answers ``busy`` and closes at once -- possibly
            # before this request was even sent. Its answer may be waiting.
            return _queued_answer(sock) or _unavailable(f"validation service unavailable: {exc.strerror or exc}")
        finally:
            if media_fd is not None:
                os.close(media_fd)
        sock.setblocking(False)
        give_up = time.monotonic() + float(timeout_seconds) + RESPONSE_GRACE_SECONDS
        reply = bytearray()
        limit = 4 * 1024 * 1024
        while True:
            if stop_event is not None and stop_event.is_set():
                # Closing the connection is the cancellation: the service kills the run.
                return {"status": "stopped", "returncode": None, "stdout": "", "stderr": "", "confined": True}
            if time.monotonic() >= give_up:
                return _unavailable("the validation service did not answer in time")
            readable, _, _ = select.select([sock], [], [], 0.2)
            if not readable:
                continue
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError as exc:
                return _unavailable(f"validation service connection failed: {exc.strerror or exc}")
            if not chunk:
                break
            reply.extend(chunk)
            if len(reply) > limit:
                return _unavailable("oversized reply from the validation service")
            if reply.endswith(b"\n"):
                break                       # the service answers with exactly one line
        return _answer(reply) or _unavailable("the validation service ended the run without an answer")
    finally:
        sock.close()


def _answer(reply) -> dict | None:
    try:
        result = json.loads(bytes(reply).decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(result, dict) or result.get("status") not in STATUSES:
        return None
    result["confined"] = True
    return result


def _queued_answer(sock) -> dict | None:
    """An answer already waiting on ``sock`` (never blocks)."""
    try:
        reply = sock.recv(65536, socket.MSG_DONTWAIT)
    except OSError:
        return None
    return _answer(reply) if reply.endswith(b"\n") else None
