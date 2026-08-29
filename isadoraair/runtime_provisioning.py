"""Deterministic, offline TTS provisioning for Runtime Foundation E3."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from isadoraair.runtime_bundle import (
    ComponentBundle,
    RuntimeBundle,
    RuntimeBundleError,
    load_runtime_bundle,
)
from isadoraair.runtime_components import MANIFEST_PATH, load_runtime_components
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    PiperModelRequirement,
    RuntimeRequirements,
)
from isadoraair.runtime_validation import (
    RuntimeEvidence,
    RuntimeValidator,
    STATUS_PASS,
)


TTS_COMPONENTS = ("kokoro", "piper")
BUILD_TIMEOUT_SECONDS = 300.0
PIP_TIMEOUT_SECONDS = 900.0


class RuntimeProvisioningError(RuntimeError):
    """A safe operator-facing provisioning failure."""


def _safe_message(value: object) -> str:
    return " ".join(str(value).split())[:512] or "runtime provisioning failed"


def _minimal_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
        if name in os.environ
    }
    environment.update(
        {
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
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


def _run_offline(command: list[str], *, timeout: float, cwd: Path) -> None:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            umask=0o022,
        )
    except OSError as exc:
        raise RuntimeProvisioningError("offline runtime build process could not start") from exc
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise RuntimeProvisioningError("offline runtime build process timed out") from exc
    if return_code != 0:
        raise RuntimeProvisioningError(
            f"offline runtime build process exited with status {return_code}"
        )


def _build_venv(generation: Path) -> None:
    _run_offline(
        [sys.executable, "-I", "-m", "venv", str(generation)],
        timeout=BUILD_TIMEOUT_SECONDS,
        cwd=generation.parent,
    )


def _install_wheels(generation: Path, bundle: RuntimeBundle, component: ComponentBundle) -> None:
    python = generation / "bin" / "python"
    _run_offline(
        [
            str(python),
            "-I",
            "-m",
            "pip",
            "install",
            "--isolated",
            "--disable-pip-version-check",
            "--no-input",
            "--no-index",
            "--only-binary=:all:",
            "--find-links",
            str(bundle.source(component.wheelhouse)),
            "--require-hashes",
            "-r",
            str(bundle.source(component.lock.path)),
        ],
        timeout=PIP_TIMEOUT_SECONDS,
        cwd=generation,
    )


def _validate_with_e2(
    manifest: dict[str, Any], requirements: RuntimeRequirements
) -> RuntimeEvidence:
    return RuntimeValidator(manifest=manifest, manifest_path=MANIFEST_PATH).validate(requirements)


BuildVenv = Callable[[Path], None]
InstallWheels = Callable[[Path, RuntimeBundle, ComponentBundle], None]
ValidateRuntime = Callable[[dict[str, Any], RuntimeRequirements], RuntimeEvidence]
Checkpoint = Callable[[str, str | None], None]


def _no_checkpoint(_name: str, _component: str | None) -> None:
    return None


def _publication_acceptance_errors(
    evidence: RuntimeEvidence, staged_components: tuple[str, ...]
) -> tuple[str, ...]:
    """Accept repaired components without hiding unrelated E2 failures."""

    errors: list[str] = []
    if evidence.contract_errors:
        errors.append("runtime contract validation failed")
    if evidence.requirement_errors:
        errors.append("station requirement validation failed")
    for name in staged_components:
        component = evidence.components.get(name)
        if component is None or component.status != STATUS_PASS:
            errors.append(f"published {name} component failed Foundation E2 validation")
    return tuple(errors)


def _rollback_verification_errors(
    before: RuntimeEvidence,
    after: RuntimeEvidence,
    staged_components: tuple[str, ...],
) -> tuple[str, ...]:
    """Verify restored publication scope while retaining unrelated evidence."""

    errors: list[str] = []
    if after.contract_errors != before.contract_errors:
        errors.append("runtime contract errors changed during rollback")
    if after.requirement_errors != before.requirement_errors:
        errors.append("station requirement errors changed during rollback")
    for name in staged_components:
        previous = before.components.get(name)
        restored = after.components.get(name)
        if previous is None or restored is None or restored.status != previous.status:
            errors.append(f"{name} did not return to its pre-publication E2 status")
    return tuple(errors)


@dataclass(frozen=True, slots=True)
class ProvisioningSeams:
    build_venv: BuildVenv = _build_venv
    install_wheels: InstallWheels = _install_wheels
    validate_runtime: ValidateRuntime = _validate_with_e2
    checkpoint: Checkpoint = _no_checkpoint


@dataclass(frozen=True, slots=True)
class ProvisioningLayout:
    target_root: Path
    runtime_root: Path
    tts_root: Path
    kokoro_venv: Path
    kokoro_assets: Path
    piper_venv: Path
    piper_assets: Path
    fdkaac_binary: Path
    fdkaac_library_root: Path
    application_root: Path
    tts_cli: Path
    tts_scratch: Path

    @staticmethod
    def _map(path: str | Path, target_root: Path) -> Path:
        canonical = Path(path)
        if not canonical.is_absolute():
            raise RuntimeProvisioningError("product runtime path is not absolute")
        if target_root == Path("/"):
            return canonical
        return target_root.joinpath(*canonical.parts[1:])

    @classmethod
    def from_manifest(
        cls, manifest: dict[str, Any], *, target_root: str | Path = "/"
    ) -> "ProvisioningLayout":
        root = Path(target_root).absolute()
        paths = manifest["canonical_paths"]
        kokoro = manifest["components"]["kokoro"]
        piper = manifest["components"]["piper"]
        fdkaac = manifest["components"]["fdkaac"]
        return cls(
            target_root=root,
            runtime_root=cls._map(paths["runtime_root"], root),
            tts_root=cls._map(paths["tts_asset_root"], root),
            kokoro_venv=cls._map(kokoro["runtime"]["venv"], root),
            kokoro_assets=cls._map(Path(kokoro["assets"]["model"]["path"]).parent, root),
            piper_venv=cls._map(piper["runtime"]["venv"], root),
            piper_assets=cls._map(piper["models"]["root"], root),
            fdkaac_binary=cls._map(fdkaac["runtime"]["binary"], root),
            fdkaac_library_root=cls._map(fdkaac["runtime"]["library_root"], root),
            application_root=cls._map(paths["application_root"], root),
            tts_cli=cls._map(paths["tts_cli"], root),
            tts_scratch=cls._map(paths["tts_scratch"], root),
        )

    def runtime_pointer(self, component: str) -> Path:
        return self.kokoro_venv if component == "kokoro" else self.piper_venv

    def asset_pointer(self, component: str) -> Path:
        return self.kokoro_assets if component == "kokoro" else self.piper_assets

    def runtime_generation(self, component: str, generation_id: str) -> Path:
        return self.runtime_pointer(component).parent / "generations" / generation_id

    def asset_generation(self, component: str, generation_id: str) -> Path:
        return self.tts_root / "generations" / component / generation_id


def manifest_for_layout(
    manifest: dict[str, Any],
    layout: ProvisioningLayout,
    *,
    staged_component: str | None = None,
    staged_runtime: Path | None = None,
    staged_assets: Path | None = None,
) -> dict[str, Any]:
    """Map only physical paths while retaining all E2 acceptance semantics."""

    mapped = deepcopy(manifest)
    for name, value in tuple(mapped["canonical_paths"].items()):
        mapped["canonical_paths"][name] = str(layout._map(value, layout.target_root))

    kokoro_venv = layout.kokoro_venv
    kokoro_assets = layout.kokoro_assets
    piper_venv = layout.piper_venv
    piper_assets = layout.piper_assets
    if staged_component == "kokoro":
        kokoro_venv = Path(staged_runtime)
        kokoro_assets = Path(staged_assets)
    elif staged_component == "piper":
        piper_venv = Path(staged_runtime)
        piper_assets = Path(staged_assets)

    kokoro = mapped["components"]["kokoro"]
    kokoro["runtime"]["venv"] = str(kokoro_venv)
    kokoro["runtime"]["python"] = str(kokoro_venv / "bin" / "python")
    for asset in kokoro["assets"].values():
        asset["path"] = str(kokoro_assets / asset["filename"])

    piper = mapped["components"]["piper"]
    piper["runtime"]["venv"] = str(piper_venv)
    piper["runtime"]["python"] = str(piper_venv / "bin" / "python")
    piper["runtime"]["executable"] = str(piper_venv / "bin" / "piper")
    piper["models"]["root"] = str(piper_assets)
    mapped["components"]["fdkaac"]["runtime"]["binary"] = str(layout.fdkaac_binary)
    mapped["components"]["fdkaac"]["runtime"]["library_root"] = str(
        layout.fdkaac_library_root
    )
    return mapped


@contextmanager
def runtime_provision_lock(layout: ProvisioningLayout):
    """Acquire the one process-wide publication lock shared by E3 and E4."""

    try:
        _mkdir_controlled(layout.runtime_root)
        lock_path = layout.runtime_root / ".provision.lock"
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise RuntimeProvisioningError(
            "exclusive runtime provisioning lock could not be opened"
        ) from exc
    with os.fdopen(lock_descriptor, "a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise RuntimeProvisioningError(
                "exclusive runtime provisioning lock could not be acquired"
            ) from exc
        yield


@dataclass(frozen=True, slots=True)
class ComponentProvisioningPlan:
    name: str
    required: bool
    reasons: tuple[str, ...]
    current_status: str
    action: str
    generation_id: str | None
    runtime_pointer: str
    asset_pointer: str
    runtime_generation: str | None
    asset_generation: str | None
    payload_files: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "asset_generation": self.asset_generation,
            "asset_pointer": self.asset_pointer,
            "current_status": self.current_status,
            "errors": list(self.errors),
            "generation_id": self.generation_id,
            "name": self.name,
            "payload_files": list(self.payload_files),
            "reasons": list(self.reasons),
            "required": self.required,
            "runtime_generation": self.runtime_generation,
            "runtime_pointer": self.runtime_pointer,
        }


@dataclass(frozen=True, slots=True)
class ProvisioningPlan:
    bundle_id: str
    bundle_manifest_sha256: str
    product_contract_sha256: str
    target_root: str
    components: tuple[ComponentProvisioningPlan, ...]
    current_evidence: RuntimeEvidence
    errors: tuple[str, ...] = ()
    schema_version: int = 1

    @property
    def needs_work(self) -> bool:
        return any(component.action == "provision" for component in self.components)

    @property
    def ready(self) -> bool:
        return not self.errors and not any(component.errors for component in self.components)

    @property
    def plan_id(self) -> str:
        payload = self.to_dict(include_plan_id=False)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self, *, include_plan_id: bool = True) -> dict[str, Any]:
        result = {
            "bundle_id": self.bundle_id,
            "bundle_manifest_sha256": self.bundle_manifest_sha256,
            "components": [component.to_dict() for component in self.components],
            "current_evidence": self.current_evidence.to_dict(),
            "errors": list(self.errors),
            "needs_work": self.needs_work,
            "product_contract_sha256": self.product_contract_sha256,
            "ready": self.ready,
            "schema_version": self.schema_version,
            "target_root": self.target_root,
        }
        if include_plan_id:
            result["plan_id"] = self.plan_id
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    plan_id: str
    changed_components: tuple[str, ...]
    evidence: RuntimeEvidence
    no_op: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_components": list(self.changed_components),
            "evidence": self.evidence.to_dict(),
            "no_op": self.no_op,
            "plan_id": self.plan_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class _StagedComponent:
    name: str
    generation_id: str
    runtime_generation: Path
    asset_generation: Path


@dataclass(slots=True)
class _PointerState:
    pointer: Path
    previous_symlink: str | None = None
    previous_backup: Path | None = None
    previously_absent: bool = False


def _generation_id(
    bundle: RuntimeBundle,
    component: ComponentBundle,
    requirement: ComponentRequirement,
) -> str:
    selected_models = [model.to_dict() for model in requirement.piper_models]
    material = {
        "assets": {name: item.to_dict() for name, item in sorted(component.assets.items())},
        "bundle_id": bundle.bundle_id,
        "component": component.name,
        "platform": bundle.platform,
        "lock": component.lock.to_dict(),
        "models": selected_models,
        "product_contract_sha256": bundle.product_contract_sha256,
        "wheels": [wheel.to_dict() for wheel in component.wheels],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"e3-{hashlib.sha256(encoded).hexdigest()[:24]}"


def _normalized_language(value: str) -> str:
    return value.replace("_", "-").lower()


def _piper_payload_errors(
    component: ComponentBundle, requirement: ComponentRequirement
) -> tuple[str, ...]:
    errors: list[str] = []
    for expected in requirement.piper_models:
        payload = component.piper_models.get(expected.model_id)
        if payload is None:
            errors.append(f"selected Piper model '{expected.model_id}' is missing from bundle")
            continue
        comparisons = (
            (payload.model.path.name, expected.model_filename, "model filename"),
            (payload.config.path.name, expected.config_filename, "config filename"),
            (payload.model.sha256, expected.model_sha256, "model checksum"),
            (payload.config.sha256, expected.config_sha256, "config checksum"),
            (_normalized_language(payload.language), _normalized_language(expected.language), "language"),
            (payload.sample_rate_hz, expected.sample_rate_hz, "sample rate"),
        )
        for actual, wanted, label in comparisons:
            if actual != wanted:
                errors.append(
                    f"selected Piper model '{expected.model_id}' bundle {label} disagrees with station configuration"
                )
    return tuple(sorted(errors))


def _next_generation_id(layout: ProvisioningLayout, component: str, base: str) -> str:
    runtime_pointer = layout.runtime_pointer(component)
    for index in range(0, 1000):
        candidate = base if index == 0 else f"{base}-repair-{index}"
        runtime_generation = layout.runtime_generation(component, candidate)
        asset_generation = layout.asset_generation(component, candidate)
        if not runtime_generation.exists() and not asset_generation.exists():
            return candidate
        if runtime_pointer.is_symlink():
            try:
                if runtime_pointer.resolve(strict=False) == runtime_generation.resolve(strict=False):
                    continue
            except OSError:
                continue
    raise RuntimeProvisioningError("no safe immutable generation identity is available")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_controlled(path: Path) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o755)
    if not existed:
        os.chmod(path, 0o755)


def _write_atomic(path: Path, data: bytes, *, mode: int = 0o644) -> None:
    temporary = path.parent / f".{path.name}.e3-{uuid.uuid4().hex}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _copy_verified(source: Path, destination: Path, expected_sha256: str) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(source, source_flags)
    temporary = destination.parent / f".{destination.name}.e3-{uuid.uuid4().hex}"
    destination_descriptor = -1
    digest = hashlib.sha256()
    try:
        destination_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        with os.fdopen(source_descriptor, "rb") as input_file, os.fdopen(
            destination_descriptor, "wb"
        ) as output_file:
            source_descriptor = -1
            destination_descriptor = -1
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        if digest.hexdigest() != expected_sha256:
            raise RuntimeProvisioningError("bundle material changed after plan verification")
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        temporary.unlink(missing_ok=True)


def _remove_unpublished(path: Path, pointers: tuple[Path, ...]) -> None:
    if path.is_symlink():
        raise RuntimeProvisioningError("immutable generation path is an unexpected symlink")
    if not path.exists():
        return
    resolved = path.resolve(strict=False)
    for pointer in pointers:
        if pointer.is_symlink() and pointer.resolve(strict=False) == resolved:
            return
    shutil.rmtree(path)


def _assert_confined_non_symlink(root: Path, target: Path) -> None:
    if root != Path("/") and not target.is_relative_to(root):
        raise RuntimeProvisioningError("publication target escapes the requested root")
    relative = target.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise RuntimeProvisioningError("publication path contains an unexpected symlink")
        if not cursor.exists():
            break


def _atomic_pointer(pointer: Path, target: Path, backup_root: Path) -> _PointerState:
    _mkdir_controlled(pointer.parent)
    _mkdir_controlled(backup_root)
    relative_target = os.path.relpath(target, pointer.parent)
    temporary = pointer.parent / f".{pointer.name}.e3-{uuid.uuid4().hex}"
    os.symlink(relative_target, temporary)
    state = _PointerState(pointer=pointer)
    try:
        if pointer.is_symlink():
            state.previous_symlink = os.readlink(pointer)
        elif pointer.exists():
            backup = backup_root / f"legacy-{uuid.uuid4().hex}"
            os.replace(pointer, backup)
            state.previous_backup = backup
        else:
            state.previously_absent = True
        try:
            os.replace(temporary, pointer)
            _fsync_directory(pointer.parent)
        except Exception as exc:
            try:
                _restore_pointer(state)
            except Exception as rollback_exc:
                raise RuntimeProvisioningError(
                    "runtime pointer publication and immediate restoration both failed"
                ) from rollback_exc
            raise exc
    finally:
        temporary.unlink(missing_ok=True)
    return state


def _restore_pointer(state: _PointerState) -> None:
    pointer = state.pointer
    if state.previous_symlink is not None:
        temporary = pointer.parent / f".{pointer.name}.e3-rollback-{uuid.uuid4().hex}"
        os.symlink(state.previous_symlink, temporary)
        try:
            os.replace(temporary, pointer)
            _fsync_directory(pointer.parent)
        finally:
            temporary.unlink(missing_ok=True)
    elif state.previous_backup is not None:
        if pointer.is_symlink() or pointer.is_file():
            pointer.unlink()
        elif pointer.exists():
            raise RuntimeProvisioningError("cannot restore prior runtime over unexpected directory")
        os.replace(state.previous_backup, pointer)
        _fsync_directory(pointer.parent)
    elif state.previously_absent:
        if pointer.is_symlink() or pointer.is_file():
            pointer.unlink()
            _fsync_directory(pointer.parent)
        elif pointer.exists():
            raise RuntimeProvisioningError("cannot remove unexpected rollback directory")


class RuntimeProvisioner:
    """Reusable API accepting resolved requirements rather than querying Django."""

    def __init__(
        self,
        *,
        bundle_root: str | Path,
        requirements: RuntimeRequirements,
        product_manifest: dict[str, Any] | None = None,
        target_root: str | Path = "/",
        seams: ProvisioningSeams = ProvisioningSeams(),
    ) -> None:
        self.bundle_root = Path(bundle_root).absolute()
        self.manifest = product_manifest or load_runtime_components()
        self.requirements = requirements
        self.layout = ProvisioningLayout.from_manifest(self.manifest, target_root=target_root)
        self.seams = seams

    def _bundle(self) -> RuntimeBundle:
        return load_runtime_bundle(self.bundle_root, self.manifest)

    def _current_evidence(self) -> RuntimeEvidence:
        return self.seams.validate_runtime(
            manifest_for_layout(self.manifest, self.layout), self.requirements
        )

    def plan(self) -> ProvisioningPlan:
        bundle = self._bundle()
        current = self._current_evidence()
        global_errors = tuple(sorted(set(self.requirements.errors)))
        components: list[ComponentProvisioningPlan] = []
        for name in TTS_COMPONENTS:
            requirement = self.requirements.components[name]
            if not requirement.required:
                continue
            component = bundle.components.get(name)
            errors: tuple[str, ...] = ()
            if component is None:
                errors = (f"required component '{name}' is missing from bundle",)
            elif name == "piper":
                errors = _piper_payload_errors(component, requirement)
            current_status = current.components.get(name)
            status = current_status.status if current_status is not None else "fail"
            action = "blocked" if errors or global_errors else (
                "no_op" if status == STATUS_PASS else "provision"
            )
            generation_id = None
            runtime_generation = None
            asset_generation = None
            payload_files: tuple[str, ...] = ()
            if component is not None:
                payload_files = tuple(path.as_posix() for path in component.declared_paths)
                base = _generation_id(bundle, component, requirement)
                generation_id = base if action == "no_op" else _next_generation_id(
                    self.layout, name, base
                )
                runtime_generation = str(self.layout.runtime_generation(name, generation_id))
                asset_generation = str(self.layout.asset_generation(name, generation_id))
            components.append(
                ComponentProvisioningPlan(
                    name=name,
                    required=True,
                    reasons=requirement.reasons,
                    current_status=status,
                    action=action,
                    generation_id=generation_id,
                    runtime_pointer=str(self.layout.runtime_pointer(name)),
                    asset_pointer=str(self.layout.asset_pointer(name)),
                    runtime_generation=runtime_generation,
                    asset_generation=asset_generation,
                    payload_files=payload_files,
                    errors=errors,
                )
            )
        if (
            not global_errors
            and not any(component.action == "provision" for component in components)
            and current.result != STATUS_PASS
        ):
            global_errors = ("Foundation E2 acceptance is not currently passing",)
        return ProvisioningPlan(
            bundle_id=bundle.bundle_id,
            bundle_manifest_sha256=bundle.manifest_sha256,
            product_contract_sha256=bundle.product_contract_sha256,
            target_root=str(self.layout.target_root),
            components=tuple(components),
            current_evidence=current,
            errors=global_errors,
        )

    def _preflight_apply(self) -> None:
        root = self.layout.target_root
        if root.is_symlink() or not root.is_dir():
            raise RuntimeProvisioningError("target root must be an existing non-symlink directory")
        if root == Path("/") and os.geteuid() != 0:
            raise RuntimeProvisioningError("canonical runtime publication requires root privileges")
        if root != Path("/") and root.stat().st_uid != os.geteuid():
            raise RuntimeProvisioningError("noncanonical target root must be owned by the caller")
        if not os.access(root, os.W_OK | os.X_OK):
            raise RuntimeProvisioningError("target root is not writable by the caller")
        for target in (
            self.layout.runtime_root,
            self.layout.tts_root,
            self.layout.kokoro_venv.parent,
            self.layout.piper_venv.parent,
        ):
            _assert_confined_non_symlink(root, target)

    def _record_build_material(
        self,
        generation: Path,
        bundle: RuntimeBundle,
        component: ComponentBundle,
        generation_id: str,
    ) -> None:
        record_root = generation / "isadoraair-build"
        provenance_root = record_root / "provenance"
        _mkdir_controlled(record_root)
        _mkdir_controlled(provenance_root)
        _copy_verified(
            bundle.source(component.lock.path),
            record_root / "requirements.lock",
            component.lock.sha256,
        )
        for index, item in enumerate(component.provenance):
            _copy_verified(
                bundle.source(item.path),
                provenance_root / f"{index:03d}-{item.path.name}",
                item.sha256,
            )
        record = {
            "bundle_id": bundle.bundle_id,
            "bundle_manifest_sha256": bundle.manifest_sha256,
            "component": component.name,
            "generation_id": generation_id,
            "product_contract_sha256": bundle.product_contract_sha256,
        }
        _write_atomic(
            record_root / "generation.json",
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n",
        )

    def _stage_assets(
        self,
        stage: _StagedComponent,
        bundle: RuntimeBundle,
        component: ComponentBundle,
        requirement: ComponentRequirement,
    ) -> None:
        _mkdir_controlled(stage.asset_generation)
        if stage.name == "kokoro":
            product_assets = self.manifest["components"]["kokoro"]["assets"]
            for name in ("model", "voices"):
                payload = component.assets[name]
                _copy_verified(
                    bundle.source(payload.path),
                    stage.asset_generation / product_assets[name]["filename"],
                    payload.sha256,
                )
        else:
            for expected in requirement.piper_models:
                payload = component.piper_models[expected.model_id]
                _copy_verified(
                    bundle.source(payload.model.path),
                    stage.asset_generation / expected.model_filename,
                    expected.model_sha256,
                )
                _copy_verified(
                    bundle.source(payload.config.path),
                    stage.asset_generation / expected.config_filename,
                    expected.config_sha256,
                )
        _fsync_directory(stage.asset_generation)

    def _stage_component(
        self,
        plan: ComponentProvisioningPlan,
        bundle: RuntimeBundle,
    ) -> _StagedComponent:
        assert plan.generation_id is not None
        component = bundle.components[plan.name]
        requirement = self.requirements.components[plan.name]
        stage = _StagedComponent(
            name=plan.name,
            generation_id=plan.generation_id,
            runtime_generation=Path(plan.runtime_generation),
            asset_generation=Path(plan.asset_generation),
        )
        pointers = (
            self.layout.runtime_pointer(plan.name),
            self.layout.asset_pointer(plan.name),
        )
        _assert_confined_non_symlink(self.layout.target_root, stage.runtime_generation.parent)
        _assert_confined_non_symlink(self.layout.target_root, stage.asset_generation.parent)
        try:
            _remove_unpublished(stage.runtime_generation, pointers)
            _remove_unpublished(stage.asset_generation, pointers)
            _mkdir_controlled(self.layout.runtime_root)
            _mkdir_controlled(stage.runtime_generation.parent.parent)
            _mkdir_controlled(stage.runtime_generation.parent)
            _mkdir_controlled(self.layout.tts_root)
            _mkdir_controlled(stage.asset_generation.parent.parent)
            _mkdir_controlled(stage.asset_generation.parent)
            self.seams.checkpoint("before_venv_build", plan.name)
            self.seams.build_venv(stage.runtime_generation)
            self.seams.checkpoint("after_venv_build", plan.name)
            self.seams.install_wheels(stage.runtime_generation, bundle, component)
            self._record_build_material(
                stage.runtime_generation, bundle, component, stage.generation_id
            )
            self.seams.checkpoint("before_asset_staging", plan.name)
            self._stage_assets(stage, bundle, component, requirement)
            self.seams.checkpoint("after_asset_staging", plan.name)
            staged_manifest = manifest_for_layout(
                self.manifest,
                self.layout,
                staged_component=plan.name,
                staged_runtime=stage.runtime_generation,
                staged_assets=stage.asset_generation,
            )
            staged_evidence = self.seams.validate_runtime(staged_manifest, self.requirements)
            evidence = staged_evidence.components.get(plan.name)
            if evidence is None or evidence.status != STATUS_PASS:
                raise RuntimeProvisioningError(
                    f"staged {plan.name} generation failed Foundation E2 validation"
                )
            self.seams.checkpoint("after_staged_validation", plan.name)
            return stage
        except Exception:
            _remove_unpublished(stage.runtime_generation, pointers)
            _remove_unpublished(stage.asset_generation, pointers)
            raise

    def _cleanup_stages(self, stages: list[_StagedComponent]) -> None:
        for stage in stages:
            pointers = (
                self.layout.runtime_pointer(stage.name),
                self.layout.asset_pointer(stage.name),
            )
            _remove_unpublished(stage.runtime_generation, pointers)
            _remove_unpublished(stage.asset_generation, pointers)

    def apply(self) -> ProvisioningResult:
        plan = self.plan()
        if not plan.ready:
            raise RuntimeProvisioningError("provisioning plan contains blocking errors")
        self._preflight_apply()
        with runtime_provision_lock(self.layout):
            locked_plan = self.plan()
            if not locked_plan.ready:
                raise RuntimeProvisioningError("provisioning plan changed and is no longer ready")
            if not locked_plan.needs_work:
                return ProvisioningResult(
                    plan_id=locked_plan.plan_id,
                    changed_components=(),
                    evidence=locked_plan.current_evidence,
                    no_op=True,
                )
            bundle = self._bundle()
            stages: list[_StagedComponent] = []
            pointer_states: list[_PointerState] = []
            try:
                for component_plan in locked_plan.components:
                    if component_plan.action == "provision":
                        stages.append(self._stage_component(component_plan, bundle))
                for stage in stages:
                    asset_state = _atomic_pointer(
                        self.layout.asset_pointer(stage.name),
                        stage.asset_generation,
                        stage.asset_generation.parent,
                    )
                    pointer_states.append(asset_state)
                    self.seams.checkpoint("after_asset_publication", stage.name)
                    runtime_state = _atomic_pointer(
                        self.layout.runtime_pointer(stage.name),
                        stage.runtime_generation,
                        stage.runtime_generation.parent,
                    )
                    pointer_states.append(runtime_state)
                    self.seams.checkpoint("after_runtime_publication", stage.name)
                final_evidence = self._current_evidence()
                staged_names = tuple(stage.name for stage in stages)
                acceptance_errors = _publication_acceptance_errors(
                    final_evidence, staged_names
                )
                if acceptance_errors:
                    raise RuntimeProvisioningError(
                        "published runtime failed authoritative Foundation E2 acceptance: "
                        + "; ".join(acceptance_errors)
                    )
                self.seams.checkpoint("after_final_acceptance", None)
            except Exception as exc:
                rollback_errors: list[str] = []
                for state in reversed(pointer_states):
                    try:
                        _restore_pointer(state)
                    except Exception as rollback_exc:
                        rollback_errors.append(_safe_message(rollback_exc))
                try:
                    rollback_evidence = self._current_evidence()
                    rollback_errors.extend(
                        _rollback_verification_errors(
                            locked_plan.current_evidence,
                            rollback_evidence,
                            tuple(stage.name for stage in stages),
                        )
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(_safe_message(rollback_exc))
                self._cleanup_stages(stages)
                if rollback_errors:
                    raise RuntimeProvisioningError(
                        "provisioning failed and rollback failed: "
                        + "; ".join(rollback_errors)
                    ) from exc
                if isinstance(exc, (RuntimeProvisioningError, RuntimeBundleError)):
                    raise
                raise RuntimeProvisioningError(_safe_message(exc)) from exc
            return ProvisioningResult(
                plan_id=locked_plan.plan_id,
                changed_components=tuple(stage.name for stage in stages),
                evidence=final_evidence,
                no_op=False,
            )
