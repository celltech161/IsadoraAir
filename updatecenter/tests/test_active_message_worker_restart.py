"""D3E-B correction B2: release-driven restart authority for
isadoraair-active-message-worker (runtime 13; the r0112 spoken-worker
precedent).

Two different permissions, kept apart:

* automatic, release-driven restart -- the protected updater restarts the
  exact worker when a trusted, validated release plan names it in
  services_requiring_restart (Django manifest + planner, protected manifest
  parser, deterministic RESTART_ORDER, SystemdManager.restart_declared);
* human-operated Monitoring restart -- still NOT authorized: the worker is not
  in the root-configured operator-restart allowlist and its card has no
  Restart button.

Membership of the managed-unit set stays the signed policy's: with the active
generation-14 policy the worker is unknown, so neither install nor restart is
possible until a verified generation-15 candidate (which names it ENABLE_NOW)
is active -- even though CORE_SERVICES (and with it the compiled fallback map)
now names it.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from .phase_b_helpers import RUNTIME_ROOT, config_dict, create_release_repository  # noqa: F401

import isadoraair_updater
from isadoraair_updater.config import validate_config_dict
from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import (
    CORE_SERVICES, GENERATION_1_POLICY_DOCUMENT, KNOWN_MANAGED_UNITS, MANAGED_UNIT_POLICIES, RESTART_ORDER,
    ReleaseError, TrustedRepository, UnitActivationPolicy, derive_plan, manual_blockers, parse_manifest,
    resolve_known_managed_units, resolve_unit_policy,
)
from isadoraair_updater.systemd import SystemdError, SystemdManager
from updatecenter import manifest as manifest_mod
from updatecenter import planner

from .phase_b_helpers import manifest as protected_manifest
from .test_active_message_worker_policy import CANDIDATE, GENERATION_14, Station
from .test_manifest import _valid_followup
from .test_phase_b_systemd_daemon import FakeSystemRunner, plan
from . import test_planner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVICE = "isadoraair-active-message-worker"
UNIT = f"{SERVICE}.service"
R0115 = "29f6a3353e41f659e1e1fbe3a46859bed671cad7"
R0115_RESTART_ORDER = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
    "isadoraair-spoken-render-worker", "isadoraair-encoders", "isadoraair-rbds",
)
RUNTIME_13_RESTART_ORDER = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
    "isadoraair-spoken-render-worker", SERVICE, "isadoraair-encoders", "isadoraair-rbds",
)
NEVER_TOUCHED = ("isadoraair-engine", "isadoraair-encoders", "isadoraair-rbds", "isadoraair-render", "ogremote")


def _show(path: str, commit: str = R0115) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True, text=True).stdout


class RestartContractTests(SimpleTestCase):
    def test_application_and_protected_contracts_are_in_lockstep(self):
        self.assertEqual(RESTART_ORDER, RUNTIME_13_RESTART_ORDER)
        self.assertEqual(planner.RESTART_ORDER, RESTART_ORDER)
        self.assertEqual(manifest_mod.CORE_RESTARTABLE_SERVICES, CORE_SERVICES)
        self.assertEqual(set(RESTART_ORDER), CORE_SERVICES)
        self.assertIn(SERVICE, CORE_SERVICES)

    def test_existing_services_keep_their_relative_order(self):
        self.assertEqual(tuple(name for name in RESTART_ORDER if name != SERVICE), R0115_RESTART_ORDER)
        self.assertEqual(set(CORE_SERVICES) - {SERVICE}, set(R0115_RESTART_ORDER))

    def test_runtime_13_is_the_restart_authority_boundary_and_nothing_else_moved(self):
        self.assertEqual(isadoraair_updater.RUNTIME_VERSION, 13)
        self.assertEqual(isadoraair_updater.MANIFEST_PROTOCOL_VERSION, 5)
        self.assertEqual(isadoraair_updater.BOOTSTRAP_PROTOCOL_VERSION, 1)
        source = (RUNTIME_ROOT / "isadoraair_updater" / "__init__.py").read_text()
        self.assertIn("12 -> 13", source)
        self.assertIn(SERVICE, source)
        old = _show("deploy/updater_runtime/isadoraair_updater/__init__.py")
        for constant in ("MANIFEST_PROTOCOL_VERSION", "PROTOCOL_VERSION", "BOOTSTRAP_PROTOCOL_VERSION",
                         "SUPPORTED_WIRE_PROTOCOLS"):
            with self.subTest(constant=constant):
                line = next(text for text in source.splitlines() if text.startswith(f"{constant} ="))
                self.assertIn(f"\n{line}\n", old)

    def test_the_compiled_fallback_is_consistent_with_the_policy_source(self):
        """CORE_SERVICES seeds MANAGED_UNIT_POLICIES (the r0112 precedent): the
        compiled fallback -- consulted only when NO signed policy is active --
        names the worker ENABLE_NOW, exactly as the generation-15 policy source
        does. The generation-1 document is derived from the same map."""
        self.assertIs(MANAGED_UNIT_POLICIES[UNIT], UnitActivationPolicy.ENABLE_NOW)
        self.assertIn(UNIT, KNOWN_MANAGED_UNITS)
        self.assertEqual(GENERATION_1_POLICY_DOCUMENT.as_mapping()[UNIT], "ENABLE_NOW")
        self.assertEqual(CANDIDATE.as_mapping()[UNIT], MANAGED_UNIT_POLICIES[UNIT].name)
        for unit, policy in MANAGED_UNIT_POLICIES.items():
            with self.subTest(unit=unit):
                self.assertEqual(CANDIDATE.as_mapping().get(unit), policy.name)

    def test_the_active_signed_policy_stays_authoritative_for_membership(self):
        """Application/runtime source naming the worker never authorizes it on a
        station whose ACTIVE signed policy (generation 14) does not."""
        self.assertNotIn(UNIT, resolve_known_managed_units(active_policy=GENERATION_14))
        self.assertIn(UNIT, resolve_known_managed_units(active_policy=CANDIDATE))
        self.assertIn(UNIT, resolve_known_managed_units(active_policy=None))           # the D0/legacy fallback only
        # resolve_unit_policy() answers WHAT activating a unit means (signed entry
        # first, else the compiled map -- D3-C, unchanged); WHETHER a unit may be
        # touched at all is membership, decided by the active signed policy alone,
        # which SystemdManager checks before installing or restarting anything.
        self.assertIs(resolve_unit_policy(UNIT, signed_policy=GENERATION_14), UnitActivationPolicy.ENABLE_NOW)
        self.assertIs(resolve_unit_policy(UNIT, signed_policy=CANDIDATE), UnitActivationPolicy.ENABLE_NOW)

    def test_the_bridge_release_must_not_declare_the_restart(self):
        """Runtime 12 (generation 14, what r0115 runs) parses every manifest in the
        chain BEFORE its handoff and refuses an unknown restart name -- so the
        release that delivers runtime 13 must not itself name the worker (as r0112
        did not name the spoken worker); later releases may."""
        old_release = _show("deploy/updater_runtime/isadoraair_updater/release.py")
        old_core = old_release.split("CORE_SERVICES = frozenset({", 1)[1].split("})", 1)[0]
        self.assertNotIn(SERVICE, old_core)
        self.assertNotIn(SERVICE, _show("updatecenter/manifest.py"))


class DjangoManifestAndPlannerTests(TestCase):
    _two_release_repo = test_planner.ValidUpdateTests._two_release_repo

    def test_the_manifest_accepts_exactly_the_basename(self):
        parsed = manifest_mod.validate_manifest_dict(_valid_followup(
            services_requiring_restart=["isadoraair-gunicorn", SERVICE]))
        self.assertEqual(parsed.services_requiring_restart, ("isadoraair-gunicorn", SERVICE))
        for name in (UNIT, "isadoraair-active-message-*", "isadoraair-render", "ogremote-poll",
                     "isadoraair-active-message-worker ", "*"):
            with self.subTest(name=name), self.assertRaises(manifest_mod.ManifestError):
                manifest_mod.validate_manifest_dict(_valid_followup(services_requiring_restart=[name]))

    def test_planning_keeps_the_deterministic_order(self):
        repo, _releases, _bootstrap = self._two_release_repo({
            "services_requiring_restart": ["isadoraair-rbds", SERVICE, "isadoraair-gunicorn",
                                           "isadoraair-spoken-render-worker", "isadoraair-encoders"],
        })
        with repo:
            built = planner.build_plan(repo.work, "deploy/releases")
            self.assertEqual(built.safety_status, planner.SafetyStatus.READY_TO_PLAN)
            self.assertEqual(built.services_requiring_restart,
                             ("isadoraair-gunicorn", "isadoraair-spoken-render-worker", SERVICE,
                              "isadoraair-encoders", "isadoraair-rbds"))


class ProtectedPlanTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def derive(self, name, **kwargs):
        _author, upstream, _bootstrap, r0002, _r0003 = create_release_repository(self.root / name, **kwargs)
        repository = TrustedRepository(self.root / f"{name}.git", str(upstream), "main", CommandRunner())
        return derive_plan(repository, repository.fetch(), r0002, "r0003",
                           known_units=resolve_known_managed_units(active_policy=CANDIDATE))

    def test_the_protected_parser_accepts_exactly_the_basename(self):
        parsed = parse_manifest(protected_manifest("r0003", "r0002", services_requiring_restart=[SERVICE]),
                                label="r0003")
        self.assertEqual(parsed.services_requiring_restart, (SERVICE,))
        for name in (UNIT, "isadoraair-render", "ogremote-poll", "isadoraair-active-message-*", ""):
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                parse_manifest(protected_manifest("r0003", "r0002", services_requiring_restart=[name]),
                               label="r0003")

    def test_a_future_worker_code_release_plans_exactly_that_restart(self):
        """Synthetic r0003 after installation: the worker's code changes and the
        release names the worker -- and nothing else -- for restart."""
        result = self.derive("future", third_release_changes={"services_requiring_restart": [SERVICE]},
                             third_release_files={"active_messages/worker.py": "# worker code v2\n"})
        self.assertEqual(result.services_requiring_restart, (SERVICE,))
        self.assertEqual(manual_blockers(result, known_units=resolve_known_managed_units(active_policy=CANDIDATE)),
                         ())
        self.assertEqual((result.systemd_units_new_required, result.systemd_units_changed), ((), ()))

    def test_worker_code_without_a_declaration_plans_no_restart(self):
        result = self.derive("undeclared", third_release_files={"active_messages/worker.py": "# v2\n"})
        self.assertEqual(result.services_requiring_restart, ())

    def test_the_order_is_derived_never_taken_from_the_manifest(self):
        result = self.derive("order", third_release_changes={
            "services_requiring_restart": ["isadoraair-rbds", SERVICE, "isadoraair-gunicorn"]})
        self.assertEqual(result.services_requiring_restart, ("isadoraair-gunicorn", SERVICE, "isadoraair-rbds"))


class ProtectedExecutionTests(Station):
    def restart_calls(self, runner):
        return [call[1:3] for call in runner.calls if call[1] == "restart"]

    def test_an_authorized_restart_restarts_exactly_the_worker(self):
        runner = FakeSystemRunner()
        self.assertEqual(self.manager(runner, CANDIDATE).restart_declared((SERVICE,)), [SERVICE])
        self.assertEqual(runner.calls, [
            ["/usr/bin/systemctl", "restart", UNIT],
            ["/usr/bin/systemctl", "show", UNIT, "--property=Type", "--property=ActiveState",
             "--property=SubState", "--property=Result"],
        ])
        touched = " ".join(" ".join(call) for call in runner.calls)
        for other in NEVER_TOUCHED:
            self.assertNotIn(f"{other}.service", touched)
            self.assertNotIn(f" {other}", touched.replace(SERVICE, ""))

    def test_with_the_active_generation_14_policy_it_is_refused(self):
        runner = FakeSystemRunner()
        with self.assertRaisesRegex(SystemdError, "outside the restart allowlist"):
            self.manager(runner, GENERATION_14).restart_declared((SERVICE,))
        self.assertEqual(runner.calls, [])

    def test_a_wrong_order_or_an_unknown_name_is_refused(self):
        runner = FakeSystemRunner()
        manager = self.manager(runner, CANDIDATE)
        for services in (("isadoraair-encoders", SERVICE), (SERVICE, "isadoraair-monitoring"),
                         ("isadoraair-render",), (UNIT,), ("ogremote-poll",)):
            with self.subTest(services=services), self.assertRaises(SystemdError):
                manager.restart_declared(services)
        self.assertEqual(runner.calls, [])

    def test_existing_core_restarts_are_unchanged(self):
        for service in R0115_RESTART_ORDER:
            with self.subTest(service=service):
                runner = FakeSystemRunner()
                self.assertEqual(self.manager(runner, CANDIDATE).restart_declared((service,)), [service])
                self.assertEqual([call[1:3] for call in runner.calls],
                                 [["restart", f"{service}.service"], ["show", f"{service}.service"]])
        runner = FakeSystemRunner()
        self.manager(runner, CANDIDATE).restart_declared(R0115_RESTART_ORDER)
        self.assertEqual([call[2] for call in runner.calls if call[1] == "restart"],
                         [f"{service}.service" for service in R0115_RESTART_ORDER])

    def test_initial_activation_is_one_enable_now_and_no_restart(self):
        runner = FakeSystemRunner()
        initial = plan(systemd_units_new_required=(UNIT,), services_requiring_restart=("isadoraair-gunicorn",))
        manager = self.manager(runner, CANDIDATE)
        manager.reconcile(self.source, initial)
        manager.restart_declared(initial.services_requiring_restart)       # what the executor does next
        self.assertEqual([call[1:] for call in runner.calls if call[1] in ("enable", "restart", "start")],
                         [["enable", "--now", UNIT], ["restart", "isadoraair-gunicorn.service"]])

    def test_a_changed_template_without_a_declaration_is_installed_not_restarted(self):
        manager = self.manager(FakeSystemRunner(), CANDIDATE)
        manager.reconcile(self.source, plan(systemd_units_new_required=(UNIT,)))
        (self.source / "deploy" / UNIT).write_text((self.source / "deploy" / UNIT).read_text() + "# v2\n")
        runner = FakeSystemRunner()
        result = self.manager(runner, CANDIDATE).reconcile(self.source, plan(systemd_units_changed=(UNIT,)))
        self.assertEqual((result["changed"], result["enabled"]), ([UNIT], []))
        self.assertEqual([call[1:] for call in runner.calls], [["daemon-reload"]])   # documented: no restart

    def test_the_monitoring_operator_restart_stays_unauthorized(self):
        self.assertNotIn(UNIT, self.config.operator_restart_units)
        with self.assertRaisesRegex(SystemdError, "operator restart allowlist"):
            self.manager(FakeSystemRunner(), CANDIDATE).restart_operator_service(UNIT)
        defaults = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")),
                                        allow_local_repository=True)
        self.assertNotIn(UNIT, defaults.operator_restart_units)
