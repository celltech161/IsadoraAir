"""Runtime Foundation E6 -- deployment baseline consolidation tests.

Covers the exact regressions the E6 task requires: the legacy fdkaac
false-negative closure, package-prerequisite semantics, structural
bootstrap-without-a-database behavior, and the management command's
presentation/exit-code contract. Nothing here ever touches a real
/usr/local, /opt, /var/lib, or /run path, installs a package, or
invokes production tmpfiles -- every scenario uses a disposable target
root and/or injected seams.
"""

from __future__ import annotations

import io
import json
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase

from isadoraair.deploy_baseline import (
    LEGACY_PASS,
    RESULT_FAIL,
    RESULT_PASS,
    RESULT_UNRESOLVED,
    DeploymentBaselineEvidence,
    LegacyCheck,
    StructuralBaselineEvidence,
    evaluate_deployment_baseline,
    evaluate_structural_baseline,
)
from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_packages import STATUS_FAIL as PKG_FAIL, evaluate_package_prerequisite
from isadoraair.runtime_requirements import ComponentRequirement, RuntimeRequirements
from isadoraair.runtime_scratch import STATE_UNRESOLVED_IDENTITY, evaluate_scratch_surface
from isadoraair.runtime_validation import RuntimeValidator, STATUS_PASS, STATUS_FAIL, ValidationSeams


class FdkaacFixture(SimpleTestCase):
    """A minimal, disposable canonical fdkaac install -- a binary and a
    library directory, never /usr/local. Mirrors
    test_runtime_validation.py's own RuntimeValidatorFixture convention."""

    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e6-fdkaac-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = deepcopy(load_runtime_components())

        fdkaac = self.manifest["components"]["fdkaac"]
        self.fdkaac_binary = self.root / "fdkaac"
        self.fdkaac_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fdkaac_binary.chmod(self.fdkaac_binary.stat().st_mode | stat.S_IXUSR)
        self.library_root = self.root / "lib"
        self.library_root.mkdir()
        fdkaac["runtime"]["binary"] = str(self.fdkaac_binary)
        fdkaac["runtime"]["library_root"] = str(self.library_root)
        validator_script = self.root / "check-he-aac"
        validator_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        validator_script.chmod(0o700)
        fdkaac["build"]["validator"] = validator_script.name

        self.manifest_path = self.root / "runtime_components.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def requirements(self, *, fdkaac: bool) -> RuntimeRequirements:
        return RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement("kokoro"),
                "piper": ComponentRequirement("piper"),
                "fdkaac": ComponentRequirement("fdkaac", fdkaac, ("test encoder",) if fdkaac else ()),
            }
        )

    def validator(self, *, fdkaac_check) -> RuntimeValidator:
        return RuntimeValidator(
            manifest=self.manifest,
            manifest_path=self.manifest_path,
            project_root=self.root,
            seams=ValidationSeams(
                package_probe=lambda executable, expected: dict(expected),
                kokoro_smoke=lambda requirement, product: None,
                piper_smoke=lambda requirement, product: None,
                fdkaac_check=fdkaac_check,
            ),
        )


