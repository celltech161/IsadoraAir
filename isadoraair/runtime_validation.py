"""Read-only Foundation E runtime validation and stable evidence output."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from isadoraair.runtime_components import (
    MANIFEST_PATH,
    RuntimeComponentContractError,
    load_runtime_components,
)
from isadoraair.runtime_requirements import (
    COMPONENT_NAMES,
    ComponentRequirement,
    PiperModelRequirement,
    RuntimeRequirements,
    resolve_current_runtime_requirements,
    unresolved_runtime_requirements,
)
from isadoraair.tts.errors import TTSError
from isadoraair.tts.providers import (
    PiperTTSProvider,
    PiperVoiceSpec,
    SubprocessTTSProvider,
    kokoro_provider_command,
)
from isadoraair.tts.request import SynthesisRequest, TTSEngine
from isadoraair.tts.service import PROJECT_ROOT, TTSService
from isadoraair.tts.validation import KOKORO_WAV_REQUIREMENTS


EVIDENCE_SCHEMA_VERSION = 1
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_OPTIONAL_ABSENT = "optional_absent"
SMOKE_TEXT = "IsadoraAir runtime validation."
PACKAGE_PROBE_TIMEOUT_SECONDS = 15.0
TTS_SMOKE_TIMEOUT_SECONDS = 120.0
FDKAAC_TIMEOUT_SECONDS = 60.0


class RuntimeValidationError(RuntimeError):
    """A safe, operator-facing validation failure."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_diagnostic(value: object) -> str:
    return " ".join(str(value).split())[:512] or "validation failed"


@dataclass(frozen=True, slots=True)
class ComponentEvidence:
    required: bool
    status: str
    reasons: tuple[str, ...] = ()
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[dict[str, Any], ...] = ()
    capabilities: tuple[dict[str, Any], ...] = ()
    models: tuple[dict[str, Any], ...] = ()
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": list(self.artifacts),
            "capabilities": list(self.capabilities),
            "diagnostics": list(self.diagnostics),
            "expected": self.expected,
            "models": list(self.models),
            "observed": self.observed,
            "reasons": list(self.reasons),
            "required": self.required,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEvidence:
    runtime_contract_sha256: str | None
    runtime_manifest_schema_version: int | None
    components: dict[str, ComponentEvidence]
    requirement_errors: tuple[str, ...] = ()
    contract_errors: tuple[str, ...] = ()
    schema_version: int = EVIDENCE_SCHEMA_VERSION

    @property
    def result(self) -> str:
        if self.contract_errors or self.requirement_errors:
            return STATUS_FAIL
        if any(item.required and item.status != STATUS_PASS for item in self.components.values()):
            return STATUS_FAIL
        return STATUS_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "components": {
                name: self.components[name].to_dict() for name in sorted(self.components)
            },
            "contract_errors": list(sorted(self.contract_errors)),
            "requirement_errors": list(sorted(self.requirement_errors)),
            "result": self.result,
            "runtime_contract_sha256": self.runtime_contract_sha256,
            "runtime_manifest_schema_version": self.runtime_manifest_schema_version,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


PackageProbe = Callable[[str, Mapping[str, str]], dict[str, str]]
KokoroSmoke = Callable[[ComponentRequirement, dict[str, Any]], None]
PiperSmoke = Callable[[ComponentRequirement, dict[str, Any]], None]
FdkaacCheck = Callable[[Path, Path, Path], None]


@dataclass(frozen=True, slots=True)
class ValidationSeams:
    package_probe: PackageProbe
    kokoro_smoke: KokoroSmoke
    piper_smoke: PiperSmoke
    fdkaac_check: FdkaacCheck


