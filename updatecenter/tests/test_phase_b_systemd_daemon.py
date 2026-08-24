import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import uuid

from django.test import SimpleTestCase

from .phase_b_helpers import config_dict
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.jobs import JobStore
from isadoraair_updater.process import CommandRunner, ProcessResult
from isadoraair_updater.release import TrustedPlan
from isadoraair_updater.systemd import SystemdError, SystemdManager
from isadoraair_updater.daemon import DaemonError, UpdaterDaemon


class FakeSystemRunner(CommandRunner):
    def __init__(self):
        super().__init__()
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        output = b"Type=simple\nActiveState=active\nSubState=running\nResult=success\n" if "show" in argv else b""
        return ProcessResult(tuple(argv), 0, output, b"")


def plan(**changes):
    data = dict(
        installed_release_id="r0002", installed_commit="a" * 40,
        target_release_id="r0003", target_commit="b" * 40,
        releases_in_plan=("r0003",), migrations_required=(), migration_compatibility=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=(),
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(),
        nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=1, manual_bootstrap_required=False,
        fingerprint="f" * 64,
    )
    data.update(changes)
    return TrustedPlan(**data)


class SystemdReconciliationTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        self.runner = FakeSystemRunner()
        self.manager = SystemdManager(self.config, self.runner, enforce_root_ownership=False)

    def tearDown(self):
        self.temp.cleanup()

    def _unit(self, name="isadoraair-gunicorn.service", content="[Service]\nUser=@@ISA_USER@@\nWorkingDirectory=@@ISA_ROOT@@\n"):
        (self.source / "deploy" / name).write_text(content, encoding="utf-8")

    def test_atomic_render_install_and_one_daemon_reload(self):
        self._unit()
        result = self.manager.reconcile(self.source, plan(systemd_units_changed=("isadoraair-gunicorn.service",)))
        installed = self.config.systemd_unit_root / "isadoraair-gunicorn.service"
        self.assertIn(self.config.application_user, installed.read_text())
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        self.assertEqual(result["changed"], ["isadoraair-gunicorn.service"])
        self.assertEqual(sum(call[-1] == "daemon-reload" for call in self.runner.calls), 1)

    def test_identical_unit_not_rewritten_or_reloaded(self):
        self._unit()
        selected = plan(systemd_units_changed=("isadoraair-gunicorn.service",))
        self.manager.reconcile(self.source, selected)
        self.runner.calls.clear()
        result = self.manager.reconcile(self.source, selected)
        self.assertFalse(result["changed"])
        self.assertFalse(self.runner.calls)

    def test_unknown_unit_refused(self):
        self._unit("evil.service")
        with self.assertRaises(SystemdError):
            self.manager.reconcile(self.source, plan(systemd_units_changed=("evil.service",)))

    def test_unknown_render_token_refused(self):
        self._unit(content="[Service]\nExecStart=@@ARBITRARY@@\n")
        with self.assertRaises(SystemdError):
            self.manager.reconcile(self.source, plan(systemd_units_changed=("isadoraair-gunicorn.service",)))

    def test_optional_unit_is_report_only(self):
        self._unit("isadoraair-updater.service", "[Service]\nExecStart=/bin/true\n")
        result = self.manager.reconcile(self.source, plan(systemd_units_new_optional=("isadoraair-updater.service",)))
        self.assertEqual(result["optional_report_only"], ["isadoraair-updater.service"])
        self.assertFalse((self.config.systemd_unit_root / "isadoraair-updater.service").exists())
        self.assertFalse(self.runner.calls)

    def test_restart_list_must_be_exact_manifest_order(self):
        self.manager.restart_declared(("isadoraair-gunicorn", "isadoraair-engine"))
        restarts = [call for call in self.runner.calls if "restart" in call]
        self.assertEqual([call[-1] for call in restarts], ["isadoraair-gunicorn.service", "isadoraair-engine.service"])
        with self.assertRaises(SystemdError):
            self.manager.restart_declared(("isadoraair-engine", "isadoraair-gunicorn"))

    def test_symlink_destination_refused(self):
        self._unit()
        self.config.systemd_unit_root.mkdir(parents=True)
        (self.config.systemd_unit_root / "isadoraair-gunicorn.service").symlink_to("/etc/passwd")
        with self.assertRaises(SystemdError):
            self.manager.reconcile(self.source, plan(systemd_units_changed=("isadoraair-gunicorn.service",)))


class _NeverExecutor:
    def execute(self, job_id):
        raise AssertionError("PING must never start execution")