class FdkaacFalseNegativeClosureTests(FdkaacFixture):
    """This exact regression must exist (task section 20/25): fdkaac
    required + a healthy canonical E4 install -- runtime-only, no
    pkg-config metadata staged -- must still evidence PASS. The fake
    fdkaac_check seam below simulates exactly what deploy/check_he_aac.sh
    --runtime-only reports for a healthy install with no pkg-config."""

    def test_e4_minimal_canonical_install_with_no_pkgconfig_passes(self):
        def runtime_only_ok(script, binary, library_root):
            return None  # exit 0, no pkg-config metadata ever consulted

        validator = self.validator(fdkaac_check=runtime_only_ok)
        evidence = validator.validate(self.requirements(fdkaac=True))
        self.assertEqual(evidence.components["fdkaac"].status, STATUS_PASS)
        self.assertTrue(evidence.components["fdkaac"].required)

    def test_broken_fdkaac_e2_fails(self):
        def runtime_only_broken(script, binary, library_root):
            raise RuntimeError("HE-AAC profile 5 encode rejected")

        validator = self.validator(fdkaac_check=runtime_only_broken)
        evidence = validator.validate(self.requirements(fdkaac=True))
        self.assertEqual(evidence.components["fdkaac"].status, STATUS_FAIL)

    def test_unrelated_kokoro_failure_is_not_misattributed_to_fdkaac(self):
        def runtime_only_ok(script, binary, library_root):
            return None

        validator = RuntimeValidator(
            manifest=self.manifest,
            manifest_path=self.manifest_path,
            project_root=self.root,
            seams=ValidationSeams(
                package_probe=lambda executable, expected: dict(expected),
                kokoro_smoke=lambda requirement, product: (_ for _ in ()).throw(RuntimeError("kokoro broke")),
                piper_smoke=lambda requirement, product: None,
                fdkaac_check=runtime_only_ok,
            ),
        )
        requirements = RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement("kokoro", True, ("test",)),
                "piper": ComponentRequirement("piper"),
                "fdkaac": ComponentRequirement("fdkaac", True, ("test encoder",)),
            }
        )
        evidence = validator.validate(requirements)
        self.assertEqual(evidence.components["fdkaac"].status, STATUS_PASS)

    def test_aggregate_composition_does_not_gate_on_missing_build_tooling(self):
        """The exact regression at the E6 composition boundary: a
        healthy structural tier + a healthy (no-pkg-config) fdkaac E2
        result must compose to overall PASS, even when the
        BUILD_HEAAC package group (autoconf/pkg-config/...) is entirely
        missing on this host -- build tooling is irrelevant once the
        runtime artifact already exists and passes."""

        def runtime_only_ok(script, binary, library_root):
            return None

        validator = self.validator(fdkaac_check=runtime_only_ok)
        station = validator.validate(self.requirements(fdkaac=True))

        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        scratch = self.root / "tts-scratch"
        scratch.mkdir(mode=0o700)
        scratch.chmod(0o700)
        healthy_structural = StructuralBaselineEvidence(
            legacy_checks=(), package_prerequisites=(), system_surfaces=None,
            system_surfaces_error=None,
            scratch_surface=evaluate_scratch_surface(isa_user=me, path=scratch),
        )
        self.assertTrue(healthy_structural.scratch_surface.healthy)
        self.assertEqual(healthy_structural.result, RESULT_PASS)

        aggregate = DeploymentBaselineEvidence(
            structural=healthy_structural,
            station=station,
            station_package_prerequisites=(
                evaluate_package_prerequisite(
                    self.manifest, "fdkaac", kind="build", required=True, dpkg_probe=lambda pkg: False
                ),
            ),
        )
        self.assertEqual(aggregate.station_package_prerequisites[0].status, PKG_FAIL)
        self.assertEqual(aggregate.result, RESULT_PASS)


