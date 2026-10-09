"""The isadoraair-render service: sole executor of every recipe render.

A SEPARATE service from isadoraair-validation -- its own systemd unit
(deploy/isadoraair-render.service; ``manage.py production_render_service``),
Unix socket, delegated cgroup subtree (``iportal-render``), admission pool,
limits, status file and Monitoring health. A render (two-pass ffmpeg over a
whole programme) is far heavier than a validation; keeping them apart means a
saturated or failed renderer can never consume validation capacity, delay
uploads or change the validator's authority, and vice versa.

For each connection it:

1. authenticates the peer (SO_PEERCRED: the service's own account only);
2. ADMITS it only while fewer than ``max_active + max_pending`` requests are
   admitted; otherwise answers ``busy`` from the accept loop without reading
   anything (descriptors sent with it are discarded by the kernel);
3. reads ONE request (render.protocol, version 1) and its descriptors within
   REQUEST_READ_TIMEOUT_SECONDS: exactly one ``render`` of a code-registered
   recipe, the source and pinned references as read-only descriptors. There
   is no command, executable, option, filter, path or URL in the protocol;
4. waits for a run slot (at most ``max_active`` renders at once) within the
   request's own deadline, cancelling if the client goes away;
5. renders with render.executor -- every ffmpeg step in a fresh leaf of its
   exclusive subtree via confinement.execute with the launcher's ``render``
   profile: cgroup limits, no_new_privs, Landlock (NO filesystem writes; reads
   only beneath /usr, /lib, /lib64, the loader cache/timezone and the CPU
   topology), seccomp (no
   clone3, no socket(2)), rlimits, a fixed minimal environment, the service's
   hard deadline -- and destroys every leaf when the step ends;
6. answers with one JSON line and, on success, ONE sealed memfd holding the
   FLAC; then closes. The client turns that descriptor into a validated,
   immutable rendition (render.rendition) -- the service never touches the
   database or the media store.

Start-up is gated on cleanup (every leftover ``run-*`` leaf destroyed and the
subtree verified empty) and on a confined toolchain probe; otherwise the
service exits without ever listening and systemd restarts it.

Deliberately NOT a job system: no persistence, no retries, no callbacks.
"""
from __future__ import annotations

import array
import json
import os
import select
import shutil
import signal
import socket
import stat
import sys
import threading
import time
from decimal import Decimal

from . import admission, confinement
from .render import executor, protocol
from .render.engine import ENGINE_VERSION, RenderError

ACTIVE_DEFAULT, ACTIVE_MIN, ACTIVE_MAX = 1, 1, 2
PENDING_DEFAULT, PENDING_MIN, PENDING_MAX = 2, 0, 4
STATUS_FILENAME = "status.json"
DEFAULT_SOCKET = "/run/isadoraair-render/render.sock"
REQUEST_READ_TIMEOUT_SECONDS = 5.0
LISTEN_BACKLOG = 16
TOOL_PATH = "/usr/bin:/bin"
# The environment every render tool gets -- nothing inherited from the service
# (no GST_*/LD_*/FFREPORT/AV_LOG_*/plugin paths, no proxy variables).
TOOL_ENVIRONMENT = {"PATH": TOOL_PATH, "LC_ALL": "C.UTF-8", "HOME": "/nonexistent", "TMPDIR": "/nonexistent"}
# The longest source this service renders, and the render deadline it allows.
MAX_SOURCE_SECONDS = Decimal(3600)


def parse_active(value) -> int:
    return admission.parse_bounded(value, label="Concurrent renders", minimum=ACTIVE_MIN, maximum=ACTIVE_MAX)


def parse_pending(value) -> int:
    return admission.parse_bounded(value, label="Pending render requests", minimum=PENDING_MIN,
                                   maximum=PENDING_MAX)


