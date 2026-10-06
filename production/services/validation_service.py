"""The isadoraair-validation service: sole owner of every validation run.

Run by systemd as ``isadoraair-validation.service`` (deploy/; entry point
``manage.py production_validation_service``) with ``Delegate=cpu memory pids``,
``DelegateSubgroup=supervisor`` and ``KillMode=control-group``. It listens on a
Unix socket in its 0700 runtime directory and, for each connection:

1. authenticates the peer (SO_PEERCRED: the service's own account only);
2. ADMITS it only while fewer than ``max_active + max_pending`` requests are
   admitted; otherwise it answers ``busy`` at once and closes, from the accept
   loop, without reading anything (whatever was sent, descriptors included, is
   discarded by the kernel with the connection). Only an admitted connection
   gets a handler thread, so threads, connections, requests and media
   descriptors are all bounded by the admission limit;
3. reads ONE request -- ``{"v": 1, "argv": [...], "timeout": seconds}`` plus,
   for a media command, exactly one regular-file descriptor (SCM_RIGHTS) --
   within REQUEST_READ_TIMEOUT_SECONDS in total, however slowly it arrives;
4. accepts it only if ``argv`` is exactly one of the validator commands in
   production.services.validator_commands, rebuilt from THIS service's tool
   configuration (no executable, option, path, environment or limit can be
   chosen by a client); the media placeholder becomes ``/proc/self/fd/<n>``;
5. runs it with confinement.execute() in a fresh leaf of its exclusive
   subtree -- at most ``max_active`` at a time; an admitted request waits for a
   run slot within its own deadline -- under the service's own limits and a
   hard deadline of min(requested, the command's maximum), enforced here, not
   by the client;
6. cancels the run as soon as the client connection closes (a web worker that
   dies or is restarted takes its validation with it), and destroys the tree
   when the run ends in every case;
7. answers with the result as one JSON object and closes the connection.

At start-up -- before accepting work -- and on stop it kills and removes every
``run-*`` leaf in its subtree (confinement.reap_all). If the service itself is
killed, systemd kills everything left in its cgroup and restarts it.

Deliberately NOT a job system: no persistence, no retries, no callbacks -- one
synchronous run per connection, a small fixed admission bound.
"""
from __future__ import annotations

import json
import math
import os
import select
import signal
import socket
import stat
import sys
import threading
import time

from . import confinement, validator_commands

# Admission (settings PRODUCTION_VALIDATION_MAX_ACTIVE / _MAX_PENDING override).
# Conservative for an on-air host: each run may use one CPU and 512 MiB, so at
# most two run at once; four more may wait for a run slot; any further
# connection is answered ``busy`` immediately.
MAX_ACTIVE_RUNS = 2
MAX_PENDING_REQUESTS = 4
# Total time an admitted client has to deliver its whole request.
REQUEST_READ_TIMEOUT_SECONDS = 5.0
LISTEN_BACKLOG = 16
MAX_ARGV_ITEMS = 40
MAX_ARG_LENGTH = 4096
BUSY = {"status": "busy", "returncode": None, "stdout": "",
        "stderr": "the validation service is at capacity; retry later"}
_BUSY_LINE = (json.dumps(BUSY) + "\n").encode()


def configured_tools() -> validator_commands.Tools:
    from . import validation
    return validator_commands.Tools(
        ffprobe=validation.FFPROBE, ffmpeg=validation.FFMPEG,
        interpreter=tuple(validation.GSTREAMER_PROBE_INTERPRETER),
        probe_script=validation.GSTREAMER_PROBE_SCRIPT,
        probe_child_timeout=validation.GSTREAMER_CHILD_TIMEOUT_SECONDS,
        capability_seconds=validation.CAPABILITY_TIMEOUT_SECONDS,
        probe_seconds=validation.FFPROBE_TIMEOUT_SECONDS,
        decode_seconds=validation.FFMPEG_TIMEOUT_SECONDS,
        engine_seconds=validation.GSTREAMER_HARD_TIMEOUT_SECONDS,
    )


