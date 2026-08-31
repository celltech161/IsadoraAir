"""Update Center Phase D, D4-B/D4-C: UpdaterDaemon's own candidate-
readiness reporting and old-worker self-shutdown wiring, in isolation
(a fake supervisor_client injected -- the REAL end-to-end IPC round
trip is proven separately, over a real socket, by
test_phase_d3_supervisor_ipc.py and test_phase_d4_supervisor_daemon.py)."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict

from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.daemon import DaemonError, UpdaterDaemon
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner


class _FakeSupervisorClient:
    def __init__(self):
        self.readiness_reports = []

    def report_readiness(self, **kwargs):
        self.readiness_reports.append(kwargs)
        return {"ok": True}


class _NoopExecutor:
    def execute(self, job_id):
        raise AssertionError("not exercised in these tests")


class DaemonCandidateIdentityValidationTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)

    def test_partial_identity_group_refused(self):
        store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(store.close)
        with self.assertRaises(DaemonError):
            UpdaterDaemon(
                self.config, store=store, executor=_NoopExecutor(), authorized_uids={0},
                expected_slot="A", expected_handoff_generation=2,
                expected_handoff_descriptor_sha256=None, expected_resumable_job_uuid=None,
            )

    def test_all_none_is_legal_ordinary_daemon(self):
        store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(store.close)
        daemon = UpdaterDaemon(self.config, store=store, executor=_NoopExecutor(), authorized_uids={0})
        self.assertIsNone(daemon.expected_slot)


class ReportCandidateReadinessTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)

    def _daemon(self, **identity):
        return UpdaterDaemon(
            self.config, store=self.store, executor=_NoopExecutor(), authorized_uids={0},
            supervisor_client=self.fake_client, **identity,
        )

    def test_ordinary_daemon_never_reports_readiness(self):
        self.fake_client = _FakeSupervisorClient()
        daemon = self._daemon()
        daemon._socket = object()  # pretend the Django-facing socket is bound
        daemon.report_candidate_readiness()
        self.assertEqual(self.fake_client.readiness_reports, [])

    def test_candidate_daemon_reports_every_required_fact(self):
        self.fake_client = _FakeSupervisorClient()
        job_uuid = str(uuid.uuid4())
        daemon = self._daemon(
            expected_slot="B", expected_handoff_generation=2,
            expected_handoff_descriptor_sha256="a" * 64, expected_resumable_job_uuid=job_uuid,
        )
        daemon._socket = object()
        daemon.report_candidate_readiness()
        self.assertEqual(len(self.fake_client.readiness_reports), 1)
        report = self.fake_client.readiness_reports[0]
        required = {
            "transaction_id", "candidate_slot", "candidate_generation", "candidate_descriptor_sha256",
            "bootstrap_protocol_version", "supported_wire_protocols", "config_parsed",
            "privilege_drop_self_check_passed", "job_store_ready", "worker_socket_bound",
            "trusted_repository_usable", "resumable_job_uuid",
        }
        self.assertEqual(set(report), required)
        self.assertEqual(report["candidate_slot"], "B")
        self.assertEqual(report["candidate_generation"], 2)
        self.assertEqual(report["candidate_descriptor_sha256"], "a" * 64)
        self.assertEqual(report["resumable_job_uuid"], job_uuid)
        self.assertEqual(report["transaction_id"], job_uuid)
        # job_store_ready reflects whether THIS daemon's own store
        # currently holds the exclusive lock -- acquire_daemon_lock=
        # False above means it does not, so this must report False,
        # never an optimistic hardcoded True (mere process existence
        # is never readiness).
        self.assertFalse(report["job_store_ready"])

    def test_job_store_ready_reflects_the_real_lock_state(self):
        self.fake_client = _FakeSupervisorClient()
        locked_store = JobStore(self.root / "locked-jobs", self.root / "locked-logs", acquire_daemon_lock=True)
        self.addCleanup(locked_store.close)
        job_uuid = str(uuid.uuid4())
        daemon = UpdaterDaemon(
            self.config, store=locked_store, executor=_NoopExecutor(), authorized_uids={0},
            supervisor_client=self.fake_client, expected_slot="B", expected_handoff_generation=2,
            expected_handoff_descriptor_sha256="a" * 64, expected_resumable_job_uuid=job_uuid,
        )
        daemon._socket = object()
        daemon.report_candidate_readiness()
        self.assertTrue(self.fake_client.readiness_reports[0]["job_store_ready"])


class ShutdownAfterYieldTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(self.store.close)
        self.daemon = UpdaterDaemon(self.config, store=self.store, executor=_NoopExecutor(), authorized_uids={0})

    def test_yielded_job_triggers_shutdown(self):
        result = {"state": "running", "milestones": ["runtime_activation_requested"]}
        self.assertFalse(self.daemon._stop.is_set())
        self.daemon._shutdown_if_old_worker_just_yielded(result)
        self.assertTrue(self.daemon._stop.is_set())

    def test_accepted_job_does_not_trigger_shutdown(self):
        result = {"state": "running", "milestones": ["runtime_activation_requested", "runtime_activation_accepted"]}
        self.daemon._shutdown_if_old_worker_just_yielded(result)
        self.assertFalse(self.daemon._stop.is_set())

    def test_succeeded_ordinary_job_does_not_trigger_shutdown(self):
        result = {"state": "succeeded", "milestones": ["services_restarted", "postflight_complete"]}
        self.daemon._shutdown_if_old_worker_just_yielded(result)
        self.assertFalse(self.daemon._stop.is_set())

    def test_failed_job_does_not_trigger_shutdown(self):
        result = {"state": "failed", "milestones": []}
        self.daemon._shutdown_if_old_worker_just_yielded(result)
        self.assertFalse(self.daemon._stop.is_set())
