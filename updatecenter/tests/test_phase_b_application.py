import json
from pathlib import Path
import socket
import tempfile
import threading
import uuid

from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase

from updatecenter.backend_client import BackendError, UpdaterClient
from updatecenter.job_service import create_job, reconcile_job, submit_job
from updatecenter.models import UpdateJobState


class _Plan:
    safety_status = "ready_to_plan"
    installed_release_id = "r0002"
    target_release_id = "r0003"
    installed_commit = "a" * 40
    target_commit = "b" * 40
    fingerprint = "f" * 64

    def to_serializable(self):
        return {"target_release_id": self.target_release_id, "fingerprint": self.fingerprint}


class _Client:
    def __init__(self, state="running"):
        self.state = state
        self.starts = []

    def start_update(self, **kwargs):
        self.starts.append(kwargs)
        return {"ok": True, "accepted": True}

    def get_job_status(self, job_id):
        return {"ok": True, "job": {
            "job_id": str(job_id), "state": self.state, "current_step": "postflight",
            "failure_classification": "", "failure_detail": "",
            "trusted_plan": {"target_commit": "b" * 40, "fingerprint": "f" * 64},
        }}

    def get_job_log(self, job_id, max_bytes):
        return "root-owned log"


class JobMirrorTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("operator", password="x")

    def test_create_is_explicit_service_primitive_not_http(self):
        job = create_job(plan=_Plan(), user=self.user)
        self.assertEqual(job.state, UpdateJobState.PLANNED)
        self.assertEqual(job.active_lock, 1)
        self.assertEqual(job.initiated_by_username, "operator")

    def test_submit_sends_only_identifiers(self):
        job = create_job(plan=_Plan(), user=self.user)
        client = _Client()
        submit_job(job, client=client)
        self.assertEqual(set(client.starts[0]), {"job_id", "target_release_id", "plan_fingerprint"})
        self.assertNotIn("target_commit", client.starts[0])

    def test_terminal_reconciliation_releases_db_lock_and_snapshots_log(self):
        job = create_job(plan=_Plan(), user=self.user)
        submit_job(job, client=_Client())
        client = _Client(state="succeeded")
        reconcile_job(job, client=client)
        self.assertEqual(job.state, UpdateJobState.SUCCEEDED)
        self.assertIsNone(job.active_lock)
        self.assertEqual(job.completed_log_snapshot, "root-owned log")

    def test_manual_intervention_is_terminal(self):
        job = create_job(plan=_Plan(), user=self.user)
        client = _Client(state="manual_intervention_required")
        reconcile_job(job, client=client)
        self.assertTrue(job.requires_manual_intervention)
        self.assertIsNone(job.active_lock)

    def test_root_derived_identity_mismatch_rejected(self):
        job = create_job(plan=_Plan(), user=self.user)
        client = _Client()
        response = client.get_job_status(job.id)
        response["job"]["trusted_plan"]["target_commit"] = "c" * 40
        client.get_job_status = lambda _job: response
        with self.assertRaises(Exception):
            reconcile_job(job, client=client)


class BackendClientBoundsTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "socket"

    def tearDown(self):
        self.temp.cleanup()

    def _serve_once(self, response):
        ready = threading.Event()
        captured = {}

        def server():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(self.path))
                listener.listen(1)
                ready.set()
                connection, _ = listener.accept()
                with connection:
                    captured["request"] = connection.recv(8192)
                    connection.sendall(response)
        thread = threading.Thread(target=server)
        thread.start()
        ready.wait(2)
        return thread, captured

    def test_start_wire_shape_has_no_command_path_or_sha(self):
        thread, captured = self._serve_once(b'{"ok":true,"accepted":true}\n')
        job = uuid.uuid4()
        UpdaterClient(self.path).start_update(job_id=job, target_release_id="r0003", plan_fingerprint="f" * 64)
        thread.join(2)
        request = json.loads(captured["request"])
        self.assertEqual(set(request), {
            "protocol_version", "action", "job_id",
            "requested_target_release_id", "expected_plan_fingerprint",
        })

    def test_oversize_response_rejected(self):
        thread, _captured = self._serve_once(b"x" * 131073)
        with self.assertRaises(BackendError):
            UpdaterClient(self.path).ping()
        thread.join(2)
