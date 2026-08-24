"""Unix-socket daemon for the narrow protected updater protocol."""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import threading

from . import PROTOCOL_VERSION, RUNTIME_VERSION
from .config import StationConfig
from .executor import Executor
from .jobs import JobError, JobStore
from .process import CommandRunner
from .protocol import MAX_REQUEST_BYTES, ProtocolError, decode_request, encode_response
from .security import assert_root_protected_parents


class DaemonError(RuntimeError):
    pass


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


class UpdaterDaemon:
    def __init__(self, config: StationConfig, *, store: JobStore | None = None,
                 runner: CommandRunner | None = None, executor: Executor | None = None,
                 authorized_uids: set[int] | None = None, authorized_gids: set[int] | None = None):
        self.config = config
        self.runner = runner or CommandRunner()
        self.store = store or JobStore(config.jobs_root, config.logs_root)
        self.executor = executor or Executor(config, self.store, self.runner)
        app_uid = pwd.getpwnam(config.application_user).pw_uid
        app_gid = grp.getgrnam(config.application_group).gr_gid
        self.authorized_uids = authorized_uids if authorized_uids is not None else {0, app_uid}
        self.authorized_gids = authorized_gids if authorized_gids is not None else {app_gid}
        self._start_lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        self._socket: socket.socket | None = None

    def _read_request(self, connection: socket.socket) -> bytes:
        data = bytearray()
        while len(data) <= MAX_REQUEST_BYTES:
            chunk = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if b"\n" in chunk:
                break
        if len(data) > MAX_REQUEST_BYTES:
            raise ProtocolError("request exceeds the bounded message limit")
        if data.count(b"\n") > 1 or (b"\n" in data and data.rstrip(b"\n") != data[:-1]):
            raise ProtocolError("exactly one JSON message is permitted per connection")
        return bytes(data).rstrip(b"\n")

    def _run_worker(self, job_id: str):
        try:
            self.executor.execute(job_id)
        except Exception as exc:
            # Never strand an accepted job as active because of an unexpected
            # implementation failure. Avoid logging exception text here: an
            # unforeseen exception could carry application-owned secret data.
            try:
                self.store.fail(
                    job_id,
                    "UNEXPECTED_EXECUTOR_FAILURE",
                    f"protected runtime raised {type(exc).__name__}; operator review required",
                    manual=True,
                )
            except Exception:
                pass
        finally:
            with self._start_lock:
                self._workers.pop(job_id, None)

    def _ensure_worker(self, job_id: str):
        with self._start_lock:
            existing = self._workers.get(job_id)
            if existing and existing.is_alive():
                return
            worker = threading.Thread(target=self._run_worker, args=(job_id,), daemon=True, name=f"update-{job_id}")
            self._workers[job_id] = worker
            worker.start()

    def _dispatch(self, request):
        if request.action == "PING":
            return {"ok": True, "protocol_version": PROTOCOL_VERSION, "runtime_version": RUNTIME_VERSION}
        if request.action == "START_UPDATE":
            with self._start_lock:
                state, created = self.store.accept(
                    request.job_id, request.requested_target_release_id,
                    request.expected_plan_fingerprint,
                )
            self._ensure_worker(request.job_id)
            return {"ok": True, "accepted": True, "idempotent": not created, "job_id": request.job_id, "state": state["state"]}
        if request.action == "GET_JOB_STATUS":
            state = self.store.load(request.job_id)
            return {
                "ok": True,
                "job": {
                    "job_id": state["job_id"], "state": state["state"],
                    "current_step": state["current_step"], "milestones": state["milestones"],
                    "created_at": state["created_at"], "updated_at": state["updated_at"],
                    "failure_classification": state["failure_classification"],
                    "failure_detail": state["failure_detail"],
                    "trusted_plan": state["trusted_plan"],
                },
            }
        if request.action == "GET_JOB_LOG":
            self.store.load(request.job_id)
            return {"ok": True, "job_id": request.job_id, "log_tail": self.store.tail_log(request.job_id, request.max_bytes)}
        raise ProtocolError("unknown action")

    def handle_connection(self, connection: socket.socket):
        try:
            raw_request = self._read_request(connection)
            _pid, uid, gid = _peer_credentials(connection)
            if uid not in self.authorized_uids and gid not in self.authorized_gids:
                raise ProtocolError("peer is not authorized")
            request = decode_request(raw_request)
            response = self._dispatch(request)
        except (ProtocolError, JobError, OSError) as exc:
            response = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}
        try:
            connection.sendall(encode_response(response))
        except OSError:
            # A peer disconnect must not terminate the protected daemon or job.
            return

    def _prepare_socket(self) -> socket.socket:
        path = self.config.socket_path
        parent = path.parent
        assert_root_protected_parents(path)
        if not parent.is_dir():
            raise DaemonError("socket RuntimeDirectory does not exist")
        parent_info = parent.stat()
        if parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
            raise DaemonError("socket RuntimeDirectory is not root-owned/protected")
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise DaemonError("refusing to replace non-root/non-socket IPC path")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chown(path, 0, grp.getgrnam(self.config.application_group).gr_gid)
        os.chmod(path, 0o660)
        server.listen(16)
        server.settimeout(1.0)
        return server

    def recover_jobs(self):
        active = [
            state for state in self.store.list_states()
            if state.get("state") in {"accepted", "running"}
        ]
        if len(active) > 1:
            for state in active:
                self.store.fail(
                    state["job_id"],
                    "ROOT_STATE_CONCURRENCY_CONFLICT",
                    "multiple root-owned jobs claim active execution; automatic recovery is forbidden",
                    manual=True,
                )
            return
        if active:
            self._ensure_worker(active[0]["job_id"])

    def serve_forever(self):
        self._socket = self._prepare_socket()
        self.recover_jobs()
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(10)
                self.handle_connection(connection)

    def stop(self):
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        self.store.close()
