"""D2-H: the private root-only control protocol between the active
worker and this supervisor. A SEPARATE socket/protocol from the
Django-facing updater socket (isadoraair_updater/protocol.py) --
independently designed here (same JSON-line, bounded-size, strict-
closed-field-set STYLE, deliberately not imported), because the trust
domain is completely different: that socket is reachable from an
unprivileged Django worker process; THIS one is reachable only from
the currently-active root worker process, authorized by SO_PEERCRED,
never by anything a Django/HTTP-facing caller could reach.

REQUEST_ACTIVATION is never authorization by itself -- see
verification.py's own docstring. The supervisor independently repeats
every cryptographic/inventory proof against root-owned/candidate
material; this request only identifies WHICH already-staged candidate
the worker is asking about."""
from __future__ import annotations

import dataclasses
import json
import re
import socket
import struct
import uuid

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 131072
RELEASE_ID_RE = re.compile(r"^r[0-9]{4,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLOT_VALUES = frozenset({"A", "B"})

ACTIONS = frozenset({"PING", "GET_RUNTIME_STATE", "REQUEST_ACTIVATION", "GET_ACTIVATION_STATUS"})


class ProtocolError(ValueError):
    pass


def _uuid_field(value, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ProtocolError(f"{field} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise ProtocolError(f"{field} must use canonical lowercase UUID form")
    return value


@dataclasses.dataclass(frozen=True)
class Request:
    action: str
    transaction_id: str | None = None
    candidate_slot: str | None = None
    candidate_generation: int | None = None
    candidate_descriptor_sha256: str | None = None
    release_id: str | None = None
    previous_release_id: str | None = None


# Exactly which Request fields each action legally carries -- every
# other field must be None, checked below. No action ever carries a
# path/command/argv/environment/service/unit/shell field; those simply
# do not exist anywhere in this dataclass, so there is no way to smuggle
# one through even a permissive-looking decode.
ACTION_FIELDS: dict[str, frozenset[str]] = {
    "PING": frozenset(),
    "GET_RUNTIME_STATE": frozenset(),
    "REQUEST_ACTIVATION": frozenset({
        "transaction_id", "candidate_slot", "candidate_generation",
        "candidate_descriptor_sha256", "release_id", "previous_release_id",
    }),
    "GET_ACTIVATION_STATUS": frozenset({"transaction_id"}),
}


def decode_request(raw: bytes) -> Request:
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError(f"request is empty or exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("request must be one strict UTF-8 JSON object") from exc
    if not isinstance(data, dict):
        raise ProtocolError("request must be a JSON object")

    action = data.get("action")
    if action not in ACTIONS:
        raise ProtocolError(f"unknown action {action!r}")
    allowed = ACTION_FIELDS[action]
    known_keys = {"action", *allowed}
    unknown_keys = set(data) - known_keys
    if unknown_keys:
        raise ProtocolError(f"{action}: unrecognized field(s) {sorted(unknown_keys)!r}")
    missing_keys = allowed - set(data)
    if missing_keys:
        raise ProtocolError(f"{action}: missing required field(s) {sorted(missing_keys)!r}")

    fields: dict = {}
    if "transaction_id" in allowed:
        fields["transaction_id"] = _uuid_field(data["transaction_id"], "transaction_id")
    if "candidate_slot" in allowed:
        slot = data["candidate_slot"]
        if slot not in SLOT_VALUES:
            raise ProtocolError("candidate_slot must be exactly 'A' or 'B'")
        fields["candidate_slot"] = slot
    if "candidate_generation" in allowed:
        generation = data["candidate_generation"]
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ProtocolError("candidate_generation must be a positive integer")
        fields["candidate_generation"] = generation
    if "candidate_descriptor_sha256" in allowed:
        digest = data["candidate_descriptor_sha256"]
        if not isinstance(digest, str) or not SHA256_RE.match(digest):
            raise ProtocolError("candidate_descriptor_sha256 must be exactly 64 lowercase hex characters")
        fields["candidate_descriptor_sha256"] = digest
    if "release_id" in allowed:
        release_id = data["release_id"]
        if not isinstance(release_id, str) or not RELEASE_ID_RE.match(release_id):
            raise ProtocolError("release_id does not match the required r#### pattern")
        fields["release_id"] = release_id
    if "previous_release_id" in allowed:
        previous = data["previous_release_id"]
        if previous is not None and (not isinstance(previous, str) or not RELEASE_ID_RE.match(previous)):
            raise ProtocolError("previous_release_id does not match the required r#### pattern")
        fields["previous_release_id"] = previous

    return Request(action=action, **fields)


def encode_request(request: Request) -> bytes:
    payload = {"action": request.action}
    for field in ACTION_FIELDS[request.action]:
        payload[field] = getattr(request, field)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProtocolError("encoded request exceeds the protocol limit")
    return raw


def encode_response(payload: dict) -> bytes:
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise ProtocolError("response payload must be a dict with a boolean 'ok'")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProtocolError("encoded response exceeds the protocol limit")
    return raw


def decode_response(raw: bytes) -> dict:
    if not raw or len(raw) > MAX_RESPONSE_BYTES:
        raise ProtocolError(f"response is empty or exceeds {MAX_RESPONSE_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("response must be one strict UTF-8 JSON object") from exc
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise ProtocolError("response schema is invalid")
    return data


def authorized_peer_uid(sock: socket.socket) -> int | None:
    """Returns the connecting peer's real UID via SO_PEERCRED, or None
    if the platform/socket does not support it (never guessed/assumed
    -- a caller must treat None as unauthorized, never as "probably
    fine"). Linux-only ancillary data; struct is 3 native ints
    (pid, uid, gid).

    Checks sock.family == AF_UNIX explicitly before ever calling
    getsockopt -- observed directly (see this module's own test suite)
    that SO_PEERCRED on a non-UNIX socket does not reliably raise
    OSError on this platform; it can instead return a garbage/sentinel
    buffer that decodes to e.g. uid=-1. That garbage value is still
    safely non-zero (is_authorized_root_peer() below would still
    correctly refuse it), but this function's whole purpose is to
    never depend on incidental OS behavior for its own correctness --
    so the socket family is checked directly rather than trusted to
    the kernel call failing when it "should"."""
    if sock.family != socket.AF_UNIX:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    except OSError:
        return None
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


def is_authorized_root_peer(sock: socket.socket) -> bool:
    """Only a peer connecting as UID 0 is authorized -- both this
    supervisor and the one worker process it manages always run as
    root; nothing else (in particular no Django/Gunicorn-owned process)
    should ever be able to open this socket at all given its own
    filesystem permissions, but SO_PEERCRED is checked as well so
    authorization never depends on filesystem permissions alone."""
    uid = authorized_peer_uid(sock)
    return uid == 0
