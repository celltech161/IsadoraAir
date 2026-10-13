"""The canonical identity of what one aggregate airing contains.

Order (the legacy OGRemote product behavior, minus filesystem mtimes):

1. urgent messages, then public-address messages;
2. within a kind, oldest activation first;
3. message UUID as the final tie-breaker.

A pinned stinger precedes the components when, and only when, at least one
component is urgent and the policy names a stinger key.

The snapshot is bounded and path-free: message, media and reference
identities with their SHA-256s, plus producer snapshots for historical
explanation. The DIGEST covers only what determines the audio -- ordered
(message, kind, media, sha256) and the stinger pin -- never the generation,
display names or timestamps, so an unchanged active set keeps its digest (and,
in D2A, its already-rendered aggregate) across replays.
"""
from __future__ import annotations

import hashlib
import json

from .models import MAX_COMPONENTS, MessageKind

SNAPSHOT_VERSION = 1
SNAPSHOT_LIMIT_BYTES = 32 * 1024
_KIND_ORDER = {MessageKind.URGENT: 0, MessageKind.PUBLIC_ADDRESS: 1}


class CompositionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value) -> str:
    """Strict canonical JSON: sorted keys, no whitespace, no floats/NaN."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def order_key(message):
    return (_KIND_ORDER[message.kind], message.activated_at, str(message.pk))


def ordered(messages) -> list:
    return sorted(messages, key=order_key)


def _iso(value) -> str:
    return value.isoformat(timespec="microseconds")


def build(messages, *, stinger_pin: dict | None, stinger_unavailable: str = "") -> dict:
    """``messages``: the ACTIVE rows (with ``message_media`` loaded). ``stinger_pin``:
    ``production.services.reference_audio.pin_of`` of the current stinger, or
    None. Raises CompositionError for an empty or oversized set."""
    components = ordered(messages)
    if not components:
        raise CompositionError("nothing_active", "an aggregate needs at least one active message")
    if len(components) > MAX_COMPONENTS:
        raise CompositionError("too_many_components", f"at most {MAX_COMPONENTS} messages can be active together")
    has_urgent = any(message.kind == MessageKind.URGENT for message in components)
    snapshot = {
        "v": SNAPSHOT_VERSION,
        "components": [
            {
                "message": str(message.pk),
                "kind": message.kind,
                "media": str(message.message_media.pk),
                "sha256": message.message_media.sha256,
                "producer_key": message.producer_key,
                "producer_username": message.producer_username,
                "activated_at": _iso(message.activated_at),
            }
            for message in components
        ],
        "stinger": dict(stinger_pin) if (has_urgent and stinger_pin) else None,
    }
    if has_urgent and stinger_unavailable:
        snapshot["stinger_unavailable"] = stinger_unavailable
    if len(canonical_json(snapshot).encode()) > SNAPSHOT_LIMIT_BYTES:
        raise CompositionError("snapshot_too_large", "the composition snapshot exceeds its bound")
    return snapshot


def identity(snapshot: dict) -> dict:
    """The audio-determining part of a snapshot."""
    return {
        "v": snapshot["v"],
        "components": [{key: component[key] for key in ("message", "kind", "media", "sha256")}
                       for component in snapshot["components"]],
        "stinger": snapshot["stinger"],
    }


def digest(snapshot: dict) -> str:
    return hashlib.sha256(canonical_json(identity(snapshot)).encode()).hexdigest()


def message_ids(snapshot: dict) -> list[str]:
    return [component["message"] for component in snapshot["components"]]
