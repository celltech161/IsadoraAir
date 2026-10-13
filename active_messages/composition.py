"""The canonical identity of what one aggregate airing contains.

Order (the legacy OGRemote product behavior, minus filesystem mtimes):

1. urgent messages, then public-address messages;
2. within a kind, oldest activation first;
3. message UUID as the final tie-breaker.

A pinned stinger precedes the components when, and only when, at least one
component is urgent and the policy names a stinger key.

The snapshot is bounded and path-free: message, media and reference
identities with their SHA-256s, producer snapshots for historical
explanation, and the durations D1 can account for. The DIGEST covers only
what determines the audio -- ordered (message, kind, media, sha256) and the
stinger pin -- never the generation, durations, display names or timestamps,
so an unchanged active set keeps its digest (and, in D2A, its already-rendered
aggregate) across replays.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from .models import ABSOLUTE_MAX_ACTIVE_MESSAGES, MessageKind

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


def accounted_seconds(messages, stinger_seconds=None) -> Decimal:
    """The airtime D1 can account for: every component's validated decoded
    duration, plus the stinger's when an urgent component brings it in. (The
    exact RENDERED duration exists only after D2A renders the aggregate; D2A
    must re-check it against the policy limit before the mix may air.)"""
    total = sum((Decimal(message.message_media.decoded_duration_seconds) for message in messages), Decimal(0))
    if stinger_seconds is not None and any(message.kind == MessageKind.URGENT for message in messages):
        total += Decimal(stinger_seconds)
    return total


def build(messages, *, stinger_pin: dict | None, stinger_unavailable: str = "", stinger_seconds=None) -> dict:
    """``messages``: the ACTIVE rows (with ``message_media`` loaded). ``stinger_pin``:
    ``production.services.reference_audio.pin_of`` of the current stinger, or
    None; ``stinger_seconds`` its validated decoded duration. Raises
    CompositionError for an empty or structurally oversized set (the policy
    limits are the services' eligibility check, made before this)."""
    components = ordered(messages)
    if not components:
        raise CompositionError("nothing_active", "an aggregate needs at least one active message")
    if len(components) > ABSOLUTE_MAX_ACTIVE_MESSAGES:      # the structural bound; the policy limit is checked first
        raise CompositionError("too_many_components",
                               f"at most {ABSOLUTE_MAX_ACTIVE_MESSAGES} messages can ever be combined")
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
                "duration_seconds": str(message.message_media.decoded_duration_seconds),
            }
            for message in components
        ],
        "stinger": dict(stinger_pin) if (has_urgent and stinger_pin) else None,
    }
    stinger_used = has_urgent and stinger_pin is not None and stinger_seconds is not None
    snapshot["stinger_seconds"] = str(stinger_seconds) if stinger_used else None
    snapshot["accounted_seconds"] = str(accounted_seconds(components, stinger_seconds if stinger_used else None))
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
