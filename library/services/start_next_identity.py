"""Roadmap 2.23 Start Next -- the command's mandatory identity (r0117 review
SN-AUTH-1), shared by the Django endpoint and the engine command boundary so
both enforce exactly the same contract.

A Start Next request is bound to:

* ``client_request_id`` -- the browser's identity for ONE operator press,
  reused verbatim on an HTTP retry. The server derives the engine
  ``request_id`` from (user, client_request_id), so a retried POST queues the
  SAME request id and the engine executes it at most once;
* ``engine_instance`` -- the engine run the operator saw (state_revision /
  engine_session_id); any other instance is refused;
* ``expected.live_decks`` -- the exact live deck occurrences the operator saw:
  one ``{"slot", "log_item_id", "generation"}`` per live deck, or an explicit
  ``[]`` for an intentionally stopped console. Absence is never a wildcard.

Every field is mandatory; anything missing or malformed is refused before a
transport change.
"""

from __future__ import annotations

import hashlib
import re

CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_ENGINE_INSTANCE_LENGTH = 64
SLOTS = ("A", "B")


def valid_client_request_id(value):
    return isinstance(value, str) and bool(CLIENT_REQUEST_ID.match(value))


def valid_engine_instance(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_ENGINE_INSTANCE_LENGTH and value.strip() == value


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def parse_expected_live_decks(expected):
    """``{"live_decks": [{"slot", "log_item_id", "generation"}, ...]}`` ->
    a sorted tuple of (slot, log_item_id, generation). Raises ValueError for
    anything missing, extra, duplicated or ill-typed."""
    if not isinstance(expected, dict) or set(expected) != {"live_decks"}:
        raise ValueError("expected must be an object with exactly 'live_decks'")
    decks = expected["live_decks"]
    if not isinstance(decks, list) or len(decks) > len(SLOTS):
        raise ValueError("expected.live_decks must be a list of at most two decks")
    parsed = []
    for deck in decks:
        if not isinstance(deck, dict) or set(deck) != {"slot", "log_item_id", "generation"}:
            raise ValueError("each live deck needs exactly slot, log_item_id and generation")
        if deck["slot"] not in SLOTS or not _positive_int(deck["log_item_id"]) \
                or not _positive_int(deck["generation"]):
            raise ValueError("live deck identity is malformed")
        parsed.append((deck["slot"], deck["log_item_id"], deck["generation"]))
    if len({slot for slot, _item, _generation in parsed}) != len(parsed):
        raise ValueError("a slot appears twice in expected.live_decks")
    return tuple(sorted(parsed))


def live_decks_payload(parsed):
    return {"live_decks": [{"slot": slot, "log_item_id": item, "generation": generation}
                           for slot, item, generation in parsed]}


def derive_request_id(user_id, client_request_id):
    """The engine request id for one operator press: stable across HTTP
    retries of the same client_request_id by the same user."""
    digest = hashlib.sha256(f"start_next:{user_id}:{client_request_id}".encode()).hexdigest()
    return digest[:32]
