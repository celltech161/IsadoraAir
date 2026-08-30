"""D2-G: the activation phase state machine. D2-K: pre-mutation
rollback. D2-L: crash/power-loss recovery. D2-M: runtime slot
retention. All against synthetic RuntimeState fixtures -- no real
subprocess/socket, matching D2-S's own scope boundary."""
from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.activation import (
    ActivationPhase, ActivationTransitionError, is_terminal, runtime_activation_accepted, validate_transition,
)
from isadoraair_updater_bootstrap.slots import Slot, slot_is_reclaimable
from isadoraair_updater_bootstrap.state import RuntimeState
from isadoraair_updater_bootstrap.supervisor import (
    RecoveryAction, SupervisorError, apply_recovery, begin_transaction, commit_transaction,
    fail_transaction, finish_rollback, mark_candidate_ready, mark_candidate_starting,
    mark_candidate_verified, recovery_action_for, request_activation, request_rollback,
)

VALID_SHA = "a" * 64
CANDIDATE_SHA = "b" * 64


def idle_state(**overrides) -> RuntimeState:
    base = dict(
        schema_version=1, active_slot=Slot.A, active_generation=4, active_descriptor_sha256=VALID_SHA,
        previous_slot=None, previous_generation=None, previous_descriptor_sha256=None, activation=None,
    )
    base.update(overrides)
    return RuntimeState(**base)


class ActivationPhaseTransitionTests(SimpleTestCase):
    def test_full_happy_path_is_legal(self):
        path = [
            ActivationPhase.IDLE, ActivationPhase.CANDIDATE_STAGED, ActivationPhase.CANDIDATE_VERIFIED,
            ActivationPhase.ACTIVATION_REQUESTED, ActivationPhase.CANDIDATE_STARTING,
            ActivationPhase.CANDIDATE_READY, ActivationPhase.COMMITTED,
        ]
        for current, nxt in zip(path, path[1:]):
            with self.subTest(current=current, nxt=nxt):
                validate_transition(current, nxt)  # must not raise

    def test_full_rollback_path_is_legal(self):
        for source in (ActivationPhase.CANDIDATE_STARTING, ActivationPhase.CANDIDATE_READY):
            with self.subTest(source=source):
                validate_transition(source, ActivationPhase.ROLLBACK_REQUESTED)
        validate_transition(ActivationPhase.ROLLBACK_REQUESTED, ActivationPhase.ROLLED_BACK)

    def test_committed_is_never_a_source_of_further_transitions(self):
        for target in ActivationPhase:
            with self.subTest(target=target):
                with self.assertRaises(ActivationTransitionError):
                    validate_transition(ActivationPhase.COMMITTED, target)

    def test_terminal_phases_have_no_outgoing_edges(self):
        for phase in (ActivationPhase.COMMITTED, ActivationPhase.ROLLED_BACK, ActivationPhase.FAILED):
            with self.subTest(phase=phase):
                self.assertTrue(is_terminal(phase))
                for target in ActivationPhase:
                    with self.assertRaises(ActivationTransitionError):
                        validate_transition(phase, target)

    def test_cannot_skip_from_idle_directly_to_committed(self):
        with self.assertRaises(ActivationTransitionError):
            validate_transition(ActivationPhase.IDLE, ActivationPhase.COMMITTED)

    def test_cannot_rollback_from_candidate_staged(self):
        # No process could be alive that early -- rollback is not the
        # right operation; fail_transaction() is.
        with self.assertRaises(ActivationTransitionError):
            validate_transition(ActivationPhase.CANDIDATE_STAGED, ActivationPhase.ROLLBACK_REQUESTED)

    def test_runtime_activation_accepted_only_true_for_committed(self):
        for phase in ActivationPhase:
            with self.subTest(phase=phase):
                self.assertEqual(runtime_activation_accepted(phase), phase is ActivationPhase.COMMITTED)

    def test_candidate_ready_is_not_activation_accepted(self):
        # The exact semantic boundary D2-G calls out explicitly.
        self.assertFalse(runtime_activation_accepted(ActivationPhase.CANDIDATE_READY))


