"""Update Center Phase D, D4-O: adversarial new-managed-unit tests
NOT already covered by D1's own extensive policy/trust/attestation
suite (test_phase_d1_policy_trust_attestation.py already proves path-
traversal, wildcard, unknown-enum, missing-dot-suffix, threshold-
shortfall, wrong-release/predecessor-attestation, and tampered-
signature rejection at the primitive level) -- this file covers only
the genuinely NEW attack surface D4's own two-stage candidate-policy
mechanism introduces."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from isadoraair_updater.runtime_handoff import verify_candidate_independently
from protected_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from protected_bootstrap.policy import PolicyError, parse_policy_dict
from protected_bootstrap.trust import parse_trust_policy_dict


def _keypair(directory: Path, name: str):
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pem"
    subprocess.run([OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private_path)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    subprocess.run([OPENSSL_BINARY, "pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return private_path, public_path


def _sign(private_key_path: Path, statement: bytes) -> bytes:
    with tempfile.NamedTemporaryFile() as statement_file:
        statement_file.write(statement)
        statement_file.flush()
        result = subprocess.run(
            [OPENSSL_BINARY, "pkeyutl", "-sign", "-inkey", str(private_key_path), "-rawin", "-in", statement_file.name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout


class AdversarialUnitNameStringsRejectedTests(SimpleTestCase):
    """D4-O's own explicit adversarial strings, verified directly
    against the exact policy document parser real bundles are checked
    with -- not merely inferred from the regex."""

    def _refused(self, unit_name):
        with self.assertRaises(PolicyError):
            parse_policy_dict(
                {"schema_version": 1, "managed_units": [{"unit": unit_name, "policy": "ENABLE_NOW"}]},
                label="adversarial",
            )

    def test_path_traversal_rejected(self):
        self._refused("../evil.service")

    def test_embedded_path_separator_rejected(self):
        self._refused("evil/path.service")

    def test_bare_wildcard_rejected(self):
        self._refused("*.service")

    def test_regex_looking_string_rejected(self):
        self._refused("wx-forecast-.*")

    def test_systemd_template_unit_rejected(self):
        # '@' is not in the allowed character class at all -- a
        # template unit can never be named this way, safe or not.
        self._refused("foo@.service")

    def test_no_special_blocklist_exists_for_the_supervisors_own_units_by_name(self):
        # Documents the ACTUAL security boundary, rather than assuming
        # one that does not exist: parse_policy_dict has no name-based
        # denylist for isadoraair-updater.service/updater-
        # bootstrapd.service -- the real protection is that NEITHER
        # can ever appear in a document that reaches this parser
        # already signed by a threshold of trusted keys an attacker
        # does not hold (see AttestationThresholdIsTheOnlyGateTests
        # below). A structurally well-formed name alone is never
        # refused by shape.
        document = parse_policy_dict({
            "schema_version": 1,
            "managed_units": [{"unit": "isadoraair-updater.service", "policy": "ENABLE_NOW"}],
        }, label="self-referential")
        self.assertEqual(document.as_mapping(), {"isadoraair-updater.service": "ENABLE_NOW"})


class DescriptorMustCoverThePolicyFileTests(SimpleTestCase):
    """"descriptor not covering policy": protected-policy.json present
    on disk in a candidate bundle but NOT one of the descriptor's own
    declared file entries -- proves verify_descriptor_against_
    directory's existing "present on disk but not declared" check
    (D1) already refuses this, so resolve_candidate_policy_from_bundle
    is NEVER reached with an unverified file."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.signer_dir = self.root / "signers"
        self.signer_dir.mkdir()
        self.private_key, self.public_key = _keypair(self.signer_dir, "primary")
        self.trust_policy = parse_trust_policy_dict(
            {"schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
             "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}]},
            signer_directory=self.signer_dir,
        )
        self.bundle_root = self.root / "bundle"
        self.bundle_root.mkdir()
        self.attestations_dir = self.root / "attestations"
        self.attestations_dir.mkdir()

    def test_undeclared_policy_file_on_disk_fails_independent_verification(self):
        entry_content = b"import sys\nsys.exit(0)\n"
        (self.bundle_root / "updaterd.py").write_bytes(entry_content)
        (self.bundle_root / "updaterd.py").chmod(0o755)
        entries = (FileEntry("updaterd.py", hashlib.sha256(entry_content).hexdigest(), "0755", len(entry_content)),)
        descriptor = {
            "schema_version": 1, "generation": 2, "runtime_version": 5, "manifest_protocol_version": 5,
            "supported_wire_protocols": [3], "entrypoint": "updaterd.py",
            "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
            "bundle_sha256": compute_bundle_sha256(entries),
        }
        descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id="r0004", previous_release_id="r0003", generation=2, descriptor_sha256=descriptor_sha256,
        )
        signature = _sign(self.private_key, statement)
        (self.attestations_dir / "00.json").write_text(json.dumps({
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode(),
        }))
        # Sneak an UNDECLARED policy file onto disk -- never listed in
        # the signed descriptor's own file inventory, so its hash was
        # never pinned by anything the attestation actually covers.
        (self.bundle_root / "protected-policy.json").write_text(json.dumps({
            "schema_version": 1, "managed_units": [{"unit": "evil.service", "policy": "ENABLE_NOW"}],
        }))
        (self.bundle_root / "protected-policy.json").chmod(0o644)

        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.candidate_policy)
        self.assertTrue(any("not declared" in r for r in outcome.reasons))


class AttestationThresholdIsTheOnlyGateTests(SimpleTestCase):
    """"unsigned policy change": zero valid signatures at all (not
    merely a corrupted one) -- the empty-assertions case."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.signer_dir = self.root / "signers"
        self.signer_dir.mkdir()
        _private_key, self.public_key = _keypair(self.signer_dir, "primary")
        self.trust_policy = parse_trust_policy_dict(
            {"schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
             "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}]},
            signer_directory=self.signer_dir,
        )
        self.bundle_root = self.root / "bundle"
        self.bundle_root.mkdir()
        self.attestations_dir = self.root / "attestations"
        self.attestations_dir.mkdir()  # deliberately left EMPTY -- no signature files at all

    def test_zero_attestations_refused(self):
        entry_content = b"import sys\nsys.exit(0)\n"
        (self.bundle_root / "updaterd.py").write_bytes(entry_content)
        (self.bundle_root / "updaterd.py").chmod(0o755)
        entries = (FileEntry("updaterd.py", hashlib.sha256(entry_content).hexdigest(), "0755", len(entry_content)),)
        descriptor = {
            "schema_version": 1, "generation": 2, "runtime_version": 5, "manifest_protocol_version": 5,
            "supported_wire_protocols": [3], "entrypoint": "updaterd.py",
            "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
            "bundle_sha256": compute_bundle_sha256(entries),
        }
        descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()

        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.candidate_policy)
        self.assertTrue(any("threshold" in r for r in outcome.reasons))
