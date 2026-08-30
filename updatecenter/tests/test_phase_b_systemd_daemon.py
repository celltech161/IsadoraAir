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
        output = (
            b"Type=simple\nActiveState=active\nSubState=running\nResult=success\nLoadState=loaded\n"
            if "show" in argv else b""
        )
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


class _UnitFailingShowRunner(CommandRunner):
    """Like FakeSystemRunner, but one named unit's `show` calls return a
    caller-supplied bad status instead of the healthy canned one --
    used to prove a failed load/activation verification actually fails
    the update rather than being silently ignored."""

    def __init__(self, failing_unit: str, failing_output: bytes):
        super().__init__()
        self.calls = []
        self.failing_unit = failing_unit
        self.failing_output = failing_output

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if "show" in argv and self.failing_unit in argv:
            return ProcessResult(tuple(argv), 0, self.failing_output, b"")
        output = (
            b"Type=simple\nActiveState=active\nSubState=running\nResult=success\nLoadState=loaded\n"
            if "show" in argv else b""
        )
        return ProcessResult(tuple(argv), 0, output, b"")


class ManagedUnitPolicyReconciliationTests(SimpleTestCase):
    """SystemdManager.reconcile()'s activation-policy dispatch (r0022
    work) -- ENABLE_NOW vs. INSTALL_ONLY, decided purely from
    MANAGED_UNIT_POLICIES, never from which systemd_units_* list a
    manifest happens to use or from the unit's own .service/.timer
    suffix."""

    SYNC_SERVICE = "isadoraair-sync-road-conditions.service"
    SYNC_TIMER = "isadoraair-sync-road-conditions.timer"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def _unit(self, name, content="[Service]\nExecStart=/bin/true\n"):
        (self.source / "deploy" / name).write_text(content, encoding="utf-8")

    # ---- install-only service ----------------------------------------

    def test_install_only_service_is_installed_and_load_verified_but_never_enabled_or_started(self):
        self._unit(self.SYNC_SERVICE)
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)

        result = manager.reconcile(self.source, plan(systemd_units_new_required=(self.SYNC_SERVICE,)))

        installed = self.config.systemd_unit_root / self.SYNC_SERVICE
        self.assertTrue(installed.exists())
        self.assertEqual(result["installed_only"], [self.SYNC_SERVICE])
        self.assertEqual(result["enabled"], [])
        self.assertEqual(sum(call[-1] == "daemon-reload" for call in runner.calls), 1)
        enable_calls = [call for call in runner.calls if "enable" in call]
        start_calls = [call for call in runner.calls if "start" in call]
        self.assertEqual(enable_calls, [])
        self.assertEqual(start_calls, [])
        # Load verification WAS performed -- a `show --property=LoadState` call for this unit.
        show_calls = [call for call in runner.calls if "show" in call and self.SYNC_SERVICE in call]
        self.assertEqual(len(show_calls), 1)
        self.assertIn("--property=LoadState", show_calls[0])

    def test_install_only_service_daemon_reload_only_when_bytes_changed(self):
        self._unit(self.SYNC_SERVICE)
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        selected = plan(systemd_units_new_required=(self.SYNC_SERVICE,))
        manager.reconcile(self.source, selected)
        runner.calls.clear()

        manager.reconcile(self.source, selected)  # identical content, second run

        self.assertFalse(any(call[-1] == "daemon-reload" for call in runner.calls))
        # Load verification still runs every reconcile -- it's cheap,
        # read-only, and confirms the unit is STILL loaded, not merely
        # that it was once installed.
        self.assertTrue(any("show" in call and self.SYNC_SERVICE in call for call in runner.calls))

    def test_install_only_service_failing_to_load_fails_the_update(self):
        self._unit(self.SYNC_SERVICE)
        runner = _UnitFailingShowRunner(self.SYNC_SERVICE, b"LoadState=not-found\n")
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        with self.assertRaises(SystemdError):
            manager.reconcile(self.source, plan(systemd_units_new_required=(self.SYNC_SERVICE,)))

    def test_install_only_service_never_produces_an_active_state_call(self):
        # verify_unit_loaded() must query ONLY LoadState -- never
        # ActiveState/SubState/Result, which would suggest this method
        # cares whether the (never-started) unit is "running."
        self._unit(self.SYNC_SERVICE)
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        manager.reconcile(self.source, plan(systemd_units_new_required=(self.SYNC_SERVICE,)))
        show_calls = [call for call in runner.calls if "show" in call and self.SYNC_SERVICE in call]
        self.assertEqual(len(show_calls), 1)
        self.assertNotIn("--property=ActiveState", show_calls[0])

    # ---- enable-now timer ----------------------------------------------

    def test_enable_now_timer_is_installed_enabled_once_and_verified_active(self):
        self._unit(self.SYNC_TIMER, "[Timer]\nOnUnitActiveSec=5min\n")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)

        result = manager.reconcile(self.source, plan(systemd_units_new_required=(self.SYNC_TIMER,)))

        self.assertTrue((self.config.systemd_unit_root / self.SYNC_TIMER).exists())
        self.assertEqual(result["enabled"], [self.SYNC_TIMER])
        self.assertEqual(result["installed_only"], [])
        self.assertEqual(sum("enable" in call and "--now" in call and self.SYNC_TIMER in call for call in runner.calls), 1)
        active_verify_calls = [
            call for call in runner.calls
            if "show" in call and self.SYNC_TIMER in call and "--property=ActiveState" in call
        ]
        self.assertEqual(len(active_verify_calls), 1)

    def test_enable_now_timer_failure_fails_the_update(self):
        self._unit(self.SYNC_TIMER, "[Timer]\nOnUnitActiveSec=5min\n")
        runner = _UnitFailingShowRunner(
            self.SYNC_TIMER, b"Type=\nActiveState=inactive\nSubState=dead\nResult=\n",
        )
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        with self.assertRaises(SystemdError):
            manager.reconcile(self.source, plan(systemd_units_new_required=(self.SYNC_TIMER,)))

    # ---- mixed pair ------------------------------------------------------

    def test_mixed_pair_both_installed_before_activation_one_reload_service_never_run_timer_enabled(self):
        self._unit(self.SYNC_SERVICE)
        self._unit(self.SYNC_TIMER, "[Timer]\nOnUnitActiveSec=5min\n")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)

        result = manager.reconcile(
            self.source, plan(systemd_units_new_required=(self.SYNC_SERVICE, self.SYNC_TIMER)),
        )

        # Both installed.
        self.assertTrue((self.config.systemd_unit_root / self.SYNC_SERVICE).exists())
        self.assertTrue((self.config.systemd_unit_root / self.SYNC_TIMER).exists())
        # Exactly one daemon-reload for the whole pair.
        self.assertEqual(sum(call[-1] == "daemon-reload" for call in runner.calls), 1)
        # The companion service was never enabled or started.
        self.assertEqual(result["installed_only"], [self.SYNC_SERVICE])
        self.assertEqual(result["enabled"], [self.SYNC_TIMER])
        self.assertFalse(any("enable" in call and self.SYNC_SERVICE in call for call in runner.calls))
        self.assertFalse(any("start" in call and self.SYNC_SERVICE in call for call in runner.calls))
        # The install pass for BOTH units happens before any enable
        # call at all -- the timer's paired service is already on disk
        # (and already daemon-reloaded) by the time it's enabled.
        first_enable_index = next(i for i, call in enumerate(runner.calls) if "enable" in call)
        reload_index = next(i for i, call in enumerate(runner.calls) if call[-1] == "daemon-reload")
        self.assertLess(reload_index, first_enable_index)

    def test_mixed_pair_reversed_declaration_order_still_installs_both_before_activating(self):
        # The manifest/plan lists the timer BEFORE the service -- must
        # not matter, since install always happens for every unit
        # first, in one pass, before any activation at all.
        self._unit(self.SYNC_SERVICE)
        self._unit(self.SYNC_TIMER, "[Timer]\nOnUnitActiveSec=5min\n")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)

        result = manager.reconcile(
            self.source, plan(systemd_units_new_required=(self.SYNC_TIMER, self.SYNC_SERVICE)),
        )
        self.assertEqual(set(result["enabled"]), {self.SYNC_TIMER})
        self.assertEqual(set(result["installed_only"]), {self.SYNC_SERVICE})
        self.assertTrue((self.config.systemd_unit_root / self.SYNC_SERVICE).exists())

    # ---- existing core-service regression --------------------------------

    def test_existing_core_service_required_behavior_is_unchanged(self):
        self._unit("isadoraair-gunicorn.service")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)

        result = manager.reconcile(self.source, plan(systemd_units_new_required=("isadoraair-gunicorn.service",)))

        self.assertEqual(result["enabled"], ["isadoraair-gunicorn.service"])
        self.assertEqual(result["installed_only"], [])
        self.assertEqual(
            sum("enable" in call and "--now" in call and "isadoraair-gunicorn.service" in call for call in runner.calls),
            1,
        )

    def test_unknown_unit_is_still_refused_before_any_policy_lookup(self):
        self._unit("evil.service")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        with self.assertRaises(SystemdError):
            manager.reconcile(self.source, plan(systemd_units_new_required=("evil.service",)))

    def test_unknown_timer_is_refused(self):
        self._unit("evil.timer", "[Timer]\nOnUnitActiveSec=5min\n")
        runner = FakeSystemRunner()
        manager = SystemdManager(self.config, runner, enforce_root_ownership=False)
        with self.assertRaises(SystemdError):
            manager.reconcile(self.source, plan(systemd_units_new_required=("evil.timer",)))


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
