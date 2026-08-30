"""D2-G: the activation phase state machine. Not yet connected to the
real Update Center job executor (that is D3's job -- see this
package's own supervisor.py module docstring for the exact boundary).

The essential semantic boundary this whole phase list exists to
enforce: CANDIDATE_READY is not COMMITTED. A candidate worker being
confirmed alive, correctly identified, and ready is NOT the same fact
as "runtime activation accepted for production mutation" -- D3 will
wire a real job milestone (`runtime_activation_accepted`) that only
fires once the supervisor has reached COMMITTED, and only THEN does
D3's own executor allow database/source/systemd mutation to proceed.
This module enforces that COMMITTED is reachable only through
CANDIDATE_READY, never skipped, and that every phase transition is a
real, validated, forward-only step -- never an arbitrary phase
assignment."""
from __future__ import annotations

import enum


class ActivationPhase(enum.Enum):
    IDLE = "idle"
    CANDIDATE_STAGED = "candidate_staged"
    CANDIDATE_VERIFIED = "candidate_verified"
    ACTIVATION_REQUESTED = "activation_requested"
    CANDIDATE_STARTING = "candidate_starting"
    CANDIDATE_READY = "candidate_ready"
    COMMITTED = "committed"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


# Forward-only. Every phase reachable from IDLE via exactly one legal
# path to COMMITTED; FAILED is reachable from any non-terminal phase
# (a failure can occur at any step); ROLLBACK_REQUESTED is reachable
# only from the two phases where a running candidate process could
# exist to roll back FROM (CANDIDATE_STARTING, CANDIDATE_READY) --
# see D2-K's own "pre-mutation rollback" scope: rollback is legal only
# BEFORE COMMITTED, never after (COMMITTED has no outgoing edges here
# at all -- a committed generation's own future replacement starts a
# brand new transaction from IDLE, it is never "rolled back" as this
# same transaction).
ALLOWED_TRANSITIONS: dict[ActivationPhase, frozenset[ActivationPhase]] = {
    ActivationPhase.IDLE: frozenset({ActivationPhase.CANDIDATE_STAGED}),
    ActivationPhase.CANDIDATE_STAGED: frozenset({ActivationPhase.CANDIDATE_VERIFIED, ActivationPhase.FAILED}),
    ActivationPhase.CANDIDATE_VERIFIED: frozenset({ActivationPhase.ACTIVATION_REQUESTED, ActivationPhase.FAILED}),
    ActivationPhase.ACTIVATION_REQUESTED: frozenset({ActivationPhase.CANDIDATE_STARTING, ActivationPhase.FAILED}),
    ActivationPhase.CANDIDATE_STARTING: frozenset({
        ActivationPhase.CANDIDATE_READY, ActivationPhase.ROLLBACK_REQUESTED, ActivationPhase.FAILED,
    }),
    ActivationPhase.CANDIDATE_READY: frozenset({
        ActivationPhase.COMMITTED, ActivationPhase.ROLLBACK_REQUESTED, ActivationPhase.FAILED,
    }),
    ActivationPhase.COMMITTED: frozenset(),
    ActivationPhase.ROLLBACK_REQUESTED: frozenset({ActivationPhase.ROLLED_BACK, ActivationPhase.FAILED}),
    ActivationPhase.ROLLED_BACK: frozenset(),
    ActivationPhase.FAILED: frozenset(),
}

TERMINAL_PHASES = frozenset({ActivationPhase.COMMITTED, ActivationPhase.ROLLED_BACK, ActivationPhase.FAILED})

# The phases during which a candidate worker process may legitimately
# be running (used by supervisor.py's rollback decision -- see D2-K).
PHASES_WITH_A_LIVE_CANDIDATE_PROCESS = frozenset({
    ActivationPhase.CANDIDATE_STARTING, ActivationPhase.CANDIDATE_READY,
})

PRE_MUTATION_PHASES = frozenset(ActivationPhase) - {ActivationPhase.COMMITTED}


class ActivationTransitionError(ValueError):
    pass


def validate_transition(current: ActivationPhase, next_phase: ActivationPhase) -> None:
    if next_phase not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise ActivationTransitionError(f"illegal transition {current.value!r} -> {next_phase.value!r}")


def is_terminal(phase: ActivationPhase) -> bool:
    return phase in TERMINAL_PHASES


def runtime_activation_accepted(phase: ActivationPhase) -> bool:
    """The exact boundary D3 will gate production mutation on. False
    for every phase except COMMITTED -- explicitly including
    CANDIDATE_READY, which a careless caller might otherwise mistake
    for "good enough."""
    return phase is ActivationPhase.COMMITTED
