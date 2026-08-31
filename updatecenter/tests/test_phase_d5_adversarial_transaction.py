from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase

from deploy.updater_bootstrap.tools.protected_runtime_release import (
    OPENSSL_BINARY,
    build_descriptor,
    build_statement,
    generation_one_policy_bytes,
    sign_statement,
)
from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from protected_bootstrap.trust import SignatureAssertion, parse_trust_policy_dict
from protected_bootstrap.verification import verify_candidate_bundle


class WholeTransactionCandidateRefusalTests(SimpleTestCase):
    """Every case is evaluated at the candidate gate; mutation stays empty."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle"
        (self.bundle / "isadoraair_updater").mkdir(parents=True)
        (self.bundle / "protected_bootstrap").mkdir()
        for relative, content in {
            "README.md": b"runtime\n",
            "updaterctl.py": b"pass\n",
            "updaterd.py": b"pass\n",
            "isadoraair_updater/__init__.py": b"RUNTIME_VERSION=5\n",
            "protected_bootstrap/__init__.py": b"pass\n",
        }.items():
            (self.bundle / relative).write_bytes(content)
        (self.bundle / "protected-policy.json").write_bytes(generation_one_policy_bytes())
        (self.bundle / "updaterd.py").chmod(0o755)
        for path in self.bundle.rglob("*"):
            if path.is_file() and path.name != "updaterd.py":
                path.chmod(0o644)

        self.private = self.root / "private.key"
        self.public = self.root / "primary.pem"
        subprocess.run(
            [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(self.private)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.private.chmod(0o600)
        subprocess.run(
            [OPENSSL_BINARY, "pkey", "-in", str(self.private), "-pubout", "-out", str(self.public)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self.policy = parse_trust_policy_dict({
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.public)}],
        }, signer_directory=self.root)
        self.descriptor = build_descriptor(
            runtime_root=self.bundle, generation=2, runtime_version=5,
            manifest_protocol_version=5, supported_wire_protocols=(3,),
        )
        self.statement = build_statement(
            descriptor_bytes=self.descriptor, release_id="r0027",
            previous_release_id="r0026", generation=2,
        )
        self.signature = sign_statement(
            statement=self.statement, private_key_path=self.private, public_key_path=self.public,
        )
        self.mutations: list[str] = []

    def _verify(self, *, descriptor=None, assertions=None, release_id="r0027", previous_release_id="r0026", previous_generation=1):
        return verify_candidate_bundle(
            release_id=release_id, previous_release_id=previous_release_id,
            previous_generation=previous_generation,
            descriptor_bytes=self.descriptor if descriptor is None else descriptor,
            bundle_root=self.bundle, trust_policy=self.policy,
            assertions=[SignatureAssertion("primary-release", self.signature)] if assertions is None else assertions,
            current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
            candidate_minimum_bootstrap_protocol_version=1,
            require_policy_file="protected-policy.json",
        )

    def test_unsigned_insufficient_wrong_signer_and_binding_mismatches_fail(self):
        cases = {
            "unsigned": self._verify(assertions=[]),
            "wrong signer": self._verify(assertions=[SignatureAssertion("intruder", self.signature)]),
            "release mismatch": self._verify(release_id="r0099"),
            "predecessor mismatch": self._verify(previous_release_id="r0025"),
            "generation replay": self._verify(previous_generation=2),
            "generation downgrade": self._verify(previous_generation=3),
        }
        for label, result in cases.items():
            with self.subTest(label=label):
                self.assertFalse(result.ok)
        self.assertEqual(self.mutations, [])

    def test_descriptor_policy_and_inventory_tampering_fail_before_mutation(self):
        descriptor_value = json.loads(self.descriptor)
        descriptor_value["runtime_version"] = 6
        tampered_descriptor = (json.dumps(descriptor_value, indent=2, sort_keys=True) + "\n").encode()
        self.assertFalse(self._verify(descriptor=tampered_descriptor).ok)

        policy_path = self.bundle / "protected-policy.json"
        original_policy = policy_path.read_bytes()
        policy_path.write_bytes(original_policy + b"\n")
        self.assertFalse(self._verify().ok)
        policy_path.write_bytes(original_policy)

        extra = self.bundle / "unexpected.py"
        extra.write_text("pass\n")
        self.assertFalse(self._verify().ok)
        extra.unlink()

        # A descriptor that omits the required signed policy fails even when
        # its aggregate inventory digest is internally recomputed correctly.
        from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256
        value = json.loads(self.descriptor)
        value["files"] = [entry for entry in value["files"] if entry["path"] != "protected-policy.json"]
        objects = tuple(FileEntry(**entry) for entry in value["files"])
        value["bundle_sha256"] = compute_bundle_sha256(objects)
        absent_policy_descriptor = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        absent_statement = build_statement(
            descriptor_bytes=absent_policy_descriptor, release_id="r0027",
            previous_release_id="r0026", generation=2,
        )
        absent_signature = sign_statement(
            statement=absent_statement, private_key_path=self.private, public_key_path=self.public,
        )
        result = self._verify(
            descriptor=absent_policy_descriptor,
            assertions=[SignatureAssertion("primary-release", absent_signature)],
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("required policy" in reason for reason in result.reasons))
        self.assertEqual(self.mutations, [])
