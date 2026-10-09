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
MAX_FDS = 1 + MAX_REFERENCES
MAX_TIMEOUT_SECONDS = 3600.0

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REQUEST_KEYS = {"v", "op", "request_id", "correlation", "source", "recipe", "params", "references",
                 "expected_digest", "timeout"}
_MEDIA_KEYS = {"media", "sha256", "bytes", "duration_seconds"}
_REFERENCE_KEYS = _MEDIA_KEYS | {"key", "reference"}

# Stable failure codes. retryable: the inputs are fine; a later attempt may work.
CODES = {
    "invalid_request": False, "unknown_recipe": False, "recipe_version_mismatch": False,
    "invalid_parameters": False, "reference_mismatch": False, "descriptor_invalid": False,
    "descriptor_mismatch": False, "provenance_mismatch": False, "source_too_long": False,
    "empty_voice": False, "silent_render": False, "loudness_analysis_failed": False, "render_failed": False,
    "render_resource_limit": False,
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


def pin_of(entry: dict) -> dict:
    """A reference entry as render provenance records it (reference_audio.pin_of)."""
    return {"key": entry["key"], "reference": entry["reference"], "media": entry["media"], "sha256": entry["sha256"]}


def error(code: str, *, stage: str = "") -> dict:
    return {"v": PROTOCOL_VERSION, "status": "busy" if code == "busy" else "error", "code": code,
            "retryable": CODES.get(code, False), "stage": stage}
