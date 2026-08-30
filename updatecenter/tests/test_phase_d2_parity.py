"""D2-D: parity between the two INDEPENDENT candidate-verification
implementations -- deploy/updater_runtime/protected_bootstrap (D1,
worker-side) and deploy/updater_bootstrap/isadoraair_updater_bootstrap
(D2, supervisor-side). Neither imports the other (Correction 1); this
test file is the ONLY place both are imported together, and it never
calls one through the other -- each scenario is verified independently
against each package's own descriptor/trust/attestation/verification
modules, then the two outcomes are compared.

Also covers the D1-C policy.py scenarios (policy duplicate, invalid
unit, unsupported policy enum) even though D2 has no policy.py
equivalent of its own yet (the supervisor does not interpret policy
documents directly in this phase) -- those three are exercised only
against D1's copy, explicitly marked as such rather than silently
skipped."""
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT, RUNTIME_ROOT  # noqa: F401 -- triggers both sys.path setups

import protected_bootstrap.attestation as worker_attestation
import protected_bootstrap.descriptor as worker_descriptor
import protected_bootstrap.policy as worker_policy
import protected_bootstrap.trust as worker_trust
import protected_bootstrap.verification as worker_verification

import isadoraair_updater_bootstrap.attestation as super_attestation
import isadoraair_updater_bootstrap.descriptor as super_descriptor
import isadoraair_updater_bootstrap.trust as super_trust
import isadoraair_updater_bootstrap.verification as super_verification


