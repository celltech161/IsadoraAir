"""Strict local/offline payload contract for Runtime Foundation E3.

The product contract says what IsadoraAir requires.  A runtime bundle says
which immutable local files are available to reproduce that product runtime.
The bundle may never replace or override product/station authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


BUNDLE_FILENAME = "runtime-bundle.json"
BUNDLE_SCHEMA_VERSION = 1
MAX_BUNDLE_MANIFEST_BYTES = 1024 * 1024
MAX_LOCK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCK_LINE_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+) --hash=sha256:([0-9a-f]{64})$"
)
_COMPONENT_NAMES = {"kokoro", "piper"}


class RuntimeBundleError(ValueError):
    """A safe operator-facing error in an offline runtime bundle."""


def product_contract_digest(manifest: dict[str, Any]) -> str:
    """Stable semantic identity of the validated product contract."""

    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_platform_contract() -> dict[str, str]:
    return {
        "architecture": platform.machine().lower(),
        "os": platform.system().lower(),
        "python_abi": sys.implementation.cache_tag,
        "python_implementation": sys.implementation.name,
        "python_version": platform.python_version(),
    }


def _wheel_is_compatible(filename: str, host: dict[str, str]) -> bool:
    stem = filename.removesuffix(".whl")
    try:
        _prefix, python_tags, abi_tags, platform_tags = stem.rsplit("-", 3)
    except ValueError:
        return False
    major, minor, *_rest = host["python_version"].split(".")
    cp_tag = f"cp{major}{minor}"
    supported_python = {"py3", f"py{major}", f"py{major}{minor}", cp_tag}
    if not set(python_tags.split(".")) & supported_python:
        return False
    if not set(abi_tags.split(".")) & {"none", "abi3", cp_tag}:
        return False
    platforms = set(platform_tags.split("."))
    if "any" in platforms:
        return True
    architecture = host["architecture"].replace("-", "_")
    os_name = host["os"]
    if os_name == "linux":
        return any(
            architecture in tag
            and ("linux" in tag or "manylinux" in tag or "musllinux" in tag)
            for tag in platforms
        )
    if os_name == "darwin":
        return any(architecture in tag and "macosx" in tag for tag in platforms)
    if os_name == "windows":
        windows_arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            architecture, architecture
        )
        return any(tag == f"win_{windows_arch}" for tag in platforms)
    return False


def _canonical_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeBundleError(f"{location} must be an object")
    return value


def _list(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeBundleError(f"{location} must be a list")
    return value


def _keys(
    value: dict[str, Any],
    location: str,
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise RuntimeBundleError(f"{location} is missing fields: {', '.join(missing)}")
    if extra:
        raise RuntimeBundleError(f"{location} has unsupported fields: {', '.join(extra)}")


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeBundleError(f"{location} must be a non-empty string")
    return value


def _sha256_value(value: Any, location: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RuntimeBundleError(f"{location} must be a lowercase SHA-256 digest")
    return value


def _relative_path(value: Any, location: str) -> PurePosixPath:
    text = _text(value, location)
    candidate = PurePosixPath(text)
    if (
        candidate.is_absolute()
        or text.startswith("/")
        or "\\" in text
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise RuntimeBundleError(f"{location} must be a confined POSIX relative path")
    return candidate


def _basename(value: Any, location: str, *, suffix: str | None = None) -> str:
    text = _text(value, location)
    candidate = PurePosixPath(text)
    if candidate.name != text or "\\" in text:
        raise RuntimeBundleError(f"{location} must be a basename")
    if suffix is not None and not text.endswith(suffix):
        raise RuntimeBundleError(f"{location} must end with {suffix}")
    return text


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: PurePosixPath
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {"filename": self.path.as_posix(), "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class WheelFile:
    filename: str
    package: str
    version: str
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "filename": self.filename,
            "package": self.package,
            "sha256": self.sha256,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class PiperModelBundle:
    model_id: str
    model: BundleFile
    config: BundleFile
    language: str
    sample_rate_hz: int


@dataclass(frozen=True, slots=True)
class ComponentBundle:
    name: str
    lock: BundleFile
    wheelhouse: PurePosixPath
    wheels: tuple[WheelFile, ...]
    provenance: tuple[BundleFile, ...]
    assets: dict[str, BundleFile]
    piper_models: dict[str, PiperModelBundle]

    @property
    def declared_paths(self) -> tuple[PurePosixPath, ...]:
        wheel_paths = tuple(self.wheelhouse / wheel.filename for wheel in self.wheels)
        model_paths = tuple(
            path
            for model in self.piper_models.values()
            for path in (model.model.path, model.config.path)
        )
        return (
            self.lock.path,
            *wheel_paths,
            *(item.path for item in self.provenance),
            *(item.path for item in self.assets.values()),
            *model_paths,
        )


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    root: Path
    bundle_id: str
    platform: dict[str, str]
    product_contract_sha256: str
    components: dict[str, ComponentBundle]
    manifest_sha256: str

    def source(self, relative: PurePosixPath) -> Path:
        return self.root.joinpath(*relative.parts)


def _bundle_file(value: Any, location: str) -> BundleFile:
    item = _mapping(value, location)
    _keys(item, location, required={"filename", "sha256"})
    return BundleFile(
        path=_relative_path(item["filename"], f"{location}.filename"),
        sha256=_sha256_value(item["sha256"], f"{location}.sha256"),
    )


def _wheel(value: Any, location: str) -> WheelFile:
    item = _mapping(value, location)
    _keys(item, location, required={"filename", "package", "version", "sha256"})
    return WheelFile(
        filename=_basename(item["filename"], f"{location}.filename", suffix=".whl"),
        package=_canonical_package_name(_text(item["package"], f"{location}.package")),
        version=_text(item["version"], f"{location}.version"),
        sha256=_sha256_value(item["sha256"], f"{location}.sha256"),
    )


def _component(value: Any, name: str) -> ComponentBundle:
    location = f"components.{name}"
    item = _mapping(value, location)
    required = {"lock", "wheelhouse", "wheels", "provenance"}
    required.add("assets" if name == "kokoro" else "models")
    _keys(item, location, required=required)
    lock = _bundle_file(item["lock"], f"{location}.lock")
    wheelhouse = _relative_path(item["wheelhouse"], f"{location}.wheelhouse")
    wheels = tuple(
        _wheel(entry, f"{location}.wheels[{index}]")
        for index, entry in enumerate(_list(item["wheels"], f"{location}.wheels"))
    )
    if not wheels:
        raise RuntimeBundleError(f"{location}.wheels must contain a complete wheel closure")
    if len({wheel.package for wheel in wheels}) != len(wheels):
        raise RuntimeBundleError(f"{location}.wheels contains duplicate package identities")
    if {wheel.package for wheel in wheels} & {"isadoraair", "isadoraair-django"}:
        raise RuntimeBundleError(
            f"{location}.wheels may not install a second copy of IsadoraAir source"
        )
    provenance = tuple(
        _bundle_file(entry, f"{location}.provenance[{index}]")
        for index, entry in enumerate(_list(item["provenance"], f"{location}.provenance"))
    )
    if not provenance:
        raise RuntimeBundleError(f"{location}.provenance must include license/provenance material")

    assets: dict[str, BundleFile] = {}
    piper_models: dict[str, PiperModelBundle] = {}
    if name == "kokoro":
        raw_assets = _mapping(item["assets"], f"{location}.assets")
        _keys(raw_assets, f"{location}.assets", required={"model", "voices"})
        assets = {
            asset_name: _bundle_file(raw_assets[asset_name], f"{location}.assets.{asset_name}")
            for asset_name in ("model", "voices")
        }
    else:
        for index, raw_model in enumerate(_list(item["models"], f"{location}.models")):
            model_location = f"{location}.models[{index}]"
            model = _mapping(raw_model, model_location)
            _keys(
                model,
                model_location,
                required={"model_id", "model", "config", "language", "sample_rate_hz"},
            )
            model_id = _text(model["model_id"], f"{model_location}.model_id")
            if model_id in piper_models:
                raise RuntimeBundleError(f"{location}.models contains duplicate model_id '{model_id}'")
            sample_rate = model["sample_rate_hz"]
            if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
                raise RuntimeBundleError(f"{model_location}.sample_rate_hz is invalid")
            piper_models[model_id] = PiperModelBundle(
                model_id=model_id,
                model=_bundle_file(model["model"], f"{model_location}.model"),
                config=_bundle_file(model["config"], f"{model_location}.config"),
                language=_text(model["language"], f"{model_location}.language"),
                sample_rate_hz=sample_rate,
            )
    return ComponentBundle(
        name=name,
        lock=lock,
        wheelhouse=wheelhouse,
        wheels=tuple(sorted(wheels, key=lambda wheel: wheel.package)),
        provenance=tuple(sorted(provenance, key=lambda entry: entry.path.as_posix())),
        assets=assets,
        piper_models=piper_models,
    )


def _assert_bundle_tree(root: Path, expected_files: set[PurePosixPath]) -> None:
    observed: set[PurePosixPath] = set()
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in (*directories, *filenames):
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeBundleError(
                    f"bundle contains a forbidden symlink: {candidate.relative_to(root).as_posix()}"
                )
        for name in filenames:
            candidate = current_path / name
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeBundleError(
                    f"bundle contains a non-regular file: {candidate.relative_to(root).as_posix()}"
                )
            if metadata.st_nlink != 1:
                raise RuntimeBundleError(
                    f"bundle contains a hardlinked file: {candidate.relative_to(root).as_posix()}"
                )
            observed.add(PurePosixPath(candidate.relative_to(root).as_posix()))
    missing = sorted(path.as_posix() for path in expected_files - observed)
    extra = sorted(path.as_posix() for path in observed - expected_files)
    if missing:
        raise RuntimeBundleError(f"bundle is missing declared files: {', '.join(missing)}")
    if extra:
        raise RuntimeBundleError(f"bundle contains undeclared files: {', '.join(extra)}")


def _verify_files(bundle: RuntimeBundle) -> None:
    for component in bundle.components.values():
        expected: dict[PurePosixPath, str] = {
            component.lock.path: component.lock.sha256,
            **{item.path: item.sha256 for item in component.provenance},
            **{item.path: item.sha256 for item in component.assets.values()},
        }
        for wheel in component.wheels:
            expected[component.wheelhouse / wheel.filename] = wheel.sha256
        for model in component.piper_models.values():
            expected[model.model.path] = model.model.sha256
            expected[model.config.path] = model.config.sha256
        if len(expected) != len(component.declared_paths):
            raise RuntimeBundleError(f"components.{component.name} declares a file more than once")
        for relative, expected_hash in sorted(expected.items(), key=lambda item: item[0].as_posix()):
            source = bundle.source(relative)
            try:
                observed_hash = _hash_file(source)
            except OSError as exc:
                raise RuntimeBundleError(
                    f"bundle file cannot be read: {relative.as_posix()}"
                ) from exc
            if observed_hash != expected_hash:
                raise RuntimeBundleError(
                    f"bundle file checksum does not match: {relative.as_posix()}"
                )


def _parse_lock(bundle: RuntimeBundle, component: ComponentBundle) -> dict[str, tuple[str, str]]:
    lock_path = bundle.source(component.lock.path)
    try:
        if lock_path.stat().st_size > MAX_LOCK_BYTES:
            raise RuntimeBundleError(f"components.{component.name}.lock is too large")
        text = lock_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeBundleError(f"components.{component.name}.lock cannot be read") from exc
    packages: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LOCK_LINE_RE.fullmatch(line)
        if match is None:
            raise RuntimeBundleError(
                f"components.{component.name}.lock line {line_number} is not one exact hash-pinned wheel requirement"
            )
        package = _canonical_package_name(match.group(1))
        if package in packages:
            raise RuntimeBundleError(
                f"components.{component.name}.lock repeats package '{package}'"
            )
        packages[package] = (match.group(2), match.group(3))
    wheel_packages = {
        wheel.package: (wheel.version, wheel.sha256) for wheel in component.wheels
    }
    if packages != wheel_packages:
        raise RuntimeBundleError(
            f"components.{component.name} dependency lock does not exactly match its wheel closure"
        )
    return packages


def load_runtime_bundle(
    root: str | Path,
    product_manifest: dict[str, Any],
) -> RuntimeBundle:
    """Load, confine, hash, and cross-check one immutable offline bundle."""

    bundle_root = Path(root)
    if not bundle_root.is_absolute():
        bundle_root = bundle_root.absolute()
    if bundle_root.is_symlink() or not bundle_root.is_dir():
        raise RuntimeBundleError("bundle root must be an existing non-symlink directory")
    manifest_path = bundle_root / BUNDLE_FILENAME
    try:
        manifest_mode = manifest_path.lstat().st_mode
        if not stat.S_ISREG(manifest_mode) or manifest_path.is_symlink():
            raise RuntimeBundleError(f"{BUNDLE_FILENAME} must be a regular non-symlink file")
        if manifest_path.stat().st_size > MAX_BUNDLE_MANIFEST_BYTES:
            raise RuntimeBundleError(f"{BUNDLE_FILENAME} is too large")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except RuntimeBundleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeBundleError(f"cannot load {BUNDLE_FILENAME}") from exc
    manifest = _mapping(raw, "bundle")
    _keys(
        manifest,
        "bundle",
        required={
            "schema_version",
            "bundle_id",
            "platform",
            "product_contract_sha256",
            "components",
        },
    )
    if manifest["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise RuntimeBundleError(f"bundle schema_version must be {BUNDLE_SCHEMA_VERSION}")
    bundle_id = _text(manifest["bundle_id"], "bundle.bundle_id")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", bundle_id):
        raise RuntimeBundleError("bundle.bundle_id has an invalid stable identity")
    expected_product_hash = product_contract_digest(product_manifest)
    declared_product_hash = _sha256_value(
        manifest["product_contract_sha256"], "bundle.product_contract_sha256"
    )
    if declared_product_hash != expected_product_hash:
        raise RuntimeBundleError("bundle targets a different product runtime contract")

    platform_contract = _mapping(manifest["platform"], "bundle.platform")
    expected_platform = current_platform_contract()
    _keys(platform_contract, "bundle.platform", required=set(expected_platform))
    if platform_contract != expected_platform:
        raise RuntimeBundleError("bundle platform/Python ABI is incompatible with this host")

    raw_components = _mapping(manifest["components"], "bundle.components")
    unknown = sorted(set(raw_components) - _COMPONENT_NAMES)
    if unknown:
        raise RuntimeBundleError(f"bundle contains unsupported components: {', '.join(unknown)}")
    if not raw_components:
        raise RuntimeBundleError("bundle must contain at least one TTS component")
    components = {
        name: _component(raw_components[name], name) for name in sorted(raw_components)
    }
    for name, component in components.items():
        for wheel in component.wheels:
            if not _wheel_is_compatible(wheel.filename, expected_platform):
                raise RuntimeBundleError(
                    f"components.{name} wheel is incompatible with the declared host: {wheel.filename}"
                )
    try:
        manifest_hash = _hash_file(manifest_path)
    except OSError as exc:
        raise RuntimeBundleError(f"cannot hash {BUNDLE_FILENAME}") from exc
    bundle = RuntimeBundle(
        root=bundle_root,
        bundle_id=bundle_id,
        platform=expected_platform,
        product_contract_sha256=declared_product_hash,
        components=components,
        manifest_sha256=manifest_hash,
    )
    expected_files = {PurePosixPath(BUNDLE_FILENAME)}
    for component in components.values():
        expected_files.update(component.declared_paths)
    _assert_bundle_tree(bundle_root, expected_files)
    _verify_files(bundle)

    for name, component in components.items():
        locked = _parse_lock(bundle, component)
        product_packages = product_manifest["components"][name]["runtime"]["packages"]
        for product_name, product_version in sorted(product_packages.items()):
            observed = locked.get(_canonical_package_name(product_name))
            if observed is None or observed[0] != product_version:
                raise RuntimeBundleError(
                    f"components.{name} lock disagrees with product package '{product_name}'"
                )

    if "kokoro" in components:
        product_assets = product_manifest["components"]["kokoro"]["assets"]
        for name, payload in components["kokoro"].assets.items():
            product = product_assets[name]
            if payload.path.name != product["filename"] or payload.sha256 != product["sha256"]:
                raise RuntimeBundleError(
                    f"components.kokoro.assets.{name} disagrees with the product contract"
                )
    return bundle
