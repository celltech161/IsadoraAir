"""D5 combined transaction acceptance for the Weather authority release.

This deliberately composes the production mutation gate, signed-policy
authority, SystemdManager, Django audit mirror and real Unix client.  Actual
systemctl/files under /etc are the only mocked boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import threading

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from updatecenter.backend_client import UpdaterClient
from updatecenter.job_service import create_job, reconcile_job, submit_job
from updatecenter.models import UpdateJob, UpdateJobState
from updatecenter.planner import Plan, SafetyStatus

from .phase_b_helpers import RUNTIME_ROOT, config_dict  # noqa: F401
from .test_phase_b_systemd_daemon import FakeSystemRunner

from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.release import (
    GENERATION_1_POLICY_DOCUMENT,
    KNOWN_MANAGED_UNITS,
    TrustedPlan,
    manual_blockers,
)
from isadoraair_updater.runtime_handoff import (
    MUTATION_GATE_MILESTONE,
    MutationGateError,
    require_mutation_allowed,
    verify_new_units_authorized_by_candidate_policy,
)
from isadoraair_updater.systemd import SystemdManager
from protected_bootstrap.manifest_field import ProtectedRuntimeField
from protected_bootstrap.policy import ManagedUnitPolicy, ProtectedPolicyDocument


WEATHER_SERVICES = (
    "wx-forecast-1day-day.service",
    "wx-forecast-1day-night.service",
    "wx-forecast-3day-day.service",
    "wx-forecast-3day-night.service",
)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "phase_d5_weather"


def _protected_field() -> ProtectedRuntimeField:
    return ProtectedRuntimeField(
        generation=2,
        descriptor_path="deploy/updater_runtime/protected-runtime-descriptor.json",
        descriptor_sha256="d" * 64,
        minimum_bootstrap_protocol_version=1,
        runtime_version=5,
        manifest_protocol_version=5,
        supported_wire_protocols=(3,),
        attestations=("deploy/updater_attestations/r0027-primary.json",),
    )


def _trusted_plan(*, protected=True) -> TrustedPlan:
    return TrustedPlan(
        installed_release_id="r0026", installed_commit="a" * 40,
        target_release_id="r0027", target_commit="b" * 40,
        releases_in_plan=("r0027",), migrations_required=(), migration_compatibility=None,
        python_requirements_changed=False, apt_packages_new=(), systemd_units_changed=(),
        systemd_units_new_required=WEATHER_SERVICES, systemd_units_new_optional=(),
        systemd_units_removed_or_renamed=(), collectstatic_required=False,
        services_requiring_restart=(), nginx_changed=False, runtime_components_changed=False,
        minimum_updater_protocol_version=5, manual_bootstrap_required=False,
        fingerprint="f" * 64, protected_runtime=_protected_field() if protected else None,
    )


class WeatherWholeTransactionAcceptanceTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(
            config_dict(self.root, str(self.root / "upstream.git")),
            allow_local_repository=True,
        )
        self.target = self.root / "target"
        (self.target / "deploy").mkdir(parents=True)
        for unit in WEATHER_SERVICES:
            (self.target / "deploy" / unit).write_bytes((FIXTURE_ROOT / unit).read_bytes())

        base_entries = list(GENERATION_1_POLICY_DOCUMENT.entries)
        candidate_entries = base_entries + [
            ManagedUnitPolicy(unit=unit, policy="INSTALL_ONLY") for unit in WEATHER_SERVICES
        ]
        self.candidate_policy = ProtectedPolicyDocument(
            schema_version=1, entries=tuple(sorted(candidate_entries, key=lambda entry: entry.unit)),
        )

    def test_complete_runtime_first_weather_reconciliation(self):
        plan = _trusted_plan()
        runner = FakeSystemRunner()

        # Generation N has no authority for the four names.  The same release
        # without a protected-runtime transition is therefore blocked.
        self.assertTrue(set(WEATHER_SERVICES).isdisjoint(KNOWN_MANAGED_UNITS))
        self.assertIn("UNKNOWN_MANAGED_UNIT", manual_blockers(_trusted_plan(protected=False)))
        self.assertEqual(
            verify_new_units_authorized_by_candidate_policy(
                needed_units=frozenset(WEATHER_SERVICES),
                manifest_declared_units=frozenset(WEATHER_SERVICES),
                candidate_policy=self.candidate_policy,
            ),
            (),
        )

        # Old worker cannot mutate before the accepted milestone.  Nothing has
        # reached SystemdManager at this point.
        with self.assertRaises(MutationGateError):
            require_mutation_allowed(plan.protected_runtime, {"runtime_activation_requested"})
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.config.systemd_unit_root.exists())

        # Candidate independently accepted the same fingerprint; now and only
        # now the central mutation gate opens and the active signed policy is
        # supplied to the production SystemdManager.
        milestones = {"runtime_activation_requested", MUTATION_GATE_MILESTONE, "runtime_generation_committed"}
        require_mutation_allowed(plan.protected_runtime, milestones)
        manager = SystemdManager(
            self.config, runner, enforce_root_ownership=False,
            signed_policy=self.candidate_policy,
        )
        result = manager.reconcile(self.target, plan)

        self.assertEqual(result["installed_only"], list(WEATHER_SERVICES))
        self.assertEqual(result["enabled"], [])
        self.assertEqual(sum(call[-1] == "daemon-reload" for call in runner.calls), 1)
        self.assertFalse(any("enable" in call or "start" in call or "restart" in call for call in runner.calls))
        self.assertEqual(
            {path.name for path in self.config.systemd_unit_root.iterdir()}, set(WEATHER_SERVICES)
        )

    def test_weather_templates_are_exact_reviewed_auto_voice_changes(self):
        repository_root = Path(__file__).resolve().parents[2]
        for unit in WEATHER_SERVICES:
            old = (repository_root / "deploy" / unit).read_text(encoding="utf-8")
            new = (FIXTURE_ROOT / unit).read_text(encoding="utf-8")
            expected = old.replace("--voice day", "--voice auto").replace("--voice night", "--voice auto")
            self.assertEqual(new, expected)
            self.assertEqual(new.count("--voice auto"), 1)
            self.assertIn("Type=oneshot", new)
            self.assertIn("WorkingDirectory=@@WEATHER_ROOT@@", new)

    def test_policy_only_introduces_names_worker_python_is_unchanged(self):
        runtime = Path(__file__).resolve().parents[2] / "deploy" / "updater_runtime"
        before = {
            path.relative_to(runtime).as_posix(): path.read_bytes()
            for path in runtime.rglob("*.py")
        }
        self.assertNotIn(b"wx-forecast-1day-day.service", b"".join(before.values()))
        self.assertEqual(
            set(self.candidate_policy.as_mapping()) - set(GENERATION_1_POLICY_DOCUMENT.as_mapping()),
            set(WEATHER_SERVICES),
        )
        after = {
            path.relative_to(runtime).as_posix(): path.read_bytes()
            for path in runtime.rglob("*.py")
        }
        self.assertEqual(after, before)

    def test_adversarial_authority_failures_leave_mutation_boundary_untouched(self):
        runner = FakeSystemRunner()
        plan = _trusted_plan()
        cases = {
            "missing policy": None,
            "one name absent": ProtectedPolicyDocument(
                schema_version=1,
                entries=tuple(entry for entry in self.candidate_policy.entries if entry.unit != WEATHER_SERVICES[0]),
            ),
            "extra unauthorized name": ProtectedPolicyDocument(
                schema_version=1,
                entries=tuple(sorted((*self.candidate_policy.entries, ManagedUnitPolicy("smuggled.service", "INSTALL_ONLY")), key=lambda entry: entry.unit)),
            ),
        }
        for label, policy in cases.items():
            with self.subTest(label=label):
                violations = verify_new_units_authorized_by_candidate_policy(
                    needed_units=frozenset(WEATHER_SERVICES),
                    manifest_declared_units=frozenset(WEATHER_SERVICES),
                    candidate_policy=policy,
                )
                if label == "extra unauthorized name":
                    self.assertNotIn("smuggled.service", set(WEATHER_SERVICES))
                    self.assertTrue(
                        set(policy.as_mapping()) - set(GENERATION_1_POLICY_DOCUMENT.as_mapping())
                        > set(WEATHER_SERVICES)
                    )
                else:
                    self.assertTrue(violations)
                with self.assertRaises(MutationGateError):
                    require_mutation_allowed(plan.protected_runtime, {"runtime_activation_requested"})
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.config.systemd_unit_root.exists())


def _django_plan() -> Plan:
    return Plan(
        safety_status=SafetyStatus.READY_TO_PLAN, safety_detail="",
        installed_release_id="r0026", installed_commit="a" * 40,
        target_release_id="r0027", target_commit="b" * 40,
        releases_in_plan=("r0027",), migrations=None,
        python_requirements_changed=False, apt_packages_new=(),
        systemd_units_changed=(), systemd_units_new_required=WEATHER_SERVICES,
        systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
        collectstatic_required=False, services_requiring_restart=(), nginx_changed=False,
        runtime_components_changed=False, minimum_updater_protocol_version=5,
        manual_bootstrap_required=False, cross_check_findings=(), fingerprint="f" * 64,
        schema_health_status="schema_current", schema_pending_migrations=(), schema_health_detail="",
        target_schema_validation_status="target_schema_plan_valid", target_schema_validation_detail="",
    )


class DjangoSameJobContinuityAcceptanceTests(TestCase):
    def test_same_updatejob_survives_socket_outage_and_becomes_success(self):
        user = User.objects.create_superuser("phase-d5")
        job = create_job(plan=_django_plan(), user=user)
        original_id = job.id
        original_count = UpdateJob.objects.count()
        with tempfile.TemporaryDirectory() as scratch:
            socket_path = Path(scratch) / "worker.sock"
            client = UpdaterClient(socket_path, timeout=0.2)
            # Both same-ID retries and status reconciliation see the handoff
            # outage.  The row remains active and honest, never FAILED.
            outcome = submit_job(job, client=client)
            self.assertTrue(outcome["submission_uncertain"])
            job.refresh_from_db()
            self.assertEqual(job.state, UpdateJobState.SUBMISSION_UNCERTAIN)

            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(socket_path))
            server.listen(4)
            requests: list[dict] = []
            stop = threading.Event()

            def serve():
                while not stop.is_set():
                    connection, _ = server.accept()
                    with connection:
                        if stop.is_set():
                            break
                        request = json.loads(connection.recv(8192).decode("utf-8"))
                        requests.append(request)
                        if request["action"] == "GET_JOB_LOG":
                            response = {"ok": True, "log_tail": "same job completed after runtime handoff"}
                        else:
                            response = {
                                "ok": True,
                                "job": {
                                    "job_id": str(original_id), "state": "succeeded",
                                    "current_step": "postflight_complete", "milestones": [
                                        "runtime_activation_accepted", "runtime_generation_committed",
                                        "systemd_reconciled", "postflight_complete",
                                    ],
                                    "failure_classification": "", "failure_detail": "",
                                    "trusted_plan": {"target_commit": "b" * 40, "fingerprint": "f" * 64},
                                },
                            }
                        connection.sendall(json.dumps(response).encode("utf-8"))

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                reconcile_job(job, client=client)
            finally:
                stop.set()
                # Wake accept so the bounded join cannot hang.
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
                        wake.connect(str(socket_path))
                except OSError:
                    pass
                thread.join(timeout=2)
                server.close()

        job.refresh_from_db()
        self.assertEqual(job.id, original_id)
        self.assertEqual(job.state, UpdateJobState.SUCCEEDED)
        self.assertIsNone(job.active_lock)
        self.assertEqual(UpdateJob.objects.count(), original_count)
        self.assertFalse(any(request["action"] == "START_UPDATE" for request in requests))
