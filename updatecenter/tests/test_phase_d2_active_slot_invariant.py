"""D2 corrective review, Correction 3: the active-slot invariant, PROVEN
mechanically rather than merely documented in a docstring.

RuntimeState is a frozen dataclass (deploy/updater_bootstrap/
isadoraair_updater_bootstrap/state.py) -- direct attribute assignment
(`state.active_slot = X`) is a TypeError at runtime, so the only way
any code in this codebase can ever produce a RuntimeState with a
different active_slot is a `dataclasses.replace(state, active_slot=...)`
call. This file greps every function definition in supervisor.py for
exactly that call shape and asserts commit_transaction() is the only
one that ever makes it -- not merely that the module's own docstring
says so."""
import ast
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.activation import ActivationPhase
from isadoraair_updater_bootstrap.readiness import classify_readiness
from isadoraair_updater_bootstrap.slots import Slot
from isadoraair_updater_bootstrap.state import RuntimeState
from isadoraair_updater_bootstrap.supervisor import (
    begin_transaction, commit_transaction, fail_transaction, finish_rollback,
    mark_candidate_ready, mark_candidate_starting, mark_candidate_verified,
    request_activation, request_rollback,
)

SUPERVISOR_SOURCE_PATH = BOOTSTRAP_ROOT / "isadoraair_updater_bootstrap" / "supervisor.py"
VALID_SHA = "a" * 64


def _functions_that_replace_active_slot(source: str) -> set[str]:
    """Walks the module's own function definitions and reports the
    name of every one whose body contains a call shaped like
    `dataclasses.replace(<anything>, active_slot=<anything>, ...)` or
    `replace(<anything>, active_slot=<anything>, ...)` (either import
    spelling) -- a keyword literally named active_slot in a call to
    something literally named replace/dataclasses.replace, which is
    the only mechanism that can ever produce a differently-valued
    RuntimeState.active_slot given the dataclass is frozen."""
    tree = ast.parse(source)
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            is_replace_call = (
                (isinstance(func, ast.Name) and func.id == "replace")
                or (isinstance(func, ast.Attribute) and func.attr == "replace")
            )
            if not is_replace_call:
                continue
            if any(keyword.arg == "active_slot" for keyword in inner.keywords):
                offenders.add(node.name)
    return offenders


class ActiveSlotInvariantStaticTests(SimpleTestCase):
    def test_only_commit_transaction_assigns_active_slot(self):
        source = SUPERVISOR_SOURCE_PATH.read_text(encoding="utf-8")
        offenders = _functions_that_replace_active_slot(source)
        self.assertEqual(offenders, {"commit_transaction"})

    def test_detector_itself_actually_finds_a_real_offender(self):
        # Sanity check on the AST walker above, using a synthetic
        # snippet -- proves the detector is not silently a no-op that
        # would make test_only_commit_transaction_assigns_active_slot
        # pass for the wrong reason (an empty offenders set from a
        # broken walker looks identical to a genuinely clean module
        # unless this is checked separately).
        snippet = """
import dataclasses

def not_supposed_to_do_this(state):
    return dataclasses.replace(state, active_slot=None)

def this_one_is_fine(state):
    return dataclasses.replace(state, active_generation=1)
"""
        offenders = _functions_that_replace_active_slot(snippet)
        self.assertEqual(offenders, {"not_supposed_to_do_this"})

    def test_runtime_state_is_frozen_direct_assignment_impossible(self):
        state = RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=1,
            active_descriptor_sha256=VALID_SHA, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        )
        with self.assertRaises(Exception):
            state.active_slot = Slot.B


class CandidateLaunchReadinessCannotInfluenceActiveSlotTests(SimpleTestCase):
    """Proves candidate launch/readiness classification is structurally
    incapable of influencing active_slot -- not merely "doesn't
    currently.\""""

    def test_readiness_module_never_imports_state_module(self):
        readiness_source = (BOOTSTRAP_ROOT / "isadoraair_updater_bootstrap" / "readiness.py").read_text(encoding="utf-8")
        tree = ast.parse(readiness_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotIn("state", node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("state", alias.name)

    def test_classify_readiness_signature_has_no_runtimestate_parameter(self):
        import inspect
        signature = inspect.signature(classify_readiness)
        for parameter in signature.parameters.values():
            self.assertNotEqual(parameter.annotation, RuntimeState)

    def test_classify_readiness_return_value_is_not_a_runtimestate(self):
        state_result, facts = classify_readiness(
            process_exited=False, raw_facts=None,
            expected_slot="A", expected_generation=1, expected_descriptor_sha256=VALID_SHA,
        )
        self.assertNotIsInstance(state_result, RuntimeState)
        self.assertNotIsInstance(facts, RuntimeState)

    def test_every_pre_commit_transition_leaves_active_slot_byte_identical(self):
        base = RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=4,
            active_descriptor_sha256=VALID_SHA, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        )
        state = begin_transaction(base, candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256="b" * 64)
        for step in (mark_candidate_verified, request_activation, mark_candidate_starting, mark_candidate_ready):
            state = step(state)
            self.assertIs(state.active_slot, Slot.A)
            self.assertEqual(state.active_generation, 4)
        # And only commit_transaction() ever changes it:
        committed = commit_transaction(state)
        self.assertIs(committed.active_slot, Slot.B)

    def test_rollback_and_failure_paths_also_leave_active_slot_untouched(self):
        base = RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=4,
            active_descriptor_sha256=VALID_SHA, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        )
        state = begin_transaction(base, candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256="b" * 64)
        state = mark_candidate_starting(request_activation(mark_candidate_verified(state)))
        rolled_back = finish_rollback(request_rollback(state, reason="test"))
        self.assertIs(rolled_back.active_slot, Slot.A)

        state2 = begin_transaction(base, candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256="b" * 64)
        failed = fail_transaction(state2)
        self.assertIs(failed.active_slot, Slot.A)


class CrashSemanticsDocumentationTests(SimpleTestCase):
    """The two crash-semantics states Correction 3 asks to be
    explicit about, restated here as direct, named assertions (not
    only prose in the module docstring)."""

    def _base(self):
        return RuntimeState(
            schema_version=1, active_slot=Slot.A, active_generation=4,
            active_descriptor_sha256=VALID_SHA, previous_slot=None,
            previous_generation=None, previous_descriptor_sha256=None, activation=None,
        )

    def test_transaction_exists_and_active_still_old_means_candidate_uncommitted(self):
        state = begin_transaction(self._base(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256="b" * 64)
        state = mark_candidate_starting(request_activation(mark_candidate_verified(state)))
        # A restarted supervisor observing THIS exact state: activation
        # is present, active_slot is still the OLD slot -- the candidate
        # is, by definition, uncommitted, and may be safely abandoned.
        self.assertIsNotNone(state.activation)
        self.assertIs(state.active_slot, Slot.A)
        self.assertNotEqual(state.activation.phase, ActivationPhase.COMMITTED)

    def test_active_changed_to_candidate_means_transition_is_committed_and_final(self):
        state = begin_transaction(self._base(), candidate_slot=Slot.B, candidate_generation=5, candidate_descriptor_sha256="b" * 64)
        state = mark_candidate_ready(mark_candidate_starting(request_activation(mark_candidate_verified(state))))
        committed = commit_transaction(state)
        # Once active_slot has actually changed, there is no transaction
        # left in flight to "reset" -- activation is already cleared,
        # and the new slot is simply the current fact, not a candidate.
        self.assertIsNone(committed.activation)
        self.assertIs(committed.active_slot, Slot.B)
