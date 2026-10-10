"""The fixed spoken-item transition table -- shared by the transition service
(which plans transitions) and the write guard (which refuses any write
authorization the table does not allow).

Each rule names its event and the protected item columns (attnames) that
transition may write, besides ``state``/``state_changed_at`` (always) and
``terminal_at`` (when entering a terminal state). C2 rules deliver to, or
remove from, the library: only the projection service (spoken.delivery)
performs them, and the guard executes one only with a PROJECTION WITNESS it
re-checks against the locked database (spoken.guard.ProjectedItemWrite) --
so no hand-built authorization can deliver an item that has no active
edition, or recall one whose content is still live.

``repromoted`` (superseded -> delivered) stays in the table as an identity
but is NOT performable in C2B: nothing ever resurrects a superseded edition
automatically (C2B_PERFORMABLE excludes it).
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import TERMINAL_STATES, ItemState

S = ItemState


@dataclass(frozen=True)
class Rule:
    event: str
    c2: bool = False                     # needs the library-projection service (C2)
    columns: frozenset = frozenset()     # protected attnames this transition may write


ALWAYS = frozenset({"state", "state_changed_at"})
TERMINAL = frozenset({"terminal_at"})
_LEASE = frozenset({"lease_token", "lease_expires_at"})
_FAILURE = frozenset({"failure_code", "failure_detail"})
_RECALL = frozenset({"recalled_by_id", "recalled_by_username", "recalled_at", "recall_reason"})
_DELIVERY = frozenset({"delivered_at", "expires_at"})
_REVIEW = frozenset({"reviewed_by_id", "reviewed_by_username", "reviewed_at", "review_note"})

# What item creation may set (everything else must hold the model default).
CREATE_COLUMNS = frozenset({"content_type_id", "origin", "submitted_by_id", "submitted_by_username", "title",
                            "notes", "state", "state_changed_at"})
CREATE_ORIGINS = frozenset({"submitted", "assigned"})

TRANSITIONS: dict[tuple, Rule] = {
    (None, S.DRAFT): Rule("created"),
    (S.DRAFT, S.SUBMITTED): Rule("submitted", columns=frozenset({"submission_media_id", "submitted_at"})),
    (S.SUBMITTED, S.VALIDATED): Rule("validated", columns=frozenset({"validated_at"})),
    (S.SUBMITTED, S.INVALID): Rule("invalid", columns=_FAILURE),
    (S.VALIDATED, S.PROCESSING): Rule("processing_started", columns=_LEASE | _FAILURE | frozenset(
        {"processing_attempts", "processing_started_at"})),
    (S.PROCESSING, S.PROCESSING): Rule("processing_reclaimed", columns=_LEASE | _FAILURE | frozenset(
        {"processing_attempts", "processing_started_at"})),
    (S.PROCESSING, S.PROCESSED): Rule("processed", columns=_LEASE | frozenset({"rendition_media_id",
                                                                               "processed_at"})),
    (S.PROCESSING, S.FAILED): Rule("failed", columns=_LEASE | _FAILURE),
    (S.FAILED, S.VALIDATED): Rule("retried", columns=_FAILURE),
    (S.PROCESSED, S.AWAITING_REVIEW): Rule("review_requested"),
    (S.AWAITING_REVIEW, S.REJECTED): Rule("rejected", columns=frozenset(
        {"reviewed_by_id", "reviewed_by_username", "reviewed_at", "review_note"})),
    # recall before delivery (no library effect)
    (S.DRAFT, S.RECALLED): Rule("recalled", columns=_RECALL),
    (S.SUBMITTED, S.RECALLED): Rule("recalled", columns=_RECALL),
    (S.VALIDATED, S.RECALLED): Rule("recalled", columns=_RECALL),
    (S.PROCESSED, S.RECALLED): Rule("recalled", columns=_RECALL),
    (S.AWAITING_REVIEW, S.RECALLED): Rule("recalled", columns=_RECALL),
    (S.FAILED, S.RECALLED): Rule("recalled", columns=_RECALL),
    # library projection -- C2 (performed only by spoken.delivery, witnessed)
    (S.PROCESSED, S.DELIVERED): Rule("delivered", c2=True, columns=_DELIVERY),
    (S.AWAITING_REVIEW, S.DELIVERED): Rule("approved_delivered", c2=True, columns=_DELIVERY | _REVIEW),
    (S.DELIVERED, S.SUPERSEDED): Rule("superseded", c2=True),
    (S.SUPERSEDED, S.DELIVERED): Rule("repromoted", c2=True),
    (S.DELIVERED, S.RECALLED): Rule("recalled", c2=True, columns=_RECALL),
    (S.DELIVERED, S.EXPIRED): Rule("expired", c2=True),
    # C2B: a superseded item can still be recalled or expire -- with NO
    # projection effect (it carries nothing; it can never touch the newer
    # edition that superseded it).
    (S.SUPERSEDED, S.RECALLED): Rule("recalled", c2=True, columns=_RECALL),
    (S.SUPERSEDED, S.EXPIRED): Rule("expired", c2=True),
}

# The C2 transitions the C2B projection service performs (and the guard
# executes). ``repromoted`` is deliberately absent: no automatic re-promotion.
C2B_PERFORMABLE = frozenset({
    (S.PROCESSED, S.DELIVERED), (S.AWAITING_REVIEW, S.DELIVERED), (S.DELIVERED, S.SUPERSEDED),
    (S.DELIVERED, S.RECALLED), (S.DELIVERED, S.EXPIRED), (S.SUPERSEDED, S.RECALLED), (S.SUPERSEDED, S.EXPIRED),
})


def writable_columns(from_state, to_state) -> frozenset:
    """Every protected attname the transition may write."""
    rule = TRANSITIONS[(from_state, to_state)]
    return ALWAYS | rule.columns | (TERMINAL if to_state in TERMINAL_STATES else frozenset())
