"""Supervisor-side M-of-N trust policy -- an INDEPENDENT implementation
of the same contract as
deploy/updater_runtime/protected_bootstrap/trust.py (D1). Not imported
from there (Correction 1); uses THIS package's own security.py, not the
worker's. This is the supervisor's real, load-bearing copy -- unlike
the worker-side copy (which validates a candidate largely as a courtesy
before ever asking the supervisor to activate it), THIS evaluation is
the one the supervisor's own activation decision actually depends on."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import re

from .attestation import VerificationOutcome, verify_ed25519
from .descriptor import validate_relative_path
from .security import ProtectionError, assert_root_protected, assert_root_protected_parents

SCHEMA_VERSION = 1
SIGNATURE_ALGORITHM = "ed25519"
SIGNER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_SIGNERS = 16


class TrustPolicyError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Signer:
    id: str
    public_key_path: Path


@dataclasses.dataclass(frozen=True)
class TrustPolicy:
    schema_version: int
    signature_algorithm: str
    threshold: int
    signers: tuple[Signer, ...]

    def signer_by_id(self) -> dict[str, Signer]:
        return {signer.id: signer for signer in self.signers}


def _resolve_root_protected_directory(directory: Path, *, label: str) -> Path:
    directory = Path(directory)
    if not directory.is_absolute():
        raise TrustPolicyError(f"{label}: signer_directory must be an absolute path")
    try:
        assert_root_protected_parents(directory)
        assert_root_protected(directory, recursive=False)
    except ProtectionError as exc:
        raise TrustPolicyError(f"{label}: signer directory is not safely root-protected: {exc}") from exc
    return directory


def _resolve_signer_key_path(raw_path, resolved_directory: Path, *, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise TrustPolicyError(f"{label}: public_key_path must be an absolute string")
    candidate = Path(raw_path)
    if ".." in candidate.parts:
        raise TrustPolicyError(f"{label}: public_key_path must not contain '..'")
    validate_relative_path(candidate.name, field=f"{label}.public_key_path basename")
    if candidate.parent != resolved_directory:
        raise TrustPolicyError(f"{label}: public_key_path must be a direct child of {resolved_directory}")
    return candidate


def _parse_trust_policy_dict_against_resolved_directory(
    data: dict, *, resolved_directory: Path, label: str,
) -> TrustPolicy:
    """Shared parsing/validation body for both entry points below.
    `resolved_directory` has already been through whichever ownership
    check (or deliberate lack of one) its caller's category requires --
    everything from here on (schema shape, signer id syntax/
    uniqueness, MAX_SIGNERS, key-path containment under
    resolved_directory, threshold range) is identical and un-relaxed
    for both."""
    if not isinstance(data, dict):
        raise TrustPolicyError(f"{label}: trust policy must be a JSON object")
    known = {"schema_version", "signature_algorithm", "threshold", "signers"}
    if set(data) != known:
        raise TrustPolicyError(f"{label}: must have exactly {sorted(known)!r}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise TrustPolicyError(f"{label}: unsupported schema_version")
    if data["signature_algorithm"] != SIGNATURE_ALGORITHM:
        raise TrustPolicyError(f"{label}: signature_algorithm must be {SIGNATURE_ALGORITHM!r}")

    raw_signers = data["signers"]
    if not isinstance(raw_signers, list) or not raw_signers or len(raw_signers) > MAX_SIGNERS:
        raise TrustPolicyError(f"{label}: signers must be a non-empty, bounded list")

    signers: list[Signer] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_signers):
        item = f"{label}: signers[{index}]"
        if not isinstance(raw, dict) or set(raw) != {"id", "public_key_path"}:
            raise TrustPolicyError(f"{item}: must be an object with exactly id/public_key_path")
        signer_id = raw["id"]
        if not isinstance(signer_id, str) or not SIGNER_ID_RE.match(signer_id):
            raise TrustPolicyError(f"{item}: id must match {SIGNER_ID_RE.pattern!r}")
        if signer_id in seen_ids:
            raise TrustPolicyError(f"{item}: duplicate signer id {signer_id!r}")
        seen_ids.add(signer_id)
        key_path = _resolve_signer_key_path(raw["public_key_path"], resolved_directory, label=item)
        signers.append(Signer(id=signer_id, public_key_path=key_path))

    threshold = data["threshold"]
    if not isinstance(threshold, int) or isinstance(threshold, bool) or not (1 <= threshold <= len(signers)):
        raise TrustPolicyError(f"{label}: threshold must be between 1 and {len(signers)}")

    return TrustPolicy(schema_version=data["schema_version"], signature_algorithm=data["signature_algorithm"],
                       threshold=threshold, signers=tuple(signers))


def parse_trust_policy_dict(data: dict, *, signer_directory: Path, label: str = "<trust-policy>") -> TrustPolicy:
    """Parse and validate a trust policy against LIVE, INSTALLED,
    root-protected signer state -- the real supervisor's own startup/
    activation decision (updater_bootstrapd.py) and installed-state
    inspection (isadoraair.phase_d_recovery.load_installed_phase_d_state)
    both depend on this. `signer_directory` must genuinely be the
    system's current, root-owned signer directory; this is never the
    right entry point for a portable recovery artifact still sitting in
    ordinary staging -- see parse_trust_policy_dict_for_recovery_artifact
    for that case. Unchanged in behavior and signature from before r0033;
    every existing caller keeps its exact enforcement."""
    resolved_directory = _resolve_root_protected_directory(signer_directory, label=label)
    return _parse_trust_policy_dict_against_resolved_directory(data, resolved_directory=resolved_directory, label=label)


def parse_trust_policy_dict_for_recovery_artifact(
    data: dict, *, signer_directory: Path, label: str = "<trust-policy>",
) -> TrustPolicy:
    """Parse and validate a trust policy embedded in a portable Phase-D
    recovery artifact (a capture/attach/restore staging tree) -- never
    live installed state. r0031/r0030's capture, attach, and restore
    pipelines all validate a component copy that legitimately still
    sits under ordinary, non-root-owned scratch space (e.g. a
    tempfile.mkdtemp() staging directory) until/unless it is later
    installed by an entirely separate, still fully root-ownership-
    enforced path. Asking "is this the real, currently-installed,
    root-guarded signer directory" is categorically inapplicable to an
    artifact that has not been installed anywhere, so this entry point
    intentionally skips only that one check (_resolve_root_protected_directory).
    Every other check is identical and un-relaxed: schema shape, signer
    id syntax/uniqueness, MAX_SIGNERS, key-path containment under
    signer_directory, threshold range, and (for callers that verify
    signatures afterward) Ed25519 verification itself. This function
    must never be called with a live installed-state signer_directory --
    use parse_trust_policy_dict for that."""
    directory = Path(signer_directory)
    if not directory.is_absolute():
        raise TrustPolicyError(f"{label}: signer_directory must be an absolute path")
    return _parse_trust_policy_dict_against_resolved_directory(data, resolved_directory=directory, label=label)


@dataclasses.dataclass(frozen=True)
class SignatureAssertion:
    signer_id: str
    signature: bytes


@dataclasses.dataclass(frozen=True)
class ThresholdEvaluation:
    satisfied: bool
    threshold: int
    verified_signer_ids: tuple[str, ...]
    rejected: tuple[str, ...]

    @property
    def verified_count(self) -> int:
        return len(self.verified_signer_ids)


def evaluate_threshold(policy: TrustPolicy, statement: bytes, assertions, *, runner=None) -> ThresholdEvaluation:
    known = policy.signer_by_id()
    verified: list[str] = []
    rejected: list[str] = []
    counted: set[str] = set()
    for assertion in assertions:
        if not isinstance(assertion, SignatureAssertion):
            rejected.append("malformed assertion")
            continue
        signer = known.get(assertion.signer_id)
        if signer is None:
            rejected.append(f"{assertion.signer_id}: unknown signer")
            continue
        if assertion.signer_id in counted:
            continue
        try:
            outcome: VerificationOutcome = verify_ed25519(
                public_key_path=signer.public_key_path, statement=statement,
                signature=assertion.signature, runner=runner,
            )
        except Exception as exc:  # noqa: BLE001
            rejected.append(f"{assertion.signer_id}: verification could not run ({exc})")
            continue
        if outcome.verified:
            verified.append(assertion.signer_id)
            counted.add(assertion.signer_id)
        else:
            rejected.append(f"{assertion.signer_id}: {outcome.detail}")
    satisfied = len(verified) >= policy.threshold
    return ThresholdEvaluation(
        satisfied=satisfied, threshold=policy.threshold,
        verified_signer_ids=tuple(sorted(verified)), rejected=tuple(rejected),
    )
