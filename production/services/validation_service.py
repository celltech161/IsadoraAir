"""The isadoraair-validation service: sole owner of every validation run.

Run by systemd as ``isadoraair-validation.service`` (deploy/; entry point
``manage.py production_validation_service``) with ``Delegate=cpu memory pids``,
``DelegateSubgroup=supervisor`` and ``KillMode=control-group``. It listens on a
Unix socket in its 0700 runtime directory and, for each request:

1. authenticates the peer (SO_PEERCRED: the service's own account only);
2. reads ONE request -- ``{"v": 1, "argv": [...], "timeout": seconds}`` plus,
   for a media command, exactly one regular-file descriptor (SCM_RIGHTS);
3. accepts it only if ``argv`` is exactly one of the validator commands in
   production.services.validator_commands, rebuilt from THIS service's tool
   configuration (no executable, option, path, environment or limit can be
   chosen by a client); the media placeholder becomes ``/proc/self/fd/<n>``;
4. runs it with confinement.execute() in a fresh leaf of its exclusive
   subtree, under the service's own limits and a hard deadline of
   min(requested, the command's maximum) -- enforced here, not by the client;
5. cancels the run as soon as the client connection closes (a web worker that
   dies or is restarted takes its validation with it), and destroys the tree
   when the run ends in every case;
6. answers with the result as one JSON object and closes the connection.

At start-up -- before accepting work -- and on stop it kills and removes every
``run-*`` leaf in its subtree (confinement.reap_all). If the service itself is
killed, systemd kills everything left in its cgroup and restarts it.

Deliberately NOT a job system: no queue, no persistence, no retries, no
callbacks -- one synchronous run per connection, at most ``max_concurrent`` at
a time.
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

from . import confinement, validator_commands

MAX_CONCURRENT_RUNS = 4
REQUEST_READ_TIMEOUT_SECONDS = 10.0
MAX_ARGV_ITEMS = 40
MAX_ARG_LENGTH = 4096


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


def _read_request(conn):
    conn.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
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
        chunk = conn.recv(confinement.REQUEST_LIMIT_BYTES - len(buffer))
        if not chunk:
            raise _Refused("incomplete request", fds)
        buffer.extend(chunk)
    try:
        request = json.loads(bytes(buffer).decode("utf-8"))
    except ValueError:
        raise _Refused("malformed request", fds) from None
    return request, fds


class ValidationService:
    def __init__(self, socket_path: str, *, tools=None, limits=None, max_concurrent=MAX_CONCURRENT_RUNS):
        self.socket_path = socket_path
        self.tools = tools or configured_tools()
        self.limits = limits or confinement.configured_limits()
        self.slots = threading.BoundedSemaphore(max_concurrent)
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
        listener.listen(16)
        self.listener = listener

    def serve_forever(self) -> None:
        self.reap("start-up")                  # BEFORE accepting any work
        self.bind()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: self.stop())
        _log(f"listening on {self.socket_path}")
        try:
            while not self.stopping.is_set():
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    if self.stopping.is_set():
                        break
                    raise
                threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        finally:
            self.shutdown()

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
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) \
                or timeout <= 0:
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
        deadline = min(float(timeout), command.max_seconds)
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