class SupervisorTransactionLifecycleTests(SimpleTestCase):
    def test_begin_transaction_from_idle(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        self.assertIs(state.activation.phase, ActivationPhase.CANDIDATE_STAGED)
        self.assertIs(state.activation.candidate_slot, Slot.B)
        self.assertEqual(state.active_slot, Slot.A)  # untouched

    def test_begin_transaction_refuses_candidate_slot_equal_active(self):
        with self.assertRaises(SupervisorError):
            begin_transaction(idle_state(), candidate_slot=Slot.A, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)

    def test_begin_transaction_refuses_when_already_in_flight(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        with self.assertRaises(SupervisorError):
            begin_transaction(state, candidate_slot=Slot.B, candidate_generation=6, candidate_descriptor_sha256=CANDIDATE_SHA)

    def test_full_happy_path_ends_committed_and_flips_active(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        state = commit_transaction(state)

        self.assertIsNone(state.activation)  # folded back to idle
        self.assertIs(state.active_slot, Slot.B)
        self.assertEqual(state.active_generation, 5)
        self.assertEqual(state.active_descriptor_sha256, CANDIDATE_SHA)
        self.assertIs(state.previous_slot, Slot.A)
        self.assertEqual(state.previous_generation, 4)
        self.assertEqual(state.previous_descriptor_sha256, VALID_SHA)

    def test_commit_is_the_only_function_that_moves_active_slot(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        for step in (mark_candidate_verified, request_activation, mark_candidate_starting, mark_candidate_ready):
            state = step(state)
            self.assertIs(state.active_slot, Slot.A, f"{step.__name__} must never move active_slot")

    def test_rollback_from_candidate_starting_leaves_active_untouched(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = request_rollback(state, reason="candidate never became ready")
        state = finish_rollback(state)
        self.assertIsNone(state.activation)
        self.assertIs(state.active_slot, Slot.A)
        self.assertEqual(state.active_generation, 4)
        self.assertIsNone(state.previous_slot)

    def test_rollback_from_candidate_ready_leaves_active_untouched(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        state = request_rollback(state, reason="operator-cancelled before commit")
        state = finish_rollback(state)
        self.assertIsNone(state.activation)
        self.assertIs(state.active_slot, Slot.A)

    def test_rollback_refused_before_a_process_could_be_alive(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        with self.assertRaises(SupervisorError):
            request_rollback(state, reason="too early")

    def test_fail_transaction_from_any_non_terminal_phase_leaves_active_untouched(self):
        for target_phase_index in range(1, 6):
            state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
            steps = [mark_candidate_verified, request_activation, mark_candidate_starting, mark_candidate_ready]
            for step in steps[:target_phase_index - 1]:
                state = step(state)
            with self.subTest(phase=state.activation.phase):
                failed = fail_transaction(state)
                self.assertIsNone(failed.activation)
                self.assertIs(failed.active_slot, Slot.A)
                self.assertEqual(failed.active_generation, 4)

    def test_operations_without_a_transaction_refused(self):
        state = idle_state()
        for op in (mark_candidate_verified, request_activation, mark_candidate_starting, mark_candidate_ready, commit_transaction, finish_rollback, fail_transaction):
            with self.subTest(op=op.__name__):
                with self.assertRaises(SupervisorError):
                    op(state)


class CrashRecoveryTests(SimpleTestCase):
    """D2-L's 9 numbered crash boundaries -- represented as the exact
    RuntimeState a restarted supervisor would find on disk at each
    point, since a genuinely durable atomic writer (D2-F, tested
    separately) guarantees the ONLY two possible on-disk states around
    any single write are the pre-write and post-write state, never
    something in between."""

    def test_1_before_candidate_staging(self):
        state = idle_state()
        self.assertEqual(recovery_action_for(state), RecoveryAction.NO_ACTION)
        self.assertEqual(apply_recovery(state, RecoveryAction.NO_ACTION), state)

    def test_2_during_candidate_staging_crash_before_write_lands(self):
        # The write never landed -- on-disk state is still exactly the
        # pre-staging idle state (case 1's own scenario). The abandoned
        # staging directory itself is orphaned but harmless -- covered
        # by SlotLayoutTests, not restated here.
        self.test_1_before_candidate_staging()

    def test_3_after_staged_but_before_verification(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        self.assertEqual(recovery_action_for(state), RecoveryAction.DISCARD_CANDIDATE)
        recovered = apply_recovery(state, RecoveryAction.DISCARD_CANDIDATE)
        self.assertIsNone(recovered.activation)
        self.assertIs(recovered.active_slot, Slot.A)

    def test_4_after_verification_but_before_activation_request_published(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        self.assertEqual(recovery_action_for(state), RecoveryAction.DISCARD_CANDIDATE)
        recovered = apply_recovery(state, recovery_action_for(state))
        self.assertIsNone(recovered.activation)

    def test_5_activation_requested_before_pointer_switch(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        self.assertEqual(recovery_action_for(state), RecoveryAction.DISCARD_CANDIDATE)
        recovered = apply_recovery(state, recovery_action_for(state))
        self.assertIs(recovered.active_slot, Slot.A)
        self.assertEqual(recovered.active_generation, 4)

    def test_6_immediately_after_pointer_switch(self):
        # commit_transaction()'s own write is the pointer switch --
        # once landed, activation is already None; recovery sees an
        # ordinary idle state with the NEW generation active.
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        state = commit_transaction(state)
        self.assertEqual(recovery_action_for(state), RecoveryAction.NO_ACTION)
        self.assertIs(state.active_slot, Slot.B)
        self.assertEqual(state.active_generation, 5)

    def test_7_candidate_starting_not_ready(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        self.assertEqual(recovery_action_for(state), RecoveryAction.DISCARD_CANDIDATE)
        recovered = apply_recovery(state, recovery_action_for(state))
        self.assertIs(recovered.active_slot, Slot.A)

    def test_8_candidate_ready_not_committed(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        self.assertEqual(recovery_action_for(state), RecoveryAction.DISCARD_CANDIDATE)
        recovered = apply_recovery(state, recovery_action_for(state))
        self.assertIs(recovered.active_slot, Slot.A)  # never silently promoted

    def test_9_after_committed_state(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        state = commit_transaction(state)
        recovered = apply_recovery(state, recovery_action_for(state))
        # Idempotent -- applying recovery to an already-idle post-
        # commit state changes nothing.
        self.assertEqual(recovered, state)

    def test_exactly_one_active_slot_selected_across_every_scenario(self):
        for build in (
            lambda: idle_state(),
            lambda: begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA),
        ):
            state = build()
            recovered = apply_recovery(state, recovery_action_for(state))
            self.assertIn(recovered.active_slot, (Slot.A, Slot.B))
            self.assertNotEqual(recovered.active_slot, recovered.previous_slot)

    def test_recovery_never_downgrades_a_committed_generation(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_verified(state)
        state = request_activation(state)
        state = mark_candidate_starting(state)
        state = mark_candidate_ready(state)
        committed = commit_transaction(state)
        recovered = apply_recovery(committed, recovery_action_for(committed))
        self.assertEqual(recovered.active_generation, 5)

    def test_repeated_recovery_application_is_idempotent_no_infinite_flip(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        once = apply_recovery(state, recovery_action_for(state))
        twice = apply_recovery(once, recovery_action_for(once))
        self.assertEqual(once, twice)


class RetentionTests(SimpleTestCase):
    """D2-M: retain exactly active + previous-LKG + candidate-while-
    in-flight. Never unbounded accumulation -- with only two physical
    slots this is structurally guaranteed, restated here as explicit
    tests of slot_is_reclaimable() against real post-commit state."""

    def test_after_first_ever_commit_old_active_becomes_previous_lkg_not_reclaimable(self):
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_ready(mark_candidate_starting(request_activation(mark_candidate_verified(state))))
        committed = commit_transaction(state)
        self.assertFalse(slot_is_reclaimable(committed.previous_slot, active_slot=committed.active_slot, previous_lkg_slot=committed.previous_slot))

    def test_after_commit_the_new_candidate_slot_for_the_next_transaction_must_be_the_previous_lkg(self):
        # With only A and B, once B is active and A is previous-LKG,
        # the NEXT transaction's only legal candidate_slot is A --
        # begin_transaction() itself refuses candidate_slot == active.
        state = begin_transaction(idle_state(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256=CANDIDATE_SHA)
        state = mark_candidate_ready(mark_candidate_starting(request_activation(mark_candidate_verified(state))))
        committed = commit_transaction(state)
        with self.assertRaises(SupervisorError):
            begin_transaction(committed, candidate_slot=Slot.B, candidate_generation=6, candidate_descriptor_sha256="d" * 64)
        # The legal one:
        next_state = begin_transaction(committed, candidate_slot=Slot.A, candidate_generation=6, candidate_descriptor_sha256="d" * 64)
        self.assertIs(next_state.activation.candidate_slot, Slot.A)
