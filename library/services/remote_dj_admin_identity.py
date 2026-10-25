"""Roadmap 2.17A -- Station Administrator emergency controls for a connected
Remote DJ: Emergency Mute (and its release) and Kick. Shared by the Django
endpoint and the engine command boundary, like remote_dj_gate_identity.

An administrator request names:

* ``action`` -- ``emergency_mute``, ``release_mute`` or ``kick``. Each needs
  its own capability (ACTION_CAPABILITY). None of them can open a microphone;
  ``release_mute`` only lifts the latch and leaves the microphone OFF;
* ``target_session`` -- the session the administrator observed: the status
  block's session reference, sha256(attempt_id)[:32]. Never "whatever session
  is current": Django resolves it against the engine's live owner and queues
  the exact attempt id, which the engine compares again before acting, so a
  delayed action for attempt A can never touch attempt B (even the same
  user's);
* ``engine_instance`` -- the engine run the administrator saw;
* ``client_request_id`` -- one press, reused on an HTTP retry; the engine
  ``request_id`` is derived from (administrator, client_request_id) and
  executed at most once;
* ``reason`` -- optional free text for the audit trail (bounded).

Django signs the queued command with the server SECRET_KEY over every field
INCLUDING the literal command type and action, so a signature for one
operation can never be replayed as another, and a Remote Mic gate proof can
never pass as an administrator command (different salt and message).
"""

from __future__ import annotations

import hashlib
import re

from django.utils.crypto import constant_time_compare, salted_hmac

from library.services.remote_dj_gate_identity import valid_attempt_id
from library.services.start_next_identity import valid_client_request_id, valid_engine_instance

COMMAND = "remote_dj_admin"
SIGNATURE_SALT = "isadoraair.remote-dj.admin-command.v1"
ACTION_CAPABILITY = {
    "emergency_mute": "remote_dj.emergency_mute",
    "release_mute": "remote_dj.emergency_mute",
    "kick": "remote_dj.force_disconnect",
}
ADMIN_CAPABILITIES = ("remote_dj.emergency_mute", "remote_dj.force_disconnect")
SESSION_REF = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_REASON_LENGTH = 200
MAX_COMMAND_AGE_MS = 5000
MAX_FUTURE_SKEW_MS = 2000

REQUEST_REQUIRED = frozenset({"action", "target_session", "engine_instance", "client_request_id"})
REQUEST_OPTIONAL = frozenset({"reason"})
COMMAND_FIELDS = frozenset({
    "command", "action", "request_id", "admin_user_id", "attempt_id", "target_user_id",
    "engine_instance", "issued_at_ms", "reason", "signature",
})


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def session_ref(attempt_id):
    """The public session reference for one attempt (== the status
    ``browser_key``). Identifies a target; it is not a credential."""
    return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]


def clean_reason(value):
    return " ".join(str(value).split())[:MAX_REASON_LENGTH]


def derive_request_id(admin_user_id, client_request_id):
    digest = hashlib.sha256(f"{COMMAND}:{admin_user_id}:{client_request_id}".encode()).hexdigest()
    return digest[:32]


def parse_request(body):
    """A browser request body -> dict, or ValueError."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    keys = set(body)
    if not REQUEST_REQUIRED <= keys or keys - REQUEST_REQUIRED - REQUEST_OPTIONAL:
        raise ValueError("body must contain exactly action, target_session, engine_instance, "
                         "client_request_id and optionally reason")
    if body["action"] not in ACTION_CAPABILITY:
        raise ValueError("action must be emergency_mute, release_mute or kick")
    if not (isinstance(body["target_session"], str) and SESSION_REF.match(body["target_session"])):
        raise ValueError("target_session is malformed")
    if not valid_engine_instance(body["engine_instance"]):
        raise ValueError("engine_instance is malformed")
    if not valid_client_request_id(body["client_request_id"]):
        raise ValueError("client_request_id is malformed")
    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"reason must be text of at most {MAX_REASON_LENGTH} characters")
    return {**body, "reason": clean_reason(reason)}


def _signature_message(command):
    return "|".join(str(part) for part in (
        command["command"], command["action"], command["request_id"], command["admin_user_id"],
        command["attempt_id"], command["target_user_id"], command["engine_instance"],
        command["issued_at_ms"], hashlib.sha256(command["reason"].encode("utf-8")).hexdigest(),
    ))


def sign(command):
    return salted_hmac(SIGNATURE_SALT, _signature_message(command), algorithm="sha256").hexdigest()


def build_command(*, admin_user_id, request, attempt_id, target_user_id, issued_at_ms):
    command = {
        "command": COMMAND,
        "action": request["action"],
        "request_id": derive_request_id(admin_user_id, request["client_request_id"]),
        "admin_user_id": int(admin_user_id),
        "attempt_id": attempt_id,
        "target_user_id": int(target_user_id),
        "engine_instance": request["engine_instance"],
        "issued_at_ms": int(issued_at_ms),
        "reason": request["reason"],
    }
    command["signature"] = sign(command)
    return command


def parse_command(data):
    """A queued command -> dict, or ValueError (strict field set and types)."""
    if not isinstance(data, dict) or set(data) != COMMAND_FIELDS or data.get("command") != COMMAND:
        raise ValueError("remote_dj_admin command fields are missing or unexpected")
    if data["action"] not in ACTION_CAPABILITY:
        raise ValueError("action is malformed")
    if not (isinstance(data["request_id"], str) and re.fullmatch(r"[0-9a-f]{32}", data["request_id"])):
        raise ValueError("request_id is malformed")
    for field in ("admin_user_id", "target_user_id", "issued_at_ms"):
        if not _positive_int(data[field]):
            raise ValueError(f"{field} is malformed")
    if not valid_attempt_id(data["attempt_id"]):
        raise ValueError("attempt_id is malformed")
    if not valid_engine_instance(data["engine_instance"]):
        raise ValueError("engine_instance is malformed")
    if not isinstance(data["reason"], str) or len(data["reason"]) > MAX_REASON_LENGTH:
        raise ValueError("reason is malformed")
    if not (isinstance(data["signature"], str) and HEX64.match(data["signature"])):
        raise ValueError("signature is malformed")
    return dict(data)


def verify_signature(command):
    return constant_time_compare(command["signature"], sign(command))
