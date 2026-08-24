"""Unprivileged strict client for the protected updater socket."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import uuid


PROTOCOL_VERSION = 3
MAX_RESPONSE_BYTES = 131072


class BackendError(RuntimeError):
    pass


class BackendTransportError(BackendError):
    """No trustworthy application-level response was received."""


class BackendRejectedError(BackendError):
    """The protected runtime returned an explicit negative response."""

    def __init__(self, detail: str, *, error_code: str = ""):
        super().__init__(detail)
        self.error_code = error_code


class UpdaterClient:
    def __init__(self, socket_path: Path = Path("/run/isadoraair-updater/updater.sock"), *, timeout: float = 5.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def _request(self, payload: dict) -> dict:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > 8192:
            raise BackendError("request exceeds protocol limit")
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
            raise BackendTransportError("protected updater is temporarily unavailable") from exc
        if len(response) > MAX_RESPONSE_BYTES:
            raise BackendTransportError("updater response exceeds protocol limit")
        try:
            decoded = json.loads(bytes(response).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendTransportError("updater response is not strict JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
            raise BackendTransportError("updater response schema is invalid")
        if not decoded["ok"]:
            raise BackendRejectedError(
                str(decoded.get("detail", "protected updater rejected the request"))[:500],
                error_code=str(decoded.get("error", ""))[:64],
            )
        return decoded

    def ping(self) -> dict:
        return self._request({"protocol_version": PROTOCOL_VERSION, "action": "PING"})

    def start_update(self, *, job_id: uuid.UUID, target_release_id: str, plan_fingerprint: str) -> dict:
        return self._request({
            "protocol_version": PROTOCOL_VERSION,
            "action": "START_UPDATE",
            "job_id": str(job_id),
            "requested_target_release_id": target_release_id,
            "expected_plan_fingerprint": plan_fingerprint,
        })

    def get_job_status(self, job_id: uuid.UUID) -> dict:
        return self._request({"protocol_version": PROTOCOL_VERSION, "action": "GET_JOB_STATUS", "job_id": str(job_id)})

    def get_job_log(self, job_id: uuid.UUID, *, max_bytes: int = 32768) -> str:
        response = self._request({
            "protocol_version": PROTOCOL_VERSION, "action": "GET_JOB_LOG",
            "job_id": str(job_id), "max_bytes": max_bytes,
        })
        value = response.get("log_tail")
        if not isinstance(value, str):
            raise BackendError("updater log response schema is invalid")
        return value

    def restart_operator_service(self, service: str) -> dict:
        return self._maintenance_request({
            "protocol_version": PROTOCOL_VERSION,
            "action": "RESTART_OPERATOR_SERVICE",
            "service": service,
        })

    def store_alsa_state(self) -> dict:
        return self._maintenance_request({
            "protocol_version": PROTOCOL_VERSION,
            "action": "STORE_ALSA_STATE",
        })

    def _maintenance_request(self, payload: dict) -> dict:
        response = self._request(payload)
        record = response.get("maintenance")
        if not isinstance(record, dict) or record.get("state") not in {
            "accepted", "running", "succeeded", "failed",
        }:
            raise BackendTransportError("updater maintenance response schema is invalid")
        if record["state"] == "failed":
            raise BackendRejectedError(
                "protected maintenance action failed",
                error_code=str(record.get("result_code", "OPERATION_FAILED"))[:64],
            )
        return record

    def get_maintenance_status(self, operation_id: uuid.UUID | str) -> dict:
        response = self._request({
            "protocol_version": PROTOCOL_VERSION,
            "action": "GET_MAINTENANCE_STATUS",
            "operation_id": str(operation_id),
        })
        record = response.get("maintenance")
        if not isinstance(record, dict) or record.get("operation_id") != str(operation_id):
            raise BackendTransportError("updater maintenance status schema is invalid")
        return record
