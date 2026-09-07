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
    LEGACY_DEGRADED,
    LEGACY_MISSING,
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
from isadoraair.runtime_packages import (
    STATUS_FAIL as PKG_FAIL,
    STATUS_PASS as PKG_PASS,
    STATUS_UNRESOLVED as PKG_UNRESOLVED,
    PackagePrerequisiteEvidence,
    evaluate_package_prerequisite,
)
from isadoraair.runtime_requirements import ComponentRequirement, RuntimeRequirements
from isadoraair.runtime_scratch import STATE_UNRESOLVED_IDENTITY, evaluate_scratch_surface
from isadoraair.runtime_validation import RuntimeEvidence, RuntimeValidator, STATUS_PASS, STATUS_FAIL, ValidationSeams


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


class SndAloopDeferredLiveEvidenceTests(SimpleTestCase):
    """r0042, Defect B: Stage 95's canonical acceptance point is
    post-Stage-90, pre-service-activation -- BEFORE any reboot or manual
    'modprobe snd-aloop' has ever happened. The kernel module's actual
    LOADED state (and card layout) is therefore live, deferred
    operational evidence, never gating pre-boot software-restore
    completeness -- exactly the same non-gating treatment
    /run/isadoraair's own absence already gets in _check_directories().
    The structural, gating question -- was
    deploy/isadoraair-aloop.conf correctly INSTALLED? -- is answered
    separately (see SndAloopModprobeConfigStructuralCheckTests below).
    Real /proc reads are faked via a targeted Path.read_text patch --
    never real kernel-module state, which this test process cannot
    control anyway."""

    def _check(self, *, modules_text=None, cards_text=None, modules_error=False, cards_error=False):
        from pathlib import Path as RealPath
        from unittest.mock import patch

        from isadoraair.deploy_baseline import _check_snd_aloop

        real_read_text = RealPath.read_text

        def fake_read_text(self, *args, **kwargs):
            if str(self) == "/proc/modules":
                if modules_error:
                    raise OSError("simulated /proc/modules failure")
                return modules_text
            if str(self) == "/proc/asound/cards":
                if cards_error:
                    raise OSError("simulated /proc/asound/cards failure")
                return cards_text
            return real_read_text(self, *args, **kwargs)

        with patch("pathlib.Path.read_text", fake_read_text):
            return _check_snd_aloop()

    def test_module_not_loaded_is_degraded_not_missing(self):
        results = self._check(modules_text="some_other_module 12345 0 - Live 0x0\n")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].label, "snd-aloop module")
        self.assertEqual(results[0].state, LEGACY_DEGRADED)

    def test_procfs_unreadable_is_degraded_not_missing(self):
        results = self._check(modules_error=True)
        self.assertEqual(results[0].state, LEGACY_DEGRADED)

    def test_no_loopback_cards_found_is_degraded_not_missing(self):
        results = self._check(
            modules_text="snd_aloop 20480 3 - Live 0x0\n",
            cards_text=" 0 [PCH ]: HDA-Intel - HDA Intel PCH\n",
        )
        card_check = next(r for r in results if r.label == "snd-aloop card layout")
        self.assertEqual(card_check.state, LEGACY_DEGRADED)

    def test_cards_procfs_unreadable_is_degraded_not_missing(self):
        results = self._check(modules_text="snd_aloop 20480 3 - Live 0x0\n", cards_error=True)
        card_check = next(r for r in results if r.label == "snd-aloop card layout")
        self.assertEqual(card_check.state, LEGACY_DEGRADED)

    def test_correct_layout_still_passes(self):
        results = self._check(
            modules_text="snd_aloop 20480 3 - Live 0x0\n",
            cards_text=(
                " 0 [Loopback       ]: Loopback - Loopback\n"
                " 3 [Loopback_1     ]: Loopback - Loopback\n"
                " 4 [Loopback_2     ]: Loopback - Loopback\n"
            ),
        )
        for check in results:
            self.assertEqual(check.state, LEGACY_PASS, check.detail)

    def test_no_deferred_reason_ever_gates_legacy_checks_result(self):
        """None of _check_snd_aloop's own possible states may equal
        LEGACY_MISSING -- the one state legacy_checks()/
        StructuralBaselineEvidence.result actually gates on."""
        for kwargs in (
            {"modules_text": "unrelated 1 2 - Live 0x0\n"},
            {"modules_error": True},
            {
                "modules_text": "snd_aloop 20480 3 - Live 0x0\n",
                "cards_text": "",
            },
            {"modules_text": "snd_aloop 20480 3 - Live 0x0\n", "cards_error": True},
        ):
            for check in self._check(**kwargs):
                self.assertNotEqual(check.state, LEGACY_MISSING, f"{check.label}: {check.detail}")


