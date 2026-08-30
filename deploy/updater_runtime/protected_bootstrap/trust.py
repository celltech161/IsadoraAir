"""D1-E: a configurable M-of-N Ed25519 trust policy -- deliberately not
a hardcoded 2-of-2 (or any other fixed count). The actual production
threshold/signer set is an operational decision made later (see
docs/UPDATE_CENTER_PHASE_D.md); this module only builds the CAPABILITY
to express and enforce whatever that decision turns out to be.

Reuses isadoraair_updater.security.assert_root_protected -- the same
no-symlink/root-owned/non-group-or-world-writable check every other
protected-runtime-trusted path in this codebase already uses, including
its existing, already-tested "inactive under a non-root test process"
convention (checked only when os.geteuid() == 0), so signer key files
get the identical guarantee the trusted git repository and the systemd
unit root already have -- not a second, independently-written check
that could subtly disagree with the first."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import re

from isadoraair_updater.security import ProtectionError, assert_root_protected, assert_root_protected_parents

from .attestation import VerificationOutcome, verify_ed25519
from .descriptor import validate_relative_path

SCHEMA_VERSION = 1

# Fixed by this module, exactly like attestation.py's OPENSSL_BINARY --
# never read from the policy document itself. A trust policy document
# that named a DIFFERENT algorithm would be pointless anyway (nothing
# here knows how to verify it), so the field exists only as an
# explicit, self-documenting assertion of intent, checked against this
# one fixed value.
SIGNATURE_ALGORITHM = "ed25519"

SIGNER_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
MAX_SIGNERS = 16


class TrustPolicyError(ValueError):
    """Raised for a malformed trust-policy document, or a signer key
    file that fails its filesystem trust check -- both are refused
    before any signature is ever evaluated."""


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


def parse_trust_policy_dict(data: dict, *, signer_directory: Path, label: str = "<trust-policy>") -> TrustPolicy:
    """`signer_directory` is the one root-owned configured directory
    every signer's public_key_path must resolve inside of -- passed by
    the caller (never read from the document itself, which would let a
    compromised document point anywhere) and verified root-protected
    here. Each signer's own key file is verified separately, at
    evaluate_threshold() time, not here -- this function only checks
    the DOCUMENT's own structure and path containment, so a signer
    whose actual key file is temporarily absent/misconfigured does not
    prevent PARSING the policy (only prevents that signer's signature
    from ever counting)."""
    if not isinstance(data, dict):
        raise TrustPolicyError(f"{label}: trust policy must be a JSON object")

    known_top = {"schema_version", "signature_algorithm", "threshold", "signers"}
    unknown_top = set(data) - known_top
    if unknown_top:
        raise TrustPolicyError(f"{label}: unrecognized field(s) {sorted(unknown_top)!r}")
    missing_top = known_top - set(data)
    if missing_top:
        raise TrustPolicyError(f"{label}: missing required field(s) {sorted(missing_top)!r}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise TrustPolicyError(f"{label}: schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise TrustPolicyError(f"{label}: unsupported schema_version {schema_version} (expected {SCHEMA_VERSION})")

    algorithm = data["signature_algorithm"]
    if algorithm != SIGNATURE_ALGORITHM:
        raise TrustPolicyError(f"{label}: signature_algorithm must be {SIGNATURE_ALGORITHM!r}, got {algorithm!r}")

    raw_signers = data["signers"]
    if not isinstance(raw_signers, list) or not raw_signers:
        raise TrustPolicyError(f"{label}: signers must be a non-empty list")
    if len(raw_signers) > MAX_SIGNERS:
        raise TrustPolicyError(f"{label}: signers exceeds {MAX_SIGNERS} entries")

    resolved_directory = _resolve_root_protected_directory(signer_directory, label=label)

    known_signer_keys = {"id", "public_key_path"}
    signers: list[Signer] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_signers):
        item_label = f"{label}: signers[{index}]"
        if not isinstance(raw, dict):
            raise TrustPolicyError(f"{item_label}: must be a JSON object")
        unknown_keys = set(raw) - known_signer_keys
        if unknown_keys:
            raise TrustPolicyError(f"{item_label}: unrecognized field(s) {sorted(unknown_keys)!r}")
        missing_keys = known_signer_keys - set(raw)
        if missing_keys:
            raise TrustPolicyError(f"{item_label}: missing field(s) {sorted(missing_keys)!r}")

        signer_id = raw["id"]
        if not isinstance(signer_id, str) or not SIGNER_ID_RE.match(signer_id):
            raise TrustPolicyError(f"{item_label}: id must match {SIGNER_ID_RE.pattern!r}")
        if signer_id in seen_ids:
            raise TrustPolicyError(f"{item_label}: duplicate signer id {signer_id!r}")
        seen_ids.add(signer_id)

        raw_path = raw["public_key_path"]
        key_path = _resolve_signer_key_path(raw_path, resolved_directory, label=item_label)

        signers.append(Signer(id=signer_id, public_key_path=key_path))

    threshold = data["threshold"]
    if not isinstance(threshold, int) or isinstance(threshold, bool):
        raise TrustPolicyError(f"{label}: threshold must be an integer")
    if not (1 <= threshold <= len(signers)):
        raise TrustPolicyError(
            f"{label}: threshold must be between 1 and the signer count ({len(signers)}), got {threshold}"
        )

    return TrustPolicy(
        schema_version=schema_version, signature_algorithm=algorithm,
        threshold=threshold, signers=tuple(signers),
    )


def _resolve_root_protected_directory(signer_directory: Path, *, label: str) -> Path:
    directory = Path(signer_directory)
    if not directory.is_absolute():
        raise TrustPolicyError(f"{label}: signer_directory must be an absolute path")
    try:
        assert_root_protected_parents(directory)
        assert_root_protected(directory, recursive=False)
    except ProtectionError as exc:
        raise TrustPolicyError(f"{label}: signer directory is not safely root-protected: {exc}") from exc
    return directory


def _resolve_signer_key_path(raw_path, resolved_directory: Path, *, label: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise TrustPolicyError(f"{label}: public_key_path must be a non-empty string")
    if not raw_path.startswith("/"):
        raise TrustPolicyError(f"{label}: public_key_path must be absolute")
    candidate = Path(raw_path)
    if ".." in candidate.parts:
        raise TrustPolicyError(f"{label}: public_key_path must not contain '..'")
    # The basename alone reuses descriptor.py's own relative-path
    # safety rule (no control characters, no funny business) -- the
    # directory containment check below is what actually enforces
    # "lives under the one configured signer directory."
    validate_relative_path(candidate.name, field=f"{label}.public_key_path basename")
    if candidate.parent != resolved_directory:
        raise TrustPolicyError(
            f"{label}: public_key_path must be a direct child of the configured signer directory "
            f"{resolved_directory}, got {candidate}"
        )
    return candidate


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


def evaluate_threshold(policy: TrustPolicy, statement: bytes, assertions,
                       *, runner=None) -> ThresholdEvaluation:
    """Verifies every provided signature assertion and evaluates the
    M-of-N threshold -- satisfied is True ONLY if the count of DISTINCT
    signer ids whose signature ACTUALLY VERIFIED (via attestation.
    verify_ed25519, the fixed OS-owned openssl verifier -- never a
    trusted-because-claimed shortcut) meets or exceeds policy.threshold.

    - An assertion naming a signer id absent from the policy is
      rejected ("unknown signer") without ever attempting to verify it
      (there is no key path to check it against).
    - A second assertion from a signer id already counted as verified
      is a no-op for the threshold -- it does not double-count, and it
      is not itself an error (the SAME signer may legitimately submit
      more than one copy of an identical signature during retries).
    - A key file that fails its filesystem trust check (missing,
      symlink, not root-owned/protected) makes that signer's assertion
      reject with a specific reason -- it is never silently skipped as
      though it simply hadn't been provided.
    - Threshold is evaluated ONLY after this full pass -- there is no
      early-exit "good enough" that would let an unverified assertion
      influence the count."""
    active_runner = runner
    known = policy.signer_by_id()
    verified: list[str] = []
    rejected: list[str] = []
    already_counted: set[str] = set()

    for assertion in assertions:
        if not isinstance(assertion, SignatureAssertion):
            rejected.append("malformed assertion (not a SignatureAssertion)")
            continue
        signer = known.get(assertion.signer_id)
        if signer is None:
            rejected.append(f"{assertion.signer_id}: unknown signer")
            continue
        if assertion.signer_id in already_counted:
            continue  # duplicate signature from an already-verified signer -- counts once
        try:
            outcome: VerificationOutcome = verify_ed25519(
                public_key_path=signer.public_key_path,
                statement=statement,
                signature=assertion.signature,
                runner=active_runner,
            )
        except Exception as exc:  # noqa: BLE001 -- a broken key file must reject, never crash evaluation
            rejected.append(f"{assertion.signer_id}: verification could not run ({exc})")
            continue
        if outcome.verified:
            verified.append(assertion.signer_id)
            already_counted.add(assertion.signer_id)
        else:
            rejected.append(f"{assertion.signer_id}: {outcome.detail}")

    satisfied = len(verified) >= policy.threshold
    return ThresholdEvaluation(
        satisfied=satisfied, threshold=policy.threshold,
        verified_signer_ids=tuple(sorted(verified)), rejected=tuple(rejected),
    )
