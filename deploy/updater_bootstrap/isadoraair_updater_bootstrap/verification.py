"""D2-C: the supervisor's OWN independent candidate verification --
INDEPENDENT of deploy/updater_runtime/protected_bootstrap/verification.py
(D1, worker-side). Not imported from there (Correction 1). The
supervisor NEVER trusts a worker-supplied `verified=true` -- a worker
may REQUEST activation (see protocol.py's REQUEST_ACTIVATION), but this
function is what the supervisor itself calls to independently repeat
the full cryptographic/inventory proof against root-owned/candidate
material before ever acting on that request.

Also enforces two checks the worker-side copy left to a caller/context
it did not itself own: no symlink or special file anywhere in the
candidate tree (security.assert_no_symlink_in_tree -- the worker-side
descriptor verification refuses a symlink AT a declared path, but this
supervisor-side copy additionally walks for any special file the
descriptor doesn't even mention), and bootstrap-protocol/wire-
compatibility against the values THIS supervisor process actually
understands (never a caller-supplied assumption)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Sequence

from .attestation import build_attestation_statement
from .descriptor import (
    DescriptorError, RuntimeDescriptor, generation_advances, parse_descriptor_dict,
    verify_descriptor_against_directory,
)
from .security import ProtectionError, assert_no_symlink_in_tree
from .trust import SignatureAssertion, ThresholdEvaluation, TrustPolicy, evaluate_threshold


class CandidateRejected(ValueError):
    """Only for a caller-input contract violation, never an ordinary
    failed-verification outcome (which is a CandidateVerificationResult
    with reasons, not an exception)."""


@dataclasses.dataclass(frozen=True)
class CandidateVerificationResult:
    ok: bool
    reasons: tuple[str, ...]
    descriptor: RuntimeDescriptor | None
    descriptor_sha256: str | None
    threshold_evaluation: ThresholdEvaluation | None


def verify_candidate_bundle(
    *,
    release_id: str,
    previous_release_id: str | None,
    previous_generation: int | None,
    descriptor_bytes: bytes,
    bundle_root: Path,
    trust_policy: TrustPolicy,
    assertions: Sequence[SignatureAssertion],
    current_bootstrap_protocol_version: int,
    current_wire_protocol_version: int,
    candidate_minimum_bootstrap_protocol_version: int,
    require_policy_file: str | None = None,
    runner=None,
) -> CandidateVerificationResult:
    """candidate_minimum_bootstrap_protocol_version is supplied by the
    CALLER, never read from the descriptor itself -- it is a fact about
    the release MANIFEST's protected_runtime field (D1-A,
    ProtectedRuntimeField.minimum_bootstrap_protocol_version), which
    only the worker (the thing that actually reads the trusted git
    manifest chain) has parsed; REQUEST_ACTIVATION (protocol.py) is
    where that value crosses from worker to supervisor. Keeping it a
    plain parameter here -- rather than adding a new descriptor field --
    means the descriptor schema stays IDENTICAL in shape to D1's
    worker-side protected_bootstrap.descriptor (see
    test_phase_d2_parity.py), and the bootstrap-protocol check itself
    is trivially parity-testable as a pure parameter comparison."""
    if not isinstance(release_id, str) or not release_id:
        raise CandidateRejected("release_id is required")
    if not isinstance(candidate_minimum_bootstrap_protocol_version, int) or isinstance(
        candidate_minimum_bootstrap_protocol_version, bool
    ) or candidate_minimum_bootstrap_protocol_version < 1:
        raise CandidateRejected("candidate_minimum_bootstrap_protocol_version must be a positive integer")

    reasons: list[str] = []
    if candidate_minimum_bootstrap_protocol_version > current_bootstrap_protocol_version:
        reasons.append(
            f"candidate requires bootstrap protocol {candidate_minimum_bootstrap_protocol_version}, "
            f"this supervisor only understands up to {current_bootstrap_protocol_version} -- "
            "unsupported bootstrap protocol"
        )
    descriptor: RuntimeDescriptor | None = None
    descriptor_sha256: str | None = None
    try:
        parsed = json.loads(descriptor_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        reasons.append(f"descriptor is not valid UTF-8 JSON: {exc}")
    else:
        try:
            descriptor = parse_descriptor_dict(parsed, label="candidate descriptor")
        except DescriptorError as exc:
            reasons.append(f"descriptor is invalid: {exc}")
        else:
            descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()

    if descriptor is not None:
        if not generation_advances(descriptor.generation, previous_generation):
            reasons.append(
                f"generation {descriptor.generation} does not legitimately follow "
                f"previous generation {previous_generation!r} -- replay/rollback refused"
            )
        if descriptor.manifest_protocol_version < 1:
            reasons.append("descriptor manifest_protocol_version is invalid")
        if current_wire_protocol_version not in descriptor.supported_wire_protocols:
            reasons.append(
                f"descriptor supported_wire_protocols {list(descriptor.supported_wire_protocols)!r} "
                f"does not include this supervisor's current wire protocol "
                f"{current_wire_protocol_version} -- would strand an already-connected client"
            )
        try:
            assert_no_symlink_in_tree(Path(bundle_root))
        except ProtectionError as exc:
            reasons.append(f"bundle contains an unsafe entry: {exc}")
        disk_reasons = verify_descriptor_against_directory(descriptor, Path(bundle_root))
        reasons.extend(f"bundle mismatch: {reason}" for reason in disk_reasons)
        if require_policy_file is not None and require_policy_file not in descriptor.file_by_path():
            reasons.append(f"required policy file {require_policy_file!r} is absent from the descriptor")

    threshold_evaluation: ThresholdEvaluation | None = None
    if descriptor is not None and descriptor_sha256 is not None:
        statement = build_attestation_statement(
            release_id=release_id, previous_release_id=previous_release_id,
            generation=descriptor.generation, descriptor_sha256=descriptor_sha256,
        )
        threshold_evaluation = evaluate_threshold(trust_policy, statement, assertions, runner=runner)
        if not threshold_evaluation.satisfied:
            reasons.append(
                f"attestation threshold not satisfied: {threshold_evaluation.verified_count}/"
                f"{threshold_evaluation.threshold} required signers verified"
            )
    else:
        reasons.append("attestation threshold could not be evaluated -- no valid descriptor")

    return CandidateVerificationResult(
        ok=not reasons, reasons=tuple(reasons), descriptor=descriptor,
        descriptor_sha256=descriptor_sha256, threshold_evaluation=threshold_evaluation,
    )