class SndAloopModprobeConfigStructuralCheckTests(SimpleTestCase):
    """r0042, Defect B: the modprobe.d config's INSTALLATION (never
    whether the module happens to be loaded right now) is what must
    gate here -- target-root-mapped, so an offline staging target is
    covered too, unlike the live-only module/card-layout evidence
    above. Never touches the real /etc -- a disposable target_root
    stands in for it throughout."""

    def _check(self, target_root):
        from isadoraair.deploy_baseline import _check_directories

        project_root = Path(__file__).resolve().parent.parent.parent
        return _check_directories(target_root=Path(target_root), project_root=project_root, isa_user=None)

    def _aloop_check(self, target_root):
        return next(c for c in self._check(target_root) if c.label == "snd-aloop modprobe.d config")

    def test_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-aloop-") as tmp:
            check = self._aloop_check(tmp)
        self.assertEqual(check.state, LEGACY_MISSING)

    def test_correctly_installed_config_passes(self):
        source = Path(__file__).resolve().parent.parent.parent / "deploy" / "isadoraair-aloop.conf"
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-aloop-") as tmp:
            dest_dir = Path(tmp) / "etc" / "modprobe.d"
            dest_dir.mkdir(parents=True)
            (dest_dir / "isadoraair-aloop.conf").write_text(
                source.read_text(encoding="utf-8"), encoding="utf-8"
            )
            check = self._aloop_check(tmp)
        self.assertEqual(check.state, LEGACY_PASS)

    def test_mismatched_content_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-aloop-") as tmp:
            dest_dir = Path(tmp) / "etc" / "modprobe.d"
            dest_dir.mkdir(parents=True)
            (dest_dir / "isadoraair-aloop.conf").write_text(
                "options snd-aloop enable=1 index=9\n", encoding="utf-8"
            )
            check = self._aloop_check(tmp)
        self.assertEqual(check.state, LEGACY_MISSING)
        self.assertEqual(check.detail, "content mismatch")


class ScratchSurfaceAbsentIsDeferredNotFailTests(SimpleTestCase):
    """r0042, Defect B: /run/isadoraair/tts does not exist until
    systemd-tmpfiles runs it at boot (deploy/isadoraair-tmpfiles.conf) --
    Stage 95's canonical acceptance point is BEFORE that ever happens.
    STATE_ABSENT must therefore be non-gating here, exactly like
    /run/isadoraair's own parent-directory absence already is in
    _check_directories() -- every OTHER non-healthy scratch state
    (wrong type/owner, unsafe permissions/ancestry, symlink) reflects an
    actual problem with something that DOES exist, and must still fail
    closed."""

    def _structural(self, scratch):
        return StructuralBaselineEvidence(
            legacy_checks=(), package_prerequisites=(), system_surfaces=None,
            system_surfaces_error=None, scratch_surface=scratch,
        )

    def test_absent_scratch_surface_does_not_fail_the_structural_tier(self):
        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-scratch-") as tmp:
            scratch = evaluate_scratch_surface(isa_user=me, path=Path(tmp) / "does-not-exist" / "tts")
        self.assertEqual(scratch.state, "absent")
        structural = self._structural(scratch)
        self.assertEqual(structural.result, RESULT_PASS)

    def test_wrong_owner_still_fails_closed(self):
        """A DIFFERENT non-healthy state -- something that actually
        exists but is wrong -- must not be swept up by the same
        deferral; only genuine absence is deferred."""
        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-scratch-") as tmp:
            bogus_uid = os.getuid() + 1
            scratch = evaluate_scratch_surface(
                isa_user=me, path=Path(tmp), expected_uid=bogus_uid, expected_gid=os.getgid()
            )
        self.assertEqual(scratch.state, "wrong_owner")
        structural = self._structural(scratch)
        self.assertEqual(structural.result, RESULT_FAIL)


