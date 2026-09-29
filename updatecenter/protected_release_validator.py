"""Release-authoring validation for a Phase-D protected runtime.

Unlike station execution this reads a reviewed working tree.  It uses the same
strict manifest, descriptor, policy, trust and Ed25519 contracts as the worker,
then cross-checks the declaration against predecessor Git facts when supplied.
It is read-only and has no private-key input.

Release-identity closure (added after r0093): production never reads a release
from the working tree or from canonical main's tip.  It resolves the release's
immutable identity with ``TrustedRepository.introducing_commit()`` -- the one
commit that added ``deploy/releases/<id>.json`` -- and then reads the
descriptor, every runtime inventory file and every attestation from THAT
commit.  A release whose attestation was added in a later commit (r0093's
``4643760`` manifest vs ``f43266c`` attestation) therefore cannot activate,
even though its working tree is fully valid.  ``validate_protected_release``
now always proves that closure, in one of two modes:

* committed -- the manifest is already in history: the production identity
  rule and production handoff functions (``materialize_candidate`` /
  ``stage_attestations``) are run against the resolved commit, and the whole
  authoring validation is then performed on bytes read from that commit,
  never on the working tree.
* prospective_atomic -- the manifest has never been committed (the correct
  state while an atomic release is still being signed): every declared
  artifact must exist in the working tree and not be Git-ignored, so the
  single commit that first introduces the manifest can contain all of them.
  The committed-mode check MUST be re-run on the resulting commit before
  publication.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
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


def _check_release_tree(
    *, checkout_root: Path, manifest_path: Path, trust_policy_path: Path,
    signer_directory: Path, previous_generation: int,
    previous_policy_path: Path | None,
) -> dict:
    """Validate one reviewed tree (the working tree, or bytes reconstructed
    from the release's introducing commit).  Returns the verified facts."""
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

    return {
        "manifest": manifest, "field": field, "descriptor": descriptor,
        "outcome": outcome, "trust_policy": trust_policy,
        "candidate_policy": candidate_policy,
    }


_RELEASE_DIR = "deploy/releases"
_SHA1 = re.compile(r"[0-9a-f]{40}")


@dataclasses.dataclass(frozen=True)
class ReleaseIdentityClosure:
    """How this validation established that the exact immutable commit
    production will resolve is self-sufficient."""
    mode: str  # "committed" | "prospective_atomic"
    identity_tip: str
    introducing_commit: str | None

    def as_evidence(self) -> dict:
        return {
            "mode": self.mode,
            "identity_tip": self.identity_tip,
            "introducing_commit": self.introducing_commit,
        }


def _runtime_modules():
    runtime_source = Path(__file__).resolve().parents[1] / "deploy" / "updater_runtime"
    if str(runtime_source) not in sys.path:
        sys.path.insert(0, str(runtime_source))
    from isadoraair_updater.process import CommandRunner
    from isadoraair_updater.release import TrustedRepository
    from isadoraair_updater import runtime_handoff
    return CommandRunner, TrustedRepository, runtime_handoff


def _resolve_identity_tip(checkout: Path, identity_tip: str | None) -> str:
    tip = git_adapter.rev_parse(checkout, identity_tip or "HEAD")
    if tip is None or not _SHA1.fullmatch(tip) or not git_adapter.commit_exists(checkout, tip):
        raise ProtectedReleaseValidationError(
            "cannot establish immutable release identity: "
            f"{identity_tip or 'HEAD'!r} does not resolve to a commit in a Git checkout"
        )
    return tip


def _production_repository(checkout: Path):
    common = git_adapter.run_git(["rev-parse", "--path-format=absolute", "--git-common-dir"], checkout)
    if not common.ok or not common.stdout:
        raise ProtectedReleaseValidationError(
            "cannot establish immutable release identity: checkout is not a Git repository"
        )
    command_runner, trusted_repository, _ = _runtime_modules()
    # The very class production uses; only its (never-invoked here) fetch
    # configuration is irrelevant to identity, read and handoff operations.
    return trusted_repository(Path(common.stdout), upstream="", branch="main", runner=command_runner())


def _declared_paths(manifest_relative: str, field, descriptor_bytes: bytes) -> list[str]:
    """Every repo path production reads from the release's identity commit."""
    _runtime_modules()  # ensures deploy/updater_runtime is importable
    from protected_bootstrap.descriptor import parse_descriptor_dict
    descriptor = parse_descriptor_dict(json.loads(descriptor_bytes.decode("utf-8")), label="descriptor")
    base = PurePosixPath(field.descriptor_path).parent
    return [
        manifest_relative,
        field.descriptor_path,
        *(str(base / entry.path) for entry in descriptor.files),
        *field.attestations,
    ]


def _verify_prospective_atomic(
    checkout: Path, manifest_relative: str, field, tip: str,
) -> ReleaseIdentityClosure:
    """The manifest has never been committed.  The future introducing commit
    is whichever single commit first adds it, so every artifact it declares
    must be present now and committable into that same commit."""
    descriptor_file = checkout / field.descriptor_path
    if not descriptor_file.is_file():
        raise ProtectedReleaseValidationError(
            f"declared descriptor {field.descriptor_path} is absent from the working tree; "
            "it must be part of the single release-introduction commit"
        )
    problems = []
    for relative in _declared_paths(manifest_relative, field, descriptor_file.read_bytes()):
        if not (checkout / relative).is_file():
            problems.append(f"{relative} is absent from the working tree")
        elif git_adapter.is_path_ignored(checkout, relative) is not False:
            problems.append(f"{relative} is Git-ignored (or ignore status is unknown) and would be omitted from the release commit")
    if problems:
        raise ProtectedReleaseValidationError(
            "release identity closure failed (prospective): the commit that first introduces "
            f"{manifest_relative} could not contain every declared artifact: " + "; ".join(problems)
        )
    return ReleaseIdentityClosure("prospective_atomic", tip, None)


@contextlib.contextmanager
def _verify_committed(
    checkout: Path, manifest_relative: str, tip: str, manifest_file: Path,
    working_manifest_bytes: bytes,
):
    """Run production's identity rule and production's handoff functions
    against the exact commit production will resolve, then return that
    commit's own bytes for a full commit-sourced authoring validation."""
    _, _, runtime_handoff = _runtime_modules()
    repository = _production_repository(checkout)
    commit = repository.introducing_commit(manifest_relative, tip)
    if commit is None:
        raise ProtectedReleaseValidationError(
            f"release identity closure failed: {manifest_relative} has no unique immutable "
            f"introducing commit on the ancestry of {tip} (production requires it to be "
            "added exactly once and never modified afterwards)"
        )
    committed_manifest = repository.read_file(commit, manifest_relative, maximum=65536)
    if committed_manifest is None:
        raise ProtectedReleaseValidationError(
            f"release identity closure failed: {manifest_relative} is unreadable at its "
            f"introducing commit {commit}"
        )
    if committed_manifest != working_manifest_bytes:
        raise ProtectedReleaseValidationError(
            f"{manifest_relative} in the working tree differs from its immutable committed bytes at {commit}"
        )
    manifest = validate_manifest_dict(json.loads(committed_manifest.decode("utf-8")), source_label=manifest_file.name)
    field = manifest.protected_runtime
    if field is None:
        raise ProtectedReleaseValidationError("release does not declare protected_runtime")
    with tempfile.TemporaryDirectory(prefix="isadoraair-release-identity-") as scratch:
        slots_root = Path(scratch) / "slots"
        staging = runtime_handoff.new_supervisor_staging_directory(slots_root)
        try:
            materialized = runtime_handoff.materialize_candidate(repository, field, commit, staging)
            runtime_handoff.stage_attestations(repository, field, commit, slots_root, "B")
        except runtime_handoff.HandoffError as exc:
            raise ProtectedReleaseValidationError(
                f"release identity closure failed: production handoff from the release's "
                f"introducing commit {commit} would fail: {exc}"
            ) from exc
        # Rebuild, from the commit's own bytes only, the tree the full
        # authoring validation reads.
        reconstructed = Path(scratch) / "commit-tree"
        destination_manifest = reconstructed / manifest_relative
        destination_manifest.parent.mkdir(parents=True)
        destination_manifest.write_bytes(committed_manifest)
        descriptor_target = reconstructed / field.descriptor_path
        descriptor_target.parent.mkdir(parents=True, exist_ok=True)
        descriptor_target.write_bytes(materialized.descriptor_bytes)
        for entry in materialized.descriptor.files:
            target = descriptor_target.parent / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((staging / entry.path).read_bytes())
            os.chmod(target, int(entry.mode, 8))
        for relative in field.attestations:
            target = reconstructed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(repository.read_file(commit, relative, maximum=65536))
        yield ReleaseIdentityClosure("committed", tip, commit), reconstructed


def validate_protected_release(
    *, checkout_root: Path, manifest_path: Path, trust_policy_path: Path,
    signer_directory: Path, previous_generation: int,
    previous_policy_path: Path | None = None,
    previous_commit: str | None = None, target_commit: str | None = None,
    identity_tip: str | None = None,
) -> dict:
    """`identity_tip` is the commit whose ancestry stands in for production's
    fetched canonical tip when resolving release identity (default: HEAD)."""
    checkout = Path(checkout_root).resolve()
    manifest_file = Path(manifest_path).resolve()
    try:
        manifest_relative = manifest_file.relative_to(checkout).as_posix()
        manifest_file.relative_to(checkout / "deploy" / "releases")
    except ValueError as exc:
        raise ProtectedReleaseValidationError("manifest must live under this checkout's deploy/releases") from exc
    working_manifest_bytes = manifest_file.read_bytes()
    tip = _resolve_identity_tip(checkout, identity_tip)
    history = git_adapter.run_git(["log", tip, "--format=%H", "--", manifest_relative], checkout)
    if not history.ok:
        raise ProtectedReleaseValidationError("could not read the manifest's Git history")
    tree_arguments = dict(
        trust_policy_path=trust_policy_path, signer_directory=signer_directory,
        previous_generation=previous_generation, previous_policy_path=previous_policy_path,
    )
    if history.stdout:
        with _verify_committed(checkout, manifest_relative, tip, manifest_file, working_manifest_bytes) as (
            closure, committed_tree,
        ):
            checked = _check_release_tree(
                checkout_root=committed_tree, manifest_path=committed_tree / manifest_relative,
                **tree_arguments,
            )
    else:
        working = validate_manifest_dict(_load_json(manifest_file, label="release manifest"), source_label=manifest_file.name)
        if working.protected_runtime is None:
            raise ProtectedReleaseValidationError("release does not declare protected_runtime")
        closure = _verify_prospective_atomic(checkout, manifest_relative, working.protected_runtime, tip)
        checked = _check_release_tree(checkout_root=checkout, manifest_path=manifest_file, **tree_arguments)
    manifest, field, descriptor = checked["manifest"], checked["field"], checked["descriptor"]
    outcome, trust_policy, candidate_policy = checked["outcome"], checked["trust_policy"], checked["candidate_policy"]

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
        "release_identity": closure.as_evidence(),
    }
