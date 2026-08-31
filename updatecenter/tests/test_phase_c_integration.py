import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.apps import apps
from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from library.models import NavMenuItem
from hardware.admin import AudioPipelineAdmin, _alsa_store
from hardware.models import AudioPipeline, DuckingConfig, RemoteDJAudioInput
from monitoring.models import MonitorCheck
from updatecenter import manifest as manifest_mod
from updatecenter.backend_client import (
    BackendRejectedError,
    BackendTransportError,
    PROTOCOL_VERSION,
    UpdaterClient,
)
from updatecenter.job_service import JobSubmissionError, create_job, reconcile_job, submit_job
from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.views import _backend_readiness, _execution_blockers


class ReadyPlan:
    safety_status = "ready_to_plan"
    schema_health_status = "schema_current"
    installed_release_id = "r0003"
    target_release_id = "r0005"
    installed_commit = "a" * 40
    target_commit = "b" * 40
    fingerprint = "f" * 64
    migrations = None
    python_requirements_changed = False
    apt_packages_new = ()
    systemd_units_removed_or_renamed = ()
    nginx_changed = False
    runtime_components_changed = False
    minimum_updater_protocol_version = PROTOCOL_VERSION
    manual_bootstrap_required = False

    def to_serializable(self):
        return {
            "installed_release_id": self.installed_release_id,
            "target_release_id": self.target_release_id,
            "target_commit": self.target_commit,
            "fingerprint": self.fingerprint,
        }


READY_PING = {
    "ok": True,
    "protocol_version": PROTOCOL_VERSION,
    "runtime_version": 3,
    "protected_runtime_valid": True,
    "config_valid": True,
    "trusted_repository_ready": True,
    "update_execution_enabled": True,
    "maintenance_busy": False,
}


class ReadinessTests(SimpleTestCase):
    def test_absent_timeout_wrong_protocol_and_disarmed_fail_closed(self):
        absent = Mock()
        absent.ping.side_effect = BackendTransportError("absent")
        self.assertFalse(_backend_readiness(absent)["ready"])

        wrong = Mock()
        wrong.ping.return_value = {**READY_PING, "protocol_version": 1}
        self.assertFalse(_backend_readiness(wrong)["ready"])

        disarmed = Mock()
        disarmed.ping.return_value = {**READY_PING, "update_execution_enabled": False}
        self.assertFalse(_backend_readiness(disarmed)["ready"])

    def test_armed_compatible_helper_is_ready(self):
        client = Mock()
        client.ping.return_value = READY_PING
        self.assertTrue(_backend_readiness(client)["ready"])


    def test_manifest_protocol_gate_is_independent_of_wire_protocol(self):
        """A helper can correctly speak socket protocol 3 while
        understanding release-manifest execution semantics protocol 5.
        The Install gate must compare a release's minimum against the
        manifest-semantics version, never backend_client.PROTOCOL_VERSION.
        """
        self.assertEqual(PROTOCOL_VERSION, 3)
        self.assertEqual(manifest_mod.UPDATER_PROTOCOL_VERSION, 5)

        request = SimpleNamespace(
            user=SimpleNamespace(is_superuser=True)
        )
        readiness = {
            **READY_PING,
            "ready": True,
            "execution_armed": True,
            "detail": "ready",
        }

        supported = ReadyPlan()
        supported.minimum_updater_protocol_version = (
            manifest_mod.UPDATER_PROTOCOL_VERSION
        )

        blockers = _execution_blockers(
            request, supported, readiness, None
        )
        self.assertNotIn(
            "The protected updater must be upgraded manually first.",
            blockers,
        )

        unsupported = ReadyPlan()
        unsupported.minimum_updater_protocol_version = (
            manifest_mod.UPDATER_PROTOCOL_VERSION + 1
        )

        blockers = _execution_blockers(
            request, unsupported, readiness, None
        )
        self.assertIn(
            "The protected updater must be upgraded manually first.",
            blockers,
        )


class MaintenanceClientTests(SimpleTestCase):
    def test_success_pending_failure_and_status_are_narrow(self):
        operation_id = str(uuid.uuid4())
        client = UpdaterClient()
        client._request = Mock(return_value={"ok": True, "maintenance": {
            "operation_id": operation_id, "action": "STORE_ALSA_STATE",
            "service": None, "state": "succeeded", "result_code": "SUCCEEDED",
        }})
        self.assertEqual(client.store_alsa_state()["state"], "succeeded")

        client._request.return_value["maintenance"]["state"] = "running"
        self.assertEqual(client.store_alsa_state()["operation_id"], operation_id)

        client._request.return_value["maintenance"]["state"] = "failed"
        client._request.return_value["maintenance"]["result_code"] = "OPERATION_FAILED"
        with self.assertRaises(BackendRejectedError):
            client.store_alsa_state()

        client._request.return_value["maintenance"]["state"] = "succeeded"
        self.assertEqual(client.get_maintenance_status(operation_id)["operation_id"], operation_id)

    def test_broker_outage_is_not_reported_as_maintenance_success(self):
        client = UpdaterClient()
        client._request = Mock(side_effect=BackendTransportError("down"))
        with self.assertRaises(BackendTransportError):
            client.restart_operator_service("isadoraair-engine.service")