class Stage95PreBootAcceptanceTargetTests(SimpleTestCase):
    """r0042: the explicit Stage-95 pre-boot acceptance target. Proves
    Stage 95's real canonical scenario -- Stage 90 has just installed
    every declarative config, but nothing has booted/reloaded/started
    yet -- reaches an overall PASS: the snd-aloop kernel module is not
    loaded, /run/isadoraair/tts does not exist yet, but every
    structurally-installed declaration (modprobe.d config, tmpfiles.d
    config) is present and correct, and the scratch-surface identity IS
    resolvable. Does NOT require, and must never require, actual
    audio/RF hardware commissioning."""

    def test_pre_boot_state_with_correct_declarations_passes(self):
        import os
        import pwd
        from pathlib import Path as RealPath
        from unittest.mock import patch

        me = pwd.getpwuid(os.getuid()).pw_name
        project_root = Path(__file__).resolve().parent.parent.parent
        real_read_text = RealPath.read_text

        def fake_proc_read_text(self, *args, **kwargs):
            if str(self) == "/proc/modules":
                return "unrelated_module 1 2 - Live 0x0\n"  # snd_aloop not loaded
            if str(self) == "/proc/asound/cards":
                return " 0 [PCH ]: HDA-Intel - HDA Intel PCH\n"  # no loopback cards yet
            return real_read_text(self, *args, **kwargs)

        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-stage95-target-") as tmp:
            target_root = Path(tmp)
            # Stage 90's declared, structural configs -- installed, correct,
            # but not yet acted on by systemd-tmpfiles/a reboot.
            modprobe_dir = target_root / "etc" / "modprobe.d"
            modprobe_dir.mkdir(parents=True)
            (modprobe_dir / "isadoraair-aloop.conf").write_text(
                (project_root / "deploy" / "isadoraair-aloop.conf").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            tmpfiles_dir = target_root / "etc" / "tmpfiles.d"
            tmpfiles_dir.mkdir(parents=True)
            (tmpfiles_dir / "isadoraair.conf").write_text(
                (project_root / "deploy" / "isadoraair-tmpfiles.conf")
                .read_text(encoding="utf-8")
                .replace("@@ISA_USER@@", me),
                encoding="utf-8",
            )
            # /run/isadoraair/tts deliberately NOT created -- deferred
            # until systemd-tmpfiles runs it at boot.
            opt_root = target_root / "opt" / "isadoraair"
            opt_root.mkdir(parents=True)
            (opt_root / "manage.py").write_text("", encoding="utf-8")
            library_root = target_root / "srv" / "isadoraair" / "music"
            library_root.mkdir(parents=True)

            env_without_library_root = {k: v for k, v in os.environ.items() if k != "LIBRARY_ROOT"}
            with patch("pathlib.Path.read_text", fake_proc_read_text), patch.dict(
                os.environ, env_without_library_root, clear=True
            ):
                from isadoraair.deploy_baseline import _check_directories, _check_snd_aloop

                # The same two evidence sources legacy_checks() composes
                # for a real (non-staging) target -- live module/card
                # evidence plus target-mapped structural directory/config
                # evidence -- exercised together against this disposable
                # tree rather than the real host's own live "/".
                checks = list(_check_snd_aloop()) + list(
                    _check_directories(target_root=target_root, project_root=project_root, isa_user=me)
                )
            for check in checks:
                self.assertNotEqual(
                    check.state, LEGACY_MISSING, f"{check.label} unexpectedly MISSING: {check.detail}"
                )
            self.assertTrue(any(c.label == "snd-aloop module" for c in checks))
            self.assertTrue(any(c.label == "snd-aloop modprobe.d config" and c.state == LEGACY_PASS for c in checks))
            self.assertTrue(any(c.label == "TTS scratch tmpfiles config" and c.state == LEGACY_PASS for c in checks))


class StructuralStationPackageSelectionSupersessionTests(SimpleTestCase):
    """r0042, Defect C: DeploymentBaselineEvidence.result used to defer
    to `structural.result` unconditionally whenever it was
    RESULT_UNRESOLVED, even after the station/live tier immediately
    above had already positively resolved (PASS) the exact corresponding
    runtime package group that caused it -- structural-only can never
    inspect station DB/config for package selection, so a fully healthy
    canonical live baseline could stay UNRESOLVED forever. Only that ONE
    specific reason may now be superseded by a subsequently-available,
    more-informed station tier; scratch-surface identity ambiguity must
    never be, and neither may any other structural FAIL -- both are
    covered by ScratchSurfaceAbsentIsDeferredNotFailTests'
    test_wrong_owner_still_fails_closed sibling and by
    UnresolvedIdentityIsNeverSupersededTests below."""

    def _healthy_scratch(self):
        import os
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name
        with tempfile.TemporaryDirectory(prefix="isadoraair-e6-defect-c-") as tmp:
            scratch = tempfile.mkdtemp(dir=tmp)
            os.chmod(scratch, 0o700)
            return evaluate_scratch_surface(isa_user=me, path=Path(scratch))

    def test_station_pass_supersedes_structural_package_selection_unresolved(self):
        structural = StructuralBaselineEvidence(
            legacy_checks=(),
            package_prerequisites=(
                PackagePrerequisiteEvidence(
                    component="kokoro", kind="runtime", group="OPTIONAL_KOKORO_TTS",
                    required=None, reasons=("station configuration not inspected",),
                    status=PKG_UNRESOLVED,
                    diagnostics=("unable to determine package status because the trusted dpkg "
                                 "probe failed for: python3-kokoro",),
                ),
            ),
            system_surfaces=None, system_surfaces_error=None,
            scratch_surface=self._healthy_scratch(),
        )
        self.assertEqual(structural.result, RESULT_UNRESOLVED)
        self.assertFalse(structural.has_unresolved_identity)

        station = RuntimeEvidence(
            runtime_contract_sha256=None, runtime_manifest_schema_version=None, components={},
        )
        self.assertEqual(station.result, STATUS_PASS)

        aggregate = DeploymentBaselineEvidence(
            structural=structural,
            station=station,
            station_package_prerequisites=(
                PackagePrerequisiteEvidence(
                    component="kokoro", kind="runtime", group="OPTIONAL_KOKORO_TTS",
                    required=True, status=PKG_PASS,
                ),
            ),
        )
        self.assertEqual(aggregate.result, RESULT_PASS)

    def test_structural_only_still_reports_unresolved_for_the_same_evidence(self):
        """The exact same structural-only evidence, with no station tier
        at all (--structural-only, or a non-canonical target_root) --
        there is no more-informed tier to defer to, so the full
        structural.result must still govern exactly as before."""
        structural = StructuralBaselineEvidence(
            legacy_checks=(),
            package_prerequisites=(
                PackagePrerequisiteEvidence(
                    component="kokoro", kind="runtime", group="OPTIONAL_KOKORO_TTS",
                    required=None, reasons=("station configuration not inspected",),
                    status=PKG_UNRESOLVED,
                    diagnostics=("unable to determine package status because the trusted dpkg "
                                 "probe failed for: python3-kokoro",),
                ),
            ),
            system_surfaces=None, system_surfaces_error=None,
            scratch_surface=self._healthy_scratch(),
        )
        aggregate = DeploymentBaselineEvidence(structural=structural, station=None)
        self.assertEqual(aggregate.result, RESULT_UNRESOLVED)


class UnresolvedIdentityIsNeverSupersededTests(SimpleTestCase):
    """r0042, Defect C guardrail: scratch-surface identity ambiguity must
    always still propagate as UNRESOLVED, even with a fully-healthy,
    positively-resolved station tier present -- it is never a
    station-dependent package-selection question, and station data can
    never resolve it. Never globally suppress this class of structural
    unresolved state."""

    def test_unresolved_identity_survives_a_healthy_station_tier(self):
        structural = StructuralBaselineEvidence(
            legacy_checks=(), package_prerequisites=(), system_surfaces=None,
            system_surfaces_error=None,
            scratch_surface=evaluate_scratch_surface(isa_user=None),  # unresolved_identity
        )
        self.assertEqual(structural.result, RESULT_UNRESOLVED)
        self.assertTrue(structural.has_unresolved_identity)

        station = RuntimeEvidence(
            runtime_contract_sha256=None, runtime_manifest_schema_version=None, components={},
        )
        aggregate = DeploymentBaselineEvidence(structural=structural, station=station)
        self.assertEqual(aggregate.result, RESULT_UNRESOLVED)
