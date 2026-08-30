"""D2-O: the supervisor orchestrator -- and the explicit audit boundary
of everything this package must NOT contain. It does not, anywhere in
this package, perform: release-manifest chain planning, migrations,
pg_dump/checkpoint logic, application Git checkout advancement,
collectstatic, generic systemd unit management, arbitrary unit policy
interpretation beyond ENABLE_NOW/INSTALL_ONLY string comparison it
never itself decides the meaning of, nginx management, repository-
defined commands, hooks, shell execution, Django, or station feature
policy. Every one of those belongs to the replaceable worker
(deploy/updater_runtime/isadoraair_updater/**), never here. This
module's own responsibilities, and only these: load immutable trust/
bootstrap config, validate A/B slots, verify a signed candidate,
maintain activation state, launch/stop the worker, verify readiness
identity, switch A/B, safe pre-mutation rollback, recovery after an
interrupted activation.

D2-G's essential boundary lives here too: commit_transaction() is the
ONLY function in this package that ever changes RuntimeState.
active_slot/active_generation -- nothing about candidate staging,
verification, launch, or readiness alone ever does. See
activation.runtime_activation_accepted()."""
from __future__ import annotations

import dataclasses
import enum
import uuid

from .activation import ActivationPhase, PHASES_WITH_A_LIVE_CANDIDATE_PROCESS, validate_transition
from .state import ActivationTransaction, RuntimeState


class SupervisorError(RuntimeError):
    pass


def _new_transaction_id() -> str:
    return str(uuid.uuid4())


def begin_transaction(state: RuntimeState, *, candidate_slot, candidate_generation: int,
                      candidate_descriptor_sha256: str) -> RuntimeState:
    """IDLE -> CANDIDATE_STAGED. Refuses if a transaction is already in
    flight (activation is not None) or if candidate_slot is the
    currently active slot -- the active slot is never a candidate,
    enforced here at the state level in addition to slots.publish_slot's
    own filesystem-level refusal."""
    if state.activation is not None:
        raise SupervisorError("a transaction is already in flight")
    validate_transition(ActivationPhase.IDLE, ActivationPhase.CANDIDATE_STAGED)
    if candidate_slot is state.active_slot:
        raise SupervisorError("candidate_slot must not be the active slot")
    transaction = ActivationTransaction(
        transaction_id=_new_transaction_id(), candidate_slot=candidate_slot,
        candidate_generation=candidate_generation, candidate_descriptor_sha256=candidate_descriptor_sha256,
        phase=ActivationPhase.CANDIDATE_STAGED,
    )
    return dataclasses.replace(state, activation=transaction)


def _advance(state: RuntimeState, next_phase: ActivationPhase) -> RuntimeState:
    if state.activation is None:
        raise SupervisorError("no transaction is in flight")
    validate_transition(state.activation.phase, next_phase)
    return dataclasses.replace(state, activation=dataclasses.replace(state.activation, phase=next_phase))


def mark_candidate_verified(state: RuntimeState) -> RuntimeState:
    return _advance(state, ActivationPhase.CANDIDATE_VERIFIED)


def request_activation(state: RuntimeState) -> RuntimeState:
    """Called only AFTER the supervisor's own verify_candidate_bundle()
    (verification.py) has independently proven the candidate -- never
    merely because a worker asked. See protocol.py's own docstring:
    REQUEST_ACTIVATION identifies intent, it is never itself
    authorization."""
    return _advance(state, ActivationPhase.ACTIVATION_REQUESTED)


def mark_candidate_starting(state: RuntimeState) -> RuntimeState:
    return _advance(state, ActivationPhase.CANDIDATE_STARTING)


def mark_candidate_ready(state: RuntimeState) -> RuntimeState:
    return _advance(state, ActivationPhase.CANDIDATE_READY)


