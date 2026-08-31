"""Release-authoring validation for a Phase-D protected runtime.

Unlike station execution this reads a reviewed working tree.  It uses the same
strict manifest, descriptor, policy, trust and Ed25519 contracts as the worker,
then cross-checks the declaration against predecessor Git facts when supplied.
It is read-only and has no private-key input.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from deploy.updater_bootstrap.tools.protected_runtime_release import (
    DESCRIPTOR_FILENAME,
    ReleaseAuthoringError,
    build_descriptor,
    validate_descriptor_inventory,
)
from updatecenter import git_adapter
from updatecenter.manifest import validate_manifest_dict


class ProtectedReleaseValidationError(ValueError):
    pass


def _load_json(path: Path, *, label: str) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ProtectedReleaseValidationError(f"{label} must be a non-symlink regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtectedReleaseValidationError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtectedReleaseValidationError(f"{label} must be a JSON object")
    return value


def validate_protected_release(
    *, checkout_root: Path, manifest_path: Path, trust_policy_path: Path,
    signer_directory: Path, previous_generation: int,
    previous_policy_path: Path | None = None,
    previous_commit: str | None = None, target_commit: str | None = None,
) -> dict:
    runtime_source = Path(__file__).resolve().parents[1] / "deploy" / "updater_runtime"
    if str(runtime_source) not in sys.path:
        sys.path.insert(0, str(runtime_source))
    from protected_bootstrap.policy import parse_policy_dict
    from protected_bootstrap.trust import SignatureAssertion, parse_trust_policy_dict
    from protected_bootstrap.verification import verify_candidate_bundle

    checkout = Path(checkout_root).resolve()
    manifest_file = Path(manifest_path).resolve()
    try:
        manifest_file.relative_to(checkout / "deploy" / "releases")
    except ValueError as exc:
        raise ProtectedReleaseValidationError("manifest must live under this checkout's deploy/releases") from exc
    manifest = validate_manifest_dict(_load_json(manifest_file, label="release manifest"), source_label=manifest_file.name)
    field = manifest.protected_runtime
    if field is None:
        raise ProtectedReleaseValidationError("release does not declare protected_runtime")

    descriptor_path = checkout / field.descriptor_path
    if descriptor_path.name != DESCRIPTOR_FILENAME:
        raise ProtectedReleaseValidationError(
            f"descriptor must use the fixed release-authoring name {DESCRIPTOR_FILENAME!r}"
        )
    descriptor_bytes = descriptor_path.read_bytes()
    if hashlib.sha256(descriptor_bytes).hexdigest() != field.descriptor_sha256:
        raise ProtectedReleaseValidationError("manifest descriptor_sha256 does not match descriptor bytes")
    runtime_root = descriptor_path.parent
    try:
        descriptor = validate_descriptor_inventory(
            descriptor_bytes=descriptor_bytes, runtime_root=runtime_root,
        )
    except ReleaseAuthoringError as exc:
        raise ProtectedReleaseValidationError(str(exc)) from exc
    metadata = (
        descriptor.generation,
        descriptor.runtime_version,
        descriptor.manifest_protocol_version,
        descriptor.supported_wire_protocols,
    )
    declared = (
        field.generation,
        field.runtime_version,
        field.manifest_protocol_version,
        field.supported_wire_protocols,
    )
    if metadata != declared:
        raise ProtectedReleaseValidationError("manifest protected_runtime identity disagrees with descriptor")
    rebuilt = build_descriptor(
        runtime_root=runtime_root, generation=descriptor.generation,
        runtime_version=descriptor.runtime_version,
        manifest_protocol_version=descriptor.manifest_protocol_version,
        supported_wire_protocols=descriptor.supported_wire_protocols,
    )
    if rebuilt != descriptor_bytes:
        raise ProtectedReleaseValidationError("descriptor is not the deterministic canonical builder output")

    candidate_policy = parse_policy_dict(
        _load_json(runtime_root / "protected-policy.json", label="candidate protected policy"),
        label="candidate protected policy",
    )
    trust_policy = parse_trust_policy_dict(
        _load_json(Path(trust_policy_path), label="release trust fixture"),
        signer_directory=Path(signer_directory), label="release trust fixture",
    )
    assertions: list[SignatureAssertion] = []
    for relative in field.attestations:
        attestation = _load_json(checkout / relative, label=f"attestation {relative}")
        if set(attestation) != {"schema_version", "signer_id", "signature_base64"} or attestation["schema_version"] != 1:
            raise ProtectedReleaseValidationError(f"attestation {relative} has an invalid schema")
        try:
            signature = base64.b64decode(attestation["signature_base64"], validate=True)
        except (TypeError, ValueError) as exc:
            raise ProtectedReleaseValidationError(f"attestation {relative} has invalid base64") from exc
        assertions.append(SignatureAssertion(signer_id=attestation["signer_id"], signature=signature))
    # The reviewed Git source normally has repository checkout modes and the
    # adjacent descriptor metadata file. Materialize exactly the descriptor's
    # publication inventory in a temporary tree before invoking the station
    # verifier, matching the worker's real staging behavior.
    with tempfile.TemporaryDirectory(prefix="isadoraair-release-validate-") as scratch:
        staged_root = Path(scratch)
        for entry in descriptor.files:
            destination = staged_root / entry.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((runtime_root / entry.path).read_bytes())
            os.chmod(destination, int(entry.mode, 8))
        outcome = verify_candidate_bundle(
            release_id=manifest.release_id, previous_release_id=manifest.previous_release_id,
            previous_generation=previous_generation, descriptor_bytes=descriptor_bytes,
            bundle_root=staged_root, trust_policy=trust_policy, assertions=assertions,
            current_bootstrap_protocol_version=field.minimum_bootstrap_protocol_version,
            current_wire_protocol_version=field.supported_wire_protocols[0],
            candidate_minimum_bootstrap_protocol_version=field.minimum_bootstrap_protocol_version,
            require_policy_file="protected-policy.json",
        )
    if not outcome.ok:
        raise ProtectedReleaseValidationError(f"candidate verification failed: {outcome.reasons!r}")

    manifest_units = set(manifest.systemd_units_changed) | set(manifest.systemd_units_new_required)
    candidate_units = set(candidate_policy.as_mapping())
    if not manifest_units <= candidate_units:
        raise ProtectedReleaseValidationError(
            f"manifest unit(s) are absent from candidate signed policy: {sorted(manifest_units - candidate_units)!r}"
        )
    if previous_policy_path is not None:
        previous_policy = parse_policy_dict(
            _load_json(Path(previous_policy_path), label="previous protected policy"),
            label="previous protected policy",
        )
        newly_authorized = candidate_units - set(previous_policy.as_mapping())
        declared_intent = manifest_units | set(manifest.systemd_units_new_optional)
        if not newly_authorized <= declared_intent:
            raise ProtectedReleaseValidationError(
                "candidate policy authorizes new unit(s) not justified by manifest intent: "
                f"{sorted(newly_authorized - declared_intent)!r}"
            )

    if (previous_commit is None) != (target_commit is None):
        raise ProtectedReleaseValidationError("previous_commit and target_commit must be supplied together")
    changed_paths: tuple[str, ...] = ()
    if previous_commit is not None:
        paths = git_adapter.changed_paths_between(
            checkout, previous_commit, target_commit, "deploy",
        )
        if paths is None:
            raise ProtectedReleaseValidationError("could not derive predecessor Git diff")
        changed_paths = tuple(sorted(paths))
        runtime_changed = any(path.startswith("deploy/updater_runtime/") for path in paths)
        if not runtime_changed:
            raise ProtectedReleaseValidationError(
                "protected_runtime is declared but predecessor diff has no deploy/updater_runtime change"
            )
        changed_units = {Path(path).name for path in paths if path.startswith("deploy/") and path.endswith((".service", ".timer"))}
        if changed_units != (
            set(manifest.systemd_units_changed)
            | set(manifest.systemd_units_new_required)
            | set(manifest.systemd_units_new_optional)
            | set(manifest.systemd_units_removed_or_renamed)
        ):
            raise ProtectedReleaseValidationError("systemd manifest intent does not match predecessor diff")

    return {
        "release_id": manifest.release_id,
        "generation": descriptor.generation,
        "descriptor_sha256": field.descriptor_sha256,
        "bundle_sha256": descriptor.bundle_sha256,
        "verified_signers": list(outcome.threshold_evaluation.verified_signer_ids),
        "trust_threshold": trust_policy.threshold,
        "managed_units": candidate_policy.as_mapping(),
        "fingerprint_v3_input": {
            "generation": field.generation,
            "descriptor_sha256": field.descriptor_sha256,
            "minimum_bootstrap_protocol_version": field.minimum_bootstrap_protocol_version,
            "runtime_version": field.runtime_version,
            "manifest_protocol_version": field.manifest_protocol_version,
            "supported_wire_protocols": list(field.supported_wire_protocols),
        },
        "changed_paths": list(changed_paths),
    }
