"""Load and structurally validate IsadoraAir's runtime-component contract.

The JSON file beside this module is the single machine-readable authority for
runtime versions, durable asset identities, canonical paths, and conditional
availability semantics. Provisioning, validation, disaster recovery, and the
fresh installer should consume it rather than copying these values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MANIFEST_PATH = Path(__file__).with_name("runtime_components.json")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AVAILABILITY_POLICIES = {"feature_selected", "optional"}


class RuntimeComponentContractError(ValueError):
    """The checked-in runtime-component contract is invalid."""


def _require_mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeComponentContractError(f"{location} must be a JSON object")
    return value


def _require_nonempty_string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeComponentContractError(f"{location} must be a non-empty string")
    return value


def _validate_sha256(value: Any, location: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise RuntimeComponentContractError(f"{location} must be a lowercase SHA-256 digest")


def _validate_contract(
    manifest: dict[str, Any], *, packages_path: Path | None = None
) -> None:
    if manifest.get("schema_version") != 1:
        raise RuntimeComponentContractError("schema_version must be 1")

    paths = _require_mapping(manifest.get("canonical_paths"), "canonical_paths")
    for name, value in paths.items():
        path = Path(_require_nonempty_string(value, f"canonical_paths.{name}"))
        if not path.is_absolute():
            raise RuntimeComponentContractError(f"canonical_paths.{name} must be absolute")

    components = _require_mapping(manifest.get("components"), "components")
    for required_name in ("kokoro", "piper", "fdkaac"):
        component = _require_mapping(components.get(required_name), f"components.{required_name}")
        availability = _require_mapping(
            component.get("availability"), f"components.{required_name}.availability"
        )
        policy = availability.get("policy")
        if policy not in _AVAILABILITY_POLICIES:
            raise RuntimeComponentContractError(
                f"components.{required_name}.availability.policy must be one of "
                f"{sorted(_AVAILABILITY_POLICIES)}"
            )
        if availability.get("unselected_absent") != "optional_pass":
            raise RuntimeComponentContractError(
                f"components.{required_name}.availability.unselected_absent must be optional_pass"
            )
        if availability.get("selected_missing_or_broken") != "fail":
            raise RuntimeComponentContractError(
                f"components.{required_name}.availability.selected_missing_or_broken must be fail"
            )

    # Package membership remains authoritative in the Git-owned Ubuntu
    # package file. The product contract may reference its group names,
    # but an unknown reference makes the combined contract invalid now --
    # never later only when station evidence happens to request it.
    from isadoraair.runtime_packages import (
        PACKAGES_MANIFEST_PATH,
        RuntimePackageAuthorityError,
        parse_package_groups,
    )

    try:
        package_groups = parse_package_groups(packages_path or PACKAGES_MANIFEST_PATH)
    except RuntimePackageAuthorityError as exc:
        raise RuntimeComponentContractError(
            f"Ubuntu package authority is invalid: {exc}"
        ) from exc

    for name in ("kokoro", "piper"):
        runtime_block = components[name].get("runtime")
        if isinstance(runtime_block, dict) and "ubuntu_packages_group" in runtime_block:
            group = _require_nonempty_string(
                runtime_block["ubuntu_packages_group"],
                f"components.{name}.runtime.ubuntu_packages_group",
            )
            if group not in package_groups:
                raise RuntimeComponentContractError(
                    f"components.{name}.runtime.ubuntu_packages_group references "
                    f"unknown Ubuntu package group '{group}'"
                )

    kokoro_assets = _require_mapping(components["kokoro"].get("assets"), "components.kokoro.assets")
    for asset_name in ("model", "voices"):
        asset = _require_mapping(kokoro_assets.get(asset_name), f"components.kokoro.assets.{asset_name}")
        _require_nonempty_string(asset.get("filename"), f"components.kokoro.assets.{asset_name}.filename")
        asset_path = Path(
            _require_nonempty_string(asset.get("path"), f"components.kokoro.assets.{asset_name}.path")
        )
        if not asset_path.is_absolute():
            raise RuntimeComponentContractError(
                f"components.kokoro.assets.{asset_name}.path must be absolute"
            )
        _validate_sha256(asset.get("sha256"), f"components.kokoro.assets.{asset_name}.sha256")

    source_archives = _require_mapping(
        components["fdkaac"].get("source_archives"), "components.fdkaac.source_archives"
    )
    for archive_name in ("fdk-aac", "fdkaac"):
        archive = _require_mapping(
            source_archives.get(archive_name), f"components.fdkaac.source_archives.{archive_name}"
        )
        _require_nonempty_string(
            archive.get("filename"), f"components.fdkaac.source_archives.{archive_name}.filename"
        )
        if not isinstance(archive.get("bytes"), int) or archive["bytes"] <= 0:
            raise RuntimeComponentContractError(
                f"components.fdkaac.source_archives.{archive_name}.bytes must be a positive integer"
            )
        _validate_sha256(
            archive.get("sha256"), f"components.fdkaac.source_archives.{archive_name}.sha256"
        )
        acquisition_url = _require_nonempty_string(
            archive.get("acquisition_url"),
            f"components.fdkaac.source_archives.{archive_name}.acquisition_url",
        )
        if not acquisition_url.startswith("https://"):
            raise RuntimeComponentContractError(
                f"components.fdkaac.source_archives.{archive_name}.acquisition_url must use HTTPS"
            )
        license_file = Path(
            _require_nonempty_string(
                archive.get("license_file"),
                f"components.fdkaac.source_archives.{archive_name}.license_file",
            )
        )
        if license_file.name != str(license_file):
            raise RuntimeComponentContractError(
                f"components.fdkaac.source_archives.{archive_name}.license_file must be a basename"
            )

    fdkaac_build = _require_mapping(components["fdkaac"].get("build"), "components.fdkaac.build")
    for field in ("script", "validator", "ubuntu_packages_group", "local_source_mode", "network_source_mode"):
        _require_nonempty_string(fdkaac_build.get(field), f"components.fdkaac.build.{field}")
    build_group = fdkaac_build["ubuntu_packages_group"]
    if build_group not in package_groups:
        raise RuntimeComponentContractError(
            "components.fdkaac.build.ubuntu_packages_group references unknown "
            f"Ubuntu package group '{build_group}'"
        )


def load_runtime_components(
    path: str | Path | None = None, *, packages_path: str | Path | None = None
) -> dict[str, Any]:
    """Return a validated runtime-component manifest.

    ``path`` exists for provisioner/validator tests and staged installs. Normal
    product callers should use the checked-in :data:`MANIFEST_PATH`.
    """

    manifest_path = Path(path) if path is not None else MANIFEST_PATH
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeComponentContractError(
            f"cannot load runtime-component contract {manifest_path}: {exc}"
        ) from exc
    manifest = _require_mapping(manifest, "manifest")
    _validate_contract(
        manifest,
        packages_path=Path(packages_path) if packages_path is not None else None,
    )
    return manifest


def get_runtime_component(name: str, path: str | Path | None = None) -> dict[str, Any]:
    """Return one component definition or fail with a stable error."""

    manifest = load_runtime_components(path)
    try:
        return manifest["components"][name]
    except KeyError as exc:
        raise RuntimeComponentContractError(f"unknown runtime component: {name}") from exc
