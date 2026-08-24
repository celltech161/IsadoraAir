import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import uuid

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import ConfigError, validate_config_dict
from isadoraair_updater.daemon import UpdaterDaemon
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.protocol import ProtocolError, decode_request
from isadoraair_updater.systemd import ALSACTL, SYSTEMCTL, SystemdError, SystemdManager


class RootArmingConfigTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = config_dict(self.root, str(self.root / "upstream.git"))

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_arm_field_is_disabled(self):
        config = validate_config_dict(self.data, allow_local_repository=True)
        self.assertFalse(config.update_execution_enabled)

    def test_true_and_false_are_explicit_but_nonboolean_rejected(self):
        for value in (False, True):
            self.data["update_execution_enabled"] = value
            self.assertIs(validate_config_dict(self.data, allow_local_repository=True).update_execution_enabled, value)
        self.data["update_execution_enabled"] = 1
        with self.assertRaises(ConfigError):
            validate_config_dict(self.data, allow_local_repository=True)

    def test_operator_allowlist_is_finite_exact_and_root_configured(self):
        self.data["operator_restart_units"] = ["isadoraair-engine.service", "isadoraair-rbds.service"]
        config = validate_config_dict(self.data, allow_local_repository=True)
        self.assertEqual(config.operator_restart_units, tuple(self.data["operator_restart_units"]))
        for bad in (["*.service"], ["/bin/sh.service"], ["engine"], ["a.service", "a.service"]):
            self.data["operator_restart_units"] = bad
            with self.assertRaises(ConfigError):
                validate_config_dict(self.data, allow_local_repository=True)


class StrictMaintenanceProtocolTests(SimpleTestCase):
    def _decode(self, payload):
        return decode_request(json.dumps({"protocol_version": 3, **payload}).encode())

    def test_restart_has_only_one_exact_service_field(self):
        request = self._decode({"action": "RESTART_OPERATOR_SERVICE", "service": "isadoraair-engine.service"})
        self.assertEqual(request.service, "isadoraair-engine.service")
        for service in ("*.service", "/bin/sh", "engine", "isadoraair-engine.service --now"):
            with self.assertRaises(ProtocolError):
                self._decode({"action": "RESTART_OPERATOR_SERVICE", "service": service})
        with self.assertRaises(ProtocolError):
            self._decode({
                "action": "RESTART_OPERATOR_SERVICE",
                "service": "isadoraair-engine.service",
                "argv": ["--no-block"],
            })

    def test_alsa_action_accepts_no_client_arguments(self):
        self.assertEqual(self._decode({"action": "STORE_ALSA_STATE"}).action, "STORE_ALSA_STATE")
        with self.assertRaises(ProtocolError):
            self._decode({"action": "STORE_ALSA_STATE", "card": "../../etc/passwd"})

    def test_maintenance_status_accepts_only_one_canonical_operation_uuid(self):
        operation_id = str(uuid.uuid4())
        request = self._decode({
            "action": "GET_MAINTENANCE_STATUS", "operation_id": operation_id,
        })
        self.assertEqual(request.operation_id, operation_id)
        for payload in (
            {"action": "GET_MAINTENANCE_STATUS", "operation_id": "not-a-uuid"},
            {"action": "GET_MAINTENANCE_STATUS", "operation_id": operation_id, "path": "/tmp"},
        ):
            with self.assertRaises(ProtocolError):
                self._decode(payload)


class _Runner(CommandRunner):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        stdout = b"Type=simple\nActiveState=active\nSubState=running\nResult=success\n" if "show" in argv else b""
        return ProcessResult(tuple(argv), 0, stdout, b"")


class FixedRootOperationTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        data = config_dict(root, str(root / "upstream.git"))
        data["operator_restart_units"] = ["isadoraair-engine.service", "isadoraair-rbds.service"]
        self.config = validate_config_dict(data, allow_local_repository=True)
        self.runner = _Runner()
        self.manager = SystemdManager(self.config, self.runner, enforce_root_ownership=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_allowed_restarts_use_fixed_systemctl_and_exact_unit(self):
        self.manager.restart_operator_service("isadoraair-engine.service")
        self.assertEqual(self.runner.calls[0][0], [SYSTEMCTL, "restart", "isadoraair-engine.service"])
        self.assertEqual(self.runner.calls[1][0][:2], [SYSTEMCTL, "show"])

    def test_arbitrary_and_malformed_units_fail_closed(self):
        for unit in ("arbitrary.service", "/bin/sh", "*.service"):
            with self.assertRaises(SystemdError):
                self.manager.restart_operator_service(unit)
        self.assertFalse(self.runner.calls)

    def test_alsa_uses_fixed_executable_and_no_caller_argv(self):
        self.manager.store_alsa_state()
        self.assertEqual(self.runner.calls, [([ALSACTL, "store"], {"timeout": 30})])


class _Repository:
    def initialize_or_verify(self):
        return None


class _Maintenance:
    def __init__(self):
        self.restart = threading.Event()
        self.alsa = threading.Event()

    def restart_operator_service(self, service):
        self.service = service
        self.restart.set()

    def store_alsa_state(self):
        self.alsa.set()


class _BlockingMaintenance(_Maintenance):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def restart_operator_service(self, service):
        super().restart_operator_service(service)
        self.release.wait(2)


class _FailingMaintenance(_Maintenance):
    def restart_operator_service(self, service):
        self.service = service
        raise SystemdError("sensitive command output must not escape")

    def store_alsa_state(self):
        raise OSError("sensitive ALSA output must not escape")


class _Executor:
    def __init__(self):
        self.repository = _Repository()
        self.systemd = _Maintenance()
        self.executed = threading.Event()

    def execute(self, _job_id):
        self.executed.set()


class DaemonArmingAndMaintenanceTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = config_dict(self.root, str(self.root / "upstream.git"))
        self.data["operator_restart_units"] = ["isadoraair-engine.service", "isadoraair-rbds.service"]

    def tearDown(self):
        self.temp.cleanup()

    def _daemon(self, *, armed):
        self.data["update_execution_enabled"] = armed
        config = validate_config_dict(self.data, allow_local_repository=True)
        store = JobStore(config.jobs_root, config.logs_root, acquire_daemon_lock=False)
        self.addCleanup(store.close)
        executor = _Executor()
        daemon = UpdaterDaemon(
            config, store=store, executor=executor,
            authorized_uids={os.getuid()}, authorized_gids=set(),
        )
        return daemon, store, executor

    @staticmethod
    def _roundtrip(daemon, payload):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        worker = threading.Thread(target=lambda: daemon.handle_connection(server))
        worker.start()
        client.sendall(json.dumps({"protocol_version": 3, **payload}).encode() + b"\n")
        client.shutdown(socket.SHUT_WR)
        raw = client.recv(131072)
        worker.join(2)
        client.close(); server.close()
        return json.loads(raw)

    def test_ping_reports_disarmed_and_start_is_rejected_before_accept(self):
        daemon, store, _executor = self._daemon(armed=False)
        ping = self._roundtrip(daemon, {"action": "PING"})
        self.assertTrue(ping["ok"])
        self.assertFalse(ping["update_execution_enabled"])
        job_id = str(uuid.uuid4())
        response = self._roundtrip(daemon, {
            "action": "START_UPDATE", "job_id": job_id,
            "requested_target_release_id": "r0005", "expected_plan_fingerprint": "f" * 64,
        })
        self.assertFalse(response["ok"])
        self.assertFalse(store.list_states())

    def test_armed_start_continues_phase_b_validation(self):
        daemon, _store, executor = self._daemon(armed=True)
        response = self._roundtrip(daemon, {
            "action": "START_UPDATE", "job_id": str(uuid.uuid4()),
            "requested_target_release_id": "r0005", "expected_plan_fingerprint": "f" * 64,
        })
        self.assertTrue(response["accepted"])
        self.assertTrue(executor.executed.wait(1))

    def test_post_accept_log_failure_returns_error_but_durable_job_is_visible_and_running(self):
        daemon, store, executor = self._daemon(armed=True)
        original_append = store.append_log
        store.append_log = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log fsync failed"))
        job_id = str(uuid.uuid4())
        response = self._roundtrip(daemon, {
            "action": "START_UPDATE", "job_id": job_id,
            "requested_target_release_id": "r0005", "expected_plan_fingerprint": "f" * 64,
        })
        self.assertFalse(response["ok"])
        self.assertEqual(store.load(job_id)["state"], "accepted")
        self.assertTrue(executor.executed.wait(1))
        status = self._roundtrip(daemon, {"action": "GET_JOB_STATUS", "job_id": job_id})
        self.assertTrue(status["ok"])
        self.assertEqual(status["job"]["job_id"], job_id)
        store.append_log = original_append

    def test_same_uuid_idempotency_remains_exact(self):
        daemon, _store, _executor = self._daemon(armed=True)
        job_id = str(uuid.uuid4())
        payload = {
            "action": "START_UPDATE", "job_id": job_id,
            "requested_target_release_id": "r0005", "expected_plan_fingerprint": "f" * 64,
        }
        first = self._roundtrip(daemon, payload)
        second = self._roundtrip(daemon, payload)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        changed = self._roundtrip(daemon, {**payload, "expected_plan_fingerprint": "e" * 64})
        self.assertFalse(changed["ok"])

    def test_maintenance_is_independent_of_update_arm_and_allowlisted(self):
        daemon, _store, executor = self._daemon(armed=False)
        allowed = self._roundtrip(daemon, {
            "action": "RESTART_OPERATOR_SERVICE", "service": "isadoraair-engine.service",
        })
        self.assertTrue(allowed["accepted"])
        self.assertEqual(allowed["maintenance"]["state"], "succeeded")
        self.assertTrue(executor.systemd.restart.wait(1))
        self.assertEqual(executor.systemd.service, "isadoraair-engine.service")

        rejected = self._roundtrip(daemon, {
            "action": "RESTART_OPERATOR_SERVICE", "service": "arbitrary.service",
        })
        self.assertFalse(rejected["ok"])

        alsa = self._roundtrip(daemon, {"action": "STORE_ALSA_STATE"})
        self.assertTrue(alsa["accepted"])
        self.assertEqual(alsa["maintenance"]["state"], "succeeded")
        self.assertTrue(executor.systemd.alsa.wait(1))

    def test_failed_restart_and_alsa_are_persisted_sanitized_and_queryable(self):
        daemon, _store, executor = self._daemon(armed=False)
        executor.systemd = _FailingMaintenance()
        for payload in (
            {"action": "RESTART_OPERATOR_SERVICE", "service": "isadoraair-engine.service"},
            {"action": "STORE_ALSA_STATE"},
        ):
            with self.subTest(action=payload["action"]):
                response = self._roundtrip(daemon, payload)
                record = response["maintenance"]
                self.assertEqual(record["state"], "failed")
                self.assertEqual(record["result_code"], "OPERATION_FAILED")
                self.assertNotIn("sensitive", json.dumps(record))
                status = self._roundtrip(daemon, {
                    "action": "GET_MAINTENANCE_STATUS",
                    "operation_id": record["operation_id"],
                })
                self.assertEqual(status["maintenance"], record)

    def test_maintenance_worker_is_bounded_and_never_queues(self):
        daemon, _store, executor = self._daemon(armed=False)
        executor.systemd = _BlockingMaintenance()
        first = self._roundtrip(daemon, {
            "action": "RESTART_OPERATOR_SERVICE", "service": "isadoraair-engine.service",
        })
        self.assertTrue(first["accepted"])
        self.assertEqual(first["maintenance"]["state"], "running")
        self.assertTrue(executor.systemd.restart.wait(1))
        worker = daemon._maintenance_worker
        second = self._roundtrip(daemon, {"action": "STORE_ALSA_STATE"})
        self.assertFalse(second["ok"])
        self.assertIs(daemon._maintenance_worker, worker)
        executor.systemd.release.set()
        worker.join(1)

    def test_maintenance_records_are_bounded(self):
        daemon, store, _executor = self._daemon(armed=False)
        for _index in range(105):
            response = self._roundtrip(daemon, {"action": "STORE_ALSA_STATE"})
            self.assertEqual(response["maintenance"]["state"], "succeeded")
        self.assertLessEqual(len(list(store.maintenance_root.glob("*.json"))), 100)
