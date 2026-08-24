"""Unprivileged strict client for the protected updater socket."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import uuid


PROTOCOL_VERSION = 1
MAX_RESPONSE_BYTES = 131072


class BackendError(RuntimeError):
    pass


class UpdaterClient:
    def __init__(self, socket_path: Path = Path("/run/isadoraair-updater/updater.sock"), *, timeout: float = 5.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def _request(self, payload: dict) -> dict:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > 8192:
            raise BackendError("request exceeds protocol limit")
        response = bytearray()
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
        if len(response) > MAX_RESPONSE_BYTES:
            raise BackendError("updater response exceeds protocol limit")
        try:
            decoded = json.loads(bytes(response).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BackendError("updater response is not strict JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
            raise BackendError("updater response schema is invalid")
        if not decoded["ok"]:
            raise BackendError(str(decoded.get("detail", "protected updater rejected the request"))[:500])
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