def _log(message: str) -> None:
    sys.stderr.write(f"isadoraair-validation: {message}\n")
    sys.stderr.flush()


class _Refused(Exception):
    pass


def _client_gone(conn) -> bool:
    """True once the client has closed (or broken) its connection. A client
    never sends anything after its request, so readable == gone."""
    try:
        readable, _, _ = select.select([conn], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)              # EOF (or a protocol violation): either way, stop the run


def _reject_busy(conn) -> None:
    """Answer ``busy`` and close, never blocking and reading nothing: the kernel
    discards whatever the client sent -- descriptors included -- on close."""
    try:
        conn.setblocking(False)
        conn.send(_BUSY_LINE)
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
    data, ancdata, flags, _addr = conn.recvmsg(confinement.REQUEST_LIMIT_BYTES, socket.CMSG_SPACE(4 * 2),
                                               socket.MSG_CMSG_CLOEXEC)
    fds = []
    for level, kind, payload in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            usable = len(payload) - len(payload) % 4
            fds.extend(int.from_bytes(payload[i:i + 4], sys.byteorder, signed=True) for i in range(0, usable, 4))
    if flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC):
        raise _Refused("oversized request", fds)
    buffer = bytearray(data)
    while not buffer.endswith(b"\n"):
        if len(buffer) >= confinement.REQUEST_LIMIT_BYTES:
            raise _Refused("oversized request", fds)
        try:
            conn.settimeout(remaining())
            chunk = conn.recv(confinement.REQUEST_LIMIT_BYTES - len(buffer))
        except OSError:
            for fd in fds:
                os.close(fd)
            raise
        if not chunk:
            raise _Refused("incomplete request", fds)
        buffer.extend(chunk)
    try:
        request = json.loads(bytes(buffer).decode("utf-8"))
    except ValueError:
        raise _Refused("malformed request", fds) from None
    return request, fds


def configured_admission() -> tuple[int, int]:
    from django.conf import settings
    return (int(getattr(settings, "PRODUCTION_VALIDATION_MAX_ACTIVE", MAX_ACTIVE_RUNS)),
            int(getattr(settings, "PRODUCTION_VALIDATION_MAX_PENDING", MAX_PENDING_REQUESTS)))


