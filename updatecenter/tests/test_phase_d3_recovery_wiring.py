"""Update Center Phase D, D3-H: UpdaterDaemon.recover_jobs() actually
reaches runtime_handoff.classify_handoff_recovery() when launched as a
candidate -- not merely that the pure function works in isolation
(test_phase_d3_runtime_handoff.py already proves that)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict

from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.daemon import DaemonError, UpdaterDaemon
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner


class _NoopExecutor:
    def __init__(self):
        self.executed = []

    def execute(self, job_id):
        self.executed.append(job_id)


class RecoverJobsHandoffWiringTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)

    def _daemon(self, **kwargs):
        executor = _NoopExecutor()
        daemon = UpdaterDaemon.__new__(UpdaterDaemon)
        daemon.config = self.config
        daemon.runner = CommandRunner()
        daemon._protected_runtime_valid = True
        daemon.store = self.store
        daemon.executor = executor
        import threading
        daemon._start_lock = threading.Lock()
        daemon._workers = {}
        daemon.expected_slot = kwargs.get("expected_slot")
        daemon.expected_handoff_generation = kwargs.get("expected_handoff_generation")
        daemon.expected_handoff_descriptor_sha256 = kwargs.get("expected_handoff_descriptor_sha256")
        return daemon, executor

    def test_both_none_falls_back_to_original_single_job_rule(self):
        state, _ = self.store.accept("11111111-1111-4111-8111-111111111111", "r0004", "f" * 64)
        daemon, executor = self._daemon()
        daemon.recover_jobs()
        self.assertEqual(executor.executed, ["11111111-1111-4111-8111-111111111111"])

    def test_matching_handoff_job_is_the_one_resumed_even_with_other_stale_active_jobs(self):
        # An OTHER active job existing simultaneously would normally
        # trip the "at most one active job" rule below -- but a
        # correctly identified handoff candidate must still resume its
        # OWN job specifically, not be blocked by an unrelated job's
        # mere presence should one ever exist.
        self.store.accept("22222222-2222-4222-8222-222222222222", "r0004", "f" * 64)
        self.store.milestone("22222222-2222-4222-8222-222222222222", "runtime_descriptor_validated")
        self.store.milestone("22222222-2222-4222-8222-222222222222", "runtime_candidate_staged")
        self.store.milestone("22222222-2222-4222-8222-222222222222", "runtime_candidate_verified")
        self.store.milestone("22222222-2222-4222-8222-222222222222", "runtime_activation_requested")
        self.store.update(
            "22222222-2222-4222-8222-222222222222",
            protected_runtime_candidate={"candidate_slot": "B", "generation": 2, "descriptor_sha256": "c" * 64},
        )
        daemon, executor = self._daemon(expected_slot="B", expected_handoff_generation=2, expected_handoff_descriptor_sha256="c" * 64)
        daemon.recover_jobs()
        self.assertEqual(executor.executed, ["22222222-2222-4222-8222-222222222222"])

    def test_no_matching_handoff_job_falls_through_to_original_rule(self):
        state, _ = self.store.accept("33333333-3333-4333-8333-333333333333", "r0004", "f" * 64)
        daemon, executor = self._daemon(expected_slot="B", expected_handoff_generation=9, expected_handoff_descriptor_sha256="d" * 64)
        daemon.recover_jobs()
        # No handoff-matching job -- falls through to the ordinary
        # single-active-job rule, which still finds this one job.
        self.assertEqual(executor.executed, ["33333333-3333-4333-8333-333333333333"])

    def test_ambiguous_handoff_state_fails_closed_never_guesses(self):
        # Two simultaneously-active jobs can never arise through
        # JobStore.accept()'s own real API (it refuses a second while
        # one is already active) -- this test injects the pathological
        # state directly to prove classify_handoff_recovery's own
        # ambiguity refusal holds even if some OTHER bug path ever let
        # it happen, exactly the same defense-in-depth spirit as the
        # ROOT_STATE_CONCURRENCY_CONFLICT handling immediately below
        # it in recover_jobs() itself.
        first_id = "44444444-4444-4444-8444-444444444444"
        self.store.accept(first_id, "r0004", "f" * 64)
        self.store.milestone(first_id, "runtime_activation_requested")
        self.store.update(first_id, protected_runtime_candidate={"candidate_slot": "B", "generation": 2, "descriptor_sha256": "e" * 64})
        # Second job written DIRECTLY (bypassing accept()'s own single-
        # active-job guard, which a real caller can never get past) --
        # see this test's own docstring above.
        second_id = "55555555-5555-4555-8555-555555555555"
        second_state = dict(self.store.load(first_id))
        second_state.update(job_id=second_id, protected_runtime_candidate={"candidate_slot": "B", "generation": 2, "descriptor_sha256": "f" * 64})
        self.store._atomic_write(self.store._state_path(second_id), second_state)

        daemon, executor = self._daemon(expected_slot="B", expected_handoff_generation=2, expected_handoff_descriptor_sha256="e" * 64)
        daemon.recover_jobs()
        self.assertEqual(executor.executed, [])

    def test_mismatched_expected_fields_raises_daemon_error(self):
        with self.assertRaises(DaemonError):
            UpdaterDaemon(
                self.config, store=self.store, runner=CommandRunner(),
                executor=_NoopExecutor(), authorized_uids={0},
                expected_handoff_generation=2, expected_handoff_descriptor_sha256=None,
            )
