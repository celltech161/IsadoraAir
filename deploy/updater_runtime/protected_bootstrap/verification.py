"""D1-F: the supervisor-independent verification boundary -- one entry
point, verify_candidate_bundle(), that proves a candidate protected-
runtime generation is trustworthy WITHOUT ever trusting the currently
active worker's own conclusion about it, and WITHOUT importing Django or
isadoraair_updater.daemon/executor/jobs (only this package's own
sibling modules, isadoraair_updater.process/security -- both already
plain-stdlib -- and the standard library).

Full A/B slot activation (actually swapping which generation runs) is
explicitly D2's job, not this module's -- see this function's own
CandidateVerificationResult: it answers "is this trustworthy," never
"install it."

Two independent digests are involved, deliberately layered like a small
Merkle chain, each covering something the other does not:
  - descriptor_sha256 -- the hash of the DESCRIPTOR FILE'S OWN raw
    bytes (the JSON document). This is what the signed attestation
    statement binds, and what a manifest's own protected_runtime.
    descriptor_sha256 field (D1-A) pins.
  - bundle_sha256 -- a field INSIDE the descriptor (descriptor.py's
    compute_bundle_sha256()), the aggregate digest of the descriptor's
    OWN declared file inventory (path+hash+mode+size per file). This is
    what actually commits to the real code/data payload.
Verifying descriptor_sha256 against a signature, then separately
verifying every individual file's own sha256 (and bundle_sha256's own
internal consistency, already checked at parse time) against real bytes
on disk, transitively proves the signed statement's authority extends
all the way down to the actual files a candidate worker would execute."""
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
from .trust import SignatureAssertion, ThresholdEvaluation, TrustPolicy, evaluate_threshold


class CandidateRejected(ValueError):
    """Raised only for a caller-input contract violation (not a valid
    JSON dict for descriptor_bytes, generation not an int, etc.) --
    never for an ordinary "this candidate failed verification" outcome,
    which is always a CandidateVerificationResult with ok=False and
    specific reasons, not an exception."""


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
    require_policy_file: str | None = None,
    runner=None,
) -> CandidateVerificationResult:
    """Proves, independently of any worker's own claim:
      - the descriptor itself is well-formed (schema, bounds, sorted
        inventory, internal bundle_sha256 consistency);
      - its generation strictly exceeds previous_generation (or this is
        legitimately the very first generation ever, previous_generation
        is None);
      - the attestation threshold (M-of-N, trust_policy.threshold) is
        satisfied by REAL verified Ed25519 signatures over the exact
        (release_id, previous_release_id, generation, descriptor_sha256)
        statement -- never a claimed/unverified count;
      - require_policy_file, when given, is present in the descriptor's
        own file inventory;
      - the real files under bundle_root match the descriptor's
        inventory EXACTLY -- no missing file, no extra file, exact
        hash, exact mode, for every declared entry.

    Collects every failure reason rather than stopping at the first --
    a caller (or a test) can see the full picture of what's wrong with
    a bad candidate in one call, not just the first thing checked."""
    reasons: list[str] = []

    if not isinstance(release_id, str) or not release_id:
        raise CandidateRejected("release_id is required")
    if previous_generation is not None and (
        not isinstance(previous_generation, int) or isinstance(previous_generation, bool)
    ):
        raise CandidateRejected("previous_generation must be an integer or None")

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
            if previous_generation is None:
                reasons.append(
                    f"first-ever generation must be exactly 1, got {descriptor.generation}"
                )
            else:
                reasons.append(
                    f"generation {descriptor.generation} does not strictly exceed "
                    f"the previous generation {previous_generation} -- replay/rollback refused"
                )

        if require_policy_file is not None:
            if require_policy_file not in descriptor.file_by_path():
                reasons.append(f"required policy file {require_policy_file!r} is absent from the descriptor")

        disk_reasons = verify_descriptor_against_directory(descriptor, Path(bundle_root))
        reasons.extend(f"bundle mismatch: {reason}" for reason in disk_reasons)

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
                f"{threshold_evaluation.threshold} required signers verified "
                f"(rejected: {list(threshold_evaluation.rejected)})"
            )
    else:
        reasons.append("attestation threshold could not be evaluated -- no valid descriptor to bind a statement to")

    return CandidateVerificationResult(
        ok=not reasons, reasons=tuple(reasons), descriptor=descriptor,
        descriptor_sha256=descriptor_sha256, threshold_evaluation=threshold_evaluation,
    )
