"""Update Center Phase D, D3-A: the worker's own strict client for the
IMMUTABLE supervisor's private, root-only activation socket.

An INDEPENDENT reproduction of deploy/updater_bootstrap/
isadoraair_updater_bootstrap/protocol.py's exact wire shape --
deliberately NOT an import of that module (this package must never
import the supervisor's own tree, mirroring Correction 1's boundary in
the other direction: the supervisor never imports the replaceable
worker's tree, and the replaceable worker never imports the immutable
supervisor's tree either). The two sides are kept in agreement by
test_phase_d3_supervisor_ipc.py's own cross-package parity test (feeds
identical logical requests to both encoders and asserts byte-identical
output), never by sharing code.

This client only ever sends the four actions the supervisor's own
protocol.py already defines: PING, GET_RUNTIME_STATE, REQUEST_
ACTIVATION, GET_ACTIVATION_STATUS. There is no fifth action, and there
never will be one that carries a path/command/argv/service/unit/shell/
environment/arbitrary-executable field -- see D3-A's own explicit
requirement: "The worker's request is intent, never authorization.\""""
from __future__ import annotations

import json
from pathlib import Path
import socket

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 131072


class SupervisorClientError(RuntimeError):
    pass


class SupervisorTransportError(SupervisorClientError):
    """No trustworthy application-level response was received -- the
    supervisor socket may simply not exist yet (D3-P: this is exactly
    the brief window the CURRENT worker's own socket also disappears
    during a real handoff; callers must treat this as transient, never
    as proof the supervisor rejected anything)."""


class SupervisorRejectedError(SupervisorClientError):
    def __init__(self, detail: str, *, error_code: str = ""):
        super().__init__(detail)
        self.error_code = error_code


class SupervisorClient:
    def __init__(self, socket_path: Path, *, timeout: float = 10.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def _request(self, payload: dict) -> dict:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_REQUEST_BYTES:
            raise SupervisorClientError("request exceeds protocol limit")
        response = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(str(self.socket_path))
                connection.sendall(raw)
                connection.shutdown(socket.SHUT_WR)
                while len(response) <= MAX_RESPONSE_BYTES:
                    chunk = connection.recv(min(4096, MAX_RESPONSE_BYTES + 1 - len(response)))
                    if not chunk:
                        break
                    response.extend(chunk)
        except (OSError, TimeoutError) as exc:
            raise SupervisorTransportError("supervisor activation socket is temporarily unavailable") from exc
        if len(response) > MAX_RESPONSE_BYTES:
            raise SupervisorTransportError("supervisor response exceeds protocol limit")
        try:
            decoded = json.loads(bytes(response).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SupervisorTransportError("supervisor response is not strict JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
            raise SupervisorTransportError("supervisor response schema is invalid")
        if not decoded["ok"]:
            raise SupervisorRejectedError(
                str(decoded.get("detail", "supervisor rejected the request"))[:500],
                error_code=str(decoded.get("error", ""))[:64],
            )
        return decoded

    def ping(self) -> dict:
        return self._request({"action": "PING"})

    def get_runtime_state(self) -> dict:
        return self._request({"action": "GET_RUNTIME_STATE"})

    def request_activation(
        self, *, transaction_id: str, candidate_slot: str, candidate_generation: int,
        candidate_descriptor_sha256: str, release_id: str, previous_release_id: str | None,
    ) -> dict:
        """`transaction_id` here is THIS WORKER's own idempotency key
        for the request -- by convention (see runtime_handoff.py), the
        durable update job's own job_id, canonical lowercase UUID
        form. It is NOT the supervisor's own internal
        ActivationTransaction.transaction_id (that one is generated
        supervisor-side, see supervisor.begin_transaction()) -- the
        IPC server correlates the two so a retried REQUEST_ACTIVATION
        carrying the SAME job_id after a transport hiccup is treated
        as an idempotent status query, never as a second, conflicting
        transaction."""
        return self._request({
            "action": "REQUEST_ACTIVATION",
            "transaction_id": transaction_id,
            "candidate_slot": candidate_slot,
            "candidate_generation": candidate_generation,
            "candidate_descriptor_sha256": candidate_descriptor_sha256,
            "release_id": release_id,
            "previous_release_id": previous_release_id,
        })

    def get_activation_status(self, transaction_id: str) -> dict:
        return self._request({"action": "GET_ACTIVATION_STATUS", "transaction_id": transaction_id})

    def report_readiness(
        self, *, transaction_id: str, candidate_slot: str, candidate_generation: int,
        candidate_descriptor_sha256: str, bootstrap_protocol_version: int,
        supported_wire_protocols, config_parsed: bool, privilege_drop_self_check_passed: bool,
        job_store_ready: bool, worker_socket_bound: bool, trusted_repository_usable: bool,
        resumable_job_uuid: str,
    ) -> dict:
        """D4-C: the candidate's own real readiness handshake. Every
        fact here is exactly what readiness.ReadinessFacts (D2) already
        defines (plus resumable_job_uuid, D4's own addition) -- the
        supervisor independently checks every one of these against its
        own activation transaction before ever marking the candidate
        ready; a candidate's own self-report is evidence, never
        authorization (same principle as REQUEST_ACTIVATION)."""
        return self._request({
            "action": "REPORT_READINESS",
            "transaction_id": transaction_id,
            "candidate_slot": candidate_slot,
            "candidate_generation": candidate_generation,
            "candidate_descriptor_sha256": candidate_descriptor_sha256,
            "bootstrap_protocol_version": bootstrap_protocol_version,
            "supported_wire_protocols": list(supported_wire_protocols),
            "config_parsed": config_parsed,
            "privilege_drop_self_check_passed": privilege_drop_self_check_passed,
            "job_store_ready": job_store_ready,
            "worker_socket_bound": worker_socket_bound,
            "trusted_repository_usable": trusted_repository_usable,
            "resumable_job_uuid": resumable_job_uuid,
        })

    def confirm_runtime_acceptance(
        self, *, transaction_id: str, candidate_slot: str, candidate_generation: int,
        candidate_descriptor_sha256: str, resumable_job_uuid: str,
    ) -> dict:
        """D4-J: sent ONLY once this candidate's own Executor has
        already durably written runtime_activation_accepted for
        resumable_job_uuid -- see runtime_handoff.py's own
        MUTATION_GATE_MILESTONE. The supervisor's own commit_
        transaction() call is gated on receiving and independently
        re-checking this, never on readiness alone (D4-J's own
        explicit "do not commit the runtime generation merely because
        the candidate bound a socket" rule)."""
        return self._request({
            "action": "CONFIRM_RUNTIME_ACCEPTANCE",
            "transaction_id": transaction_id,
            "candidate_slot": candidate_slot,
            "candidate_generation": candidate_generation,
            "candidate_descriptor_sha256": candidate_descriptor_sha256,
            "resumable_job_uuid": resumable_job_uuid,
        })
