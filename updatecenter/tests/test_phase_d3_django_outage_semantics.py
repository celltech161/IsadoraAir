"""Update Center Phase D, D3-P: during a real worker handoff, the
protected updater's Django-facing Unix socket briefly disappears (the
old worker released/stopped, the candidate has not yet bound it) --
this must classify as "temporary backend unavailable / job still
pending," never "job failed," the browser/API must keep polling the
SAME job UUID, and no Gunicorn restart or new Django-side job may ever
be involved. Uses a REAL UpdaterClient against a real (initially
absent, later present) Unix socket path -- not a mock -- to prove the
actual transport-error classification, not merely job_service.py's own
exception handling in isolation."""
from __future__ import annotations

import socket
import tempfile
import threading
import time
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase

from updatecenter.backend_client import BackendTransportError, UpdaterClient
from updatecenter.job_service import submit_job, reconcile_job
from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.planner import Plan, SafetyStatus


def _plan():
    return Plan(
        safety_status=SafetyStatus.READY_TO_PLAN, safety_detail="",
        installed_release_id="r0003", installed_commit="a" * 40,
        target_release_id="r0004", target_commit="b" * 40,
        releases_in_plan=("r0004",), migrations=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=(), systemd_units_new_optional=(),
        systemd_units_removed_or_renamed=(), collectstatic_required=False,
        services_requiring_restart=(), nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=5, manual_bootstrap_required=False,
        cross_check_findings=(), fingerprint="c" * 64,
        schema_health_status="schema_current", schema_pending_migrations=(), schema_health_detail="",
        target_schema_validation_status="target_schema_plan_validation_pending",
        target_schema_validation_detail="",
    )


class DjangoOutageDuringHandoffTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("d3-outage-test")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.socket_path = Path(self.temp.name) / "updater.sock"
        self.job = UpdateJob.objects.create(
            initiated_by=self.user, initiated_by_username=self.user.get_username(),
            installed_release_id="r0003", target_release_id="r0004",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.PLANNED, current_step="awaiting_backend_submission",
            plan_snapshot=_plan().to_serializable(), plan_fingerprint="c" * 64, active_lock=1,
        )

    def test_socket_absent_during_submission_is_uncertain_never_failed(self):
        """The exact mid-handoff window: the old worker's socket is
        already gone and the candidate's is not up yet."""
        client = UpdaterClient(self.socket_path, timeout=1)
        result = submit_job(self.job, client=client)
        self.assertEqual(result, {"ok": False, "submission_uncertain": True})
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, UpdateJobState.SUBMISSION_UNCERTAIN)
        self.assertNotEqual(self.job.state, UpdateJobState.FAILED)
        # The active lock is deliberately still held -- never released
        # merely because the backend was temporarily unreachable (see
        # job_service.py's own _mark_submission_uncertain()).
        self.assertEqual(self.job.active_lock, 1)

    def test_same_job_uuid_reconciles_once_the_socket_comes_back(self):
        """Simulates the candidate worker binding the SAME Django-
        facing socket path a short time later -- reconcile_job() must
        pick up the SAME job UUID, never create a second UpdateJob."""
        original_job_count = UpdateJob.objects.count()
        client = UpdaterClient(self.socket_path, timeout=1)
        submit_job(self.job, client=client)
        self.job.refresh_from_db()
        self.assertEqual(self.job.state, UpdateJobState.SUBMISSION_UNCERTAIN)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(4)
        server.settimeout(1.0)
        stop = threading.Event()

        def serve():
            import json as _json
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    connection.recv(8192)
                    response = _json.dumps({
                        "ok": True,
                        "job": {
                            "job_id": str(self.job.id), "state": "running",
                            "current_step": "resumed_by_candidate", "milestones": [],
                            "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
                            "failure_classification": "", "failure_detail": "", "trusted_plan": None,
                        },
                    }).encode() + b"\n"
                    connection.sendall(response)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            reconcile_job(self.job, client=client)
        finally:
            stop.set()
            thread.join(timeout=5)
            server.close()

        self.job.refresh_from_db()
        self.assertEqual(self.job.state, UpdateJobState.RUNNING)
        self.assertEqual(self.job.current_step, "resumed_by_candidate")
        # No second job was ever created for this same authorization.
        self.assertEqual(UpdateJob.objects.count(), original_job_count)
        self.assertEqual(UpdateJob.objects.get().id, self.job.id)

    def test_backend_transport_error_is_the_real_classification_raised(self):
        client = UpdaterClient(self.socket_path, timeout=1)
        with self.assertRaises(BackendTransportError):
            client.ping()