def _probe_runtime_packages(executable: str, expected: Mapping[str, str]) -> dict[str, str]:
    script = (
        "import importlib.metadata,json,sys;"
        "names=json.loads(sys.argv[1]);"
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
        if name in os.environ
    }
    with tempfile.TemporaryDirectory(prefix="isadoraair-runtime-package-probe-") as directory:
        try:
            completed = subprocess.run(
                [executable, "-I", "-c", script, json.dumps(sorted(expected))],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=PACKAGE_PROBE_TIMEOUT_SECONDS,
                check=False,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeValidationError("isolated runtime package probe could not complete") from exc
    if completed.returncode != 0 or len(completed.stdout) > 65536:
        raise RuntimeValidationError("isolated runtime package probe failed")
    try:
        observed = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeValidationError("isolated runtime package probe returned invalid evidence") from exc
    if not isinstance(observed, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in observed.items()):
        raise RuntimeValidationError("isolated runtime package probe returned invalid evidence")
    return observed


def _kokoro_smoke(requirement: ComponentRequirement, product: dict[str, Any]) -> None:
    if not requirement.voices:
        return
    voice = requirement.voices[0]
    runtime = product["runtime"]
    with tempfile.TemporaryDirectory(prefix="isadoraair-kokoro-validation-") as directory:
        unrelated_cwd = Path(directory)
        service = TTSService(
            {
                TTSEngine.KOKORO: SubprocessTTSProvider(
                    engine=TTSEngine.KOKORO,
                    command_factory=kokoro_provider_command(
                        runtime["python"],
                        runtime["provider_module"],
                        model_path=product["assets"]["model"]["path"],
                        voices_path=product["assets"]["voices"]["path"],
                    ),
                    cwd=unrelated_cwd,
                    module_root=PROJECT_ROOT,
                    wav_requirements=KOKORO_WAV_REQUIREMENTS,
                )
            }
        )
        service.synthesize(
            SynthesisRequest(
                text=SMOKE_TEXT,
                engine=TTSEngine.KOKORO,
                voice=voice.provider_voice,
                language=voice.language,
                speed=voice.speed,
                timeout_seconds=TTS_SMOKE_TIMEOUT_SECONDS,
                output_path=unrelated_cwd / "smoke.wav",
            )
        )


def _piper_specs(models: tuple[PiperModelRequirement, ...]) -> tuple[PiperVoiceSpec, ...]:
    return tuple(PiperVoiceSpec(**model.to_dict()) for model in models)


def _piper_smoke(requirement: ComponentRequirement, product: dict[str, Any]) -> None:
    if not requirement.piper_models:
        return
    provider = PiperTTSProvider(
        executable=product["runtime"]["executable"],
        asset_root=product["models"]["root"],
        voices=_piper_specs(requirement.piper_models),
    )
    service = TTSService({TTSEngine.PIPER: provider})
    voices_by_model = {
        voice.piper_model.model_id: voice
        for voice in requirement.voices
        if voice.piper_model is not None
    }
    with tempfile.TemporaryDirectory(prefix="isadoraair-piper-validation-") as directory:
        for model in requirement.piper_models:
            voice = voices_by_model[model.model_id]
            service.synthesize(
                SynthesisRequest(
                    text=SMOKE_TEXT,
                    engine=TTSEngine.PIPER,
                    voice=model.model_id,
                    language=voice.language,
                    speed=voice.speed,
                    timeout_seconds=TTS_SMOKE_TIMEOUT_SECONDS,
                    output_path=Path(directory) / f"{model.model_id}.wav",
                )
            )


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _fdkaac_check(
    script: Path,
    binary: Path | None = None,
    library_root: Path | None = None,
) -> None:
    command = [str(script)]
    if binary is not None:
        command.extend(("--fdkaac", str(binary)))
    if library_root is not None:
        command.extend(("--lib-dir", str(library_root)))
    if binary is not None or library_root is not None:
        command.append("--runtime-only")
    try:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env={
                name: os.environ[name]
                for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
                if name in os.environ
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeValidationError("authoritative HE-AAC validator could not start") from exc
    try:
        return_code = process.wait(timeout=FDKAAC_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_group(process)
        raise RuntimeValidationError("authoritative HE-AAC validator timed out") from exc
    if return_code != 0:
        raise RuntimeValidationError(
            f"authoritative HE-AAC validator exited with status {return_code}"
        )


DEFAULT_SEAMS = ValidationSeams(
    package_probe=_probe_runtime_packages,
    kokoro_smoke=_kokoro_smoke,
    piper_smoke=_piper_smoke,
    fdkaac_check=_fdkaac_check,
)


def _is_executable(path: str) -> bool:
    candidate = Path(path)
    return candidate.is_file() and os.access(candidate, os.X_OK)


def _artifact_evidence(label: str, definition: dict[str, Any]) -> dict[str, Any]:
    path = Path(definition["path"])
    item: dict[str, Any] = {
        "expected_sha256": definition["sha256"],
        "filename": definition["filename"],
        "present": path.is_file(),
        "verified": False,
    }
    if path.is_file():
        try:
            item["observed_sha256"] = _sha256(path)
            item["verified"] = item["observed_sha256"] == definition["sha256"]
        except OSError:
            item["diagnostic"] = f"{label} could not be read"
    return item


def _optional_absent(requirement: ComponentRequirement, expected: dict[str, Any]) -> ComponentEvidence:
    return ComponentEvidence(
        required=False,
        status=STATUS_OPTIONAL_ABSENT,
        reasons=requirement.reasons,
        expected=expected,
        observed={"present": False},
    )


class RuntimeValidator:
    """Validate canonical runtime state without changing it."""

    def __init__(
        self,
        *,
        manifest: dict[str, Any] | None = None,
        manifest_path: Path = MANIFEST_PATH,
        seams: ValidationSeams = DEFAULT_SEAMS,
        project_root: Path = PROJECT_ROOT,
    ) -> None:
        self.manifest = manifest or load_runtime_components(manifest_path)
        self.manifest_path = Path(manifest_path)
        self.seams = seams
        self.project_root = Path(project_root)

    def _packages(self, executable: str, expected: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
        observed = self.seams.package_probe(executable, expected)
        diagnostics = [
            f"runtime package '{name}' version mismatch"
            for name in sorted(expected)
            if observed.get(name) != expected[name]
        ]
        return {name: observed.get(name, "missing") for name in sorted(expected)}, diagnostics

    def _validate_kokoro(self, requirement: ComponentRequirement) -> ComponentEvidence:
        product = self.manifest["components"]["kokoro"]
        runtime = product["runtime"]
        expected = {
            "output": product["output"],
            "packages": runtime["packages"],
            "provider_module": runtime["provider_module"],
        }
        footprint = [Path(runtime["python"]), *(Path(item["path"]) for item in product["assets"].values())]
        if not requirement.required and not any(path.exists() for path in footprint):
            return _optional_absent(requirement, expected)
        diagnostics: list[str] = []
        artifacts = tuple(
            _artifact_evidence(name, product["assets"][name]) for name in ("model", "voices")
        )
        executable = _is_executable(runtime["python"])
        observed: dict[str, Any] = {"python_executable": executable, "present": any(path.exists() for path in footprint)}
        if not executable:
            diagnostics.append("canonical Kokoro runtime Python is unavailable")
        else:
            try:
                observed["packages"], package_errors = self._packages(runtime["python"], runtime["packages"])
                diagnostics.extend(package_errors)
            except RuntimeValidationError as exc:
                diagnostics.append(_safe_diagnostic(exc))
        for item in artifacts:
            if not item["present"]:
                diagnostics.append(f"Kokoro {item['filename']} is unavailable")
            elif not item["verified"]:
                diagnostics.append(f"Kokoro {item['filename']} checksum does not match")
        capabilities: list[dict[str, Any]] = []
        if not diagnostics and requirement.required:
            try:
                self.seams.kokoro_smoke(requirement, product)
                capabilities.append({"name": "provider_synthesis_pcm16_mono_24000", "verified": True})
                capabilities.append({"name": "arbitrary_cwd_module_boundary", "verified": True})
            except (RuntimeValidationError, TTSError, OSError, ValueError) as exc:
                diagnostics.append(f"Kokoro provider smoke test failed: {_safe_diagnostic(exc)}")
                capabilities.append({"name": "provider_synthesis_pcm16_mono_24000", "verified": False})
        elif not requirement.required:
            capabilities.append({"name": "provider_synthesis", "verified": False, "reason": "no station voice selected"})
        return ComponentEvidence(
            required=requirement.required,
            status=STATUS_FAIL if diagnostics else STATUS_PASS,
            reasons=requirement.reasons,
            expected=expected,
            observed=observed,
            artifacts=artifacts,
            capabilities=tuple(capabilities),
            diagnostics=tuple(sorted(diagnostics)),
        )

    def _validate_piper(self, requirement: ComponentRequirement) -> ComponentEvidence:
        product = self.manifest["components"]["piper"]
        runtime = product["runtime"]
        expected = {"packages": runtime["packages"]}
        footprint = [Path(runtime["executable"]), Path(runtime["python"]), Path(product["models"]["root"])]
        if not requirement.required and not any(path.exists() for path in footprint):
            return _optional_absent(requirement, expected)
        diagnostics: list[str] = []
        executable = _is_executable(runtime["executable"])
        python_executable = _is_executable(runtime["python"])
        observed: dict[str, Any] = {
            "executable": executable,
            "python_executable": python_executable,
            "present": any(path.exists() for path in footprint),
        }
        if not executable:
            diagnostics.append("canonical Piper executable is unavailable")
        if not python_executable:
            diagnostics.append("canonical Piper runtime Python is unavailable")
        else:
            try:
                observed["packages"], package_errors = self._packages(runtime["python"], runtime["packages"])
                diagnostics.extend(package_errors)
            except RuntimeValidationError as exc:
                diagnostics.append(_safe_diagnostic(exc))
        models = tuple(
            {**model.to_dict(), "verified": False} for model in requirement.piper_models
        )
        capabilities: list[dict[str, Any]] = []
        if not diagnostics and requirement.required:
            try:
                self.seams.piper_smoke(requirement, product)
                models = tuple({**model.to_dict(), "verified": True} for model in requirement.piper_models)
                capabilities.append({"name": "provider_synthesis_native_pcm16_mono", "verified": True})
            except (RuntimeValidationError, TTSError, OSError, ValueError) as exc:
                diagnostics.append(f"Piper provider smoke test failed: {_safe_diagnostic(exc)}")
                capabilities.append({"name": "provider_synthesis_native_pcm16_mono", "verified": False})
        return ComponentEvidence(
            required=requirement.required,
            status=STATUS_FAIL if diagnostics else STATUS_PASS,
            reasons=requirement.reasons,
            expected=expected,
            observed=observed,
            models=models,
            capabilities=tuple(capabilities),
            diagnostics=tuple(sorted(diagnostics)),
        )

    def _validate_fdkaac(self, requirement: ComponentRequirement) -> ComponentEvidence:
        product = self.manifest["components"]["fdkaac"]
        runtime = product["runtime"]
        expected = {
            "fdkaac_version": runtime["fdkaac_version"],
            "libfdk_aac_version": runtime["libfdk_aac_version"],
            "validator": product["build"]["validator"],
        }
        binary = Path(runtime["binary"])
        if not requirement.required and not binary.exists():
            return _optional_absent(requirement, expected)
        diagnostics: list[str] = []
        capabilities = ({"name": "lc_he_hev2_encode_and_decode", "verified": False},)
        observed: dict[str, Any] = {"binary_present": binary.is_file()}
        validator_path = self.project_root / product["build"]["validator"]
        if not binary.is_file():
            diagnostics.append("canonical fdkaac binary is unavailable")
        elif not validator_path.is_file():
            diagnostics.append("authoritative HE-AAC validator is unavailable")
        else:
            try:
                self.seams.fdkaac_check(
                    validator_path,
                    binary,
                    Path(runtime["library_root"]),
                )
                capabilities = ({"name": "lc_he_hev2_encode_and_decode", "verified": True},)
                observed.update(
                    {
                        "fdkaac_version": runtime["fdkaac_version"],
                        "libfdk_aac_version": runtime["libfdk_aac_version"],
                        "identity_source": "authoritative_validator",
                    }
                )
            except (RuntimeValidationError, OSError, subprocess.SubprocessError) as exc:
                diagnostics.append(_safe_diagnostic(exc))
        return ComponentEvidence(
            required=requirement.required,
            status=STATUS_FAIL if diagnostics else STATUS_PASS,
            reasons=requirement.reasons,
            expected=expected,
            observed=observed,
            capabilities=capabilities,
            diagnostics=tuple(sorted(diagnostics)),
        )

    def validate(self, requirements: RuntimeRequirements) -> RuntimeEvidence:
        validators = {
            "fdkaac": self._validate_fdkaac,
            "kokoro": self._validate_kokoro,
            "piper": self._validate_piper,
        }
        components: dict[str, ComponentEvidence] = {}
        for name in COMPONENT_NAMES:
            try:
                components[name] = validators[name](requirements.components[name])
            except Exception as exc:  # one component must not suppress all evidence
                components[name] = ComponentEvidence(
                    required=requirements.components[name].required,
                    status=STATUS_FAIL,
                    reasons=requirements.components[name].reasons,
                    diagnostics=(f"unexpected validator failure: {exc.__class__.__name__}",),
                )
        try:
            contract_hash = _sha256(self.manifest_path)
        except OSError as exc:
            raise RuntimeValidationError("runtime contract identity could not be read") from exc
        return RuntimeEvidence(
            runtime_contract_sha256=contract_hash,
            runtime_manifest_schema_version=self.manifest["schema_version"],
            components=components,
            requirement_errors=requirements.errors,
        )


def _invalid_contract_evidence(manifest_path: Path) -> RuntimeEvidence:
    try:
        contract_hash = _sha256(manifest_path)
    except OSError:
        contract_hash = None
    return RuntimeEvidence(
        runtime_contract_sha256=contract_hash,
        runtime_manifest_schema_version=None,
        components={},
        contract_errors=("runtime component contract is invalid",),
    )


def validate_current_runtime(
    *,
    validator: RuntimeValidator | None = None,
    manifest_path: Path = MANIFEST_PATH,
) -> RuntimeEvidence:
    try:
        active_validator = validator or RuntimeValidator(manifest_path=manifest_path)
    except RuntimeComponentContractError:
        return _invalid_contract_evidence(Path(manifest_path))
    try:
        requirements = resolve_current_runtime_requirements(active_validator.manifest)
    except Exception:
        # Database connectivity/schema problems must produce evidence rather
        # than inferred station requirements or leaked connector diagnostics.
        requirements = unresolved_runtime_requirements(
            "station configuration could not be inspected"
        )
    return active_validator.validate(requirements)