class ValidationService:
    def __init__(self, socket_path: str, *, tools=None, limits=None, max_active=None, max_pending=None):
        default_active, default_pending = configured_admission()
        max_active = default_active if max_active is None else max_active
        max_pending = default_pending if max_pending is None else max_pending
        if max_active < 1 or max_pending < 0:
            raise ValueError("max_active must be >= 1 and max_pending >= 0")
        self.socket_path = socket_path
        self.tools = tools or configured_tools()
        self.limits = limits or confinement.configured_limits()
        self.max_active, self.max_pending = max_active, max_pending
        self.slots = threading.BoundedSemaphore(max_active)                     # running
        self.admission = threading.BoundedSemaphore(max_active + max_pending)    # running + waiting
        self.stopping = threading.Event()
        self.listener = None

    # -- lifecycle ------------------------------------------------------------------
    def reap(self, when: str) -> None:
        try:
            reaped = confinement.reap_all()
        except confinement.ConfinementUnavailable as exc:
            _log(f"{when}: no validation subtree ({exc}); every request will fail closed until it exists")
            return
        if reaped:
            _log(f"{when}: destroyed {len(reaped)} leftover run(s): "
                 + ", ".join(f"{os.path.basename(r['path'])} cleaned={r['cleaned']}" for r in reaped))

    def bind(self) -> None:
        directory = os.path.dirname(self.socket_path)
        info = os.stat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError(f"{directory} must be owned by this account and closed to others (0700)")
        try:
            if stat.S_ISSOCK(os.lstat(self.socket_path).st_mode):
                os.unlink(self.socket_path)                 # a previous instance's socket
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

    def serve_forever(self) -> None:
        self.reap("start-up")                  # BEFORE accepting any work
        self.bind()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.stop())
        _log(f"listening on {self.socket_path} (at most {self.max_active} running, "
             f"{self.max_pending} waiting)")
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
        """On the accept loop, never blocking: a handler thread exists only for
        an admitted connection; everything else is refused here and closed."""
        try:
            if confinement.peer_uid(conn) != os.geteuid():
                conn.close()
                return
        except OSError:
            conn.close()
            return
        if not self.admission.acquire(blocking=False):
            _reject_busy(conn)
            return
        try:
            threading.Thread(target=self._admitted, args=(conn,), daemon=True).start()
        except RuntimeError:
            self.admission.release()
            _reject_busy(conn)

    def _admitted(self, conn) -> None:
        try:
            self.handle(conn)
        finally:
            self.admission.release()

    def stop(self) -> None:
        self.stopping.set()
        if self.listener is not None:
            try:
                self.listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.listener.close()

    def shutdown(self) -> None:
        self.reap("stop")                     # no run outlives the service
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    # -- one request ----------------------------------------------------------------
    def handle(self, conn) -> None:
        fds = []
        try:
            if confinement.peer_uid(conn) != os.geteuid():
                return
            try:
                request, fds = _read_request(conn)
                result = self.run(request, fds, conn)
            except _Refused as exc:
                fds = fds or exc.args[1]
                result = {"status": "unavailable", "returncode": None, "stdout": "", "stderr": str(exc.args[0])}
            except (OSError, ValueError) as exc:
                return _log(f"request failed: {exc}")
            try:
                conn.settimeout(10)
                conn.sendall((json.dumps(result) + "\n").encode())
            except OSError:
                pass                        # the client is gone; the run is already over
        finally:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            conn.close()

    def run(self, request, fds, conn) -> dict:
        if not isinstance(request, dict) or request.get("v") != 1 or set(request) != {"v", "argv", "timeout"}:
            raise _Refused("malformed request", fds)
        argv, timeout = request["argv"], request["timeout"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV_ITEMS \
                or not all(isinstance(item, str) and 0 < len(item) <= MAX_ARG_LENGTH for item in argv):
            raise _Refused("malformed command", fds)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise _Refused("malformed timeout", fds)
        try:
            timeout = float(timeout)
        except OverflowError:
            raise _Refused("malformed timeout", fds) from None
        if not math.isfinite(timeout) or timeout <= 0:
            raise _Refused("malformed timeout", fds)
        command = validator_commands.resolve(argv, self.tools)
        if command is None:
            raise _Refused("not a validator command", fds)
        pass_fds = ()
        if command.needs_media:
            if len(fds) != 1 or not stat.S_ISREG(os.fstat(fds[0]).st_mode):
                raise _Refused("a media command needs exactly one regular-file descriptor", fds)
            argv = [f"/proc/self/fd/{fds[0]}" if item == validator_commands.MEDIA else item for item in argv]
            pass_fds = (fds[0],)
        elif fds:
            raise _Refused("unexpected descriptor", fds)
        deadline = min(timeout, command.max_seconds)
        while not self.slots.acquire(timeout=0.2):
            if _client_gone(conn) or self.stopping.is_set():
                return {"status": "stopped", "returncode": None, "stdout": "", "stderr": ""}
            deadline -= 0.2
            if deadline <= 0:
                return {"status": "timeout", "returncode": None, "stdout": "", "stderr": "no free validation slot"}
        try:
            return confinement.execute(argv, timeout_seconds=deadline, limits=self.limits, pass_fds=pass_fds,
                                       cancelled=lambda: _client_gone(conn) or self.stopping.is_set())
        finally:
            self.slots.release()
