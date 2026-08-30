"""D1-D: the exact signed statement for one protected-runtime generation,
and a fixed, OS-owned Ed25519 verification primitive.

No cryptographic implementation is invented in Python here. Verification
shells out, with a fixed absolute path and a fixed literal argv (no
shell, no PATH lookup, no repository-provided verifier, no application
venv, no network), to the operating system's own OpenSSL CLI. The
algorithm is fixed by this module -- Ed25519, and only Ed25519 -- never
read from repository/manifest/descriptor metadata; nothing here lets a
release choose its own algorithm.

Investigated on this host: `openssl pkeyutl -verify -pubin -inkey
<public-key.pem> -rawin -in <statement-file> -sigfile <signature-file>`
is a clean, fixed-argv, one-shot Ed25519 verification path (OpenSSL
3.x's `pkeyutl` fully supports Ed25519's one-shot, non-prehashed
signing/verification via `-rawin`; there is no digest/prehash option to
get wrong for this algorithm). Exit code 0 means verified; any nonzero
exit means refused -- this module never inspects stderr content for a
verification DECISION, only exit status, matching the project's
existing "exit code is the contract, stderr is for operators" idiom."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import re
import tempfile

from isadoraair_updater.process import CommandRunner

# Absolute, fixed -- never resolved via PATH, never overridable by
# manifest/repository/environment content.
OPENSSL_BINARY = "/usr/bin/openssl"

STATEMENT_DOMAIN = "ISADORAAIR-PROTECTED-RUNTIME-V1"
RELEASE_ID_RE = re.compile(r"^r[0-9]{4,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_GENERATION = 1_000_000

# Ed25519 signatures are a fixed 64 bytes -- reject anything else before
# ever invoking the verifier, both as a cheap sanity check and so a
# clearly-malformed signature file never gets a chance to be
# misinterpreted by a future different verifier binary/flag combination.
ED25519_SIGNATURE_LEN = 64


class AttestationError(ValueError):
    """Raised for a malformed input to statement construction -- never
    for a verification refusal, which is a plain False return, not an
    exception (a candidate failing verification is an expected, common
    outcome for calling code to branch on, not an error condition)."""


def build_attestation_statement(*, release_id: str, previous_release_id: str | None,
                                generation: int, descriptor_sha256: str) -> bytes:
    """The EXACT deterministic byte sequence a signer signs and a
    verifier checks -- never JSON, never any serialization whose byte
    output could plausibly differ between two independent
    implementations. Binds, at minimum, the product/domain separator,
    the release id, its predecessor (empty line for the bootstrap
    release, which is unambiguous: a real release_id always matches
    r[0-9]{4,} and can never itself be empty), the runtime generation,
    and the exact descriptor digest.

    Deliberately does NOT also bind manifest_protocol_version/
    runtime_version/supported_wire_protocols -- those already live
    INSIDE the descriptor the descriptor_sha256 commits to, so
    restating them here would be pure redundancy that only creates a
    second place two implementations could disagree about
    canonicalization, for zero additional integrity."""
    if not isinstance(release_id, str) or not RELEASE_ID_RE.match(release_id):
        raise AttestationError("release_id does not match the required r#### pattern")
    if previous_release_id is not None:
        if not isinstance(previous_release_id, str) or not RELEASE_ID_RE.match(previous_release_id):
            raise AttestationError("previous_release_id does not match the required r#### pattern")
        if previous_release_id == release_id:
            raise AttestationError("previous_release_id cannot equal release_id")
    if not isinstance(generation, int) or isinstance(generation, bool) or not (1 <= generation <= MAX_GENERATION):
        raise AttestationError(f"generation must be an integer between 1 and {MAX_GENERATION}")
    if not isinstance(descriptor_sha256, str) or not SHA256_RE.match(descriptor_sha256):
        raise AttestationError("descriptor_sha256 must be exactly 64 lowercase hex characters")

    lines = [
        STATEMENT_DOMAIN,
        f"release_id={release_id}",
        f"previous_release_id={previous_release_id or ''}",
        f"generation={generation}",
        f"descriptor_sha256={descriptor_sha256}",
        "",  # trailing newline via join below, not an extra blank field
    ]
    return "\n".join(lines).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class VerificationOutcome:
    verified: bool
    detail: str


def verify_ed25519(*, public_key_path: Path, statement: bytes, signature: bytes,
                   runner: CommandRunner | None = None, timeout: float = 10) -> VerificationOutcome:
    """Verifies ONE Ed25519 signature over `statement` against the PEM
    public key at `public_key_path`, using the fixed OS-owned openssl
    binary above. This is a single-signer primitive -- D1-E's trust.py
    calls this once per configured signer and aggregates the results
    into an M-of-N threshold decision; this function itself has no
    concept of a threshold or multiple signers.

    `public_key_path` must already exist and be a plain file -- this
    function does not resolve/trust it on its own; see trust.py for the
    root-owned-directory/no-symlink checks a real signer configuration
    requires before this is ever called with an operator-configured
    path.

    Never raises for a verification failure (wrong key, tampered
    statement, garbage signature, missing key file, openssl timeout) --
    all of those come back as verified=False with a `detail` string
    safe to log. Only raises for a genuinely malformed signature length,
    checked before ever invoking the subprocess."""
    if len(signature) != ED25519_SIGNATURE_LEN:
        return VerificationOutcome(False, f"signature is {len(signature)} bytes, expected {ED25519_SIGNATURE_LEN}")
    if not Path(public_key_path).is_file() or Path(public_key_path).is_symlink():
        return VerificationOutcome(False, "public key path is missing or is a symlink")

    active_runner = runner or CommandRunner()
    with tempfile.TemporaryDirectory(prefix="isadoraair-attestation-") as scratch:
        scratch_path = Path(scratch)
        statement_path = scratch_path / "statement"
        signature_path = scratch_path / "signature"
        statement_path.write_bytes(statement)
        signature_path.write_bytes(signature)
        result = active_runner.run(
            [
                OPENSSL_BINARY, "pkeyutl", "-verify",
                "-pubin", "-inkey", str(public_key_path),
                "-rawin", "-in", str(statement_path),
                "-sigfile", str(signature_path),
            ],
            timeout=timeout,
        )
    if result.ok:
        return VerificationOutcome(True, "Ed25519 signature verified")
    if result.timed_out:
        return VerificationOutcome(False, "openssl verification timed out")
    return VerificationOutcome(False, f"openssl refused the signature (exit {result.returncode})")
