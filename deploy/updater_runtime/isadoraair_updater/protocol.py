"""Versioned, strict and bounded updater IPC messages."""
from __future__ import annotations

import dataclasses
import json
import re
import uuid

from . import PROTOCOL_VERSION


MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 131072
MAX_LOG_TAIL_BYTES = 65536
RELEASE_ID = re.compile(r"^r[0-9]{4,}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ACTIONS = frozenset({"PING", "START_UPDATE", "GET_JOB_STATUS", "GET_JOB_LOG"})


class ProtocolError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Request:
    action: str
    job_id: str | None = None
    requested_target_release_id: str | None = None
    expected_plan_fingerprint: str | None = None
    max_bytes: int | None = None


def _uuid(value, field="job_id") -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ProtocolError(f"{field} must use canonical lowercase UUID form")
    return value


def decode_request(raw: bytes) -> Request:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("request is empty or exceeds the 8192-byte limit")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("request must be one strict UTF-8 JSON object") from exc
    if not isinstance(data, dict):
        raise ProtocolError("request must be a JSON object")
    action = data.get("action")
    if action not in ACTIONS:
        raise ProtocolError("unknown action")
    required = {"PING": {"protocol_version", "action"},
                "START_UPDATE": {"protocol_version", "action", "job_id", "requested_target_release_id", "expected_plan_fingerprint"},
                "GET_JOB_STATUS": {"protocol_version", "action", "job_id"},
                "GET_JOB_LOG": {"protocol_version", "action", "job_id", "max_bytes"}}[action]
    if set(data) != required:
        raise ProtocolError(f"{action} fields must be exactly {sorted(required)!r}")
    if data["protocol_version"] != PROTOCOL_VERSION or isinstance(data["protocol_version"], bool):
        raise ProtocolError("unsupported protocol_version")
    if action == "PING":
        return Request(action=action)
    job_id = _uuid(data["job_id"])
    if action == "START_UPDATE":
        release = data["requested_target_release_id"]
        fingerprint = data["expected_plan_fingerprint"]
        if not isinstance(release, str) or not RELEASE_ID.fullmatch(release):
            raise ProtocolError("requested_target_release_id must match r####")
        if not isinstance(fingerprint, str) or not SHA256.fullmatch(fingerprint):
            raise ProtocolError("expected_plan_fingerprint must be lowercase SHA-256")
        return Request(action, job_id, release, fingerprint)
    if action == "GET_JOB_LOG":
        maximum = data["max_bytes"]
        if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= MAX_LOG_TAIL_BYTES:
            raise ProtocolError(f"max_bytes must be between 1 and {MAX_LOG_TAIL_BYTES}")
        return Request(action, job_id, max_bytes=maximum)
    return Request(action, job_id)


def encode_response(data: dict) -> bytes:
    if not isinstance(data, dict):
        raise TypeError("response must be a dict")
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProtocolError("response exceeds the bounded protocol limit")
    return raw