def render_limits() -> confinement.Limits:
    """Per-step limits (service configuration; settings.PRODUCTION_RENDER_LIMITS
    may override fields). One CPU, 1.5 GiB for the step's whole tree (decoder
    buffers plus the memfd it writes: a one-hour stereo WAV is ~660 MiB), 30
    CPU-minutes, 1 GiB largest file, 64 tasks."""
    from django.conf import settings
    defaults = dict(memory_bytes=2048 * confinement.MIB, cpu_seconds=1800, file_size_bytes=1024 * confinement.MIB,
                    open_files=256, group_memory_bytes=1536 * confinement.MIB, group_tasks=64, cpu_percent=100)
    defaults.update(getattr(settings, "PRODUCTION_RENDER_LIMITS", None) or {})
    return confinement.Limits(**defaults)


def socket_path() -> str:
    from django.conf import settings
    return getattr(settings, "PRODUCTION_RENDER_SOCKET", None) or DEFAULT_SOCKET


def configured_ffmpeg() -> str:
    """An ABSOLUTE ffmpeg, resolved once from the fixed tool path (never from a
    request or the caller's PATH); settings.PRODUCTION_RENDER_FFMPEG may pin it."""
    from django.conf import settings
    pinned = getattr(settings, "PRODUCTION_RENDER_FFMPEG", None)
    tool = pinned or shutil.which("ffmpeg", path=TOOL_PATH)
    if not tool or not os.path.isabs(tool) or not os.path.isfile(tool):
        raise NotReady(f"no usable ffmpeg ({tool!r})")
    return tool


def render_deadline_seconds(duration: Decimal, requested: float) -> float:
    return min(float(requested), 120.0 + 6.0 * float(duration), protocol.MAX_TIMEOUT_SECONDS)


def _log(message: str) -> None:
    sys.stderr.write(f"isadoraair-render: {message[:600]}\n")
    sys.stderr.flush()


class NotReady(Exception):
    """Start-up cleanup or the toolchain probe failed: never listen."""


class _Refused(Exception):
    def __init__(self, code, fds=()):
        super().__init__(code)
        self.code, self.fds = code, list(fds)


def _client_gone(conn) -> bool:
    try:
        readable, _, _ = select.select([conn], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


def _send(conn, answer: dict, fds=()) -> None:
    line = (json.dumps(answer, sort_keys=True) + "\n").encode()
    ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))] if fds else []
    conn.settimeout(10)
    conn.sendmsg([line], ancillary)


def _reject_busy(conn) -> None:
    try:
        conn.setblocking(False)
        conn.send((json.dumps(protocol.error("busy")) + "\n").encode())
    except OSError:
        pass
    conn.close()


def _read_request(conn):
    give_up = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS

    def remaining():
        left = give_up - time.monotonic()
        if left <= 0:
            raise socket.timeout("request not delivered in time")
        return left

    conn.settimeout(remaining())
    data, ancdata, flags, _ = conn.recvmsg(protocol.REQUEST_LIMIT_BYTES,
                                           socket.CMSG_SPACE(4 * (protocol.MAX_FDS + 1)), socket.MSG_CMSG_CLOEXEC)
    fds = []
    for level, kind, payload in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            usable = len(payload) - len(payload) % 4
            fds.extend(int.from_bytes(payload[i:i + 4], sys.byteorder, signed=True) for i in range(0, usable, 4))
    if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC) or len(fds) > protocol.MAX_FDS:
        raise _Refused("invalid_request", fds)
    buffer = bytearray(data)
    while not buffer.endswith(b"\n"):
        if len(buffer) >= protocol.REQUEST_LIMIT_BYTES:
            raise _Refused("invalid_request", fds)
        conn.settimeout(remaining())
        try:
            chunk = conn.recv(protocol.REQUEST_LIMIT_BYTES - len(buffer))
        except OSError:
            raise _Refused("invalid_request", fds) from None
        if not chunk:
            raise _Refused("invalid_request", fds)
        buffer.extend(chunk)
    try:
        request = json.loads(bytes(buffer).decode("utf-8"))
    except ValueError:
        raise _Refused("invalid_request", fds) from None
    return request, fds


