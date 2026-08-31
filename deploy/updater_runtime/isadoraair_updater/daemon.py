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
import uuid

from . import BOOTSTRAP_PROTOCOL_VERSION, PROTOCOL_VERSION, RUNTIME_VERSION
from .config import StationConfig
from .executor import Executor
from .jobs import JobError, JobStore
from .process import CommandRunner
from .protocol import MAX_REQUEST_BYTES, ProtocolError, decode_request, encode_response
from .runtime_handoff import MUTATION_GATE_MILESTONE, SAFE_YIELD_MILESTONE
from .security import assert_root_protected_parents


class DaemonError(RuntimeError):
    pass


def _validate_privilege_drop(application_user: str, runner: CommandRunner) -> bool:
    """Prove the daemon can perform the one privilege transition it requires."""
    expected_uid = pwd.getpwnam(application_user).pw_uid
    result = runner.run_as_user(
        application_user,
        ["/usr/bin/id", "-u"],
        timeout=5,
        output_limit=128,
    )
    if not result.ok:
        raise DaemonError(
            "application-user privilege-drop self-check failed "
            f"(returncode={result.returncode!r}, timed_out={result.timed_out}, "
            f"output_truncated={result.output_truncated})"
        )
    if result.stdout.strip() != str(expected_uid).encode("ascii"):
        raise DaemonError("application-user privilege-drop self-check returned the wrong uid")
    return True


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


