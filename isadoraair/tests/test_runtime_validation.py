"""Read-only Runtime Foundation E evidence, validators, and CLI tests."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    PiperModelRequirement,
    RuntimeRequirements,
    VoiceRequirement,
)
from isadoraair.runtime_validation import (
    ComponentEvidence,
    KOKORO_CAPABILITY_PROBE_LANGUAGE,
    KOKORO_CAPABILITY_PROBE_SPEED,
    KOKORO_CAPABILITY_PROBE_VOICE,
    RuntimeEvidence,
    RuntimeValidationError,
    RuntimeValidator,
    STATUS_FAIL,
    STATUS_OPTIONAL_ABSENT,
    STATUS_PASS,
    ValidationSeams,
    _fdkaac_check,
    _kokoro_smoke,
    _piper_smoke,
    _probe_runtime_packages,
    validate_current_runtime,
)
from isadoraair.tts.errors import TTSRuntimeUnavailable


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RuntimeValidatorFixture(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = deepcopy(load_runtime_components())

        kokoro = self.manifest["components"]["kokoro"]
        kokoro_python = self.root / "kokoro-python"
        kokoro["runtime"]["python"] = str(kokoro_python)
        self.kokoro_model = self.root / "kokoro.onnx"
        self.kokoro_voices = self.root / "voices.bin"
        for key, path in (("model", self.kokoro_model), ("voices", self.kokoro_voices)):
            path.write_bytes(key.encode())
            kokoro["assets"][key].update(
                {"path": str(path), "filename": path.name, "sha256": sha256(path)}
            )

        piper = self.manifest["components"]["piper"]
        self.piper_python = self.root / "piper-python"
        self.piper_executable = self.root / "piper"
        self.piper_root = self.root / "piper-models"
        piper["runtime"]["python"] = str(self.piper_python)
        piper["runtime"]["executable"] = str(self.piper_executable)
        piper["models"]["root"] = str(self.piper_root)

        fdkaac = self.manifest["components"]["fdkaac"]
        self.fdkaac_binary = self.root / "fdkaac"
        fdkaac["runtime"]["binary"] = str(self.fdkaac_binary)
        validator_script = self.root / "check-he-aac"
        validator_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        validator_script.chmod(0o700)
        fdkaac["build"]["validator"] = validator_script.name

        self.manifest_path = self.root / "runtime_components.json"
        self._write_manifest()
        self.calls = {"kokoro": 0, "piper": 0, "fdkaac": 0}
        self.fdkaac_args = []
        self.package_versions = {
            **kokoro["runtime"]["packages"],
            **piper["runtime"]["packages"],
        }

    def _write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def executable(self, path, content="#!/bin/sh\nexit 0\n"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def seams(self, *, package_probe=None, kokoro=None, piper=None, fdkaac=None):
        def kokoro_ok(requirement, product):
            self.calls["kokoro"] += 1

        def piper_ok(requirement, product):
            self.calls["piper"] += 1

        def fdkaac_ok(script, binary, library_root):
            self.calls["fdkaac"] += 1
            self.fdkaac_args.append((script, binary, library_root))

        return ValidationSeams(
            package_probe=package_probe or (
                lambda executable, expected: {name: self.package_versions[name] for name in expected}
            ),
            kokoro_smoke=kokoro or kokoro_ok,
            piper_smoke=piper or piper_ok,
            fdkaac_check=fdkaac or fdkaac_ok,
        )

    def validator(self, seams=None):
        self._write_manifest()
        return RuntimeValidator(
            manifest=self.manifest,
            manifest_path=self.manifest_path,
            seams=seams or self.seams(),
            project_root=self.root,
        )

    def model_requirement(self):
        return PiperModelRequirement(
            model_id="model-one",
            model_filename="model-one.onnx",
            config_filename="model-one.onnx.json",
            model_sha256="a" * 64,
            config_sha256="b" * 64,
            language="en-us",
            sample_rate_hz=22050,
        )

    def requirements(self, *, kokoro=False, piper=False, fdkaac=False):
        kokoro_voice = VoiceRequirement(
            logical_name="logical-kokoro",
            engine="kokoro",
            provider_voice="af_test",
            language="en-us",
            speed=1.0,
            reasons=("test feature",),
        )
        model = self.model_requirement()
        piper_voice = VoiceRequirement(
            logical_name="logical-piper",
            engine="piper",
            provider_voice=model.model_id,
            language="en-us",
            speed=1.0,
            reasons=("test feature",),
            piper_model=model,
        )
        return RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement(
                    "kokoro", kokoro, ("test feature",) if kokoro else (),
                    (kokoro_voice,) if kokoro else (),
                ),
                "piper": ComponentRequirement(
                    "piper", piper, ("test feature",) if piper else (),
                    (piper_voice,) if piper else (), (model,) if piper else (),
                ),
                "fdkaac": ComponentRequirement(
                    "fdkaac", fdkaac, ("test encoder",) if fdkaac else (),
                ),
            }
        )


class EvidenceModelTests(SimpleTestCase):
    def evidence(self, *, required_status=STATUS_PASS, optional_status=STATUS_OPTIONAL_ABSENT, errors=()):
        return RuntimeEvidence(
            runtime_contract_sha256="a" * 64,
            runtime_manifest_schema_version=1,
            components={
                "piper": ComponentEvidence(required=False, status=optional_status),
                "kokoro": ComponentEvidence(required=True, status=required_status),
            },
            requirement_errors=errors,
        )

    def test_serialization_is_stable_sorted_and_secret_free(self):
        evidence = self.evidence()
        first = evidence.to_json()
        self.assertEqual(first, evidence.to_json())
        self.assertEqual(list(json.loads(first)["components"]), ["kokoro", "piper"])
        self.assertNotIn("password", first.lower())
        self.assertNotIn("secret_key", first.lower())

    def test_required_failure_or_requirement_error_fails_overall(self):
        self.assertEqual(self.evidence(required_status=STATUS_FAIL).result, STATUS_FAIL)
        self.assertEqual(self.evidence(errors=("invalid station configuration",)).result, STATUS_FAIL)

    def test_optional_absent_and_optional_failure_do_not_fail_overall(self):
        self.assertEqual(self.evidence().result, STATUS_PASS)
        self.assertEqual(self.evidence(optional_status=STATUS_FAIL).result, STATUS_PASS)

    def test_uninspectable_station_configuration_returns_explicit_safe_failure(self):
        validator = type(
            "Validator",
            (),
            {
                "manifest": {},
                "validate": lambda self, requirements: RuntimeEvidence(
                    runtime_contract_sha256="a" * 64,
                    runtime_manifest_schema_version=1,
                    components={},
                    requirement_errors=requirements.errors,
                ),
            },
        )()
        with patch(
            "isadoraair.runtime_validation.resolve_current_runtime_requirements",
            side_effect=RuntimeError("postgresql://secret"),
        ):
            evidence = validate_current_runtime(validator=validator)
        self.assertEqual(evidence.result, STATUS_FAIL)
        self.assertEqual(
            evidence.requirement_errors,
            ("station configuration could not be inspected",),
        )
        self.assertNotIn("postgresql", evidence.to_json())

    def test_invalid_manifest_is_a_safe_top_level_failure_without_partial_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "runtime_components.json"
            manifest_path.write_text('{"schema_version":999}', encoding="utf-8")
            evidence = validate_current_runtime(manifest_path=manifest_path)
        self.assertEqual(evidence.result, STATUS_FAIL)
        self.assertEqual(evidence.components, {})
        self.assertEqual(evidence.runtime_manifest_schema_version, None)
        self.assertEqual(evidence.contract_errors, ("runtime component contract is invalid",))
        self.assertNotIn("Traceback", evidence.to_json())


class KokoroRuntimeValidatorTests(RuntimeValidatorFixture):
    def _prepare_runtime(self):
        self.executable(self.root / "kokoro-python")

    def test_required_absent_runtime_fails(self):
        evidence = self.validator().validate(self.requirements(kokoro=True))
        self.assertEqual(evidence.components["kokoro"].status, STATUS_FAIL)

    def test_wrong_package_version_fails(self):
        self._prepare_runtime()
        seams = self.seams(package_probe=lambda executable, expected: {**expected, "numpy": "0"})
        evidence = self.validator(seams).validate(self.requirements(kokoro=True))
        self.assertIn("version mismatch", " ".join(evidence.components["kokoro"].diagnostics))

    def test_missing_or_wrong_model_and_voices_fail(self):
        self._prepare_runtime()
        for path in (self.kokoro_model, self.kokoro_voices):
            with self.subTest(path=path.name, mode="missing"):
                original = path.read_bytes()
                path.unlink()
                self.assertEqual(
                    self.validator().validate(self.requirements(kokoro=True)).components["kokoro"].status,
                    STATUS_FAIL,
                )
                path.write_bytes(original)
            with self.subTest(path=path.name, mode="checksum"):
                original = path.read_bytes()
                path.write_bytes(original + b"wrong")
                self.assertEqual(
                    self.validator().validate(self.requirements(kokoro=True)).components["kokoro"].status,
                    STATUS_FAIL,
                )
                path.write_bytes(original)

    def test_provider_failure_fails_safely(self):
        self._prepare_runtime()
        def fail(requirement, product):
            raise RuntimeValidationError("bounded provider failure")
        component = self.validator(self.seams(kokoro=fail)).validate(
            self.requirements(kokoro=True)
        ).components["kokoro"]
        self.assertEqual(component.status, STATUS_FAIL)
        self.assertIn("bounded provider failure", component.diagnostics[0])

    def test_valid_staged_runtime_passes_and_smokes_once(self):
        self._prepare_runtime()
        component = self.validator().validate(self.requirements(kokoro=True)).components["kokoro"]
        self.assertEqual(component.status, STATUS_PASS)
        self.assertEqual(self.calls["kokoro"], 1)
        self.assertTrue(all(item["verified"] for item in component.artifacts))

    def test_real_smoke_boundary_owns_module_root_from_unrelated_cwd(self):
        requirement = self.requirements(kokoro=True).components["kokoro"]
        product = self.manifest["components"]["kokoro"]
        observed = {}

        def fake_synthesize(provider, request, output_path):
            import wave
            observed["cwd"] = provider.cwd
            observed["module_root"] = provider.module_root
            observed["command"] = provider.command_factory(request, output_path)
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 10)

        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fake_synthesize):
            _kokoro_smoke(requirement, product)
        self.assertNotEqual(observed["cwd"], Path.cwd())
        self.assertEqual(observed["module_root"].resolve(), Path(__file__).resolve().parents[2])
        self.assertEqual(
            observed["command"][-4:],
            (
                "--model-path",
                product["assets"]["model"]["path"],
                "--voices-path",
                product["assets"]["voices"]["path"],
            ),
        )
        self.assertFalse(observed["cwd"].exists())

    def test_smoke_temporary_directory_is_removed_on_provider_failure(self):
        requirement = self.requirements(kokoro=True).components["kokoro"]
        product = self.manifest["components"]["kokoro"]
        observed = {}

        def fail(provider, request, output_path):
            observed["directory"] = output_path.parent
            raise TTSRuntimeUnavailable("provider unavailable")

        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fail):
            with self.assertRaises(TTSRuntimeUnavailable):
                _kokoro_smoke(requirement, product)
        self.assertFalse(observed["directory"].exists())


class PiperRuntimeValidatorTests(RuntimeValidatorFixture):
    def _prepare_runtime_and_assets(self):
        self.executable(self.piper_python)
        self.piper_root.mkdir()
        model = self.piper_root / "model-one.onnx"
        config = self.piper_root / "model-one.onnx.json"
        model.write_bytes(b"model")
        config.write_text(
            json.dumps({"audio": {"sample_rate": 22050}, "language": {"code": "en_US"}}),
            encoding="utf-8",
        )
        requirement = self.model_requirement()
        requirement = PiperModelRequirement(
            **{**requirement.to_dict(), "model_sha256": sha256(model), "config_sha256": sha256(config)}
        )
        self.executable(
            self.piper_executable,
            """#!/usr/bin/env python3
