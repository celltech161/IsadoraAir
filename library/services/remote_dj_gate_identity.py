"""Roadmap 2.17A -- the Remote DJ microphone gate's session binding, shared by
the Django endpoint and the engine command boundary so both enforce exactly
the same contract (the same pattern as start_next_identity).

A user's gate request is bound to:

* the authenticated user, and the exact Remote DJ connection attempt it
  controls (``attempt_id``);
* ``control_key`` -- proof that the request comes from the browser that
  started that attempt. It is an HMAC of (user, attempt) under the server
  SECRET_KEY, returned ONLY in the ``api_remote_dj_token`` response that
  minted the attempt. The attempt id and the status ``browser_key`` are not
  secrets (r0116 diagnostics expose the attempt id to status readers); the
  control key is, and is never logged, published in status, queued or stored
  by the browser. A key minted for attempt B never verifies for attempt A;
* ``engine_instance`` -- the engine run the browser saw;
* ``sequence`` -- the browser's per-attempt press counter, reused verbatim on
  an HTTP retry. The engine applies a sequence at most once and never an
  older one, so a retried, duplicated or reordered press cannot undo a newer
  one;
* ``generation`` -- the engine-owned PTT-authority generation observed by the
  owning browser when the press began. Administrative Disable/Enable and
  other authoritative invalidation boundaries advance it, so a request that
  reaches Django late cannot be repackaged as a post-intervention press.

Django derives the engine ``request_id`` from (user, attempt, sequence), and
queues a ``proof`` -- an HMAC of the whole command under the control key --
instead of the key itself. The engine recomputes the key for its OWN active
session's user and attempt and verifies the proof immediately before touching
the gate, so a command for a superseded attempt, another user or another
engine run is refused with no audio effect.
"""

from __future__ import annotations

import hashlib
import re

from django.utils.crypto import constant_time_compare, salted_hmac

from library.services.remote_dj_connection import ATTEMPT_ID_PATTERN
from library.services.start_next_identity import valid_engine_instance

CONTROL_KEY_SALT = "isadoraair.remote-dj.gate-control.v1"
PROOF_VERSION = "remote_dj_gate.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_SEQUENCE = 2**31 - 1
MAX_GENERATION = 2**31 - 1
MAX_REQUEST_ID_LENGTH = 64
# The engine refuses a command queued longer ago than this (a delayed press
# must not act on a later moment), or stamped implausibly far in the future.
MAX_COMMAND_AGE_MS = 5000
MAX_FUTURE_SKEW_MS = 2000

REQUEST_FIELDS = frozenset({
    "active", "attempt_id", "control_key", "sequence", "generation", "engine_instance",
})
COMMAND_FIELDS = frozenset({
    "command", "request_id", "user_id", "attempt_id", "sequence", "active",
    "generation", "engine_instance", "issued_at_ms", "proof",
})


def _positive_int(value, maximum=None):
    return (isinstance(value, int) and not isinstance(value, bool) and value > 0
            and (maximum is None or value <= maximum))


def valid_attempt_id(value):
    return isinstance(value, str) and bool(ATTEMPT_ID_PATTERN.fullmatch(value))


def control_key_for(user_id, attempt_id):
    """The control credential for one user's one connection attempt."""
    return salted_hmac(
        CONTROL_KEY_SALT, f"{int(user_id)}:{attempt_id}", algorithm="sha256"
    ).hexdigest()


def verify_control_key(user_id, attempt_id, control_key):
    if not (isinstance(control_key, str) and HEX64.match(control_key)
            and _positive_int(user_id) and valid_attempt_id(attempt_id)):
        return False
    return constant_time_compare(control_key, control_key_for(user_id, attempt_id))


def derive_request_id(user_id, attempt_id, sequence):
    digest = hashlib.sha256(f"remote_dj_gate:{user_id}:{attempt_id}:{sequence}".encode()).hexdigest()
    return digest[:32]


