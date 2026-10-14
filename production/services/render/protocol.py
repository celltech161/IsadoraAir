"""The isadoraair-render wire protocol (version 1) -- shared by the service
(production.services.render_service) and its client (render.client).

One connection carries ONE request and ONE answer, each a single JSON line.
There is exactly one operation, ``render``: a code-registered recipe applied to
media the client hands over as OPEN, READ-ONLY descriptors (SCM_RIGHTS). No
command, executable, option, filter, path or URL is ever part of a request;
the service builds every tool invocation itself from the fixed step library.

Request (client -> service), with ``1 + len(references)`` descriptors in the
order source, then references sorted by parameter name::

    {"v": 1, "op": "render", "request_id": "<32 hex>",
     "correlation": {"item": "<uuid>" | null, "lease": "<uuid>" | null},
     "source": {"media": "<uuid>", "sha256": "<64 hex>", "bytes": n,
                "duration_seconds": "<decimal>"},
     "recipe": {"key": "<recipe key>", "version": n},
     "params": {...},                       # the type's raw recipe parameters
     "references": {"<param>": {"key": "<reference key>", "reference": "<uuid>",
                                "media": "<uuid>", "sha256": "<64 hex>",
                                "bytes": n, "duration_seconds": "<decimal>"}},
     "expected_digest": "<64 hex>",         # the client's canonical expectation
     "timeout": seconds}

A SECOND, separate operation, ``assemble`` (iPortal Phase D2A), sequentially
joins a bounded, ORDERED list of inputs -- an optional station stinger, then
producer messages -- with ``len(inputs)`` descriptors in input order::

    {"v": 1, "op": "assemble", "request_id": "<32 hex>",
     "correlation": {"execution": "<uuid>"},
     "recipe": {"key": "active_message_mix", "version": n},
     "inputs": [{"role": "stinger", "key": "<reference key>", "reference": "<uuid>",
                 "media": ..., "sha256": ..., "bytes": n, "duration_seconds": ...},
                {"role": "message", "message": "<uuid>", "kind": "urgent" | "public_address",
                 "media": ..., "sha256": ..., "bytes": n, "duration_seconds": ...}, ...],
     "expected_digest": "<64 hex>", "timeout": seconds}

At most one stinger, only first and only ahead of urgent material; 1..24
messages, urgent before public address, each message once; bounded total
bytes and seconds. It has no parameters at all: the assembly is code-defined
(render.provenance / render.executor). Its answer has the same shape as
``render``'s.

Answer (service -> client)::

    {"v": 1, "status": "ok", "request_id": ..., "recipe": {"key", "version"},
     "digest": "<64 hex>", "provenance": {...}, "toolchain": "...",
     "measured_input": {...}, "output": {"sha256": "<64 hex>", "bytes": n},
     "executor": {"service": "isadoraair-render", "protocol": 1}}

with exactly ONE descriptor: a SEALED memfd holding the FLAC (its bytes can
no longer change: F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL),
or ``{"v": 1, "status": "error" | "busy", "code": ..., "retryable": bool,
"stage": ...}`` with no descriptor. Error answers carry stable codes only --
never tool output or paths (those go to the service's own log, bounded).
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal, InvalidOperation

PROTOCOL_VERSION = 1
SERVICE_NAME = "isadoraair-render"
EXECUTOR_MARKER = f"executor {SERVICE_NAME}/{PROTOCOL_VERSION}"
REQUEST_LIMIT_BYTES = 16 * 1024
REPLY_LIMIT_BYTES = 64 * 1024
MAX_REFERENCES = 4
# assemble: at most this many messages plus one stinger (the active-message
# domain's absolute ceilings -- active_messages.models.ABSOLUTE_MAX_*; a test
# keeps the two in step: production never imports a domain).
MAX_ASSEMBLE_MESSAGES = 24
MAX_ASSEMBLE_INPUTS = MAX_ASSEMBLE_MESSAGES + 1
MAX_ASSEMBLE_INPUT_SECONDS = Decimal(1200)
MAX_ASSEMBLE_TOTAL_SECONDS = Decimal(1200)
MAX_FDS = max(1 + MAX_REFERENCES, MAX_ASSEMBLE_INPUTS)
MAX_TIMEOUT_SECONDS = 3600.0

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REQUEST_KEYS = {"v", "op", "request_id", "correlation", "source", "recipe", "params", "references",
                 "expected_digest", "timeout"}
_MEDIA_KEYS = {"media", "sha256", "bytes", "duration_seconds"}
_REFERENCE_KEYS = _MEDIA_KEYS | {"key", "reference"}
_ASSEMBLE_KEYS = {"v", "op", "request_id", "correlation", "recipe", "inputs", "expected_digest", "timeout"}
_STINGER_KEYS = _MEDIA_KEYS | {"role", "key", "reference"}
_MESSAGE_KEYS = _MEDIA_KEYS | {"role", "message", "kind"}
MESSAGE_KINDS = ("urgent", "public_address")       # in airing order

# Stable failure codes. retryable: the inputs are fine; a later attempt may work.
CODES = {
    "invalid_request": False, "unknown_recipe": False, "recipe_version_mismatch": False,
    "invalid_parameters": False, "reference_mismatch": False, "descriptor_invalid": False,
    "descriptor_mismatch": False, "provenance_mismatch": False, "source_too_long": False, "input_too_large": False,
    "empty_voice": False, "silent_render": False, "loudness_analysis_failed": False, "render_failed": False,
    "render_resource_limit": False, "input_order": False, "decoded_too_long": False,
    "busy": True, "render_timeout": True, "render_unavailable": True, "sandbox_unavailable": True,
    "service_unavailable": True, "cancelled": True,
}


class ProtocolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _uuid_text(value, what) -> str:
    if not isinstance(value, str):
        raise ProtocolError("invalid_request", f"{what} must be a UUID string")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ProtocolError("invalid_request", f"{what} must be a UUID string") from None


def _decimal(value, what, *, maximum) -> Decimal:
    if not isinstance(value, str) or len(value) > 32:
        raise ProtocolError("invalid_request", f"{what} must be a decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ProtocolError("invalid_request", f"{what} must be a decimal string") from None
    if not number.is_finite() or not Decimal(0) < number <= maximum:
        raise ProtocolError("invalid_request", f"{what} is out of range")
    return number


def _count(value, what, *, maximum) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ProtocolError("invalid_request", f"{what} is out of range")
    return value


def _media(entry, what, keys, *, max_bytes, max_seconds) -> dict:
    if not isinstance(entry, dict) or set(entry) != keys:
        raise ProtocolError("invalid_request", f"{what} has the wrong shape")
    if not isinstance(entry["sha256"], str) or not _HEX64.fullmatch(entry["sha256"]):
        raise ProtocolError("invalid_request", f"{what}.sha256 must be 64 lowercase hex digits")
    return {
        "media": _uuid_text(entry["media"], f"{what}.media"), "sha256": entry["sha256"],
        "bytes": _count(entry["bytes"], f"{what}.bytes", maximum=max_bytes),
        "duration_seconds": _decimal(entry["duration_seconds"], f"{what}.duration_seconds", maximum=max_seconds),
    }


def parse_request(request, *, max_bytes, max_seconds) -> dict:
    """Shape-check a request (not yet its recipe semantics). Returns a
    normalized copy; raises ProtocolError("invalid_request")."""
    if not isinstance(request, dict) or set(request) != _REQUEST_KEYS:
        raise ProtocolError("invalid_request", "the request has the wrong shape")
    if request["v"] != PROTOCOL_VERSION or request["op"] != "render":
        raise ProtocolError("invalid_request", "unsupported protocol version or operation")
    if not isinstance(request["request_id"], str) or not _HEX32.fullmatch(request["request_id"]):
        raise ProtocolError("invalid_request", "request_id must be 32 lowercase hex digits")
    correlation = request["correlation"]
    if not isinstance(correlation, dict) or set(correlation) != {"item", "lease"}:
        raise ProtocolError("invalid_request", "correlation has the wrong shape")
    recipe = request["recipe"]
    if not isinstance(recipe, dict) or set(recipe) != {"key", "version"} or not isinstance(recipe["key"], str) \
            or not 0 < len(recipe["key"]) <= 64:
        raise ProtocolError("invalid_request", "recipe has the wrong shape")
    if not isinstance(request["params"], dict):
        raise ProtocolError("invalid_request", "params must be an object")
    references = request["references"]
    if not isinstance(references, dict) or len(references) > MAX_REFERENCES \
            or not all(isinstance(name, str) and _PARAM.fullmatch(name) for name in references):
        raise ProtocolError("invalid_request", "references has the wrong shape")
    if not isinstance(request["expected_digest"], str) or not _HEX64.fullmatch(request["expected_digest"]):
        raise ProtocolError("invalid_request", "expected_digest must be 64 lowercase hex digits")
    timeout = request["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not 0 < float(timeout) <= MAX_TIMEOUT_SECONDS:
        raise ProtocolError("invalid_request", "timeout is out of range")
    normalized_references = {}
    for name in sorted(references):
        entry = references[name]
        media = _media(entry, f"references.{name}", _REFERENCE_KEYS, max_bytes=max_bytes, max_seconds=max_seconds)
        if not isinstance(entry["key"], str) or not 0 < len(entry["key"]) <= 64:
            raise ProtocolError("invalid_request", f"references.{name}.key has the wrong shape")
        media.update(key=entry["key"], reference=_uuid_text(entry["reference"], f"references.{name}.reference"))
        normalized_references[name] = media
    return {
        "request_id": request["request_id"],
        "correlation": {name: (None if correlation[name] is None else _uuid_text(correlation[name], name))
                        for name in ("item", "lease")},
        "source": _media(request["source"], "source", _MEDIA_KEYS, max_bytes=max_bytes, max_seconds=max_seconds),
        "recipe_key": recipe["key"], "recipe_version": recipe["version"], "params": request["params"],
        "references": normalized_references, "expected_digest": request["expected_digest"],
        "timeout": float(timeout),
    }


def _common(request, keys, op):
    if not isinstance(request, dict) or set(request) != keys:
        raise ProtocolError("invalid_request", "the request has the wrong shape")
    if request["v"] != PROTOCOL_VERSION or request["op"] != op:
        raise ProtocolError("invalid_request", "unsupported protocol version or operation")
    if not isinstance(request["request_id"], str) or not _HEX32.fullmatch(request["request_id"]):
        raise ProtocolError("invalid_request", "request_id must be 32 lowercase hex digits")
    recipe = request["recipe"]
    if not isinstance(recipe, dict) or set(recipe) != {"key", "version"} or not isinstance(recipe["key"], str) \
            or not 0 < len(recipe["key"]) <= 64:
        raise ProtocolError("invalid_request", "recipe has the wrong shape")
    if not isinstance(request["expected_digest"], str) or not _HEX64.fullmatch(request["expected_digest"]):
        raise ProtocolError("invalid_request", "expected_digest must be 64 lowercase hex digits")
    timeout = request["timeout"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or not 0 < float(timeout) <= MAX_TIMEOUT_SECONDS:
        raise ProtocolError("invalid_request", "timeout is out of range")


def operation_of(request) -> str:
    """"render" or "assemble" (anything else is "render", whose parser refuses it)."""
    return "assemble" if isinstance(request, dict) and request.get("op") == "assemble" else "render"


def parse_assemble(request, *, max_bytes) -> dict:
    """Shape-check an ``assemble`` request, INCLUDING its ordering rules.
    Returns a normalized copy; raises ProtocolError (invalid_request or
    input_order)."""
    _common(request, _ASSEMBLE_KEYS, "assemble")
    correlation = request["correlation"]
    if not isinstance(correlation, dict) or set(correlation) != {"execution"}:
        raise ProtocolError("invalid_request", "correlation has the wrong shape")
    inputs = request["inputs"]
    if not isinstance(inputs, list) or not 0 < len(inputs) <= MAX_ASSEMBLE_INPUTS:
        raise ProtocolError("invalid_request", "inputs must be a list of 1..25 entries")
    normalized, messages, kinds, seen = [], 0, [], set()
    for index, entry in enumerate(inputs):
        what = f"inputs[{index}]"
        role = entry.get("role") if isinstance(entry, dict) else None
        if role == "stinger":
            media = _media(entry, what, _STINGER_KEYS, max_bytes=max_bytes, max_seconds=MAX_ASSEMBLE_INPUT_SECONDS)
            if not isinstance(entry["key"], str) or not 0 < len(entry["key"]) <= 64:
                raise ProtocolError("invalid_request", f"{what}.key has the wrong shape")
            if index != 0:
                raise ProtocolError("input_order", "the stinger is the first input, once")
            media.update(role="stinger", key=entry["key"], reference=_uuid_text(entry["reference"], f"{what}.reference"))
        elif role == "message":
            media = _media(entry, what, _MESSAGE_KEYS, max_bytes=max_bytes, max_seconds=MAX_ASSEMBLE_INPUT_SECONDS)
            if entry["kind"] not in MESSAGE_KINDS:
                raise ProtocolError("invalid_request", f"{what}.kind is urgent or public_address")
            message = _uuid_text(entry["message"], f"{what}.message")
            if message in seen:
                raise ProtocolError("input_order", "a message appears once")
            seen.add(message)
            media.update(role="message", message=message, kind=entry["kind"])
            kinds.append(MESSAGE_KINDS.index(entry["kind"]))
            messages += 1
        else:
            raise ProtocolError("invalid_request", f"{what}.role is stinger or message")
        normalized.append(media)
    if not 0 < messages <= MAX_ASSEMBLE_MESSAGES:
        raise ProtocolError("invalid_request", "an assembly holds 1..24 messages")
    if kinds != sorted(kinds):
        raise ProtocolError("input_order", "urgent messages precede public-address messages")
    if normalized[0]["role"] == "stinger" and kinds[0] != 0:
        raise ProtocolError("input_order", "a stinger precedes urgent material only")
    if sum(item["duration_seconds"] for item in normalized) > MAX_ASSEMBLE_TOTAL_SECONDS:
        raise ProtocolError("invalid_request", "the inputs are longer than one assembly may be")
    return {
        "request_id": request["request_id"],
        "correlation": {"execution": _uuid_text(correlation["execution"], "correlation.execution")},
        "recipe_key": request["recipe"]["key"], "recipe_version": request["recipe"]["version"],
        "inputs": normalized, "expected_digest": request["expected_digest"], "timeout": float(request["timeout"]),
    }


def assembly_input_of(entry: dict) -> dict:
    """An input as assembly provenance records it: identity and exact bytes only."""
    if entry["role"] == "stinger":
        return {"role": "stinger", "key": entry["key"], "reference": entry["reference"], "media": entry["media"],
                "sha256": entry["sha256"]}
    return {"role": "message", "message": entry["message"], "kind": entry["kind"], "media": entry["media"],
            "sha256": entry["sha256"]}


def pin_of(entry: dict) -> dict:
    """A reference entry as render provenance records it (reference_audio.pin_of)."""
    return {"key": entry["key"], "reference": entry["reference"], "media": entry["media"], "sha256": entry["sha256"]}


def error(code: str, *, stage: str = "") -> dict:
    return {"v": PROTOCOL_VERSION, "status": "busy" if code == "busy" else "error", "code": code,
            "retryable": CODES.get(code, False), "stage": stage}
