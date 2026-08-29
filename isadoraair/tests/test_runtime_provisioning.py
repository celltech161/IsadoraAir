"""Planning, offline apply, idempotence, and rollback tests for E3."""

from __future__ import annotations

import base64
import csv
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
import socket
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from isadoraair.runtime_bundle import RuntimeBundleError, load_runtime_bundle
from isadoraair.runtime_provisioning import (
    ProvisioningLayout,
    ProvisioningSeams,
    RuntimeProvisioner,
    RuntimeProvisioningError,
    _build_venv,
    _install_wheels,
    _minimal_environment,
)
from isadoraair.runtime_recovery import RuntimeRecoveryBuilder, load_recovery_payload
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    PiperModelRequirement,
    RuntimeRequirements,
    VoiceRequirement,
)
from isadoraair.runtime_validation import (
    ComponentEvidence,
    RuntimeEvidence,
    STATUS_FAIL,
    STATUS_OPTIONAL_ABSENT,
    STATUS_PASS,
)
from isadoraair.tests.test_runtime_bundle import RuntimeBundleFixture, digest
from monitoring.management.commands.provision_runtime_components import (
    RECOVERY_PAYLOAD_REASON,
    _requirements_for_recovery_tts,
)


def _requirements(
    *,
    kokoro=False,
    piper_model: PiperModelRequirement | None = None,
    fdkaac=False,
    errors=(),
):
    kokoro_voice = VoiceRequirement(
        logical_name="station-kokoro",
        engine="kokoro",
        provider_voice="af_test",
        language="en-us",
        speed=1.0,
        reasons=("enabled web-request dedications",),
    )
    piper_voice = None
    if piper_model is not None:
        piper_voice = VoiceRequirement(
            logical_name="station-piper",
            engine="piper",
            provider_voice=piper_model.model_id,
            language=piper_model.language,
            speed=1.0,
            reasons=("weather persona 'day'",),
            piper_model=piper_model,
        )
    return RuntimeRequirements(
        components={
            "kokoro": ComponentRequirement(
                "kokoro",
                kokoro,
                kokoro_voice.reasons if kokoro else (),
                (kokoro_voice,) if kokoro else (),
            ),
            "piper": ComponentRequirement(
                "piper",
                piper_model is not None,
                piper_voice.reasons if piper_voice else (),
                (piper_voice,) if piper_voice else (),
                (piper_model,) if piper_model else (),
            ),
            "fdkaac": ComponentRequirement(
                "fdkaac",
                fdkaac,
                ("enabled HE-AAC output",) if fdkaac else (),
            ),
        },
        errors=tuple(errors),
    )


def _piper_requirement(component) -> PiperModelRequirement:
    model = component["models"][0]
    return PiperModelRequirement(
        model_id=model["model_id"],
        model_filename=Path(model["model"]["filename"]).name,
        config_filename=Path(model["config"]["filename"]).name,
        model_sha256=model["model"]["sha256"],
        config_sha256=model["config"]["sha256"],
        language=model["language"],
        sample_rate_hz=model["sample_rate_hz"],
    )


def _filesystem_evidence(manifest, requirements):
    components = {}
    for name in ("kokoro", "piper"):
        requirement = requirements.components[name]
        product = manifest["components"][name]
        if name == "kokoro":
            python = Path(product["runtime"]["python"])
            assets = [Path(item["path"]) for item in product["assets"].values()]
            footprint = [python, *assets]
            valid = python.is_file() and os.access(python, os.X_OK) and all(
                path.is_file() for path in assets
            )
        else:
            python = Path(product["runtime"]["python"])
            executable = Path(product["runtime"]["executable"])
            root = Path(product["models"]["root"])
            assets = [
                root / filename
                for model in requirement.piper_models
                for filename in (model.model_filename, model.config_filename)
            ]
            footprint = [python, executable, root]
            valid = (
                python.is_file()
                and executable.is_file()
                and os.access(executable, os.X_OK)
                and all(path.is_file() for path in assets)
            )
        present = any(path.exists() for path in footprint)
        status = STATUS_PASS if valid else (
            STATUS_FAIL if requirement.required or present else STATUS_OPTIONAL_ABSENT
        )
        components[name] = ComponentEvidence(required=requirement.required, status=status)
    fdkaac = requirements.components["fdkaac"]
    components["fdkaac"] = ComponentEvidence(
        required=fdkaac.required,
        status=STATUS_FAIL if fdkaac.required else STATUS_OPTIONAL_ABSENT,
    )
    return RuntimeEvidence(
        runtime_contract_sha256="a" * 64,
        runtime_manifest_schema_version=1,
        components=components,
        requirement_errors=requirements.errors,
    )