class FakeIdentityRunner(CommandRunner):
    def __init__(self, result):
        super().__init__()
        self.result = result
        self.calls = []

    def run_as_user(self, user, argv, **kwargs):
        self.calls.append((user, list(argv), kwargs))
        return self.result


class DaemonPrivilegeDropTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")),
            allow_local_repository=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _result(self, *, returncode=0, stdout=None, timed_out=False,
                output_truncated=False):
        uid = os.getuid()
        return ProcessResult(
            ("/usr/bin/id", "-u"), returncode,
            f"{uid}\n".encode() if stdout is None else stdout,
            b"private diagnostic", timed_out, output_truncated,
        )

    def test_successful_fixed_identity_check_allows_readiness(self):
        runner = FakeIdentityRunner(self._result())
        store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)
        try:
            daemon = UpdaterDaemon(
                self.config, store=store, runner=runner, executor=_NeverExecutor(),
                authorized_uids={os.getuid()}, authorized_gids=set(),
            )
            self.assertTrue(daemon._protected_runtime_valid)
            self.assertEqual(
                runner.calls,
                [(self.config.application_user, ["/usr/bin/id", "-u"],
                  {"timeout": 5, "output_limit": 128})],
            )
        finally:
            store.close()

    def test_failed_privilege_drop_blocks_startup_without_reflecting_output(self):
        runner = FakeIdentityRunner(self._result(returncode=1, stdout=b"secret material"))
        with self.assertRaises(DaemonError) as caught:
            UpdaterDaemon(self.config, runner=runner, executor=_NeverExecutor())
        self.assertNotIn("secret material", str(caught.exception))

    def test_wrong_uid_blocks_startup(self):
        runner = FakeIdentityRunner(self._result(stdout=b"999999\n"))
        with self.assertRaisesRegex(DaemonError, "wrong uid"):
            UpdaterDaemon(self.config, runner=runner, executor=_NeverExecutor())

    def test_timeout_or_truncation_blocks_startup(self):
        for result in (
            self._result(returncode=None, timed_out=True),
            self._result(output_truncated=True),
        ):
            with self.subTest(result=result):
                with self.assertRaises(DaemonError):
                    UpdaterDaemon(
                        self.config, runner=FakeIdentityRunner(result),
                        executor=_NeverExecutor(),
                    )


class DaemonPeerTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.store = JobStore(self.config.jobs_root, self.config.logs_root, acquire_daemon_lock=False)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def _roundtrip(self, daemon, payload):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        def handle_and_close():
            with server:
                daemon.handle_connection(server)
        worker = threading.Thread(target=handle_and_close)
        worker.start()
        client.sendall(json.dumps(payload).encode() + b"\n")
        client.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
        worker.join(timeout=2)
        client.close()
        return json.loads(response)

    def test_actual_peer_credential_allows_ping(self):
        daemon = UpdaterDaemon(
            self.config, store=self.store, executor=_NeverExecutor(),
            authorized_uids={os.getuid()}, authorized_gids=set(),
        )
        response = self._roundtrip(daemon, {"protocol_version": 3, "action": "PING"})
        self.assertTrue(response["ok"])

    def test_bad_peer_denied_before_dispatch(self):
        daemon = UpdaterDaemon(
            self.config, store=self.store, executor=_NeverExecutor(),
            authorized_uids={999999}, authorized_gids={999999},
        )
        response = self._roundtrip(daemon, {"protocol_version": 3, "action": "PING"})
        self.assertFalse(response["ok"])
        self.assertIn("authorized", response["detail"])

    def test_slow_or_disconnected_peer_cannot_crash_request_handler(self):
        daemon = UpdaterDaemon(
            self.config, store=self.store, executor=_NeverExecutor(),
            authorized_uids={os.getuid()}, authorized_gids=set(),
        )
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        server.settimeout(0.01)
        try:
            daemon.handle_connection(server)
            response = json.loads(client.recv(4096))
            self.assertFalse(response["ok"])
            self.assertEqual(response["error"], "TimeoutError")
        finally:
            client.close()
            server.close()

    def test_multiple_recovered_active_states_fail_manual_without_workers(self):
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        self.store.accept(first, "r0003", "a" * 64)
        self.store.succeed(first)
        self.store.accept(second, "r0003", "a" * 64)
        self.store.update(first, state="running")
        daemon = UpdaterDaemon(
            self.config, store=self.store, executor=_NeverExecutor(),
            authorized_uids={os.getuid()}, authorized_gids=set(),
        )
        daemon.recover_jobs()
        self.assertEqual(self.store.load(first)["state"], "manual_intervention_required")
        self.assertEqual(self.store.load(second)["state"], "manual_intervention_required")
        self.assertFalse(daemon._workers)