class RenderService:
    def __init__(self, path: str, *, limits=None, max_active=None, max_pending=None, ffmpeg=None):
        from django.conf import settings
        if max_active is None:
            max_active = getattr(settings, "PRODUCTION_RENDER_MAX_ACTIVE", ACTIVE_DEFAULT)
        if max_pending is None:
            max_pending = getattr(settings, "PRODUCTION_RENDER_MAX_PENDING", PENDING_DEFAULT)
        self.max_active, self.max_pending = parse_active(max_active), parse_pending(max_pending)
        self.socket_path = path
        self.limits = limits or render_limits()
        self.ffmpeg = ffmpeg or configured_ffmpeg()
        self.toolchain = None
        self.slots = threading.BoundedSemaphore(self.max_active)
        self.admitted = threading.BoundedSemaphore(self.max_active + self.max_pending)
        self.stopping = threading.Event()
        self.listener = None
        self._state = threading.Lock()
        self.active = self.pending = 0
        self.last_failure = None

    # -- status (observer-only surface for Monitoring) ---------------------------------
    def status_path(self) -> str:
        return os.path.join(os.path.dirname(self.socket_path), STATUS_FILENAME)

    def write_status(self) -> None:
        with self._state:
            body = {"max_active": self.max_active, "max_pending": self.max_pending, "active": self.active,
                    "pending": self.pending, "last_failure": self.last_failure, "protocol": protocol.PROTOCOL_VERSION}
        path = self.status_path()
        temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, json.dumps(body).encode())
            finally:
                os.close(fd)
            os.replace(temporary, path)
        except OSError as exc:
            _log(f"cannot write status: {exc}")

    def _count(self, name, delta, *, failure=None):
        with self._state:
            setattr(self, name, getattr(self, name) + delta)
            if failure is not None:
                self.last_failure = {"code": failure, "at": int(time.time())}
        self.write_status()

    # -- lifecycle ------------------------------------------------------------------
    def run_step(self, argv, pass_fds, timeout_seconds, cancelled=None):
        return confinement.execute(argv, timeout_seconds=timeout_seconds, limits=self.limits, pass_fds=pass_fds,
                                   cancelled=cancelled, subtree=confinement.RENDER, profile="render",
                                   env=dict(TOOL_ENVIRONMENT))

    def prepare(self) -> None:
        """The readiness gate: leftover runs destroyed and the subtree empty,
        then a confined toolchain probe -- or NotReady, never listening."""
        try:
            reaped = confinement.reap_all(subtree=confinement.RENDER)
        except confinement.ConfinementUnavailable as exc:
            raise NotReady(f"start-up cleanup incomplete, not listening: {exc}") from exc
        if reaped:
            _log(f"start-up: destroyed {len(reaped)} leftover run(s)")
        result = self.run_step([self.ffmpeg, "-hide_banner", "-version"], (), 30.0)
        first = str(result.get("stdout", "")).splitlines()[:1]
        if result.get("status") != "ok" or not first:
            raise NotReady(f"the confined toolchain probe failed ({result.get('status')})")
        self.toolchain = f"{first[0].strip()[:120]}; render-engine {ENGINE_VERSION}; {protocol.EXECUTOR_MARKER}"

    def bind(self) -> None:
        directory = os.path.dirname(self.socket_path)
        info = os.stat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(f"{directory} must be owned by this account and closed to others (0700)")
        try:
            if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        old = os.umask(0o177)
        try:
            listener.bind(self.socket_path)
        finally:
            os.umask(old)
        os.chmod(self.socket_path, 0o600)
        listener.listen(LISTEN_BACKLOG)
        self.listener = listener
        self.write_status()

    def serve_forever(self) -> None:
        try:
            os.unlink(self.status_path())
        except OSError:
            pass
        self.prepare()
        self.bind()
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: self.stop())
        _log(f"listening on {self.socket_path} (at most {self.max_active} rendering, {self.max_pending} waiting)")
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                self.admit(conn)
        finally:
            self.shutdown()

    def admit(self, conn) -> None:
        try:
            if confinement.peer_uid(conn) != os.geteuid():
                conn.close()
                return
        except OSError:
            conn.close()
            return
        if not self.admitted.acquire(blocking=False):
            _reject_busy(conn)
            return
        try:
            threading.Thread(target=self._admitted, args=(conn,), daemon=True).start()
        except RuntimeError:
            self.admitted.release()
            _reject_busy(conn)

    def _admitted(self, conn) -> None:
        try:
            self.handle(conn)
        finally:
            self.admitted.release()

    def stop(self) -> None:
        self.stopping.set()
        if self.listener is not None:
            try:
                self.listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.listener.close()

    def shutdown(self) -> None:
        try:
            confinement.reap_all(subtree=confinement.RENDER)
        except confinement.ConfinementUnavailable as exc:
            _log(f"stop: cleanup incomplete ({exc}); systemd kills the rest with the unit")
        for path in (self.status_path(), self.socket_path):
            try:
                os.unlink(path)
            except OSError:
                pass

    # -- one request ----------------------------------------------------------------
    def handle(self, conn) -> None:
        fds, rendered = [], None
        try:
            try:
                request, fds = _read_request(conn)
                rendered = self.render(request, fds, conn)
                answer = {"v": protocol.PROTOCOL_VERSION, "status": "ok", "request_id": request["request_id"],
                          "recipe": {"key": rendered.recipe_key, "version": rendered.recipe_version},
                          "digest": rendered.digest, "provenance": rendered.provenance, "toolchain": self.toolchain,
                          "measured_input": rendered.measured_input,
                          "output": {"sha256": rendered.sha256, "bytes": rendered.size},
                          "executor": {"service": protocol.SERVICE_NAME, "protocol": protocol.PROTOCOL_VERSION}}
                out_fds = (rendered.fd,)
            except _Refused as exc:
                fds = fds or exc.fds
                answer, out_fds = protocol.error(exc.code), ()
            except RenderError as exc:
                code = exc.code if exc.code in protocol.CODES else "render_failed"
                _log(f"render refused/failed: {code} at {exc.stage or '-'}: {exc}")
                self._count("active", 0, failure=code)
                answer, out_fds = protocol.error(code, stage=exc.stage), ()
            except (OSError, ValueError) as exc:
                return _log(f"request failed: {exc}")
            try:
                _send(conn, answer, out_fds)
            except OSError:
                pass                        # the client is gone: the output is discarded with our descriptor
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if rendered is not None:
                os.close(rendered.fd)
            conn.close()

    def render(self, request, fds, conn) -> executor.Rendered:
        try:
            parsed = protocol.parse_request(request, max_bytes=_max_bytes(), max_seconds=MAX_SOURCE_SECONDS)
        except protocol.ProtocolError as exc:
            raise _Refused(exc.code, fds) from None
        if len(fds) != 1 + len(parsed["references"]):
            raise _Refused("invalid_request", fds)
        source_fd = fds[0]
        reference_fds = dict(zip(sorted(parsed["references"]), fds[1:]))
        deadline = time.monotonic() + render_deadline_seconds(parsed["source"]["duration_seconds"], parsed["timeout"])

        def cancelled():
            return _client_gone(conn) or self.stopping.is_set()

        self._count("pending", 1)
        try:
            while not self.slots.acquire(timeout=0.2):
                if cancelled():
                    raise RenderError("cancelled", "the client went away", retryable=True, stage="admission")
                if time.monotonic() >= deadline:
                    raise RenderError("busy", "no free render slot in time", retryable=True, stage="admission")
        finally:
            self._count("pending", -1)
        self._count("active", 1)
        try:
            return executor.execute(
                parsed, source_fd, reference_fds, deadline=deadline, max_seconds=MAX_SOURCE_SECONDS,
                ffmpeg=self.ffmpeg,
                run_step=lambda argv, pass_fds, timeout: self.run_step(argv, pass_fds, timeout, cancelled))
        finally:
            self.slots.release()
            self._count("active", -1)


def _max_bytes() -> int:
    from production import policy
    return policy.PLATFORM_MAX_BYTES


def status_snapshot(path: str | None = None) -> dict | None:
    """The running service's status file (Monitoring), or None."""
    status = os.path.join(os.path.dirname(path or socket_path()), STATUS_FILENAME)
    try:
        with open(status, "rb") as handle:
            data = json.loads(handle.read(4096))
        return {"max_active": parse_active(data["max_active"]), "max_pending": parse_pending(data["max_pending"]),
                "active": int(data["active"]), "pending": int(data["pending"]),
                "last_failure": data.get("last_failure")}
    except (OSError, ValueError, KeyError, TypeError):
        return None