def _concurrent_apply_worker(
    *,
    bundle_root,
    target_root,
    product,
    requirements,
    hold_lock,
    release_lock,
    initial_inspection,
    completed,
    results,
):
    """Fork-safe worker using the real provision lock and tiny local seams."""

    def build(generation):
        binary = generation / "bin"
        binary.mkdir(parents=True)
        os.symlink(sys.executable, binary / "python")

    def validate(manifest, resolved):
        initial_inspection.set()
        return _filesystem_evidence(manifest, resolved)

    def checkpoint(name, _component):
        if hold_lock is not None and name == "before_venv_build":
            hold_lock.set()
            if not release_lock.wait(10):
                raise RuntimeProvisioningError("concurrency test release timed out")

    provisioner = RuntimeProvisioner(
        bundle_root=bundle_root,
        requirements=requirements,
        product_manifest=product,
        target_root=target_root,
        seams=ProvisioningSeams(
            build_venv=build,
            install_wheels=lambda _generation, _bundle, _component: None,
            validate_runtime=validate,
            checkpoint=checkpoint,
        ),
    )
    try:
        result = provisioner.apply()
        results.put(
            {
                "changed": result.changed_components,
                "no_op": result.no_op,
            }
        )
    except Exception as exc:  # pragma: no cover - asserted through child result
        results.put({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        completed.set()


class RuntimeProvisioningFixture(RuntimeBundleFixture):
    def setUp(self):
        super().setUp()
        self.target = self.root / "target"
        self.target.mkdir()
        self.calls = {"build": 0, "install": 0, "validate": 0}

    def fake_build(self, generation):
        self.calls["build"] += 1
        binary = generation / "bin"
        binary.mkdir(parents=True)
        os.symlink(sys.executable, binary / "python")

    def fake_install(self, generation, bundle, component):
        self.calls["install"] += 1
        if component.name == "piper":
            executable = generation / "bin" / "piper"
            executable.write_text(
                f"#!{generation / 'bin' / 'python'}\nprint('piper fixture')\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

    def validate(self, manifest, requirements):
        self.calls["validate"] += 1
        return _filesystem_evidence(manifest, requirements)

    def seams(self, *, checkpoint=None, validate=None, build=None, install=None):
        return ProvisioningSeams(
            build_venv=build or self.fake_build,
            install_wheels=install or self.fake_install,
            validate_runtime=validate or self.validate,
            checkpoint=checkpoint or (lambda name, component: None),
        )

    def provisioner(self, requirements, *, seams=None):
        return RuntimeProvisioner(
            bundle_root=self.bundle_root,
            requirements=requirements,
            product_manifest=self.product,
            target_root=self.target,
            seams=seams or self.seams(),
        )


class RuntimeProvisioningPlanTests(RuntimeProvisioningFixture):
    def test_plan_is_read_only_and_reports_missing_required_kokoro(self):
        self.complete_kokoro_bundle()
        before = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        plan = self.provisioner(_requirements(kokoro=True)).plan()
        after = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        self.assertEqual(before, after)
        self.assertTrue(plan.ready)
        self.assertTrue(plan.needs_work)
        self.assertEqual(plan.components[0].action, "provision")
        self.assertIn("enabled web-request dedications", plan.components[0].reasons)
        self.assertEqual(self.calls["build"], 0)
        self.assertEqual(self.calls["install"], 0)

    def test_no_tts_selected_has_no_component_actions(self):
        self.complete_kokoro_bundle()
        plan = self.provisioner(_requirements()).plan()
        self.assertEqual(plan.components, ())
        self.assertFalse(plan.needs_work)

    def test_optional_unselected_piper_is_not_planned(self):
        self.add_kokoro()
        self.add_piper()
        self.write_manifest()
        plan = self.provisioner(_requirements(kokoro=True)).plan()
        self.assertEqual([component.name for component in plan.components], ["kokoro"])

    def test_kokoro_piper_and_both_station_shapes(self):
        self.add_kokoro()
        piper = self.add_piper()
        self.write_manifest()
        model = _piper_requirement(piper)
        expected = (
            (_requirements(kokoro=True), ["kokoro"]),
            (_requirements(piper_model=model), ["piper"]),
            (_requirements(kokoro=True, piper_model=model), ["kokoro", "piper"]),
        )
        for requirements, names in expected:
            with self.subTest(names=names):
                plan = self.provisioner(requirements).plan()
                self.assertEqual([item.name for item in plan.components], names)

    def test_selected_invalid_voice_blocks_before_build(self):
        self.complete_kokoro_bundle()
        provisioner = self.provisioner(
            _requirements(kokoro=True, errors=("selected logical voice is invalid",))
        )
        plan = provisioner.plan()
        self.assertFalse(plan.ready)
        self.assertEqual(plan.components[0].action, "blocked")
        with self.assertRaisesRegex(RuntimeProvisioningError, "blocking errors"):
            provisioner.apply()
        self.assertEqual(self.calls["build"], 0)

    def test_missing_or_wrong_selected_piper_payload_blocks(self):
        piper = self.add_piper()
        self.write_manifest()
        model = _piper_requirement(piper)
        missing = PiperModelRequirement(
            model_id="missing",
            model_filename="missing.onnx",
            config_filename="missing.onnx.json",
            model_sha256="a" * 64,
            config_sha256="b" * 64,
            language="en-us",
            sample_rate_hz=22050,
        )
        self.assertFalse(self.provisioner(_requirements(piper_model=missing)).plan().ready)
        wrong = PiperModelRequirement(
            **{**model.to_dict(), "model_sha256": "f" * 64}
        )
        plan = self.provisioner(_requirements(piper_model=wrong)).plan()
        self.assertFalse(plan.ready)
        self.assertIn("checksum", " ".join(plan.components[0].errors))

    def test_healthy_matching_component_plans_no_op(self):
        self.complete_kokoro_bundle()
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        (layout.kokoro_venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, layout.kokoro_venv / "bin" / "python")
        layout.kokoro_assets.mkdir(parents=True)
        for asset in self.product["components"]["kokoro"]["assets"].values():
            (layout.kokoro_assets / asset["filename"]).write_bytes(b"present")
        plan = self.provisioner(_requirements(kokoro=True)).plan()
        self.assertEqual(plan.components[0].action, "no_op")
        before = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        result = self.provisioner(_requirements(kokoro=True)).apply()
        after = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        self.assertTrue(result.no_op)
        lock = ProvisioningLayout.from_manifest(
            self.product, target_root=self.target
        ).runtime_root / ".provision.lock"
        self.assertEqual(after, sorted([*before, lock.relative_to(self.target)]))
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)


class RuntimeProvisioningApplyTests(RuntimeProvisioningFixture):
    def test_offline_first_apply_e2_accepts_and_second_apply_is_exact_no_op(self):
        self.complete_kokoro_bundle()
        provisioner = self.provisioner(_requirements(kokoro=True))
        with patch.object(socket, "create_connection", side_effect=AssertionError("network")), patch(
            "requests.sessions.Session.request", side_effect=AssertionError("network")
        ):
            first = provisioner.apply()
            generations_before = sorted(
                path for path in self.target.rglob("e3-*") if path.is_dir()
            )
            second = provisioner.apply()
            generations_after = sorted(
                path for path in self.target.rglob("e3-*") if path.is_dir()
            )
        self.assertFalse(first.no_op)
        self.assertEqual(first.evidence.result, STATUS_PASS)
        self.assertTrue(second.no_op)
        self.assertEqual(generations_before, generations_after)
        self.assertEqual(self.calls["build"], 1)
        self.assertEqual(self.calls["install"], 1)

    def test_unrelated_required_fdkaac_failure_does_not_rollback_kokoro(self):
        self.complete_kokoro_bundle()
        result = self.provisioner(
            _requirements(kokoro=True, fdkaac=True)
        ).apply()
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        self.assertFalse(result.no_op)
        self.assertEqual(result.changed_components, ("kokoro",))
        self.assertEqual(result.evidence.components["kokoro"].status, STATUS_PASS)
        self.assertEqual(result.evidence.components["fdkaac"].status, STATUS_FAIL)
        self.assertEqual(result.evidence.result, STATUS_FAIL)
        self.assertTrue(layout.kokoro_venv.is_symlink())
        self.assertTrue(layout.kokoro_assets.is_symlink())

    def test_unrelated_final_piper_failure_does_not_rollback_staged_kokoro(self):
        self.add_kokoro()
        piper = self.add_piper()
        self.write_manifest()
        model = _piper_requirement(piper)
        requirements = _requirements(kokoro=True, piper_model=model)
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        (layout.piper_venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, layout.piper_venv / "bin" / "python")
        piper_executable = layout.piper_venv / "bin" / "piper"
        piper_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        piper_executable.chmod(0o755)
        layout.piper_assets.mkdir(parents=True)
        (layout.piper_assets / model.model_filename).write_bytes(b"existing model")
        (layout.piper_assets / model.config_filename).write_bytes(b"existing config")
        calls = 0

        def validation(manifest, resolved):
            nonlocal calls
            calls += 1
            evidence = _filesystem_evidence(manifest, resolved)
            if calls == 4:
                evidence.components["piper"] = ComponentEvidence(
                    required=True,
                    status=STATUS_FAIL,
                    diagnostics=("unrelated Piper failure",),
                )
            return evidence

        result = self.provisioner(
            requirements,
            seams=self.seams(validate=validation),
        ).apply()
        self.assertEqual(result.changed_components, ("kokoro",))
        self.assertEqual(result.evidence.components["kokoro"].status, STATUS_PASS)
        self.assertEqual(result.evidence.components["piper"].status, STATUS_FAIL)
        self.assertEqual(result.evidence.result, STATUS_FAIL)
        self.assertTrue(layout.kokoro_venv.is_symlink())
        self.assertTrue(layout.kokoro_assets.is_symlink())

    def test_concurrent_apply_serializes_and_second_replans_to_no_op(self):
        self.complete_kokoro_bundle()
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("real flock contention fixture requires fork")
        context = multiprocessing.get_context("fork")
        held = context.Event()
        release = context.Event()
        a_inspected = context.Event()
        b_inspected = context.Event()
        a_completed = context.Event()
        b_completed = context.Event()
        results = context.Queue()
        common = {
            "bundle_root": self.bundle_root,
            "target_root": self.target,
            "product": self.product,
            "requirements": _requirements(kokoro=True),
            "results": results,
        }
        process_a = context.Process(
            target=_concurrent_apply_worker,
            kwargs={
                **common,
                "hold_lock": held,
                "release_lock": release,
                "initial_inspection": a_inspected,
                "completed": a_completed,
            },
        )
        process_b = context.Process(
            target=_concurrent_apply_worker,
            kwargs={
                **common,
                "hold_lock": None,
                "release_lock": None,
                "initial_inspection": b_inspected,
                "completed": b_completed,
            },
        )
        process_a.start()
        try:
            self.assertTrue(held.wait(5), "apply A did not reach its locked build boundary")
            layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
            lock_path = layout.runtime_root / ".provision.lock"
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)

            process_b.start()
            self.assertTrue(b_inspected.wait(5), "apply B did not finish initial inspection")
            self.assertFalse(
                b_completed.wait(1),
                "apply B completed while apply A still held the provision lock",
            )
            runtime_generations = layout.kokoro_venv.parent / "generations"
            self.assertEqual(list(runtime_generations.glob("e3-*")), [])

            release.set()
            process_a.join(10)
            process_b.join(10)
            self.assertFalse(process_a.is_alive())
            self.assertFalse(process_b.is_alive())
            self.assertEqual(process_a.exitcode, 0)
            self.assertEqual(process_b.exitcode, 0)
            outcomes = [results.get(timeout=5), results.get(timeout=5)]
            self.assertNotIn("error", outcomes[0])
            self.assertNotIn("error", outcomes[1])
            self.assertEqual(sorted(item["no_op"] for item in outcomes), [False, True])
            self.assertEqual(
                sorted(item["changed"] for item in outcomes),
                [(), ("kokoro",)],
            )
            self.assertEqual(len(list(runtime_generations.glob("e3-*"))), 1)
            asset_generations = layout.tts_root / "generations" / "kokoro"
            self.assertEqual(len(list(asset_generations.glob("e3-*"))), 1)
            self.assertTrue(layout.kokoro_venv.is_symlink())
            self.assertTrue(layout.kokoro_assets.is_symlink())
        finally:
            release.set()
            for process in (process_a, process_b):
                if process.pid is None:
                    continue
                process.join(1)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

    def test_both_engines_publish_whole_asset_generations(self):
        self.add_kokoro()
        piper = self.add_piper()
        self.write_manifest()
        model = _piper_requirement(piper)
        result = self.provisioner(
            _requirements(kokoro=True, piper_model=model)
        ).apply()
        self.assertEqual(result.changed_components, ("kokoro", "piper"))
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        self.assertTrue(layout.kokoro_venv.is_symlink())
        self.assertTrue(layout.kokoro_assets.is_symlink())
        self.assertTrue(layout.piper_venv.is_symlink())
        self.assertTrue(layout.piper_assets.is_symlink())
        self.assertTrue((layout.piper_assets / model.model_filename).is_file())
        self.assertTrue((layout.piper_assets / model.config_filename).is_file())
        piper_executable = layout.piper_venv / "bin" / "piper"
        shebang = piper_executable.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(shebang, f"#!{piper_executable.resolve().parent / 'python'}")
        completed = subprocess.run(
            [str(piper_executable)], capture_output=True, text=True, check=True, timeout=10
        )
        self.assertEqual(completed.stdout.strip(), "piper fixture")

    def test_canonical_apply_requires_explicit_root_privilege(self):
        self.complete_kokoro_bundle()
        provisioner = RuntimeProvisioner(
            bundle_root=self.bundle_root,
            requirements=_requirements(kokoro=True),
            product_manifest=self.product,
            target_root="/",
            seams=self.seams(),
        )
        with patch("isadoraair.runtime_provisioning.os.geteuid", return_value=12345):
            with self.assertRaisesRegex(RuntimeProvisioningError, "requires root"):
                provisioner.apply()
        self.assertEqual(self.calls["build"], 0)

    def test_target_generation_symlink_cannot_escape_root(self):
        self.complete_kokoro_bundle()
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        outside = self.root / "outside"
        outside.mkdir()
        layout.kokoro_venv.parent.mkdir(parents=True)
        os.symlink(outside, layout.kokoro_venv.parent / "generations")
        with self.assertRaisesRegex(RuntimeProvisioningError, "unexpected symlink"):
            self.provisioner(_requirements(kokoro=True)).apply()
        self.assertEqual(list(outside.iterdir()), [])

    def test_failed_second_piper_asset_never_publishes_a_mixed_pair(self):
        piper = self.add_piper()
        self.write_manifest()
        model = _piper_requirement(piper)
        from isadoraair import runtime_provisioning

        real_copy = runtime_provisioning._copy_verified

        def fail_config(source, destination, expected):
            if destination.name.endswith(".onnx.json"):
                raise RuntimeProvisioningError("config copy failed")
            return real_copy(source, destination, expected)

        with patch("isadoraair.runtime_provisioning._copy_verified", side_effect=fail_config):
            with self.assertRaisesRegex(RuntimeProvisioningError, "config copy failed"):
                self.provisioner(_requirements(piper_model=model)).apply()
        layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        self.assertFalse(layout.piper_assets.exists())
        self.assertFalse(layout.piper_assets.is_symlink())
        generation_root = layout.tts_root / "generations" / "piper"
        self.assertEqual(list(generation_root.iterdir()), [])


class RuntimeProvisioningRollbackTests(RuntimeProvisioningFixture):
    def setUp(self):
        super().setUp()
        self.complete_kokoro_bundle()
        self.layout = ProvisioningLayout.from_manifest(self.product, target_root=self.target)
        old_runtime = self.layout.runtime_generation("kokoro", "old")
        old_assets = self.layout.asset_generation("kokoro", "old")
        (old_runtime / "bin").mkdir(parents=True)
        os.symlink(sys.executable, old_runtime / "bin" / "python")
        old_assets.mkdir(parents=True)
        os.symlink("generations/old", self.layout.kokoro_venv)
        self.layout.kokoro_assets.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("generations/kokoro/old", self.layout.kokoro_assets)
        self.old_runtime_target = os.readlink(self.layout.kokoro_venv)
        self.old_asset_target = os.readlink(self.layout.kokoro_assets)

    def assert_old_state(self):
        self.assertEqual(os.readlink(self.layout.kokoro_venv), self.old_runtime_target)
        self.assertEqual(os.readlink(self.layout.kokoro_assets), self.old_asset_target)
        self.assertEqual(
            [path.name for path in self.layout.kokoro_venv.parent.joinpath("generations").iterdir()],
            ["old"],
        )
        self.assertEqual(
            [path.name for path in self.layout.tts_root.joinpath("generations/kokoro").iterdir()],
            ["old"],
        )

    def test_venv_build_failure_preserves_prior_state(self):
        def fail(generation):
            generation.mkdir(parents=True)
            raise RuntimeProvisioningError("build failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "build failed"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(build=fail)
            ).apply()
        self.assert_old_state()

    def test_wheel_install_failure_preserves_prior_state(self):
        def fail(_generation, _bundle, _component):
            raise RuntimeProvisioningError("wheel install failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "wheel install failed"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(install=fail)
            ).apply()
        self.assert_old_state()

    def test_asset_staging_failure_preserves_prior_state(self):
        def checkpoint(name, component):
            if name == "before_asset_staging":
                raise RuntimeProvisioningError("asset staging failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "asset staging failed"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(checkpoint=checkpoint)
            ).apply()
        self.assert_old_state()

    def test_staged_validation_failure_preserves_prior_state(self):
        def validation(manifest, requirements):
            evidence = _filesystem_evidence(manifest, requirements)
            if "/generations/e3-" in manifest["components"]["kokoro"]["runtime"]["venv"]:
                evidence.components["kokoro"] = ComponentEvidence(required=True, status=STATUS_FAIL)
            return evidence

        with self.assertRaisesRegex(RuntimeProvisioningError, "staged kokoro"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(validate=validation)
            ).apply()
        self.assert_old_state()

    def test_publication_failure_rolls_back_both_pointers(self):
        def checkpoint(name, component):
            if name == "after_asset_publication":
                raise RuntimeProvisioningError("publication failed")

        with self.assertRaisesRegex(RuntimeProvisioningError, "publication failed"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(checkpoint=checkpoint)
            ).apply()
        self.assert_old_state()

    def test_post_publication_acceptance_failure_rolls_back(self):
        calls = 0

        def validation(manifest, requirements):
            nonlocal calls
            calls += 1
            evidence = _filesystem_evidence(manifest, requirements)
            if calls == 4:
                evidence.components["kokoro"] = ComponentEvidence(
                    required=True, status=STATUS_FAIL
                )
            return evidence

        with self.assertRaisesRegex(RuntimeProvisioningError, "authoritative Foundation E2"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(validate=validation)
            ).apply()
        self.assert_old_state()

    def test_failure_after_successful_acceptance_still_rolls_back(self):
        def checkpoint(name, component):
            if name == "after_final_acceptance":
                raise RuntimeProvisioningError("post acceptance injection")

        with self.assertRaisesRegex(RuntimeProvisioningError, "post acceptance injection"):
            self.provisioner(
                _requirements(kokoro=True), seams=self.seams(checkpoint=checkpoint)
            ).apply()
        self.assert_old_state()

    def test_pointer_restore_failure_is_explicit_and_preserves_original_cause(self):
        from isadoraair import runtime_provisioning

        def checkpoint(name, _component):
            if name == "after_runtime_publication":
                raise RuntimeProvisioningError("publication exploded")

        real_restore = runtime_provisioning._restore_pointer

        def fail_runtime_restore(state):
            if state.pointer == self.layout.kokoro_venv:
                raise RuntimeProvisioningError("runtime pointer restore denied")
            return real_restore(state)

        with patch(
            "isadoraair.runtime_provisioning._restore_pointer",
            side_effect=fail_runtime_restore,
        ):
            with self.assertRaises(RuntimeProvisioningError) as caught:
                self.provisioner(
                    _requirements(kokoro=True),
                    seams=self.seams(checkpoint=checkpoint),
                ).apply()
        self.assertIn("rollback failed", str(caught.exception))
        self.assertIn("runtime pointer restore denied", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeProvisioningError)
        self.assertIn("publication exploded", str(caught.exception.__cause__))

    def test_bundle_hash_failure_occurs_before_any_target_write(self):
        wheel = self.components["kokoro"]["wheels"][0]
        (self.bundle_root / self.components["kokoro"]["wheelhouse"] / wheel["filename"]).write_bytes(
            b"tampered"
        )
        before = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        with self.assertRaises(RuntimeBundleError):
            self.provisioner(_requirements(kokoro=True)).apply()
        after = sorted(path.relative_to(self.target) for path in self.target.rglob("*"))
        self.assertEqual(before, after)


class OfflinePipContractTests(RuntimeProvisioningFixture):
    def test_build_environment_does_not_inherit_database_or_api_secrets(self):
        with patch.dict(
            os.environ,
            {
                "DB_PASSWORD": "production-secret",
                "SECRET_KEY": "django-secret",
                "WEATHER_API_KEY": "provider-secret",
                "PATH": os.environ.get("PATH", "/usr/bin"),
            },
        ):
            environment = _minimal_environment()
        self.assertIn("PATH", environment)
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertNotIn("DB_PASSWORD", environment)
        self.assertNotIn("SECRET_KEY", environment)
        self.assertNotIn("WEATHER_API_KEY", environment)

    def test_pip_command_is_hash_pinned_binary_only_and_no_index(self):
        piper = self.add_piper()
        self.write_manifest()
        bundle = load_runtime_bundle(self.bundle_root, self.product)
        generation = self.root / "generation"
        (generation / "bin").mkdir(parents=True)
        (generation / "bin" / "python").write_bytes(b"")
        observed = {}

        def capture(command, *, timeout, cwd):
            observed.update(command=command, timeout=timeout, cwd=cwd)

        with patch("isadoraair.runtime_provisioning._run_offline", side_effect=capture):
            _install_wheels(generation, bundle, bundle.components["piper"])
        command = observed["command"]
        self.assertIn("--no-index", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--require-hashes", command)
        self.assertIn("--isolated", command)
        self.assertNotIn("--index-url", command)
        self.assertNotIn("--extra-index-url", command)
        self.assertEqual(piper["lock"]["filename"], command[-1].removeprefix(str(self.bundle_root) + "/"))

    def test_real_disposable_venv_keeps_permanent_piper_shebang(self):
        component = self.add_piper()
        package = component["wheels"][0]
        product_version = self.product["components"]["piper"]["runtime"]["packages"][
            "piper-tts"
        ]
        dist_info = f"piper_tts-{product_version}.dist-info"
        wheel_path = self.bundle_root / component["wheelhouse"] / package["filename"]
        wheel_path.unlink()
        wheel_name = f"piper_tts-{product_version}-py3-none-any.whl"
        wheel_path = wheel_path.parent / wheel_name
        files = {
            "piper_fixture/__init__.py": (
                b"def main():\n    print('offline piper fixture')\n"
            ),
            f"{dist_info}/METADATA": (
                f"Metadata-Version: 2.1\nName: piper-tts\nVersion: {product_version}\n\n".encode()
            ),
            f"{dist_info}/WHEEL": (
                b"Wheel-Version: 1.0\nGenerator: IsadoraAir-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
            ),
            f"{dist_info}/entry_points.txt": (
                b"[console_scripts]\npiper = piper_fixture:main\n"
            ),
        }
        records = []
        for name, data in files.items():
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append((name, f"sha256={encoded}", str(len(data))))
        records.append((f"{dist_info}/RECORD", "", ""))
        output = io.StringIO()
        csv.writer(output, lineterminator="\n").writerows(records)
        files[f"{dist_info}/RECORD"] = output.getvalue().encode()
        with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
            for name, data in files.items():
                wheel.writestr(name, data)
        wheel_hash = digest(wheel_path.read_bytes())
        package.update(filename=wheel_name, sha256=wheel_hash)
        lock_data = f"piper-tts=={product_version} --hash=sha256:{wheel_hash}\n".encode()
        lock_path = self.bundle_root / component["lock"]["filename"]
        lock_path.write_bytes(lock_data)
        component["lock"]["sha256"] = digest(lock_data)
        self.write_manifest()
        bundle = load_runtime_bundle(self.bundle_root, self.product)
        permanent = self.root / "permanent" / "generations" / "test"
        permanent.parent.mkdir(parents=True)
        _build_venv(permanent)
        _install_wheels(permanent, bundle, bundle.components["piper"])
        executable = permanent / "bin" / "piper"
        self.assertEqual(
            executable.read_text(encoding="utf-8").splitlines()[0],
            f"#!{permanent / 'bin' / 'python'}",
        )
        completed = subprocess.run(
            [str(executable)], capture_output=True, text=True, check=True, timeout=10
        )
        self.assertEqual(completed.stdout.strip(), "offline piper fixture")


class RuntimeProvisioningCommandTests(TestCase):
    def setUp(self):
        super().setUp()
        import tempfile

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        self.target = self.root / "target"
        self.target.mkdir()
        from isadoraair.runtime_bundle import current_platform_contract, product_contract_digest
        from isadoraair.runtime_components import load_runtime_components

        product = load_runtime_components()
        package, version = next(
            iter(sorted(product["components"]["piper"]["runtime"]["packages"].items()))
        )
        wheel_name = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
        wheel_data = b"command-test-wheel"
        wheel_hash = digest(wheel_data)
        wheelhouse = self.bundle / "piper" / "wheelhouse"
        wheelhouse.mkdir(parents=True)
        (wheelhouse / wheel_name).write_bytes(wheel_data)
        lock_data = f"{package}=={version} --hash=sha256:{wheel_hash}\n".encode()
        lock = self.bundle / "piper" / "requirements.lock"
        lock.write_bytes(lock_data)
        notice = self.bundle / "piper" / "NOTICE.txt"
        notice.write_bytes(b"test notice\n")
        payload = {
            "schema_version": 1,
            "bundle_id": "command-test",
            "platform": current_platform_contract(),
            "product_contract_sha256": product_contract_digest(product),
            "components": {
                "piper": {
                    "lock": {
                        "filename": "piper/requirements.lock",
                        "sha256": digest(lock_data),
                    },
                    "wheelhouse": "piper/wheelhouse",
                    "wheels": [
                        {
                            "filename": wheel_name,
                            "package": package,
                            "version": version,
                            "sha256": wheel_hash,
                        }
                    ],
                    "provenance": [
                        {
                            "filename": "piper/NOTICE.txt",
                            "sha256": digest(notice.read_bytes()),
                        }
                    ],
                    "models": [],
                }
            },
        }
        (self.bundle / "runtime-bundle.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_plan_command_is_database_and_filesystem_read_only_with_clean_json(self):
        from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice
        from road_conditions.models import RoadConditionsConfiguration
        from weather.models import WeatherConfig, WeatherVoicePersona
        from webrequests.models import WebRequestConfig

        models = (
            PiperVoiceModel,
            StationTTSVoice,
            RoadConditionsConfiguration,
            WeatherConfig,
            WeatherVoicePersona,
            WebRequestConfig,
        )
        before_counts = {model: model.objects.count() for model in models}
        before_files = tuple(self.target.rglob("*"))
        stdout = io.StringIO()
        call_command(
            "provision_runtime_components",
            "--bundle",
            str(self.bundle),
            "--target-root",
            str(self.target),
            "--plan",
            "--json",
            stdout=stdout,
        )
        evidence = json.loads(stdout.getvalue())
        self.assertTrue(evidence["ready"])
        self.assertFalse(evidence["needs_work"])
        self.assertEqual(evidence["components"], [])
        self.assertEqual(before_counts, {model: model.objects.count() for model in models})
        self.assertEqual(before_files, tuple(self.target.rglob("*")))


class RecoveryPayloadTTSRequirementsTests(RuntimeProvisioningFixture):
    """Runtime Foundation E7B: --recovery-payload's requirements are
    payload-derived for dormant historical Kokoro, while Piper retains
    the restored database's E1 model/config identity authority. See
    isadoraair/runtime_recovery.py's module docstring and
    monitoring/management/commands/provision_runtime_components.py's
    _requirements_for_recovery_tts."""

    def _payload(self, *, output_name: str) -> Path:
        payload_root = self.root / output_name
        piper_requirement = ComponentRequirement("piper")
        if "piper" in self.components:
            model = self.components["piper"]["models"][0]
            piper_requirement = ComponentRequirement(
                "piper",
                True,
                ("restored station",),
                piper_models=(
                    PiperModelRequirement(
                        model_id=model["model_id"],
                        model_filename=Path(model["model"]["filename"]).name,
                        config_filename=Path(model["config"]["filename"]).name,
                        model_sha256=model["model"]["sha256"],
                        config_sha256=model["config"]["sha256"],
                        language=model["language"],
                        sample_rate_hz=model["sample_rate_hz"],
                    ),
                ),
            )
        RuntimeRecoveryBuilder(product_manifest=self.product).apply(
            tts_bundle=self.bundle_root,
            output=payload_root,
            payload_id=output_name,
            piper_selection=RuntimeRequirements(components={"piper": piper_requirement}),
        )
        return payload_root

    def test_kokoro_only_bundle_requires_only_kokoro_never_from_e1(self):
        self.add_kokoro()
        self.write_manifest()
        payload_root = self._payload(output_name="p-kokoro-only")
        payload = load_recovery_payload(payload_root, product_manifest=self.product)
        requirements = _requirements_for_recovery_tts(payload.tts_bundle)
        self.assertTrue(requirements.components["kokoro"].required)
        self.assertEqual(requirements.components["kokoro"].reasons, (RECOVERY_PAYLOAD_REASON,))
        self.assertFalse(requirements.components["piper"].required)
        self.assertFalse(requirements.components["fdkaac"].required)

    def test_kokoro_plus_piper_uses_exact_db_owned_piper_model_list(self):
        self.add_kokoro()
        self.add_piper(model_id="en_us-fixture")
        self.write_manifest()
        payload_root = self._payload(output_name="p-kokoro-piper")
        payload = load_recovery_payload(payload_root, product_manifest=self.product)
        station_requirements = RuntimeRequirements(
            components={
                "piper": ComponentRequirement(
                    "piper",
                    True,
                    ("restored station",),
                    piper_models=(
                        PiperModelRequirement(
                            model_id="en_us-fixture",
                            model_filename="en_us-fixture.onnx",
                            config_filename="en_us-fixture.onnx.json",
                            model_sha256=self.components["piper"]["models"][0]["model"]["sha256"],
                            config_sha256=self.components["piper"]["models"][0]["config"]["sha256"],
                            language="en-us",
                            sample_rate_hz=22050,
                        ),
                    ),
                )
            }
        )
        requirements = _requirements_for_recovery_tts(payload.tts_bundle, station_requirements)
        piper_requirement = requirements.components["piper"]
        self.assertTrue(piper_requirement.required)
        self.assertEqual(len(piper_requirement.piper_models), 1)
        model = piper_requirement.piper_models[0]
        self.assertEqual(model.model_id, "en_us-fixture")
        self.assertEqual(model.model_filename, "en_us-fixture.onnx")
        self.assertEqual(model.config_filename, "en_us-fixture.onnx.json")
        self.assertEqual(model.language, "en-us")
        self.assertEqual(model.sample_rate_hz, 22050)

    def test_synthesized_requirements_drive_a_real_publish(self):
        """Not just shape -- prove the synthesized requirements actually
        make RuntimeProvisioner.apply() publish kokoro, using this
        file's own fake seams (no real pip/venv), exactly like every
        other apply test here."""
        self.add_kokoro()
        self.write_manifest()
        payload_root = self._payload(output_name="p-publish")
        payload = load_recovery_payload(payload_root, product_manifest=self.product)
        requirements = _requirements_for_recovery_tts(payload.tts_bundle)
        provisioner = RuntimeProvisioner(
            bundle_root=payload.tts_bundle.root,
            requirements=requirements,
            product_manifest=self.product,
            target_root=self.target,
            seams=self.seams(),
        )
        result = provisioner.apply()
        self.assertIn("kokoro", result.changed_components)
        self.assertFalse(result.no_op)

    def test_recovery_payload_cli_flag_bypasses_e1_and_wires_the_bundle(self):
        """CLI-level wiring: --recovery-payload both supplies --bundle
        and replaces resolve_current_runtime_requirements() -- proven by
        patching RuntimeProvisioner itself and inspecting what it was
        actually constructed with (station DB is never touched)."""
        self.add_kokoro()
        self.write_manifest()
        payload_root = self._payload(output_name="p-cli")
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.provision_runtime_components.RuntimeProvisioner"
        ) as provisioner_cls, patch(
            "monitoring.management.commands.provision_runtime_components.resolve_current_runtime_requirements"
        ) as resolver, patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ):
            plan = provisioner_cls.return_value.plan.return_value
            plan.to_json.return_value = json.dumps({"ready": True})
            plan.ready = True
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--target-root",
                str(self.target),
                "--plan",
                "--json",
                stdout=stdout,
            )
        resolver.assert_not_called()
        _, kwargs = provisioner_cls.call_args
        self.assertEqual(Path(kwargs["bundle_root"]), payload_root / "tts")
        self.assertTrue(kwargs["requirements"].components["kokoro"].required)

    def test_piper_recovery_fails_when_restored_database_cannot_be_inspected(self):
        self.add_piper(model_id="en_us-fixture")
        self.write_manifest()
        payload_root = self._payload(output_name="p-piper-db-unavailable")
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), patch(
            "monitoring.management.commands.provision_runtime_components.resolve_current_runtime_requirements",
            side_effect=RuntimeError("database unavailable"),
        ), self.assertRaisesRegex(CommandError, "station configuration could not be inspected"):
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--target-root",
                str(self.target),
                "--plan",
            )

    def test_piper_recovery_fails_when_restored_database_selects_different_identity(self):
        self.add_piper(model_id="en_us-fixture")
        self.write_manifest()
        payload_root = self._payload(output_name="p-piper-stale")
        different = RuntimeRequirements(
            components={
                "piper": ComponentRequirement(
                    "piper",
                    True,
                    ("different station",),
                    piper_models=(
                        PiperModelRequirement(
                            model_id="different",
                            model_filename="different.onnx",
                            config_filename="different.onnx.json",
                            model_sha256="1" * 64,
                            config_sha256="2" * 64,
                            language="en-us",
                            sample_rate_hz=22050,
                        ),
                    ),
                )
            }
        )
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), patch(
            "monitoring.management.commands.provision_runtime_components.resolve_current_runtime_requirements",
            return_value=different,
        ), self.assertRaisesRegex(CommandError, "does not match the restored station selection"):
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--target-root",
                str(self.target),
                "--plan",
            )

    def test_recovery_payload_and_bundle_together_is_rejected(self):
        self.add_kokoro()
        self.write_manifest()
        payload_root = self._payload(output_name="p-conflict")
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), self.assertRaisesRegex(CommandError, "do not also pass --bundle"):
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--bundle",
                str(self.bundle_root),
                "--plan",
            )

    def test_recovery_payload_without_tts_component_is_rejected(self):
        native_source = self.root / "native-src"
        archives = self.product["components"]["fdkaac"]["source_archives"]
        for name, expected in archives.items():
            content = f"FAKE ARCHIVE {name}".encode()
            path = native_source / expected["filename"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o644)
            expected["bytes"] = len(content)
            expected["sha256"] = digest(content)
        payload_root = self.root / "p-native-only"
        RuntimeRecoveryBuilder(product_manifest=self.product).apply(
            native_source_dir=native_source, output=payload_root, payload_id="p-native-only"
        )
        with patch(
            "monitoring.management.commands.provision_runtime_components.load_runtime_components",
            return_value=self.product,
        ), self.assertRaisesRegex(CommandError, "no tts component"):
            call_command(
                "provision_runtime_components",
                "--recovery-payload",
                str(payload_root),
                "--target-root",
                str(self.target),
                "--plan",
            )
