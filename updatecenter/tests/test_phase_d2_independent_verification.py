"""D2-C: the supervisor's OWN independent candidate-bundle verification
-- exercised directly (not merely via parity with D1's copy, which
test_phase_d2_parity.py covers separately). Focuses on the two checks
unique to this copy: no-symlink-anywhere-in-tree (beyond the descriptor's
own declared-path check) and bootstrap-protocol/wire compatibility
against values THIS supervisor understands."""
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from isadoraair_updater_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from isadoraair_updater_bootstrap.trust import SignatureAssertion, parse_trust_policy_dict
from isadoraair_updater_bootstrap.verification import CandidateRejected, verify_candidate_bundle


def _generate_ed25519_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pem"
    subprocess.run(
        [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    subprocess.run(
        [OPENSSL_BINARY, "pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
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


class VerifyCandidateBundleSupervisorTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.signer_dir = self.root / "signers"
        self.signer_dir.mkdir()
        self.private_key, self.public_key = _generate_ed25519_keypair(self.signer_dir, "primary")
        self.bundle_root = self.root / "bundle"
        self.bundle_root.mkdir()
        self.trust_policy = parse_trust_policy_dict(
            {
                "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
                "signers": [{"id": "primary-release", "public_key_path": str(self.public_key)}],
            },
            signer_directory=self.signer_dir,
        )

    def _write(self, relative, content: bytes, mode: int):
        from isadoraair_updater_bootstrap.descriptor import hash_file
        path = self.bundle_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        return relative, hash_file(path), len(content)

    def _descriptor_bytes(self, *, generation=1, wire=(3,)):
        import hashlib
        import json
        entry_path, entry_hash, entry_size = self._write("updaterd.py", b"print(1)\n", 0o755)
        files = [{"path": entry_path, "sha256": entry_hash, "mode": "0755", "size_bytes": entry_size}]
        entries = tuple(FileEntry(f["path"], f["sha256"], f["mode"], f["size_bytes"]) for f in files)
        descriptor = {
            "schema_version": 1, "generation": generation, "runtime_version": 5,
            "manifest_protocol_version": 5, "supported_wire_protocols": sorted(wire),
            "entrypoint": "updaterd.py", "files": files,
            "bundle_sha256": compute_bundle_sha256(entries),
        }
        return json.dumps(descriptor, sort_keys=True).encode("utf-8")

    def _verify(self, *, generation=1, wire=(3,), current_bootstrap=1, candidate_bootstrap=1, current_wire=3):
        descriptor_bytes = self._descriptor_bytes(generation=generation, wire=wire)
        import hashlib
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=generation, descriptor_sha256=descriptor_sha256,
        )
        assertions = [SignatureAssertion("primary-release", _sign(self.private_key, statement))]
        return verify_candidate_bundle(
            release_id="r0027", previous_release_id="r0026", previous_generation=None,
            descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root, trust_policy=self.trust_policy,
            assertions=assertions, current_bootstrap_protocol_version=current_bootstrap,
            current_wire_protocol_version=current_wire,
            candidate_minimum_bootstrap_protocol_version=candidate_bootstrap,
        )

    def test_happy_path_ok(self):
        result = self._verify()
        self.assertTrue(result.ok, result.reasons)

    def test_extra_symlink_anywhere_in_tree_rejected_even_if_undeclared(self):
        self._descriptor_bytes()  # writes updaterd.py
        (self.bundle_root / "sneaky-link").symlink_to(self.bundle_root / "updaterd.py")
        result = self._verify()
        self.assertFalse(result.ok)
        self.assertTrue(any("unsafe entry" in r or "symlink" in r for r in result.reasons))

    def test_special_file_in_tree_rejected(self):
        self._descriptor_bytes()
        import os
        fifo_path = self.bundle_root / "fifo"
        os.mkfifo(fifo_path)
        result = self._verify()
        self.assertFalse(result.ok)

    def test_unsupported_bootstrap_protocol_rejected(self):
        result = self._verify(candidate_bootstrap=2, current_bootstrap=1)
        self.assertFalse(result.ok)
        self.assertTrue(any("bootstrap protocol" in r for r in result.reasons))

    def test_bootstrap_protocol_exactly_equal_accepted(self):
        result = self._verify(candidate_bootstrap=1, current_bootstrap=1)
        self.assertTrue(result.ok, result.reasons)

    def test_missing_wire_compatibility_rejected(self):
        result = self._verify(wire=(4,), current_wire=3)
        self.assertFalse(result.ok)
        self.assertTrue(any("wire" in r for r in result.reasons))

    def test_wire_bridge_both_old_and_new_accepted(self):
        result = self._verify(wire=(3, 4), current_wire=3)
        self.assertTrue(result.ok, result.reasons)

    def test_invalid_candidate_bootstrap_protocol_value_raises(self):
        with self.assertRaises(CandidateRejected):
            verify_candidate_bundle(
                release_id="r0027", previous_release_id="r0026", previous_generation=None,
                descriptor_bytes=self._descriptor_bytes(), bundle_root=self.bundle_root,
                trust_policy=self.trust_policy, assertions=[], current_bootstrap_protocol_version=1,
                current_wire_protocol_version=3, candidate_minimum_bootstrap_protocol_version=0,
            )
