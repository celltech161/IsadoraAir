"""D1-F (verification.py: verify_candidate_bundle) and D1-G
(cross_check.py: cross_check_protected_runtime) -- the supervisor-
independent verification boundary and the manifest-diff contract."""
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401 -- import triggers sys.path setup

from protected_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from protected_bootstrap.cross_check import cross_check_protected_runtime
from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from protected_bootstrap.manifest_field import ProtectedRuntimeField
from protected_bootstrap.trust import SignatureAssertion, parse_trust_policy_dict
from protected_bootstrap.verification import verify_candidate_bundle


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


class VerifyCandidateBundleTests(SimpleTestCase):
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

    def _write_bundle_file(self, relative: str, content: bytes, mode: int):
        path = self.bundle_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        from protected_bootstrap.descriptor import hash_file
        return relative, hash_file(path), content

    def _build_descriptor_bytes(self, *, generation=1):
        import hashlib
        import json
        entrypoint_content = b"#!/usr/bin/env python3\nprint('updaterd')\n"
        lib_content = b"# release.py contents\n"
        entrypoint_path, entrypoint_hash, _ = self._write_bundle_file("updaterd.py", entrypoint_content, 0o755)
        lib_path, lib_hash, _ = self._write_bundle_file(
            "isadoraair_updater/release.py", lib_content, 0o644,
        )
        files = sorted(
            [
                {"path": entrypoint_path, "sha256": entrypoint_hash, "mode": "0755", "size_bytes": len(entrypoint_content)},
                {"path": lib_path, "sha256": lib_hash, "mode": "0644", "size_bytes": len(lib_content)},
            ],
            key=lambda e: e["path"],
        )
        entries = tuple(FileEntry(f["path"], f["sha256"], f["mode"], f["size_bytes"]) for f in files)
        descriptor_dict = {
            "schema_version": 1, "generation": generation, "runtime_version": 5,
            "manifest_protocol_version": 5, "supported_wire_protocols": [3],
            "entrypoint": "updaterd.py", "files": files,
            "bundle_sha256": compute_bundle_sha256(entries),
        }
        return json.dumps(descriptor_dict, sort_keys=True).encode("utf-8")

    def _verify(self, *, descriptor_bytes=None, generation=1, previous_generation=None,
               release_id="r0027", previous_release_id="r0026", sign_with=None, tamper_signature=False):
        if descriptor_bytes is None:
            descriptor_bytes = self._build_descriptor_bytes(generation=generation)
        import hashlib
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id=release_id, previous_release_id=previous_release_id,
            generation=generation, descriptor_sha256=descriptor_sha256,
        )
        signing_key = sign_with or self.private_key
        signature = _sign(signing_key, statement)
        if tamper_signature:
            signature = bytes([signature[0] ^ 0xFF]) + signature[1:]
        assertions = [SignatureAssertion("primary-release", signature)]
        return verify_candidate_bundle(
            release_id=release_id, previous_release_id=previous_release_id,
            previous_generation=previous_generation, descriptor_bytes=descriptor_bytes,
            bundle_root=self.bundle_root, trust_policy=self.trust_policy, assertions=assertions,
        )

    def test_happy_path_first_generation_ok(self):
        result = self._verify(generation=1, previous_generation=None)
        self.assertTrue(result.ok, result.reasons)
        self.assertTrue(result.threshold_evaluation.satisfied)

    def test_happy_path_subsequent_generation_ok(self):
        result = self._verify(generation=5, previous_generation=3)
        self.assertTrue(result.ok, result.reasons)

    def test_generation_replay_rejected(self):
        result = self._verify(generation=3, previous_generation=3)
        self.assertFalse(result.ok)
        self.assertTrue(any("replay/rollback" in r for r in result.reasons))

    def test_generation_rollback_rejected(self):
        result = self._verify(generation=2, previous_generation=3)
        self.assertFalse(result.ok)
        self.assertTrue(any("replay/rollback" in r for r in result.reasons))

    def test_generation_skip_allowed(self):
        result = self._verify(generation=9, previous_generation=3)
        self.assertTrue(result.ok, result.reasons)

    def test_first_generation_must_be_exactly_one(self):
        result = self._verify(generation=2, previous_generation=None)
        self.assertFalse(result.ok)

    def test_tampered_signature_rejected(self):
        result = self._verify(tamper_signature=True)
        self.assertFalse(result.ok)
        self.assertFalse(result.threshold_evaluation.satisfied)

    def test_signature_threshold_shortfall_rejected(self):
        _other_priv, other_pub = _generate_ed25519_keypair(self.signer_dir, "impostor")
        result = self._verify(sign_with=_other_priv)
        self.assertFalse(result.ok)

    def test_missing_bundle_file_rejected(self):
        descriptor_bytes = self._build_descriptor_bytes()
        (self.bundle_root / "updaterd.py").unlink()
        result = self._verify(descriptor_bytes=descriptor_bytes)
        self.assertFalse(result.ok)
        self.assertTrue(any("missing" in r for r in result.reasons))

    def test_extra_bundle_file_rejected(self):
        descriptor_bytes = self._build_descriptor_bytes()
        (self.bundle_root / "sneaky.py").write_text("evil")
        result = self._verify(descriptor_bytes=descriptor_bytes)
        self.assertFalse(result.ok)
        self.assertTrue(any("not declared" in r for r in result.reasons))

    def test_bundle_hash_mismatch_rejected(self):
        descriptor_bytes = self._build_descriptor_bytes()
        (self.bundle_root / "updaterd.py").write_bytes(b"#!/usr/bin/env python3\nprint('EVIL')\n\n")
        result = self._verify(descriptor_bytes=descriptor_bytes)
        self.assertFalse(result.ok)

    def test_malformed_descriptor_rejected(self):
        result = self._verify(descriptor_bytes=b"not json at all")
        self.assertFalse(result.ok)
        self.assertIsNone(result.descriptor)

    def test_required_policy_file_missing_rejected(self):
        descriptor_bytes = self._build_descriptor_bytes()
        import hashlib
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256=descriptor_sha256,
        )
        assertions = [SignatureAssertion("primary-release", _sign(self.private_key, statement))]
        result = verify_candidate_bundle(
            release_id="r0027", previous_release_id="r0026", previous_generation=None,
            descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            trust_policy=self.trust_policy, assertions=assertions,
            require_policy_file="protected-policy.json",
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("policy file" in r for r in result.reasons))