import argparse, json, wave
p=argparse.ArgumentParser(); p.add_argument('--model'); p.add_argument('--config'); p.add_argument('--output-file'); p.add_argument('--length-scale'); a=p.parse_args()
with open(a.config, encoding='utf-8') as source: rate=json.load(source)['audio']['sample_rate']
with wave.open(a.output_file, 'wb') as output:
 output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate); output.writeframes(b'\\0\\0' * 10)
""",
        )
        requirements = self.requirements(piper=True)
        old_voice = requirements.components["piper"].voices[0]
        voice = VoiceRequirement(
            logical_name=old_voice.logical_name,
            engine=old_voice.engine,
            provider_voice=requirement.model_id,
            language=old_voice.language,
            speed=old_voice.speed,
            reasons=old_voice.reasons,
            piper_model=requirement,
        )
        requirements.components["piper"] = ComponentRequirement(
            "piper", True, ("test feature",), (voice,), (requirement,)
        )
        return requirements, model, config

    def test_unselected_absent_is_optional_absent(self):
        component = self.validator().validate(self.requirements()).components["piper"]
        self.assertEqual(component.status, STATUS_OPTIONAL_ABSENT)

    def test_selected_executable_missing_fails(self):
        self.executable(self.piper_python)
        component = self.validator().validate(self.requirements(piper=True)).components["piper"]
        self.assertEqual(component.status, STATUS_FAIL)

    def test_existing_provider_proves_missing_hash_and_metadata_failures(self):
        requirements, model, config = self._prepare_runtime_and_assets()
        seams = self.seams(piper=_piper_smoke)
        mutations = (
            ("model missing", lambda: model.unlink()),
            ("model hash", lambda: model.write_bytes(b"wrong")),
            ("config hash", lambda: config.write_text("{}", encoding="utf-8")),
        )
        original_model = model.read_bytes()
        original_config = config.read_text(encoding="utf-8")
        for label, mutate in mutations:
            with self.subTest(label=label):
                model.write_bytes(original_model)
                config.write_text(original_config, encoding="utf-8")
                mutate()
                component = self.validator(seams).validate(requirements).components["piper"]
                self.assertEqual(component.status, STATUS_FAIL)
        model.write_bytes(original_model)
        config.write_text(original_config, encoding="utf-8")
        wrong_metadata = deepcopy(requirements)
        model_req = wrong_metadata.components["piper"].piper_models[0]
        bad_model = PiperModelRequirement(**{**model_req.to_dict(), "sample_rate_hz": 24000})
        voice = wrong_metadata.components["piper"].voices[0]
        bad_voice = VoiceRequirement(
            voice.logical_name, voice.engine, voice.provider_voice, voice.language,
            voice.speed, voice.reasons, bad_model,
        )
        wrong_metadata.components["piper"] = ComponentRequirement(
            "piper", True, voice.reasons, (bad_voice,), (bad_model,)
        )
        self.assertEqual(
            self.validator(seams).validate(wrong_metadata).components["piper"].status,
            STATUS_FAIL,
        )

    def test_synthesis_failure_fails_and_valid_provider_passes_native_wav(self):
        requirements, _, _ = self._prepare_runtime_and_assets()
        def fail(requirement, product):
            raise RuntimeValidationError("synthesis failed")
        self.assertEqual(
            self.validator(self.seams(piper=fail)).validate(requirements).components["piper"].status,
            STATUS_FAIL,
        )
        component = self.validator(self.seams(piper=_piper_smoke)).validate(requirements).components["piper"]
        self.assertEqual(component.status, STATUS_PASS)
        self.assertTrue(component.models[0]["verified"])

    def test_smoke_temporary_directory_is_removed_on_success_and_failure(self):
        requirements, _, _ = self._prepare_runtime_and_assets()
        requirement = requirements.components["piper"]
        product = self.manifest["components"]["piper"]
        observed = []

        def succeed(service, request):
            observed.append(request.output_path.parent)
            return request.output_path

        with patch("isadoraair.runtime_validation.TTSService.synthesize", new=succeed):
            _piper_smoke(requirement, product)
        self.assertTrue(observed)
        self.assertTrue(all(not directory.exists() for directory in observed))

        observed.clear()

        def fail(service, request):
            observed.append(request.output_path.parent)
            raise TTSRuntimeUnavailable("provider unavailable")

        with patch("isadoraair.runtime_validation.TTSService.synthesize", new=fail):
            with self.assertRaises(TTSRuntimeUnavailable):
                _piper_smoke(requirement, product)
        self.assertTrue(all(not directory.exists() for directory in observed))


class KokoroCapabilityProbeFallbackTests(RuntimeValidatorFixture):
    """Runtime Foundation E7C (2026-08-29) regression coverage: a Runtime
    Foundation E7 recovery-payload requirement sets kokoro.required=True
    with an EMPTY voices tuple (see
    isadoraair/runtime_recovery.py's module docstring and
    monitoring/management/commands/provision_runtime_components.py's
    _requirements_for_recovery_tts). Real acceptance testing found this
    previously produced a false-positive Foundation E2 PASS -- the smoke
    test silently no-op'd instead of actually probing synthesis
    capability."""

    def _prepare_runtime(self):
        self.executable(self.root / "kokoro-python")

    def test_direct_smoke_falls_back_to_capability_probe_voice_when_required_but_voiceless(self):
        self._prepare_runtime()
        requirement = ComponentRequirement("kokoro", True, ("recovery payload",), voices=())
        product = self.manifest["components"]["kokoro"]
        observed = {}

        def fake_synthesize(provider, request, output_path):
            import wave
            observed["voice"] = request.voice
            observed["language"] = request.language
            observed["speed"] = request.speed
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 10)

        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fake_synthesize):
            _kokoro_smoke(requirement, product)
        self.assertEqual(observed["voice"], KOKORO_CAPABILITY_PROBE_VOICE)
        self.assertEqual(observed["language"], KOKORO_CAPABILITY_PROBE_LANGUAGE)
        self.assertEqual(observed["speed"], KOKORO_CAPABILITY_PROBE_SPEED)

    def test_a_real_station_selected_voice_still_takes_priority_over_the_probe_default(self):
        self._prepare_runtime()
        requirement = self.requirements(kokoro=True).components["kokoro"]
        product = self.manifest["components"]["kokoro"]
        observed = {}

        def fake_synthesize(provider, request, output_path):
            import wave
            observed["voice"] = request.voice
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 10)

        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fake_synthesize):
            _kokoro_smoke(requirement, product)
        self.assertEqual(observed["voice"], "af_test")
        self.assertNotEqual(observed["voice"], KOKORO_CAPABILITY_PROBE_VOICE)

    def test_end_to_end_validator_actually_smokes_a_required_but_voiceless_kokoro_requirement(self):
        # The full RuntimeValidator.validate() path, not just the isolated
        # function -- proves the previously-silent early return no longer
        # lets a recovery-payload-shaped requirement report
        # provider_synthesis_pcm16_mono_24000: verified=True without a real
        # synthesis attempt ever having been made. Asserting the outcome
        # (status/verified) ALONE does not prove this -- the old buggy
        # early return produced the exact same outcome (confirmed by
        # temporarily reproducing it: this test passed unchanged). The
        # call-count assertion below is what actually discriminates.
        self._prepare_runtime()
        calls = []

        def fake_synthesize(provider, request, output_path):
            import wave
            calls.append(request.voice)
            with wave.open(str(output_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(24000)
                output.writeframes(b"\0\0" * 10)

        requirements = RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement("kokoro", True, ("recovery payload",), voices=()),
                "piper": ComponentRequirement("piper"),
                "fdkaac": ComponentRequirement("fdkaac"),
            }
        )
        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fake_synthesize):
            component = self.validator(self.seams(kokoro=_kokoro_smoke)).validate(requirements).components["kokoro"]
        self.assertEqual(calls, [KOKORO_CAPABILITY_PROBE_VOICE])
        self.assertEqual(component.status, STATUS_PASS)
        capability = next(
            item for item in component.capabilities if item["name"] == "provider_synthesis_pcm16_mono_24000"
        )
        self.assertTrue(capability["verified"])

    def test_a_genuinely_failing_synthesis_now_fails_closed_instead_of_a_false_positive(self):
        self._prepare_runtime()

        def fail(provider, request, output_path):
            raise TTSRuntimeUnavailable("provider unavailable")

        requirements = RuntimeRequirements(
            components={
                "kokoro": ComponentRequirement("kokoro", True, ("recovery payload",), voices=()),
                "piper": ComponentRequirement("piper"),
                "fdkaac": ComponentRequirement("fdkaac"),
            }
        )
        with patch("isadoraair.runtime_validation.SubprocessTTSProvider.synthesize", new=fail):
            component = self.validator(self.seams(kokoro=_kokoro_smoke)).validate(requirements).components["kokoro"]
        self.assertEqual(component.status, STATUS_FAIL)


class PiperCapabilityProbeFallbackTests(PiperRuntimeValidatorTests):
    """Runtime Foundation E7C (2026-08-29) regression coverage: a Runtime
    Foundation E7 recovery-payload requirement supplies piper_models
    without a matching VoiceRequirement (the sibling of
    KokoroCapabilityProbeFallbackTests above). Real acceptance testing
    found -- by code inspection, this station has no Piper -- that this
    previously crashed Foundation E2 acceptance outright with a KeyError."""

    def test_direct_smoke_does_not_crash_and_uses_model_language_when_voiceless(self):
        requirements, _model, _config = self._prepare_runtime_and_assets()
        component = requirements.components["piper"]
        voiceless = ComponentRequirement(
            "piper", True, component.reasons, voices=(), piper_models=component.piper_models
        )
        product = self.manifest["components"]["piper"]
        observed = []

        def succeed(service, request):
            observed.append((request.language, request.speed))
            return request.output_path

        with patch("isadoraair.runtime_validation.TTSService.synthesize", new=succeed):
            _piper_smoke(voiceless, product)
        self.assertEqual(observed, [(component.piper_models[0].language, 1.0)])

    def test_end_to_end_validator_no_longer_raises_keyerror_for_a_voiceless_piper_requirement(self):
        requirements, _model, _config = self._prepare_runtime_and_assets()
        component = requirements.components["piper"]
        requirements.components["piper"] = ComponentRequirement(
            "piper", True, component.reasons, voices=(), piper_models=component.piper_models
        )
        result = self.validator(self.seams(piper=_piper_smoke)).validate(requirements).components["piper"]
        self.assertEqual(result.status, STATUS_PASS)


class FdkaacRuntimeValidatorTests(RuntimeValidatorFixture):
    def test_optional_absent_is_valid(self):
        component = self.validator().validate(self.requirements()).components["fdkaac"]
        self.assertEqual(component.status, STATUS_OPTIONAL_ABSENT)

    def test_required_success_delegates_to_authoritative_validator(self):
        self.executable(self.fdkaac_binary)
        component = self.validator().validate(self.requirements(fdkaac=True)).components["fdkaac"]
        self.assertEqual(component.status, STATUS_PASS)
        self.assertEqual(self.calls["fdkaac"], 1)
        self.assertEqual(
            self.fdkaac_args[0][1:],
            (self.fdkaac_binary, Path(self.manifest["components"]["fdkaac"]["runtime"]["library_root"])),
        )

    def test_required_missing_binary_fails_without_running_host_validator(self):
        component = self.validator().validate(
            self.requirements(fdkaac=True)
        ).components["fdkaac"]
        self.assertEqual(component.status, STATUS_FAIL)
        self.assertIn("binary is unavailable", " ".join(component.diagnostics))
        self.assertEqual(self.calls["fdkaac"], 0)

    def test_required_failure_and_timeout_fail_safely(self):
        self.executable(self.fdkaac_binary)
        for error in (RuntimeValidationError("validator failed"), RuntimeValidationError("validator timed out")):
            with self.subTest(error=error):
                def fail(script, binary, library_root, error=error):
                    raise error
                component = self.validator(self.seams(fdkaac=fail)).validate(
                    self.requirements(fdkaac=True)
                ).components["fdkaac"]
                self.assertEqual(component.status, STATUS_FAIL)
                self.assertIn(str(error), component.diagnostics)

    def test_real_authoritative_validator_timeout_kills_its_process_group(self):
        script = self.root / "slow-validator"
        self.executable(script, "#!/bin/sh\nsleep 60\n")
        with patch("isadoraair.runtime_validation.FDKAAC_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(RuntimeValidationError, "timed out"):
                _fdkaac_check(script)

    def test_authoritative_validator_human_output_cannot_leak_to_python_stdout(self):
        script = self.root / "noisy-validator"
        self.executable(script, "#!/bin/sh\necho noisy-stdout\necho noisy-stderr >&2\n")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _fdkaac_check(script)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


class ReadOnlyInvariantTests(RuntimeValidatorFixture):
    def test_optional_absent_validation_creates_no_runtime_paths_or_subprocesses(self):
        canonical_root = self.root / "canonical-runtime"
        kokoro = self.manifest["components"]["kokoro"]
        kokoro["runtime"]["python"] = str(canonical_root / "kokoro" / "venv" / "bin" / "python")
        kokoro["assets"]["model"]["path"] = str(canonical_root / "kokoro" / "model.onnx")
        kokoro["assets"]["voices"]["path"] = str(canonical_root / "kokoro" / "voices.bin")
        piper = self.manifest["components"]["piper"]
        piper["runtime"]["python"] = str(canonical_root / "piper" / "venv" / "bin" / "python")
        piper["runtime"]["executable"] = str(canonical_root / "piper" / "venv" / "bin" / "piper")
        piper["models"]["root"] = str(canonical_root / "piper" / "models")
        self.manifest["components"]["fdkaac"]["runtime"]["binary"] = str(
            canonical_root / "native" / "fdkaac"
        )

        evidence = self.validator().validate(self.requirements())

        self.assertEqual(
            {name: component.status for name, component in evidence.components.items()},
            {
                "fdkaac": STATUS_OPTIONAL_ABSENT,
                "kokoro": STATUS_OPTIONAL_ABSENT,
                "piper": STATUS_OPTIONAL_ABSENT,
            },
        )
        self.assertFalse(canonical_root.exists())
        self.assertEqual(self.calls, {"kokoro": 0, "piper": 0, "fdkaac": 0})

    def test_package_probe_timeout_removes_its_temporary_directory(self):
        observed = {}

        def timeout(*args, **kwargs):
            observed["directory"] = Path(kwargs["cwd"])
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        with patch("isadoraair.runtime_validation.subprocess.run", side_effect=timeout):
            with self.assertRaisesRegex(RuntimeValidationError, "could not complete"):
                _probe_runtime_packages("/fake/python", {"package": "1"})
        self.assertFalse(observed["directory"].exists())


class RuntimeValidationCommandTests(SimpleTestCase):
    def evidence(self, *, status=STATUS_PASS, required=True):
        return RuntimeEvidence(
            runtime_contract_sha256="a" * 64,
            runtime_manifest_schema_version=1,
            components={
                "kokoro": ComponentEvidence(required=required, status=status),
                "piper": ComponentEvidence(required=False, status=STATUS_OPTIONAL_ABSENT),
                "fdkaac": ComponentEvidence(required=False, status=STATUS_OPTIONAL_ABSENT),
            },
        )

    def test_human_output(self):
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.validate_runtime_components.validate_current_runtime",
            return_value=self.evidence(),
        ):
            call_command("validate_runtime_components", stdout=stdout)
        self.assertIn("Runtime components: PASS", stdout.getvalue())
        self.assertIn("piper: optional_absent (optional)", stdout.getvalue())

    def test_json_stdout_contains_only_json(self):
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.validate_runtime_components.validate_current_runtime",
            return_value=self.evidence(),
        ):
            call_command("validate_runtime_components", "--json", stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["result"], STATUS_PASS)

    def test_failed_required_component_has_nonzero_command_semantics(self):
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.validate_runtime_components.validate_current_runtime",
            return_value=self.evidence(status=STATUS_FAIL),
        ):
            with self.assertRaises(CommandError):
                call_command("validate_runtime_components", "--json", stdout=stdout)
        self.assertEqual(json.loads(stdout.getvalue())["result"], STATUS_FAIL)

    def test_optional_absent_leaves_command_successful(self):
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.validate_runtime_components.validate_current_runtime",
            return_value=self.evidence(status=STATUS_OPTIONAL_ABSENT, required=False),
        ):
            call_command("validate_runtime_components", stdout=stdout)
        self.assertIn("Runtime components: PASS", stdout.getvalue())

    def test_invalid_contract_json_is_clean_and_command_fails_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "runtime_components.json"
            manifest_path.write_text('{"schema_version":999}', encoding="utf-8")
            evidence = validate_current_runtime(manifest_path=manifest_path)
        stdout = io.StringIO()
        with patch(
            "monitoring.management.commands.validate_runtime_components.validate_current_runtime",
            return_value=evidence,
        ):
            with self.assertRaisesRegex(CommandError, "required runtime components failed"):
                call_command("validate_runtime_components", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["contract_errors"], ["runtime component contract is invalid"])
        self.assertNotIn("Traceback", stdout.getvalue())