class UpdaterDaemon:
    def __init__(self, config: StationConfig, *, store: JobStore | None = None,
                 runner: CommandRunner | None = None, executor: Executor | None = None,
                 authorized_uids: set[int] | None = None, authorized_gids: set[int] | None = None,
                 expected_slot: str | None = None,
                 expected_handoff_generation: int | None = None,
                 expected_handoff_descriptor_sha256: str | None = None,
                 expected_resumable_job_uuid: str | None = None,
                 active_policy=None,
                 supervisor_client: object | None = None):
        self.config = config
        self.runner = runner or CommandRunner()
        self._protected_runtime_valid = _validate_privilege_drop(
            config.application_user, self.runner,
        )
        # Update Center Phase D, D3-H/D4-B: ALL FOUR None (every
        # ordinary daemon startup, including every station before
        # Phase D) means this daemon has no reason to believe it is a
        # candidate resuming a handoff -- recover_jobs() below falls
        # back to its own original, unchanged "at most one active job"
        # rule, and no readiness report is ever sent. All four
        # supplied means the supervisor launched THIS process as a
        # specific candidate for a specific slot/generation/descriptor/
        # job -- see updaterd.py's own D4-B entrypoint wiring for where
        # these values come from (the supervisor's own launch
        # arguments, never a value this process invents about itself).
        identity_fields = (expected_slot, expected_handoff_generation,
                           expected_handoff_descriptor_sha256, expected_resumable_job_uuid)
        if len({value is None for value in identity_fields}) != 1:
            raise DaemonError(
                "expected_slot/expected_handoff_generation/expected_handoff_descriptor_sha256/"
                "expected_resumable_job_uuid must be all null or all present"
            )
        self.expected_slot = expected_slot
        self.expected_handoff_generation = expected_handoff_generation
        self.expected_handoff_descriptor_sha256 = expected_handoff_descriptor_sha256
        self.expected_resumable_job_uuid = expected_resumable_job_uuid
        self.store = store or JobStore(config.jobs_root, config.logs_root)
        self.executor = executor or Executor(
            config, self.store, self.runner,
            expected_handoff_generation=expected_handoff_generation,
            expected_handoff_descriptor_sha256=expected_handoff_descriptor_sha256,
            expected_resumable_job_uuid=expected_resumable_job_uuid,
            active_policy=active_policy,
        )
        self._supervisor_client = supervisor_client
        app_uid = pwd.getpwnam(config.application_user).pw_uid
        app_gid = grp.getgrnam(config.application_group).gr_gid
        self.authorized_uids = authorized_uids if authorized_uids is not None else {0, app_uid}
        self.authorized_gids = authorized_gids if authorized_gids is not None else {app_gid}
        self._start_lock = threading.Lock()
        self._workers: dict[str, threading.Thread] = {}
        self._maintenance_lock = threading.Lock()
        self._maintenance_worker: threading.Thread | None = None
        self._maintenance_id: str | None = None
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

    def _shutdown_if_old_worker_just_yielded(self, result: dict) -> None:
        """D3-F/D4-D: the OLD worker's own voluntary-yield self-exit.
        Executor.execute() returning normally (no exception) with the
        job still "running", SAFE_YIELD_MILESTONE present, and
        MUTATION_GATE_MILESTONE absent is EXACTLY the signature of a
        successful handoff yield (see executor.py's own
        _execute_runtime_handoff) -- this process has already released
        its JobStore lock (Executor.store.close(), same object as
        self.store) and has nothing further to legitimately do for
        this job (see execute()'s own three-way branch: THIS process's
        own Executor has expected_handoff_generation=None, so even a
        stray re-entry could never progress the job further). The
        daemon therefore stops its own accept loop -- serve_forever()
        returns, and the entrypoint script (updaterd.py) exits the
        process, exactly matching D3-F's "old worker ... terminates or
        enters a state where it cannot mutate the job." An ORDINARY
        completed job (state succeeded/failed/manual_intervention_
        required, or a non-protected-runtime job) never matches this
        condition and never triggers a shutdown."""
        if not isinstance(result, dict) or result.get("state") != "running":
            return
        milestones = set(result.get("milestones", []))
        if SAFE_YIELD_MILESTONE in milestones and MUTATION_GATE_MILESTONE not in milestones:
            self.stop()

    def _run_worker(self, job_id: str):
        try:
            result = self.executor.execute(job_id)
            self._shutdown_if_old_worker_just_yielded(result)
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

    def _run_maintenance(self, operation_id: str, action: str, service: str | None):
        try:
            self.store.update_maintenance(operation_id, state="running")
            if action == "RESTART_OPERATOR_SERVICE":
                self.executor.systemd.restart_operator_service(service)
            elif action == "STORE_ALSA_STATE":
                self.executor.systemd.store_alsa_state()
            self.store.update_maintenance(
                operation_id, state="succeeded", result_code="SUCCEEDED",
                result_detail="maintenance action completed",
            )
        except Exception as exc:
            # Persist a fixed, sanitized classification. Never reflect command
            # output or arbitrary exception text through the protocol.
            try:
                self.store.update_maintenance(
                    operation_id, state="failed", result_code="OPERATION_FAILED",
                    result_detail=type(exc).__name__,
                )
            except Exception:
                # A result-store failure leaves the record nonterminal rather
                # than falsely claiming success.
                pass
        finally:
            with self._maintenance_lock:
                if self._maintenance_id == operation_id:
                    self._maintenance_worker = None
                    self._maintenance_id = None

    def _start_maintenance(self, action: str, service: str | None = None):
        """Admit at most one bounded maintenance worker; never queue work."""
        with self._maintenance_lock:
            if self._maintenance_worker is not None and self._maintenance_worker.is_alive():
                raise JobError("another operator maintenance action is active")
            operation_id = str(uuid.uuid4())
            self.store.create_maintenance(operation_id, action, service)
            worker = threading.Thread(
                target=self._run_maintenance,
                args=(operation_id, action, service),
                daemon=True,
                name=f"operator-maintenance-{operation_id}",
            )
            self._maintenance_worker = worker
            self._maintenance_id = operation_id
            worker.start()
        # Fast operations and immediate failures become truthful in the
        # initiating response without ever tying Gunicorn to a long systemd
        # stop timeout. Longer work remains queryable by correlation ID.
        worker.join(0.25)
        return self.store.load_maintenance(operation_id)

    def _trusted_repository_ready(self) -> bool:
        try:
            # Safe local initialization/verification only; fetch/network remains
            # an explicit START_UPDATE operation.
            self.executor.repository.initialize_or_verify()
            return True
        except Exception:
            return False

    def _get_supervisor_client(self):
        if self._supervisor_client is not None:
            return self._supervisor_client
        activation_socket = self.config.phase_d_supervisor_activation_socket
        if activation_socket is None:
            return None
        from .supervisor_client import SupervisorClient
        return SupervisorClient(activation_socket)

    def report_candidate_readiness(self) -> None:
        """D4-B/D4-C: the real candidate readiness handshake -- called
        once, from serve_forever(), only when this daemon was
        constructed with a full expected-candidate identity. Reports
        every fact D4-C requires (slot, generation, descriptor SHA,
        bootstrap protocol, wire protocols, config parsed, privilege-
        drop self-check, job-store lock, worker-socket bound, trusted
        repository usable, resumable job UUID) -- the supervisor
        independently checks every one against its own activation
        transaction (ipc_server._handle_report_readiness); this
        method's own report is evidence, never authorization. Mere
        process existence is never readiness -- every fact reported
        here reflects something this method has ALREADY verified by
        the time it is called (config parsed and privilege-drop are
        proven by __init__ succeeding at all; job_store_ready by
        self.store already holding its exclusive lock; worker_socket_
        bound by _prepare_socket() having already succeeded, checked
        by the caller's own ordering -- see serve_forever())."""
        if self.expected_slot is None:
            return
        client = self._get_supervisor_client()
        if client is None:
            return
        client.report_readiness(
            transaction_id=self.expected_resumable_job_uuid,
            candidate_slot=self.expected_slot,
            candidate_generation=self.expected_handoff_generation,
            candidate_descriptor_sha256=self.expected_handoff_descriptor_sha256,
            bootstrap_protocol_version=BOOTSTRAP_PROTOCOL_VERSION,
            supported_wire_protocols=(PROTOCOL_VERSION,),
            config_parsed=True,
            privilege_drop_self_check_passed=self._protected_runtime_valid,
            job_store_ready=self.store._lock_handle is not None,
            worker_socket_bound=self._socket is not None,
            trusted_repository_usable=self._trusted_repository_ready(),
            resumable_job_uuid=self.expected_resumable_job_uuid,
        )

    def _dispatch(self, request):
        if request.action == "PING":
            return {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "runtime_version": RUNTIME_VERSION,
                "protected_runtime_valid": self._protected_runtime_valid,
                "config_valid": True,
                "trusted_repository_ready": self._trusted_repository_ready(),
                "update_execution_enabled": self.config.update_execution_enabled,
                "maintenance_busy": bool(
                    self._maintenance_worker is not None and self._maintenance_worker.is_alive()
                ),
            }
        if request.action == "START_UPDATE":
            if not self.config.update_execution_enabled:
                raise ProtocolError("update execution is disarmed by root-owned station configuration")
            try:
                with self._start_lock:
                    state, created = self.store.accept(
                        request.job_id, request.requested_target_release_id,
                        request.expected_plan_fingerprint,
                    )
            except Exception:
                # Acceptance publishes the root state file before its audit-log
                # append. If anything fails after publication, make the durable
                # accepted job runnable before returning the ambiguous error.
                try:
                    durable = self.store.load(request.job_id)
                    facts_match = (
                        durable.get("requested_target_release_id")
                        == request.requested_target_release_id
                        and durable.get("expected_plan_fingerprint")
                        == request.expected_plan_fingerprint
                    )
                    if facts_match and durable.get("state") in {"accepted", "running"}:
                        self._ensure_worker(request.job_id)
                except Exception:
                    pass
                raise
            self._ensure_worker(request.job_id)
            return {"ok": True, "accepted": True, "idempotent": not created, "job_id": request.job_id, "state": state["state"]}
        if request.action == "RESTART_OPERATOR_SERVICE":
            if request.service not in self.config.operator_restart_units:
                raise ProtocolError("service is outside the root-owned operator restart allowlist")
            result = self._start_maintenance(request.action, request.service)
            return {"ok": True, "accepted": True, "maintenance": result}
        if request.action == "STORE_ALSA_STATE":
            result = self._start_maintenance(request.action)
            return {"ok": True, "accepted": True, "maintenance": result}
        if request.action == "GET_MAINTENANCE_STATUS":
            return {"ok": True, "maintenance": self.store.load_maintenance(request.operation_id)}
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
        except (ProtocolError, JobError) as exc:
            response = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:500]}
        except TimeoutError:
            response = {
                "ok": False,
                "error": "TimeoutError",
                "detail": "protected request timed out",
            }
        except OSError:
            response = {
                "ok": False,
                "error": "DurabilityError",
                "detail": "protected durability operation failed at an indeterminate boundary",
            }
        except Exception:
            response = {
                "ok": False,
                "error": "InternalError",
                "detail": "protected operation failed after an indeterminate durability boundary",
            }
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

        # Update Center Phase D, D3-H: this process was launched by the
        # supervisor as a SPECIFIC candidate generation -- distinguish
        # "ordinary clean startup" from "Phase-D handoff recovery of
        # one durable job" BEFORE falling back to the original at-
        # most-one-active-job rule below, never after. A single caller
        # of runtime_handoff.classify_handoff_recovery(), not a
        # reimplementation -- see that function's own docstring for
        # exactly what "unambiguous" means here.
        if self.expected_handoff_generation is not None:
            from .runtime_handoff import RecoveryAmbiguous, classify_handoff_recovery
            try:
                facts = classify_handoff_recovery(
                    active, expected_generation=self.expected_handoff_generation,
                    expected_descriptor_sha256=self.expected_handoff_descriptor_sha256,
                )
            except RecoveryAmbiguous:
                # Fail closed: never guess which job (if any) this
                # candidate is meant to resume.
                return
            if facts is not None:
                self._ensure_worker(facts.job_id)
                return
            # No resumable handoff job found for this candidate's own
            # generation/descriptor -- an ordinary startup would still
            # be legal (e.g. an operator started a fresh Django job
            # directly against this candidate after it's already
            # live), so fall through to the original rule below rather
            # than refusing all recovery outright.

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
        self.report_candidate_readiness()
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