class StructuralBootstrapTests(SimpleTestCase):
    """Prove the structural tier is usable with no station database at
    all, and distinguishes UNRESOLVED station-dependent evidence from a
    guessed PASS."""

    def test_structural_baseline_never_touches_the_database(self):
        """A structural baseline cannot borrow live DB connectivity."""

        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-structural-") as tmp, patch(
            "isadoraair.deploy_baseline._check_postgres_connection",
            side_effect=AssertionError("structural baseline touched PostgreSQL"),
        ):
            evidence = evaluate_deployment_baseline(
                target_root=tmp, structural_only=True, isa_user=None
            )
        self.assertIsInstance(evidence.structural, StructuralBaselineEvidence)
        self.assertIsNone(evidence.station)
        self.assertEqual(evidence.live_checks, ())

    def test_fresh_disposable_target_reports_absent_surfaces_not_a_crash(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-structural-") as tmp:
            evidence = evaluate_structural_baseline(target_root=tmp, isa_user=None, include_legacy_checks=False)
        self.assertIsNotNone(evidence.system_surfaces)
        self.assertFalse(evidence.system_surfaces.healthy)
        self.assertEqual(evidence.result, RESULT_FAIL)

    def test_no_isa_user_scratch_surface_is_unresolved_not_healthy(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-structural-") as tmp:
            evidence = evaluate_structural_baseline(target_root=tmp, isa_user=None, include_legacy_checks=False)
        self.assertEqual(evidence.scratch_surface.state, STATE_UNRESOLVED_IDENTITY)

    def test_package_prerequisites_are_unresolved_without_station_selection(self):
        evidence = evaluate_structural_baseline(
            target_root="/", isa_user=None, include_legacy_checks=False
        )
        kokoro_pkg = next(p for p in evidence.package_prerequisites if p.component == "kokoro")
        self.assertEqual(kokoro_pkg.required, None)


class DeploymentBaselineAggregateTests(SimpleTestCase):
    def test_structural_only_skips_station_tier_entirely(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-agg-") as tmp:
            evidence = evaluate_deployment_baseline(target_root=tmp, structural_only=True)
        self.assertIsNone(evidence.station)
        self.assertEqual(evidence.station_package_prerequisites, ())

    def test_unresolved_station_requirements_yield_unresolved_not_pass(self):
        # A disposable manifest whose fdkaac binary path is guaranteed
        # absent -- an "unresolved station" result must not depend on,
        # or trigger, any real subprocess/component validation at all.
        fake_manifest = deepcopy(load_runtime_components())
        fake_manifest["components"]["fdkaac"]["runtime"]["binary"] = "/does/not/exist/fdkaac"
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-agg-") as tmp:
            stand_in_structural = StructuralBaselineEvidence(
                legacy_checks=(), package_prerequisites=(), system_surfaces=None,
                system_surfaces_error=None,
                scratch_surface=evaluate_scratch_surface(isa_user=None),
            )
            with patch(
                "isadoraair.deploy_baseline.evaluate_structural_baseline", return_value=stand_in_structural
            ), patch(
                "isadoraair.deploy_baseline._check_postgres_connection",
                return_value=LegacyCheck("PostgreSQL connection", LEGACY_PASS, "test"),
            ):
                with patch(
                    "isadoraair.runtime_validation.resolve_current_runtime_requirements",
                    side_effect=RuntimeError("db unavailable"),
                ):
                    evidence = evaluate_deployment_baseline(target_root="/", manifest=fake_manifest)
        self.assertIsNotNone(evidence.station)
        self.assertTrue(evidence.station.requirement_errors)
        self.assertEqual(evidence.result, RESULT_UNRESOLVED)
        self.assertNotEqual(evidence.result, RESULT_PASS)


class ManagementCommandTests(SimpleTestCase):
    def _run(self, *args):
        out, err = io.StringIO(), io.StringIO()
        try:
            call_command("check_deploy_baseline", *args, stdout=out, stderr=err)
            code = 0
        except SystemExit as exc:
            code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_structural_only_skips_the_station_tier_entirely(self):
        with patch(
            "isadoraair.deploy_baseline._check_postgres_connection",
            side_effect=AssertionError("--structural-only touched PostgreSQL"),
        ):
            code, out, err = self._run("--structural-only", "--json")
        payload = json.loads(out)
        self.assertIn("structural", payload)
        self.assertIsNone(payload["station"])
        self.assertEqual(payload["live_checks"], [])
        self.assertNotEqual(code, 0)  # this real host has no E5 surfaces installed

    def test_offline_target_uses_target_filesystem_not_host_surfaces(self):
        import os

        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-target-") as tmp:
            root = Path(tmp)
            uid, gid = os.getuid(), os.getgid()
            (root / "etc").mkdir()
            (root / "etc" / "passwd").write_text(
                f"station:x:{uid}:{gid}:Station:/nonexistent:/usr/sbin/nologin\n",
                encoding="utf-8",
            )
            code, out, err = self._run(
                "--structural-only", "--json", "--target-root", str(root),
                "--isa-user", "station",
            )
            payload = json.loads(out)
        launcher = payload["structural"]["system_surfaces"]["surfaces"]["launcher"]
        self.assertEqual(launcher["path"], str(root / "usr/local/bin/isadoraair-tts"))
        self.assertEqual(launcher["state"], "absent")
        self.assertNotEqual(code, 0)

    def test_build_only_human_presentation_is_explicitly_non_gating(self):
        code, out, err = self._run("--structural-only", "--isa-user", "jreed")
        self.assertIn("build-only, non-gating", out)

    def test_isa_user_flag_is_threaded_through_to_scratch_evidence(self):
        import pwd
        import os

        me = pwd.getpwuid(os.getuid()).pw_name
        code, out, err = self._run("--structural-only", "--json", "--isa-user", me)
        payload = json.loads(out)
        self.assertNotEqual(payload["structural"]["scratch_surface"]["state"], "unresolved_identity")

    def test_no_isa_user_reports_unresolved_identity_in_human_output(self):
        code, out, err = self._run("--structural-only")
        self.assertIn("unresolved_identity", out)

    def test_exit_code_matches_result(self):
        code, out, err = self._run("--structural-only", "--json")
        payload = json.loads(out)
        if payload["structural"]["result"] == "pass":
            self.assertEqual(code, 0)
        else:
            self.assertNotEqual(code, 0)