@override_settings(SECURE_SSL_REDIRECT=False)
class UpdateExecutionWebTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff-c", password="x", is_staff=True)
        self.superuser = User.objects.create_superuser("root-c", password="x")

    def _post(self, user, **values):
        client = Client()
        client.force_login(user)
        payload = {
            "confirmed_target_release_id": "r0005",
            "confirmed_plan_fingerprint": "f" * 64,
            **values,
        }
        return client.post(reverse("updatecenter:start-update"), payload)

    def test_staff_may_view_but_cannot_start_and_get_is_rejected(self):
        client = Client()
        client.force_login(self.staff)
        with patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()), \
             patch("updatecenter.views._backend_readiness", return_value={**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}):
            self.assertEqual(client.get(reverse("updatecenter:dashboard")).status_code, 200)
        self.assertEqual(self._post(self.staff).status_code, 403)
        self.assertEqual(client.get(reverse("updatecenter:start-update")).status_code, 405)

    def test_superuser_eligible_post_recomputes_and_submits_server_plan(self):
        with patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()) as build, \
             patch("updatecenter.views._backend_readiness", return_value={**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}), \
             patch("updatecenter.views.submit_job", return_value={"ok": True}) as submit:
            response = self._post(self.superuser)
        self.assertEqual(response.status_code, 302)
        build.assert_called_once()
        job = UpdateJob.objects.get()
        self.assertEqual(job.target_commit, "b" * 40)
        submit.assert_called_once_with(job)

    def test_stale_release_or_fingerprint_is_rejected_before_job_creation(self):
        readiness = {**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}
        for payload in (
            {"confirmed_target_release_id": "r0004"},
            {"confirmed_plan_fingerprint": "e" * 64},
        ):
            with self.subTest(payload=payload), \
                 patch("updatecenter.views.planner.build_plan", return_value=ReadyPlan()), \
                 patch("updatecenter.views._backend_readiness", return_value=readiness), \
                 patch("updatecenter.views.submit_job") as submit:
                self.assertEqual(self._post(self.superuser, **payload).status_code, 302)
                submit.assert_not_called()
                self.assertFalse(UpdateJob.objects.exists())

    def test_refreshed_schema_or_manual_gate_blocks(self):
        readiness = {**READY_PING, "ready": True, "execution_armed": True, "detail": "ready"}
        for plan in (
            SimpleNamespace(**{**ReadyPlan.__dict__, "schema_health_status": "unapplied_migrations_detected"}),
            SimpleNamespace(**{**ReadyPlan.__dict__, "safety_status": "migration_manual_gate_required"}),
            SimpleNamespace(**{**ReadyPlan.__dict__, "manual_bootstrap_required": True}),
        ):
            with patch("updatecenter.views.planner.build_plan", return_value=plan), \
                 patch("updatecenter.views._backend_readiness", return_value=readiness), \
                 patch("updatecenter.views.create_job") as create:
                self._post(self.superuser)
                create.assert_not_called()

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.superuser)
        self.assertEqual(client.post(reverse("updatecenter:start-update"), {}).status_code, 403)


class _AcceptClient:
    def __init__(self):
        self.ids = []

    def start_update(self, **kwargs):
        self.ids.append(kwargs["job_id"])
        if len(self.ids) == 1:
            raise BackendTransportError("response lost")
        return {"ok": True, "accepted": True, "idempotent": True}


class _UnavailableClient:
    def start_update(self, **_kwargs):
        raise BackendTransportError("down")

    def get_job_status(self, _job_id):
        raise BackendTransportError("down")


class SubmissionAmbiguityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("submit-c")

    def test_lost_response_retries_same_uuid(self):
        job = create_job(plan=ReadyPlan(), user=self.user)
        client = _AcceptClient()
        submit_job(job, client=client)
        self.assertEqual(client.ids, [job.id, job.id])
        self.assertEqual(job.state, UpdateJobState.RUNNING)

    def test_unavailable_backend_is_unknown_and_retains_lock(self):
        job = create_job(plan=ReadyPlan(), user=self.user)
        result = submit_job(job, client=_UnavailableClient())
        job.refresh_from_db()
        self.assertTrue(result["submission_uncertain"])
        self.assertEqual(job.state, UpdateJobState.SUBMISSION_UNCERTAIN)
        self.assertEqual(job.active_lock, 1)
        with self.assertRaises(JobSubmissionError):
            create_job(plan=ReadyPlan(), user=self.user)

    def test_explicit_rejection_with_unavailable_status_is_uncertain_and_retains_lock(self):
        client = Mock()
        client.start_update.side_effect = BackendRejectedError("disarmed", error_code="ProtocolError")
        client.get_job_status.side_effect = BackendTransportError("down")
        job = create_job(plan=ReadyPlan(), user=self.user)
        result = submit_job(job, client=client)
        job.refresh_from_db()
        self.assertTrue(result["submission_uncertain"])
        self.assertEqual(job.state, UpdateJobState.SUBMISSION_UNCERTAIN)
        self.assertEqual(job.active_lock, 1)

    def test_explicit_rejection_reconciles_existing_root_job(self):
        client = Mock()
        client.start_update.side_effect = BackendRejectedError(
            "protected operation failed", error_code="InternalError"
        )
        job = create_job(plan=ReadyPlan(), user=self.user)
        client.get_job_status.return_value = {"ok": True, "job": {
            "job_id": str(job.id), "state": "accepted", "current_step": "accepted",
            "failure_classification": "", "failure_detail": "", "trusted_plan": None,
        }}
        result = submit_job(job, client=client)
        job.refresh_from_db()
        self.assertTrue(result["reconciled_after_uncertain_submission"])
        self.assertEqual(job.state, UpdateJobState.QUEUED)
        self.assertEqual(job.active_lock, 1)

    def test_disarmed_or_preaccept_refusal_releases_only_after_absence_proof(self):
        for detail in ("update execution is disarmed", "pre-accept validation refused"):
            with self.subTest(detail=detail):
                client = Mock()
                client.start_update.side_effect = BackendRejectedError(
                    detail, error_code="ProtocolError"
                )
                client.get_job_status.side_effect = BackendRejectedError(
                    "job does not exist", error_code="JobError"
                )
                job = create_job(plan=ReadyPlan(), user=self.user)
                with self.assertRaises(JobSubmissionError):
                    submit_job(job, client=client)
                job.refresh_from_db()
                self.assertEqual(job.state, UpdateJobState.FAILED)
                self.assertIsNone(job.active_lock)

    def test_root_proves_never_accepted_before_lock_release(self):
        client = Mock()
        client.start_update.side_effect = BackendTransportError("down")
        client.get_job_status.side_effect = BackendRejectedError(
            "job does not exist", error_code="JobError"
        )
        job = create_job(plan=ReadyPlan(), user=self.user)
        with self.assertRaises(JobSubmissionError):
            submit_job(job, client=client)
        job.refresh_from_db()
        self.assertIsNone(job.active_lock)

    def test_planned_job_left_by_gunicorn_exit_releases_only_after_root_absence_proof(self):
        client = Mock()
        client.get_job_status.side_effect = BackendRejectedError(
            "job does not exist", error_code="JobError"
        )
        job = create_job(plan=ReadyPlan(), user=self.user)
        reconcile_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.state, UpdateJobState.FAILED)
        self.assertIsNone(job.active_lock)

    def test_uncertain_job_reconciles_when_backend_returns(self):
        job = create_job(plan=ReadyPlan(), user=self.user)
        submit_job(job, client=_UnavailableClient())
        client = Mock()
        client.get_job_status.return_value = {"ok": True, "job": {
            "job_id": str(job.id), "state": "running", "current_step": "target_staged",
            "failure_classification": "", "failure_detail": "",
            "trusted_plan": {"target_commit": "b" * 40, "fingerprint": "f" * 64},
        }}
        reconcile_job(job, client=client)
        job.refresh_from_db()
        self.assertEqual(job.state, UpdateJobState.RUNNING)
        self.assertEqual(job.active_lock, 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class StatusEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("status-staff", is_staff=True)
        self.superuser = User.objects.create_superuser("status-root")

    def _job(self, **changes):
        values = dict(
            initiated_by_username="status-root",
            installed_release_id="r0003", target_release_id="r0005",
            installed_commit="a" * 40, target_commit="b" * 40,
            state=UpdateJobState.SUCCEEDED, active_lock=None,
            completed_log_snapshot="<script>bad()</script>\nfinished",
        )
        values.update(changes)
        return UpdateJob.objects.create(**values)

    def test_terminal_status_uses_durable_snapshot_without_backend(self):
        job = self._job()
        client = Client()
        client.force_login(self.staff)
        with patch("updatecenter.views.UpdaterClient") as backend:
            response = client.get(reverse("updatecenter:job-status", args=[job.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["log_tail"], job.completed_log_snapshot)
        backend.assert_not_called()

    def test_live_log_is_bounded_and_superuser_only(self):
        job = self._job(state=UpdateJobState.RUNNING, active_lock=1, completed_log_snapshot="")
        with patch("updatecenter.views._refresh_job", return_value=(job, None)), \
             patch("updatecenter.views.UpdaterClient") as backend:
            backend.return_value.get_job_log.return_value = "x" * 40000
            client = Client(); client.force_login(self.superuser)
            data = client.get(reverse("updatecenter:job-status", args=[job.id])).json()
        self.assertEqual(len(data["log_tail"]), 32768)
        self.assertFalse(data["backend_temporarily_unavailable"])

    def test_status_get_never_retries_start_update(self):
        job = self._job(state=UpdateJobState.SUBMISSION_UNCERTAIN, active_lock=1)
        with patch("updatecenter.views.UpdaterClient") as backend:
            backend.return_value.get_job_status.side_effect = BackendTransportError("down")
            client = Client(); client.force_login(self.superuser)
            response = client.get(reverse("updatecenter:job-status", args=[job.id]))
        self.assertEqual(response.status_code, 200)
        backend.return_value.start_update.assert_not_called()


@override_settings(SECURE_SSL_REDIRECT=True)
class HealthContractTests(TestCase):
    def test_health_is_unauthed_nonredirecting_and_db_backed(self):
        response = Client().get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok\n")
        self.assertNotIn(b"commit", response.content)

    def test_normal_public_http_policy_remains_redirecting(self):
        self.assertEqual(Client().get("/login/").status_code, 301)

    def test_shipped_root_postflight_target_is_dedicated_health_route(self):
        root = Path(__file__).resolve().parents[2]
        config = json.loads((root / "deploy" / "updater-station.example.json").read_text())
        self.assertEqual(config["gunicorn_health_url"], "http://127.0.0.1:8000/healthz/")

    def test_shipped_log_root_has_only_root_protected_parents(self):
        root = Path(__file__).resolve().parents[2]
        config = json.loads((root / "deploy" / "updater-station.example.json").read_text())
        self.assertEqual(config["logs_root"], "/var/lib/isadoraair-updater/logs")

    def test_bootstrap_runbook_uses_bounded_readiness_polling(self):
        root = Path(__file__).resolve().parents[2]
        runbook = (root / "docs" / "UPDATE_CENTER.md").read_text(encoding="utf-8")
        self.assertIn("for attempt in $(seq 1 30)", runbook)
        self.assertIn("Updater did not become ready within 30 seconds", runbook)
        self.assertIn("Gunicorn /healthz/ did not become ready within 30 seconds", runbook)
        self.assertIn("(\n  updater_ready=false", runbook)
        self.assertIn("(\n  gunicorn_ready=false", runbook)
        self.assertEqual(runbook.count("Do not continue unless this check succeeds."), 2)
        self.assertNotIn("isadoraair-updater.service.rendered", runbook)
        self.assertIn(
            'systemd-analyze verify "$UPDATER_STAGE/isadoraair-updater.service"',
            runbook,
        )


class NavSeedTests(TestCase):
    def setUp(self):
        NavMenuItem.objects.filter(url_name="updatecenter:dashboard").delete()
        self.migration = importlib.import_module("library.migrations.0080_seed_updates_nav_item")

    def test_missing_entry_created_once_without_touching_other_items(self):
        other = NavMenuItem.objects.create(label="Station Custom", custom_url="/custom/", sort_order=77)
        self.migration.seed_updates_nav(apps, None)
        self.migration.seed_updates_nav(apps, None)
        self.assertEqual(NavMenuItem.objects.filter(url_name="updatecenter:dashboard").count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.sort_order, 77)

    def test_existing_station_customization_is_preserved(self):
        existing = NavMenuItem.objects.create(
            label="Station Software", url_name="updatecenter:dashboard",
            sort_order=3, enabled=False,
        )
        self.migration.seed_updates_nav(apps, None)
        existing.refresh_from_db()
        self.assertEqual(existing.label, "Station Software")
        self.assertEqual(existing.sort_order, 3)
        self.assertFalse(existing.enabled)
        self.assertEqual(NavMenuItem.objects.filter(url_name="updatecenter:dashboard").count(), 1)

    def test_reverse_removes_only_untouched_product_marked_row(self):
        self.migration.seed_updates_nav(apps, None)
        self.migration.unseed_updates_nav(apps, None)
        self.assertFalse(NavMenuItem.objects.filter(url_name="updatecenter:dashboard").exists())

        custom = NavMenuItem.objects.create(
            label="My Updates", url_name="updatecenter:dashboard", sort_order=4, enabled=False
        )
        self.migration.unseed_updates_nav(apps, None)
        self.assertTrue(NavMenuItem.objects.filter(pk=custom.pk).exists())


class PrivilegeCleanupStaticTests(SimpleTestCase):
    def test_web_paths_have_no_direct_sudo_or_root_process_restart(self):
        root = Path(__file__).resolve().parents[2]
        for relative in ("hardware/admin.py", "rbds/admin.py", "monitoring/views.py"):
            text = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn('"sudo"', text, relative)
            self.assertNotIn("subprocess.Popen", text, relative)


class HardwareBrokerTests(TestCase):
    def setUp(self):
        self.model_admin = AudioPipelineAdmin(AudioPipeline, django_admin.AdminSite())
        self.pipeline = AudioPipeline.load()

    @staticmethod
    def _form(changed):
        ducking = DuckingConfig.load()
        remote = RemoteDJAudioInput.load()
        return SimpleNamespace(
            changed_data=list(changed),
            cleaned_data={
                "ducking_enabled": ducking.enabled,
                "duck_level_db": ducking.duck_level_db,
                "remote_dj_gain_db": remote.gain_db,
            },
        )

    def test_only_pipeline_topology_change_requests_exact_engine_restart(self):
        with patch("hardware.admin.UpdaterClient") as backend:
            self.model_admin.save_model(None, self.pipeline, self._form(["sample_rate"]), True)
            backend.return_value.restart_operator_service.assert_called_once_with(
                "isadoraair-engine.service"
            )
            backend.reset_mock()
            self.model_admin.save_model(None, self.pipeline, self._form(["vu_meter_min_db"]), True)
            backend.assert_not_called()

    def test_broker_failure_does_not_rollback_admin_save(self):
        with patch("hardware.admin.UpdaterClient") as backend, \
             patch("hardware.admin.emit_event") as event:
            backend.return_value.restart_operator_service.side_effect = BackendTransportError("down")
            self.model_admin.save_model(None, self.pipeline, self._form(["program_gain_db"]), True)
        event.assert_called_once()

    def test_alsa_persistence_is_fixed_client_action_and_failure_nonfatal(self):
        with patch("hardware.admin.UpdaterClient") as backend:
            _alsa_store()
            backend.return_value.store_alsa_state.assert_called_once_with()
        with patch("hardware.admin.UpdaterClient") as backend, \
             patch("hardware.admin.emit_event") as event:
            backend.return_value.store_alsa_state.side_effect = BackendTransportError("down")
            _alsa_store()
        event.assert_called_once()


@override_settings(SECURE_SSL_REDIRECT=False)
class MonitoringBrokerTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("monitor-c", is_staff=True)
        self.client = Client(); self.client.force_login(self.staff)

    def test_allowed_monitor_request_uses_narrow_client(self):
        check = MonitorCheck.objects.create(
            name="restart allowed", kind="systemd", systemd_unit="isadoraair-engine.service"
        )
        with patch("monitoring.views.UpdaterClient") as backend:
            backend.return_value.restart_operator_service.return_value = {
                "operation_id": str(uuid.uuid4()), "state": "running",
            }
            response = self.client.post(reverse("monitoring:api-restart-check", args=[check.id]))
        self.assertEqual(response.status_code, 202)
        backend.return_value.restart_operator_service.assert_called_once_with("isadoraair-engine.service")

    def test_db_injected_unit_is_rejected_by_root_broker(self):
        check = MonitorCheck.objects.create(
            name="restart injected", kind="systemd", systemd_unit="arbitrary.service"
        )
        with patch("monitoring.views.UpdaterClient") as backend:
            backend.return_value.restart_operator_service.side_effect = BackendRejectedError(
                "outside root allowlist", error_code="ProtocolError"
            )
            response = self.client.post(reverse("monitoring:api-restart-check", args=[check.id]))
        self.assertEqual(response.status_code, 503)

    def test_non_systemd_check_rejected_before_broker(self):
        check = MonitorCheck.objects.create(name="disk only", kind="disk", disk_path="/")
        with patch("monitoring.views.UpdaterClient") as backend:
            response = self.client.post(reverse("monitoring:api-restart-check", args=[check.id]))
        self.assertEqual(response.status_code, 404)
        backend.assert_not_called()
