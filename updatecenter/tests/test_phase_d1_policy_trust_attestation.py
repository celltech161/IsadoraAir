"""D1-C (policy.py), D1-D (attestation.py), D1-E (trust.py). Uses only
dedicated test-generated Ed25519 keys -- never a production key, never
committed anywhere."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401 -- import triggers sys.path setup

from isadoraair_updater.process import CommandRunner
from isadoraair_updater.release import UnitActivationPolicy

from protected_bootstrap.attestation import (
    AttestationError, OPENSSL_BINARY, STATEMENT_DOMAIN, build_attestation_statement, verify_ed25519,
)
from protected_bootstrap.policy import ALLOWED_POLICIES, PolicyError, parse_policy_dict
from protected_bootstrap.trust import (
    SignatureAssertion, TrustPolicyError, evaluate_threshold, parse_trust_policy_dict,
)


def _generate_ed25519_keypair(directory: Path, name: str) -> tuple[Path, Path]:
    """A dedicated, throwaway test keypair -- generated fresh per test
    run, never a fixture committed to the repository, and never any
    production key."""
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


class PolicyEnumMirrorTests(SimpleTestCase):
    def test_allowed_policies_matches_unit_activation_policy_exactly(self):
        # This package deliberately does NOT import isadoraair_updater.
        # release.UnitActivationPolicy (see policy.py's own docstring) --
        # this test is the one place that pairing is cross-checked, so
        # the two enums can never silently diverge without a failing test.
        self.assertEqual(ALLOWED_POLICIES, {member.name for member in UnitActivationPolicy})


class ParsePolicyDictTests(SimpleTestCase):
    def _valid(self, **overrides):
        data = {
            "schema_version": 1,
            "managed_units": [
                {"unit": "isadoraair-gunicorn.service", "policy": "ENABLE_NOW"},
                {"unit": "wx-forecast-1day-day.service", "policy": "INSTALL_ONLY"},
            ],
        }
        data.update(overrides)
        return data

    def test_valid_document_parses(self):
        document = parse_policy_dict(self._valid())
        self.assertEqual(len(document.entries), 2)
        self.assertEqual(
            document.as_mapping(),
            {"isadoraair-gunicorn.service": "ENABLE_NOW", "wx-forecast-1day-day.service": "INSTALL_ONLY"},
        )

    def test_unknown_top_field_rejected(self):
        data = self._valid()
        data["extra"] = 1
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_empty_managed_units_rejected(self):
        with self.assertRaises(PolicyError):
            parse_policy_dict(self._valid(managed_units=[]))

    def test_unsorted_units_rejected(self):
        data = self._valid()
        data["managed_units"] = list(reversed(data["managed_units"]))
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_duplicate_unit_rejected(self):
        data = self._valid()
        data["managed_units"].append({"unit": "isadoraair-gunicorn.service", "policy": "INSTALL_ONLY"})
        data["managed_units"].sort(key=lambda e: e["unit"])
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_unknown_policy_value_rejected(self):
        data = self._valid()
        data["managed_units"][0]["policy"] = "RUN_ARBITRARY_COMMAND"
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_unit_with_path_separator_rejected(self):
        data = self._valid()
        data["managed_units"][0]["unit"] = "../etc/evil.service"
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_unit_with_wildcard_rejected(self):
        data = self._valid()
        data["managed_units"][0]["unit"] = "wx-*.service"
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_unit_missing_dot_suffix_rejected(self):
        data = self._valid()
        data["managed_units"][0]["unit"] = "isadoraair-gunicorn"
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)

    def test_too_many_entries_rejected(self):
        data = self._valid()
        data["managed_units"] = [
            {"unit": f"unit-{i:04d}.service", "policy": "ENABLE_NOW"} for i in range(200)
        ]
        with self.assertRaises(PolicyError):
            parse_policy_dict(data)


class BuildAttestationStatementTests(SimpleTestCase):
    def test_deterministic_for_same_inputs(self):
        a = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
        )
        b = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
        )
        self.assertEqual(a, b)

    def test_domain_separator_present(self):
        statement = build_attestation_statement(
            release_id="r0027", previous_release_id=None, generation=1, descriptor_sha256="a" * 64,
        )
        self.assertTrue(statement.startswith(STATEMENT_DOMAIN.encode("utf-8")))

    def test_different_release_id_different_statement(self):
        a = build_attestation_statement(release_id="r0027", previous_release_id=None, generation=1, descriptor_sha256="a" * 64)
        b = build_attestation_statement(release_id="r0028", previous_release_id=None, generation=1, descriptor_sha256="a" * 64)
        self.assertNotEqual(a, b)

    def test_none_previous_release_id_is_unambiguous(self):
        bootstrap = build_attestation_statement(release_id="r0027", previous_release_id=None, generation=1, descriptor_sha256="a" * 64)
        self.assertIn(b"previous_release_id=\n", bootstrap)

    def test_invalid_release_id_rejected(self):
        with self.assertRaises(AttestationError):
            build_attestation_statement(release_id="not-a-release", previous_release_id=None, generation=1, descriptor_sha256="a" * 64)

    def test_bad_descriptor_sha_rejected(self):
        with self.assertRaises(AttestationError):
            build_attestation_statement(release_id="r0027", previous_release_id=None, generation=1, descriptor_sha256="short")

    def test_zero_generation_rejected(self):
        with self.assertRaises(AttestationError):
            build_attestation_statement(release_id="r0027", previous_release_id=None, generation=0, descriptor_sha256="a" * 64)

    def test_previous_equals_release_rejected(self):
        with self.assertRaises(AttestationError):
            build_attestation_statement(release_id="r0027", previous_release_id="r0027", generation=1, descriptor_sha256="a" * 64)


class VerifyEd25519Tests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private_key, self.public_key = _generate_ed25519_keypair(self.root, "primary")
        self.statement = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
        )
        self.signature = _sign(self.private_key, self.statement)

    def test_valid_signature_verifies(self):
        outcome = verify_ed25519(public_key_path=self.public_key, statement=self.statement, signature=self.signature)
        self.assertTrue(outcome.verified)

    def test_tampered_statement_fails(self):
        tampered = self.statement.replace(b"r0027", b"r0028")
        outcome = verify_ed25519(public_key_path=self.public_key, statement=tampered, signature=self.signature)
        self.assertFalse(outcome.verified)

    def test_wrong_key_fails(self):
        _other_priv, other_pub = _generate_ed25519_keypair(self.root, "other")
        outcome = verify_ed25519(public_key_path=other_pub, statement=self.statement, signature=self.signature)
        self.assertFalse(outcome.verified)

    def test_malformed_signature_length_rejected_without_subprocess(self):
        outcome = verify_ed25519(public_key_path=self.public_key, statement=self.statement, signature=b"too-short")
        self.assertFalse(outcome.verified)

    def test_missing_key_file_fails_safely(self):
        outcome = verify_ed25519(
            public_key_path=self.root / "does-not-exist.pem", statement=self.statement, signature=self.signature,
        )
        self.assertFalse(outcome.verified)

    def test_symlinked_key_file_refused(self):
        symlink_path = self.root / "symlinked.pem"
        symlink_path.symlink_to(self.public_key)
        outcome = verify_ed25519(public_key_path=symlink_path, statement=self.statement, signature=self.signature)
        self.assertFalse(outcome.verified)

    def test_uses_command_runner_fixed_argv(self):
        runner = CommandRunner()
        outcome = verify_ed25519(
            public_key_path=self.public_key, statement=self.statement, signature=self.signature, runner=runner,
        )
        self.assertTrue(outcome.verified)


class ParseTrustPolicyDictTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer_dir = Path(self.temp.name) / "signers"
        self.signer_dir.mkdir()
        _priv, self.pub = _generate_ed25519_keypair(self.signer_dir, "primary")

    def _valid(self, **overrides):
        data = {
            "schema_version": 1,
            "signature_algorithm": "ed25519",
            "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(self.pub)}],
        }
        data.update(overrides)
        return data

    def test_valid_policy_parses(self):
        policy = parse_trust_policy_dict(self._valid(), signer_directory=self.signer_dir)
        self.assertEqual(policy.threshold, 1)
        self.assertEqual(len(policy.signers), 1)

    def test_wrong_algorithm_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(self._valid(signature_algorithm="rsa"), signer_directory=self.signer_dir)

    def test_threshold_zero_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(self._valid(threshold=0), signer_directory=self.signer_dir)

    def test_threshold_exceeding_signer_count_rejected(self):
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(self._valid(threshold=2), signer_directory=self.signer_dir)

    def test_duplicate_signer_id_rejected(self):
        data = self._valid()
        data["signers"].append({"id": "primary-release", "public_key_path": str(self.pub)})
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_key_path_outside_signer_directory_rejected(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = "/etc/passwd"
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_key_path_with_dotdot_rejected(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = str(self.signer_dir / ".." / "escape.pem")
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_relative_key_path_rejected(self):
        data = self._valid()
        data["signers"][0]["public_key_path"] = "relative.pem"
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_too_many_signers_rejected(self):
        data = self._valid()
        data["signers"] = [
            {"id": f"signer-{i}", "public_key_path": str(self.pub)} for i in range(20)
        ]
        data["threshold"] = 1
        with self.assertRaises(TrustPolicyError):
            parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_configurable_m_of_n_not_hardcoded_2_of_2(self):
        # Explicit proof this is NOT a hardcoded 2-of-2: 1-of-3 and
        # 3-of-3 both parse cleanly from the identical signer set.
        three_signers = self._valid(threshold=1)
        three_signers["signers"] = [
            {"id": f"signer-{i}", "public_key_path": str(self.pub)} for i in range(3)
        ]
        for threshold in (1, 2, 3):
            with self.subTest(threshold=threshold):
                policy = parse_trust_policy_dict(
                    {**three_signers, "threshold": threshold}, signer_directory=self.signer_dir,
                )
                self.assertEqual(policy.threshold, threshold)


class EvaluateThresholdTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer_dir = Path(self.temp.name) / "signers"
        self.signer_dir.mkdir()
        self.priv_a, self.pub_a = _generate_ed25519_keypair(self.signer_dir, "a")
        self.priv_b, self.pub_b = _generate_ed25519_keypair(self.signer_dir, "b")
        self.statement = build_attestation_statement(
            release_id="r0027", previous_release_id="r0026", generation=1, descriptor_sha256="a" * 64,
        )

    def _policy(self, threshold):
        data = {
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": threshold,
            "signers": [
                {"id": "signer-a", "public_key_path": str(self.pub_a)},
                {"id": "signer-b", "public_key_path": str(self.pub_b)},
            ],
        }
        return parse_trust_policy_dict(data, signer_directory=self.signer_dir)

    def test_one_of_two_satisfied_by_one_valid_signature(self):
        policy = self._policy(threshold=1)
        assertions = [SignatureAssertion("signer-a", _sign(self.priv_a, self.statement))]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertTrue(result.satisfied)
        self.assertEqual(result.verified_signer_ids, ("signer-a",))

    def test_two_of_two_requires_both(self):
        policy = self._policy(threshold=2)
        assertions = [SignatureAssertion("signer-a", _sign(self.priv_a, self.statement))]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertFalse(result.satisfied)

    def test_two_of_two_satisfied_by_both(self):
        policy = self._policy(threshold=2)
        assertions = [
            SignatureAssertion("signer-a", _sign(self.priv_a, self.statement)),
            SignatureAssertion("signer-b", _sign(self.priv_b, self.statement)),
        ]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertTrue(result.satisfied)
        self.assertEqual(set(result.verified_signer_ids), {"signer-a", "signer-b"})

    def test_unknown_signer_rejected(self):
        policy = self._policy(threshold=1)
        assertions = [SignatureAssertion("signer-ghost", _sign(self.priv_a, self.statement))]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertFalse(result.satisfied)
        self.assertTrue(any("unknown signer" in r for r in result.rejected))

    def test_duplicate_signature_from_one_signer_counts_once(self):
        policy = self._policy(threshold=2)
        signature = _sign(self.priv_a, self.statement)
        assertions = [
            SignatureAssertion("signer-a", signature),
            SignatureAssertion("signer-a", signature),
        ]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertEqual(result.verified_count, 1)
        self.assertFalse(result.satisfied)

    def test_malformed_signature_rejected(self):
        policy = self._policy(threshold=1)
        assertions = [SignatureAssertion("signer-a", b"garbage")]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertFalse(result.satisfied)

    def test_signer_b_signature_does_not_count_for_signer_a_claim(self):
        # A signature that actually verifies under signer-b's key, but
        # is CLAIMED under signer-a's id, must not verify (wrong key
        # used for that id) -- proves threshold counting is bound to
        # the ACTUAL verified key, not merely the claimed id string.
        policy = self._policy(threshold=1)
        signature_by_b = _sign(self.priv_b, self.statement)
        assertions = [SignatureAssertion("signer-a", signature_by_b)]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertFalse(result.satisfied)

    def test_threshold_evaluated_only_after_all_signatures_verified(self):
        # One bad + one good, threshold 2 -- must not be satisfied
        # merely because 2 assertions were SUBMITTED; only 1 verified.
        policy = self._policy(threshold=2)
        assertions = [
            SignatureAssertion("signer-a", _sign(self.priv_a, self.statement)),
            SignatureAssertion("signer-b", b"not-a-real-signature-of-64-bytes-len"),
        ]
        result = evaluate_threshold(policy, self.statement, assertions)
        self.assertFalse(result.satisfied)
        self.assertEqual(result.verified_count, 1)