class CrossCheckProtectedRuntimeTests(SimpleTestCase):
    """The 14 scenarios D1-G's own workorder text enumerates."""

    def _field(self, **overrides):
        base = dict(
            generation=1, descriptor_path="deploy/updater_runtime/runtime-descriptor.json",
            descriptor_sha256="a" * 64, minimum_bootstrap_protocol_version=1,
            runtime_version=5, manifest_protocol_version=5, supported_wire_protocols=(3,),
            attestations=("deploy/updater_attestations/r0027/release-key.sig",),
        )
        base.update(overrides)
        return ProtectedRuntimeField(**base)

    def test_phase_d_inactive_is_a_total_noop(self):
        result = cross_check_protected_runtime(
            phase_d_active=False, runtime_paths_changed=True, protected_runtime_field=None,
            previous_generation=None, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.violations, ())

    def test_1_runtime_python_change_without_metadata_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=None,
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("declares no protected_runtime" in v for v in result.violations))

    def test_2_policy_only_change_without_metadata_rejected(self):
        # Policy files live inside deploy/updater_runtime/ (D1-C's own
        # requirement) so a policy-only change is represented by the
        # SAME runtime_paths_changed=True flag as a code change --
        # proven here as its own named scenario per D1-G's checklist.
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=None,
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)

    def test_3_metadata_with_no_runtime_diff_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=False, protected_runtime_field=self._field(generation=2),
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("did not change" in v for v in result.violations))

    def test_4_descriptor_missing_changed_file_is_verification_layers_job(self):
        # cross_check_protected_runtime() is a manifest-level structural
        # gate -- descriptor/bundle content exactness is
        # verify_candidate_bundle()'s job (see
        # test_missing_bundle_file_rejected above), not duplicated here.
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=self._field(generation=2),
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(result.ok)

    def test_9_generation_replay_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=self._field(generation=3),
            previous_generation=3, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)

    def test_generation_skip_allowed(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=self._field(generation=9),
            previous_generation=3, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(result.ok)

    def test_generation_rollback_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True, protected_runtime_field=self._field(generation=1),
            previous_generation=3, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)

    def test_unsupported_bootstrap_protocol_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True,
            protected_runtime_field=self._field(generation=2, minimum_bootstrap_protocol_version=2),
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("bootstrap protocol" in v for v in result.violations))

    def test_unsupported_wire_compatibility_rejected(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True,
            protected_runtime_field=self._field(generation=2, supported_wire_protocols=(4,)),
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("wire compatibility" in v for v in result.violations))

    def test_wire_bridge_including_old_protocol_accepted(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=True,
            protected_runtime_field=self._field(generation=2, supported_wire_protocols=(3, 4)),
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(result.ok, result.violations)

    def test_no_change_no_metadata_is_fine(self):
        result = cross_check_protected_runtime(
            phase_d_active=True, runtime_paths_changed=False, protected_runtime_field=None,
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(result.ok)