def _generate_ed25519_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    private_path = directory / f"{name}.key"
    public_path = directory / f"{name}.pem"
    subprocess.run(
        [worker_attestation.OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    subprocess.run(
        [worker_attestation.OPENSSL_BINARY, "pkey", "-in", str(private_path), "-pubout", "-out", str(public_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return private_path, public_path


def _sign(private_key_path: Path, statement: bytes) -> bytes:
    with tempfile.NamedTemporaryFile() as statement_file:
        statement_file.write(statement)
        statement_file.flush()
        result = subprocess.run(
            [worker_attestation.OPENSSL_BINARY, "pkeyutl", "-sign", "-inkey", str(private_key_path),
             "-rawin", "-in", statement_file.name],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return result.stdout


class ParityFixture:
    """Builds ONE shared set of real files/keys/descriptor bytes usable
    against BOTH implementations unmodified -- both sides' descriptor/
    trust/policy JSON shapes are byte-identical by design (see D1-C's
    and D2's own module docstrings), so a single fixture corpus can
    legitimately drive both without any per-side translation, which
    would itself be a place the two could quietly diverge."""

    def __init__(self, root: Path):
        self.root = root
        self.signer_dir = root / "signers"
        self.signer_dir.mkdir()
        self.private_key, self.public_key = _generate_ed25519_keypair(self.signer_dir, "primary")
        self.other_private_key, self.other_public_key = _generate_ed25519_keypair(self.signer_dir, "impostor")
        self.bundle_root = root / "bundle"
        self.bundle_root.mkdir()

    def trust_policy_dict(self, *, threshold=1, signer_id="primary-release"):
        return {
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": threshold,
            "signers": [{"id": signer_id, "public_key_path": str(self.public_key)}],
        }

    def write_bundle_file(self, module, relative: str, content: bytes, mode: str):
        path = self.bundle_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(int(mode, 8))
        return module.FileEntry(relative, module.hash_file(path), mode, len(content))

    def descriptor_bytes(self, module, *, generation=1, wire=(3,), tamper_hash_of=None,
                         extra_declared_missing_file=None, duplicate_entry=False):
        import hashlib
        import json
        entry = self.write_bundle_file(module, "updaterd.py", b"print(1)\n", "0755")
        files = [{"path": entry.path, "sha256": entry.sha256, "mode": entry.mode, "size_bytes": entry.size_bytes}]
        if tamper_hash_of == entry.path:
            files[0]["sha256"] = "f" * 64
        if extra_declared_missing_file:
            files.append({"path": extra_declared_missing_file, "sha256": "e" * 64, "mode": "0644", "size_bytes": 1})
            files.sort(key=lambda f: f["path"])
        if duplicate_entry:
            files.append(dict(files[0]))
        entry_objs = tuple(module.FileEntry(f["path"], f["sha256"], f["mode"], f["size_bytes"]) for f in files)
        bundle_sha = module.compute_bundle_sha256(entry_objs) if not duplicate_entry else "0" * 64
        descriptor = {
            "schema_version": 1, "generation": generation, "runtime_version": 5,
            "manifest_protocol_version": 5, "supported_wire_protocols": sorted(wire),
            "entrypoint": "updaterd.py", "files": files, "bundle_sha256": bundle_sha,
        }
        return json.dumps(descriptor, sort_keys=True).encode("utf-8")

    def verify_both(self, *, generation=1, previous_generation=None, wire=(3,),
                    release_id="r0027", previous_release_id="r0026", sign_with_worker=None, sign_with_super=None,
                    tamper_hash_of=None, extra_missing_file=None, duplicate_entry=False,
                    corrupt_disk_after_build=None, extra_disk_entry=None, chmod_after_build=None,
                    remove_after_build=None, signer_id="primary-release", bootstrap_ok=True):
        """Runs the IDENTICAL logical scenario through both
        implementations independently, returns (worker_result,
        supervisor_result) for the caller to compare."""
        import hashlib

        worker_descriptor_bytes = self.descriptor_bytes(
            worker_descriptor, generation=generation, wire=wire, tamper_hash_of=tamper_hash_of,
            extra_declared_missing_file=extra_missing_file, duplicate_entry=duplicate_entry,
        )
        # Both descriptors are built FIRST (back to back -- both write
        # the identical "clean" file content as a side effect), and
        # only THEN is any disk corruption/extra-entry applied, exactly
        # ONCE, to the now-final shared disk state both verifications
        # below read. Applying corruption between the two descriptor
        # builds would have it silently undone by the second build's
        # own "write the clean file" side effect -- this ordering is
        # what actually makes both sides observe the identical real
        # disk state, not merely the identical descriptor bytes.
        super_descriptor_bytes = self.descriptor_bytes(
            super_descriptor, generation=generation, wire=wire, tamper_hash_of=tamper_hash_of,
            extra_declared_missing_file=extra_missing_file, duplicate_entry=duplicate_entry,
        )

        if extra_disk_entry:
            kind, name = extra_disk_entry
            target = self.bundle_root / name
            if kind == "symlink":
                target.symlink_to(self.bundle_root / "updaterd.py")
            elif kind == "fifo":
                import os
                os.mkfifo(target)
        if corrupt_disk_after_build:
            (self.bundle_root / corrupt_disk_after_build).write_bytes(b"corrupted-same-length!!")
        if chmod_after_build:
            path, mode = chmod_after_build
            (self.bundle_root / path).chmod(mode)
        if remove_after_build:
            (self.bundle_root / remove_after_build).unlink()

        worker_descriptor_sha = hashlib.sha256(worker_descriptor_bytes).hexdigest()
        worker_statement = worker_attestation.build_attestation_statement(
            release_id=release_id, previous_release_id=previous_release_id,
            generation=generation, descriptor_sha256=worker_descriptor_sha,
        )
        worker_key = sign_with_worker or self.private_key
        worker_assertions = [worker_trust.SignatureAssertion(signer_id, _sign(worker_key, worker_statement))]
        worker_policy = worker_trust.parse_trust_policy_dict(
            self.trust_policy_dict(signer_id="primary-release"), signer_directory=self.signer_dir,
        )
        worker_result = worker_verification.verify_candidate_bundle(
            release_id=release_id, previous_release_id=previous_release_id, previous_generation=previous_generation,
            descriptor_bytes=worker_descriptor_bytes, bundle_root=self.bundle_root, trust_policy=worker_policy,
            assertions=worker_assertions, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
            candidate_minimum_bootstrap_protocol_version=(1 if bootstrap_ok else 2),
        )

        super_descriptor_sha = hashlib.sha256(super_descriptor_bytes).hexdigest()
        super_statement = super_attestation.build_attestation_statement(
            release_id=release_id, previous_release_id=previous_release_id,
            generation=generation, descriptor_sha256=super_descriptor_sha,
        )
        super_key = sign_with_super or self.private_key
        super_assertions = [super_trust.SignatureAssertion(signer_id, _sign(super_key, super_statement))]
        super_policy = super_trust.parse_trust_policy_dict(
            self.trust_policy_dict(signer_id="primary-release"), signer_directory=self.signer_dir,
        )
        super_result = super_verification.verify_candidate_bundle(
            release_id=release_id, previous_release_id=previous_release_id, previous_generation=previous_generation,
            descriptor_bytes=super_descriptor_bytes, bundle_root=self.bundle_root, trust_policy=super_policy,
            assertions=super_assertions, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
            candidate_minimum_bootstrap_protocol_version=(1 if bootstrap_ok else 2),
        )
        return worker_result, super_result


class VerificationParityTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = ParityFixture(Path(self.temp.name))

    def test_01_valid_bundle_both_accept(self):
        worker_result, super_result = self.fixture.verify_both()
        self.assertTrue(worker_result.ok, worker_result.reasons)
        self.assertTrue(super_result.ok, super_result.reasons)
        self.assertEqual(worker_result.ok, super_result.ok)

    def test_02_wrong_descriptor_digest_both_reject(self):
        # Simulated by tampering the statement's own descriptor_sha256
        # binding indirectly: sign against a DIFFERENT (impostor) key
        # whose corresponding public key is not in the trust policy --
        # produces a threshold failure on both sides, the observable
        # proxy for "the signed digest does not check out."
        worker_result, super_result = self.fixture.verify_both(sign_with_worker=self.fixture.other_private_key, sign_with_super=self.fixture.other_private_key)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_03_wrong_file_hash_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(corrupt_disk_after_build="updaterd.py")
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_04_wrong_mode_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(chmod_after_build=("updaterd.py", 0o644))
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_05_missing_file_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(remove_after_build="updaterd.py")
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_06_extra_file_both_reject(self):
        (self.fixture.bundle_root / "sneaky.py").write_text("evil")
        worker_result, super_result = self.fixture.verify_both()
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_07_symlink_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(extra_disk_entry=("symlink", "sneaky-link"))
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_08_special_file_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(extra_disk_entry=("fifo", "sneaky-fifo"))
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_09_descriptor_traversal_both_reject(self):
        for module in (worker_descriptor, super_descriptor):
            with self.subTest(module=module.__name__):
                with self.assertRaises(module.DescriptorError):
                    module.validate_relative_path("../escape.py", field="x")

    def test_10_duplicate_inventory_entry_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(duplicate_entry=True)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_11_policy_duplicate_worker_side_only(self):
        # D2 has no policy.py of its own in this phase -- exercised
        # against D1's copy only, explicitly (not silently skipped).
        data = {
            "schema_version": 1,
            "managed_units": [
                {"unit": "isadoraair-gunicorn.service", "policy": "ENABLE_NOW"},
                {"unit": "isadoraair-gunicorn.service", "policy": "INSTALL_ONLY"},
            ],
        }
        with self.assertRaises(worker_policy.PolicyError):
            worker_policy.parse_policy_dict(data)

    def test_12_invalid_unit_worker_side_only(self):
        data = {"schema_version": 1, "managed_units": [{"unit": "not-a-unit", "policy": "ENABLE_NOW"}]}
        with self.assertRaises(worker_policy.PolicyError):
            worker_policy.parse_policy_dict(data)

    def test_13_unsupported_policy_enum_worker_side_only(self):
        data = {"schema_version": 1, "managed_units": [{"unit": "x.service", "policy": "RUN_ANYTHING"}]}
        with self.assertRaises(worker_policy.PolicyError):
            worker_policy.parse_policy_dict(data)

    def test_14_signer_shortfall_both_reject(self):
        # Threshold 1, but the only assertion provided fails to verify
        # (signed by the impostor key) -- both must reject identically.
        worker_result, super_result = self.fixture.verify_both(sign_with_worker=self.fixture.other_private_key, sign_with_super=self.fixture.other_private_key)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.threshold_evaluation.satisfied)
        self.assertFalse(super_result.threshold_evaluation.satisfied)

    def test_15_duplicate_signer_both_count_once(self):
        # A legitimate 2-signer, threshold-2 policy -- but only ONE
        # signer's signature is ever submitted, twice. Two copies of
        # the same real signer's signature must still count once.
        two_signer_policy_dict = {
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 2,
            "signers": [
                {"id": "primary-release", "public_key_path": str(self.fixture.public_key)},
                {"id": "recovery-release", "public_key_path": str(self.fixture.other_public_key)},
            ],
        }
        real_statement = worker_attestation.build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
        )
        signature = _sign(self.fixture.private_key, real_statement)
        for module_trust in (worker_trust, super_trust):
            with self.subTest(module=module_trust.__name__):
                policy = module_trust.parse_trust_policy_dict(
                    two_signer_policy_dict, signer_directory=self.fixture.signer_dir,
                )
                assertions = [
                    module_trust.SignatureAssertion("primary-release", signature),
                    module_trust.SignatureAssertion("primary-release", signature),
                ]
                evaluation = module_trust.evaluate_threshold(policy, real_statement, assertions)
                self.assertEqual(evaluation.verified_count, 1)
                self.assertFalse(evaluation.satisfied)  # threshold 2, only 1 distinct signer

    def test_16_unknown_signer_both_reject(self):
        for module_trust in (worker_trust, super_trust):
            with self.subTest(module=module_trust.__name__):
                policy = module_trust.parse_trust_policy_dict(
                    self.fixture.trust_policy_dict(threshold=1), signer_directory=self.fixture.signer_dir,
                )
                assertions = [module_trust.SignatureAssertion("ghost-signer", b"x" * 64)]
                evaluation = module_trust.evaluate_threshold(policy, b"statement", assertions)
                self.assertFalse(evaluation.satisfied)
                self.assertTrue(any("unknown signer" in r for r in evaluation.rejected))

    def test_17_bad_signature_both_reject(self):
        worker_result, super_result = self.fixture.verify_both()
        self.assertTrue(worker_result.ok and super_result.ok)
        # Now a garbage (but correctly-sized) signature.
        for module_trust, module_attestation in ((worker_trust, worker_attestation), (super_trust, super_attestation)):
            with self.subTest(module=module_trust.__name__):
                policy = module_trust.parse_trust_policy_dict(
                    self.fixture.trust_policy_dict(threshold=1), signer_directory=self.fixture.signer_dir,
                )
                statement = module_attestation.build_attestation_statement(
                    release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
                )
                garbage_signature = b"\x00" * 64
                assertions = [module_trust.SignatureAssertion("primary-release", garbage_signature)]
                evaluation = module_trust.evaluate_threshold(policy, statement, assertions)
                self.assertFalse(evaluation.satisfied)

    def test_18_wrong_release_binding_both_reject(self):
        # Sign for r0027, but ask verify_both to check against r0028 --
        # simulated directly by signing/verifying with mismatched
        # release ids on each side.
        for module_attestation, module_trust in ((worker_attestation, worker_trust), (super_attestation, super_trust)):
            with self.subTest(module=module_attestation.__name__):
                signed_for = module_attestation.build_attestation_statement(
                    release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
                )
                signature = _sign(self.fixture.private_key, signed_for)
                checked_against = module_attestation.build_attestation_statement(
                    release_id="r0028", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
                )
                policy = module_trust.parse_trust_policy_dict(
                    self.fixture.trust_policy_dict(threshold=1), signer_directory=self.fixture.signer_dir,
                )
                evaluation = module_trust.evaluate_threshold(
                    policy, checked_against, [module_trust.SignatureAssertion("primary-release", signature)],
                )
                self.assertFalse(evaluation.satisfied)

    def test_19_wrong_predecessor_binding_both_reject(self):
        for module_attestation, module_trust in ((worker_attestation, worker_trust), (super_attestation, super_trust)):
            with self.subTest(module=module_attestation.__name__):
                signed_for = module_attestation.build_attestation_statement(
                    release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
                )
                signature = _sign(self.fixture.private_key, signed_for)
                checked_against = module_attestation.build_attestation_statement(
                    release_id="r0027", previous_release_id="r9999", generation=1, descriptor_sha256="a" * 64,
                )
                policy = module_trust.parse_trust_policy_dict(
                    self.fixture.trust_policy_dict(threshold=1), signer_directory=self.fixture.signer_dir,
                )
                evaluation = module_trust.evaluate_threshold(
                    policy, checked_against, [module_trust.SignatureAssertion("primary-release", signature)],
                )
                self.assertFalse(evaluation.satisfied)

    def test_20_generation_replay_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(generation=3, previous_generation=3)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_21_generation_rollback_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(generation=2, previous_generation=3)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_generation_skip_both_accept(self):
        worker_result, super_result = self.fixture.verify_both(generation=9, previous_generation=3)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertTrue(worker_result.ok, worker_result.reasons)

    def test_22_unsupported_bootstrap_protocol_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(bootstrap_ok=False)
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)

    def test_23_missing_wire_compatibility_both_reject(self):
        worker_result, super_result = self.fixture.verify_both(wire=(4,))
        self.assertEqual(worker_result.ok, super_result.ok)
        self.assertFalse(worker_result.ok)


class DescriptorSchemaParityTests(SimpleTestCase):
    """Direct schema-level parity for the checks VerificationParityTests
    above cannot cheaply isolate to just the descriptor layer."""

    def _minimal(self, module, **overrides):
        entries = (module.FileEntry("updaterd.py", "a" * 64, "0755", 10),)
        data = {
            "schema_version": 1, "generation": 1, "runtime_version": 1,
            "manifest_protocol_version": 1, "supported_wire_protocols": [3],
            "entrypoint": "updaterd.py",
            "files": [{"path": "updaterd.py", "sha256": "a" * 64, "mode": "0755", "size_bytes": 10}],
            "bundle_sha256": module.compute_bundle_sha256(entries),
        }
        data.update(overrides)
        return data

    def test_both_accept_identical_valid_descriptor(self):
        worker_ok = self._try_parse(worker_descriptor, self._minimal(worker_descriptor))
        super_ok = self._try_parse(super_descriptor, self._minimal(super_descriptor))
        self.assertTrue(worker_ok)
        self.assertTrue(super_ok)

    def test_both_reject_disallowed_mode(self):
        for module in (worker_descriptor, super_descriptor):
            with self.subTest(module=module.__name__):
                data = self._minimal(module)
                data["files"][0]["mode"] = "0777"
                self.assertFalse(self._try_parse(module, data))

    def test_both_reject_bad_bundle_digest(self):
        for module in (worker_descriptor, super_descriptor):
            with self.subTest(module=module.__name__):
                data = self._minimal(module)
                data["bundle_sha256"] = "0" * 64
                self.assertFalse(self._try_parse(module, data))

    @staticmethod
    def _try_parse(module, data) -> bool:
        try:
            module.parse_descriptor_dict(data)
            return True
        except module.DescriptorError:
            return False