def commit_transaction(state: RuntimeState) -> RuntimeState:
    """CANDIDATE_READY -> COMMITTED, folded into a SINGLE returned
    state (COMMITTED is never itself durably persisted as a distinct
    on-disk phase -- see this module's own top docstring): the
    candidate becomes active_slot/active_generation, the former active
    becomes previous_slot/previous_generation (the new LKG boundary --
    see D2-M's own retention rule: the previous LKG is never deleted
    until THIS commit has fully landed), and activation is cleared back
    to None (idle). This is THE ONE function in this whole package that
    ever moves RuntimeState.active_slot/active_generation -- see
    activation.runtime_activation_accepted(), which is true if and only
    if a transaction reached exactly this phase."""
    if state.activation is None:
        raise SupervisorError("no transaction is in flight")
    validate_transition(state.activation.phase, ActivationPhase.COMMITTED)
    transaction = state.activation
    return dataclasses.replace(
        state,
        active_slot=transaction.candidate_slot,
        active_generation=transaction.candidate_generation,
        active_descriptor_sha256=transaction.candidate_descriptor_sha256,
        previous_slot=state.active_slot,
        previous_generation=state.active_generation,
        previous_descriptor_sha256=state.active_descriptor_sha256,
        activation=None,
    )


def request_rollback(state: RuntimeState, *, reason: str) -> RuntimeState:  # noqa: ARG001 -- reason is for the caller's own log line, not stored in state
    """D2-K: legal only from the phases where a candidate process could
    actually be alive (CANDIDATE_STARTING, CANDIDATE_READY) -- a
    transaction that never got that far simply fails closed via
    fail_transaction() instead, since there is nothing running to roll
    back FROM."""
    if state.activation is None:
        raise SupervisorError("no transaction is in flight")
    if state.activation.phase not in PHASES_WITH_A_LIVE_CANDIDATE_PROCESS:
        raise SupervisorError(f"cannot roll back from phase {state.activation.phase.value!r}")
    return _advance(state, ActivationPhase.ROLLBACK_REQUESTED)


def finish_rollback(state: RuntimeState) -> RuntimeState:
    """ROLLBACK_REQUESTED -> ROLLED_BACK, folded immediately to IDLE --
    active_slot/active_generation/previous_* are UNTOUCHED (rollback is
    always pre-mutation: the active slot was never overwritten in place
    to begin with, so there is nothing to restore beyond clearing the
    now-abandoned transaction record)."""
    if state.activation is None:
        raise SupervisorError("no transaction is in flight")
    validate_transition(state.activation.phase, ActivationPhase.ROLLED_BACK)
    return dataclasses.replace(state, activation=None)


def fail_transaction(state: RuntimeState) -> RuntimeState:
    """Any non-terminal phase -> FAILED, folded immediately to IDLE.
    Same "untouched active/previous" property as finish_rollback --
    FAILED is reachable from every non-terminal phase specifically
    because failure can occur at any step, and every one of those steps
    is still pre-mutation."""
    if state.activation is None:
        raise SupervisorError("no transaction is in flight")
    validate_transition(state.activation.phase, ActivationPhase.FAILED)
    return dataclasses.replace(state, activation=None)


class RecoveryAction(enum.Enum):
    NO_ACTION = "no_action"
    DISCARD_CANDIDATE = "discard_candidate"


def recovery_action_for(state: RuntimeState) -> RecoveryAction:
    """D2-L: the ONE decision a restarted supervisor makes about
    whatever RuntimeState it finds on disk. Deliberately conservative:
    a supervisor that just restarted has lost every in-memory handle to
    any candidate process it may have launched before crashing (a PID
    alone cannot be trusted after a restart -- it may have been
    reused), so ANY non-idle transaction, regardless of exactly which
    non-terminal phase it was in, resolves to the SAME safe action:
    discard the candidate and return to idle, WITHOUT touching
    active_slot/active_generation at all. The currently active slot's
    own worker is a separate, already-running (or independently
    restartable) process this function does not need to reason about --
    it was never a party to the abandoned transaction."""
    if state.activation is None:
        return RecoveryAction.NO_ACTION
    return RecoveryAction.DISCARD_CANDIDATE


def apply_recovery(state: RuntimeState, action: RecoveryAction) -> RuntimeState:
    if action is RecoveryAction.NO_ACTION:
        return state
    if state.activation is None:
        return state
    return dataclasses.replace(state, activation=None)