def _proof_message(command):
    return "|".join(str(part) for part in (
        PROOF_VERSION, command["command"], command["request_id"], command["user_id"], command["attempt_id"],
        command["sequence"], command["generation"], int(command["active"]), command["engine_instance"],
        command["issued_at_ms"],
    ))


def command_proof(control_key, command):
    return salted_hmac(
        PROOF_VERSION, _proof_message(command), secret=control_key, algorithm="sha256"
    ).hexdigest()


def parse_request(body):
    """A browser request body -> dict, or ValueError naming what is wrong.
    Exactly the six fields, each strictly typed (a real JSON boolean)."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    if set(body) != REQUEST_FIELDS:
        raise ValueError("body must contain exactly active, attempt_id, control_key, "
                         "sequence, generation and engine_instance")
    if not isinstance(body["active"], bool):
        raise ValueError("active must be a boolean")
    if not valid_attempt_id(body["attempt_id"]):
        raise ValueError("attempt_id is malformed")
    if not (isinstance(body["control_key"], str) and HEX64.match(body["control_key"])):
        raise ValueError("control_key is malformed")
    if not _positive_int(body["sequence"], MAX_SEQUENCE):
        raise ValueError("sequence must be a positive integer")
    if not _positive_int(body["generation"], MAX_GENERATION):
        raise ValueError("generation must be a positive integer")
    if not valid_engine_instance(body["engine_instance"]):
        raise ValueError("engine_instance is malformed")
    return dict(body)


def build_command(*, user_id, request, issued_at_ms):
    """The queued engine command for one verified request: carries a proof
    bound to every field, never the control key itself."""
    command = {
        "command": "remote_dj_gate",
        "request_id": derive_request_id(user_id, request["attempt_id"], request["sequence"]),
        "user_id": int(user_id),
        "attempt_id": request["attempt_id"],
        "sequence": request["sequence"],
        "generation": request["generation"],
        "active": request["active"],
        "engine_instance": request["engine_instance"],
        "issued_at_ms": int(issued_at_ms),
    }
    command["proof"] = command_proof(request["control_key"], command)
    return command


def parse_command(data):
    """A queued engine command -> dict, or ValueError. Strict like
    parse_request: an old unbound ``{"command", "active"}`` file, or any
    extra/missing/ill-typed field, is malformed."""
    if not isinstance(data, dict) or set(data) != COMMAND_FIELDS:
        raise ValueError("remote_dj_gate command fields are missing or unexpected")
    if not (isinstance(data["request_id"], str) and 0 < len(data["request_id"]) <= MAX_REQUEST_ID_LENGTH):
        raise ValueError("request_id is malformed")
    if not _positive_int(data["user_id"]):
        raise ValueError("user_id is malformed")
    if not valid_attempt_id(data["attempt_id"]):
        raise ValueError("attempt_id is malformed")
    if not _positive_int(data["sequence"], MAX_SEQUENCE):
        raise ValueError("sequence is malformed")
    if not _positive_int(data["generation"], MAX_GENERATION):
        raise ValueError("generation is malformed")
    if not isinstance(data["active"], bool):
        raise ValueError("active is malformed")
    if not valid_engine_instance(data["engine_instance"]):
        raise ValueError("engine_instance is malformed")
    if not _positive_int(data["issued_at_ms"]):
        raise ValueError("issued_at_ms is malformed")
    if not (isinstance(data["proof"], str) and HEX64.match(data["proof"])):
        raise ValueError("proof is malformed")
    if data["request_id"] != derive_request_id(data["user_id"], data["attempt_id"], data["sequence"]):
        raise ValueError("request_id does not match its binding")
    return dict(data)


def verify_command_proof(command):
    """True iff the command was issued with the control key of ITS OWN
    (user, attempt). The caller separately requires that pair to be the
    engine's active session."""
    expected = command_proof(control_key_for(command["user_id"], command["attempt_id"]), command)
    return constant_time_compare(command["proof"], expected)
