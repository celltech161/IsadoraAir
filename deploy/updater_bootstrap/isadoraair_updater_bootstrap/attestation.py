"""Supervisor-side signed statement + Ed25519 verification -- an
INDEPENDENT implementation of the same contract as
deploy/updater_runtime/protected_bootstrap/attestation.py (D1). Not
imported from there (Correction 1). Same fixed OS-owned verifier
boundary and the same reasoning for it -- see that module's own
docstring; restated briefly here since this copy must remain correct on
its own, not by reference."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import re
import tempfile

from .process import CommandRunner

OPENSSL_BINARY = "/usr/bin/openssl"
STATEMENT_DOMAIN = "ISADORAAIR-PROTECTED-RUNTIME-V1"
RELEASE_ID_RE = re.compile(r"^r[0-9]{4,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_GENERATION = 1_000_000
ED25519_SIGNATURE_LEN = 64


class AttestationError(ValueError):
    pass


def build_attestation_statement(*, release_id: str, previous_release_id: str | None,
                                generation: int, descriptor_sha256: str) -> bytes:
    if not isinstance(release_id, str) or not RELEASE_ID_RE.match(release_id):
        raise AttestationError("release_id does not match the required r#### pattern")
    if previous_release_id is not None:
        if not isinstance(previous_release_id, str) or not RELEASE_ID_RE.match(previous_release_id):
            raise AttestationError("previous_release_id does not match the required r#### pattern")
        if previous_release_id == release_id:
            raise AttestationError("previous_release_id cannot equal release_id")
    if not isinstance(generation, int) or isinstance(generation, bool) or not (1 <= generation <= MAX_GENERATION):
        raise AttestationError("generation must be an integer between 1 and the maximum")
    if not isinstance(descriptor_sha256, str) or not SHA256_RE.match(descriptor_sha256):
        raise AttestationError("descriptor_sha256 must be exactly 64 lowercase hex characters")
    lines = [
        STATEMENT_DOMAIN,
        f"release_id={release_id}",
        f"previous_release_id={previous_release_id or ''}",
        f"generation={generation}",
        f"descriptor_sha256={descriptor_sha256}",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


@dataclasses.dataclass(frozen=True)
class VerificationOutcome:
    verified: bool
    detail: str


def verify_ed25519(*, public_key_path: Path, statement: bytes, signature: bytes,
                   runner: CommandRunner | None = None, timeout: float = 10) -> VerificationOutcome:
    if len(signature) != ED25519_SIGNATURE_LEN:
        return VerificationOutcome(False, f"signature is {len(signature)} bytes, expected {ED25519_SIGNATURE_LEN}")
    if not Path(public_key_path).is_file() or Path(public_key_path).is_symlink():
        return VerificationOutcome(False, "public key path is missing or is a symlink")
    active_runner = runner or CommandRunner()
    with tempfile.TemporaryDirectory(prefix="isadoraair-bootstrap-attestation-") as scratch:
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
