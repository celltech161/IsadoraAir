"""Update Center Phase D, D4-E/D4-F/D4-G/D4-H: the signed policy
becomes the runtime-authoritative managed-unit allowlist, and the
two-stage old-worker/candidate-worker authorization for a genuinely
NEW unit name."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from isadoraair_updater.release import (
    KNOWN_MANAGED_UNITS, MANAGED_UNIT_POLICIES, UnitActivationPolicy, resolve_known_managed_units,
)
from isadoraair_updater.runtime_handoff import (
    HandoffError, resolve_candidate_policy_from_bundle, verify_candidate_independently,
    verify_new_units_authorized_by_candidate_policy,
)
from protected_bootstrap.policy import ManagedUnitPolicy, ProtectedPolicyDocument
from protected_bootstrap.attestation import OPENSSL_BINARY, build_attestation_statement
from protected_bootstrap.descriptor import FileEntry, compute_bundle_sha256
from protected_bootstrap.trust import parse_trust_policy_dict


NEW_UNIT = "wx-forecast-1day-day.service"


class SystemdManagerNewUnitInstallTests(SimpleTestCase):
    """Regression test for a real bug this D4 pass found and fixed:
    SystemdManager._install_one()'s own allowlist check still compared
    against the bare compiled KNOWN_MANAGED_UNITS constant directly,
    completely bypassing resolve_unit_policy()'s new signed-policy
    authority -- a genuinely new, signed-policy-authorized unit would
    still have been refused at the INSTALL step even though its
    ACTIVATION policy resolved correctly. Fixed by routing every
    allowlist check through SystemdManager._known_units() (D4-E/D4-F)."""

    def setUp(self):
        import tempfile as _tempfile
        from .phase_b_helpers import config_dict
        from isadoraair_updater.config import validate_config_dict
        self.temp = _tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = validate_config_dict(config_dict(self.root, str(self.root / "upstream.git")), allow_local_repository=True)
        self.source = self.root / "source"
        (self.source / "deploy").mkdir(parents=True)
        (self.source / "deploy" / NEW_UNIT).write_text("[Service]\nExecStart=/bin/true\n")

    def _plan(self):
        from isadoraair_updater.release import TrustedPlan
        return TrustedPlan(
            installed_release_id="r0003", installed_commit="a" * 40, target_release_id="r0004",
            target_commit="b" * 40, releases_in_plan=("r0004",), migrations_required=(),
            migration_compatibility=None, python_requirements_changed=False, apt_packages_new=(),
            systemd_units_changed=(), systemd_units_new_required=(NEW_UNIT,),
            systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
            collectstatic_required=False, services_requiring_restart=(), nginx_changed=False,
            runtime_components_changed=False, minimum_updater_protocol_version=5,
            manual_bootstrap_required=False, fingerprint="f" * 64,
        )

    def test_new_unit_refused_without_a_signed_policy_authorizing_it(self):
        from isadoraair_updater.systemd import SystemdError, SystemdManager
        from .test_phase_b_systemd_daemon import FakeSystemRunner
        manager = SystemdManager(self.config, FakeSystemRunner(), enforce_root_ownership=False)
        with self.assertRaises(SystemdError):
            manager.reconcile(self.source, self._plan())

    def test_new_unit_installed_and_activated_when_signed_policy_authorizes_it(self):
        from isadoraair_updater.systemd import SystemdManager
        from .test_phase_b_systemd_daemon import FakeSystemRunner
        policy = ProtectedPolicyDocument(
            schema_version=1, entries=(ManagedUnitPolicy(unit=NEW_UNIT, policy="INSTALL_ONLY"),),
        )
        manager = SystemdManager(self.config, FakeSystemRunner(), enforce_root_ownership=False, signed_policy=policy)
        result = manager.reconcile(self.source, self._plan())
        self.assertEqual(result["installed_only"], [NEW_UNIT])
        self.assertTrue((self.config.systemd_unit_root / NEW_UNIT).exists())


class ResolveKnownManagedUnitsTests(SimpleTestCase):
    def test_none_active_policy_falls_back_to_compiled_map(self):
        self.assertEqual(resolve_known_managed_units(active_policy=None), KNOWN_MANAGED_UNITS)

    def test_active_policy_is_authoritative_even_when_narrower(self):
        narrow = ProtectedPolicyDocument(
            schema_version=1, entries=(ManagedUnitPolicy(unit="isadoraair-gunicorn.service", policy="ENABLE_NOW"),),
        )
        result = resolve_known_managed_units(active_policy=narrow)
        self.assertEqual(result, frozenset({"isadoraair-gunicorn.service"}))
        # NOT the compiled map's own full set -- an active signed
        # policy is authoritative, never merely additive to the
        # compiled fallback.
        self.assertNotEqual(result, KNOWN_MANAGED_UNITS)

    def test_active_policy_can_introduce_a_genuinely_new_name(self):
        wider = ProtectedPolicyDocument(
            schema_version=1,
            entries=tuple(sorted((
                *(ManagedUnitPolicy(unit=u, policy=p.name) for u, p in MANAGED_UNIT_POLICIES.items()),
                ManagedUnitPolicy(unit=NEW_UNIT, policy="INSTALL_ONLY"),
            ), key=lambda e: e.unit)),
        )
        result = resolve_known_managed_units(active_policy=wider)
        self.assertIn(NEW_UNIT, result)
        self.assertNotIn(NEW_UNIT, KNOWN_MANAGED_UNITS)  # never a Python source edit


class VerifyNewUnitsAuthorizedByCandidatePolicyTests(SimpleTestCase):
    def test_no_needed_units_is_always_ok(self):
        self.assertEqual(
            verify_new_units_authorized_by_candidate_policy(
                needed_units=frozenset(), manifest_declared_units=frozenset(), candidate_policy=None,
            ),
            (),
        )

    def test_no_candidate_policy_at_all_refused(self):
        violations = verify_new_units_authorized_by_candidate_policy(
            needed_units=frozenset({NEW_UNIT}), manifest_declared_units=frozenset({NEW_UNIT}),
            candidate_policy=None,
        )
        self.assertTrue(violations)

    def test_candidate_policy_missing_the_exact_unit_refused(self):
        policy = ProtectedPolicyDocument(
            schema_version=1, entries=(ManagedUnitPolicy(unit="other.service", policy="ENABLE_NOW"),),
        )
        violations = verify_new_units_authorized_by_candidate_policy(
            needed_units=frozenset({NEW_UNIT}), manifest_declared_units=frozenset({NEW_UNIT}),
            candidate_policy=policy,
        )
        self.assertTrue(any("does not authorize" in v for v in violations))

    def test_candidate_policy_authorizes_exact_unit_and_manifest_agrees(self):
        policy = ProtectedPolicyDocument(
            schema_version=1, entries=(ManagedUnitPolicy(unit=NEW_UNIT, policy="INSTALL_ONLY"),),
        )
        violations = verify_new_units_authorized_by_candidate_policy(
            needed_units=frozenset({NEW_UNIT}), manifest_declared_units=frozenset({NEW_UNIT}),
            candidate_policy=policy,
        )
        self.assertEqual(violations, ())

    def test_candidate_policy_authorizes_unit_manifest_never_declared_refused(self):
        # D4-G point 6: candidate policy must not smuggle in extra
        # units the manifest's own predecessor-diff-checked intent
        # never declared changing.
        policy = ProtectedPolicyDocument(
            schema_version=1, entries=(ManagedUnitPolicy(unit=NEW_UNIT, policy="INSTALL_ONLY"),),
        )
        violations = verify_new_units_authorized_by_candidate_policy(
            needed_units=frozenset({NEW_UNIT}), manifest_declared_units=frozenset(),  # manifest silent
            candidate_policy=policy,
        )
        self.assertTrue(any("were not declared" in v for v in violations))


class ResolveCandidatePolicyFromBundleTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle_root = Path(self.temp.name)

    def test_absent_policy_file_returns_none(self):
        self.assertIsNone(resolve_candidate_policy_from_bundle(self.bundle_root))

    def test_present_valid_policy_file_parses(self):
        (self.bundle_root / "protected-policy.json").write_text(json.dumps({
            "schema_version": 1,
            "managed_units": [{"unit": NEW_UNIT, "policy": "INSTALL_ONLY"}],
        }))
        result = resolve_candidate_policy_from_bundle(self.bundle_root)
        self.assertIsNotNone(result)
        self.assertEqual(result.as_mapping(), {NEW_UNIT: "INSTALL_ONLY"})

    def test_malformed_policy_file_raises_handoff_error(self):
        (self.bundle_root / "protected-policy.json").write_text("{not json")
        with self.assertRaises(HandoffError):
            resolve_candidate_policy_from_bundle(self.bundle_root)

    def test_policy_violating_schema_raises_handoff_error(self):
        (self.bundle_root / "protected-policy.json").write_text(json.dumps({
            "schema_version": 1, "managed_units": [{"unit": "*.service", "policy": "ENABLE_NOW"}],
        }))
        with self.assertRaises(HandoffError):
            resolve_candidate_policy_from_bundle(self.bundle_root)

    def test_symlinked_policy_file_never_read(self):
        real = self.bundle_root.parent / "elsewhere.json"
        real.write_text(json.dumps({"schema_version": 1, "managed_units": [{"unit": NEW_UNIT, "policy": "ENABLE_NOW"}]}))
        (self.bundle_root / "protected-policy.json").symlink_to(real)
        self.assertIsNone(resolve_candidate_policy_from_bundle(self.bundle_root))


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


class VerifyCandidateIndependentlyTests(SimpleTestCase):
    """Real openssl Ed25519 signing/verification -- the WORKER's own
    independent re-verification (D4-G points 2-3), using D1's own
    protected_bootstrap.verification.verify_candidate_bundle for the
    first time in this codebase's real execution path."""

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

    def _stage(self, *, generation=2, include_policy=False):
        entry_content = b"import sys\nsys.exit(0)\n"
        (self.bundle_root / "updaterd.py").write_bytes(entry_content)
        (self.bundle_root / "updaterd.py").chmod(0o755)
        entries = [FileEntry("updaterd.py", hashlib.sha256(entry_content).hexdigest(), "0755", len(entry_content))]
        if include_policy:
            policy_bytes = json.dumps({
                "schema_version": 1, "managed_units": [{"unit": NEW_UNIT, "policy": "INSTALL_ONLY"}],
            }).encode()
            (self.bundle_root / "protected-policy.json").write_bytes(policy_bytes)
            (self.bundle_root / "protected-policy.json").chmod(0o644)
            entries.append(FileEntry(
                "protected-policy.json", hashlib.sha256(policy_bytes).hexdigest(), "0644", len(policy_bytes),
            ))
        entries.sort(key=lambda e: e.path)
        descriptor = {
            "schema_version": 1, "generation": generation, "runtime_version": 5, "manifest_protocol_version": 5,
            "supported_wire_protocols": [3], "entrypoint": "updaterd.py",
            "files": [{"path": e.path, "sha256": e.sha256, "mode": e.mode, "size_bytes": e.size_bytes} for e in entries],
            "bundle_sha256": compute_bundle_sha256(tuple(entries)),
        }
        descriptor_bytes = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
        statement = build_attestation_statement(
            release_id="r0004", previous_release_id="r0003", generation=generation, descriptor_sha256=descriptor_sha256,
        )
        signature = _sign(self.private_key, statement)
        (self.attestations_dir / "00.json").write_text(json.dumps({
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode(),
        }))
        return descriptor_bytes

    def test_valid_bundle_with_policy_verifies_and_returns_policy(self):
        descriptor_bytes = self._stage(include_policy=True)
        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(outcome.ok, outcome.reasons)
        self.assertIsNotNone(outcome.candidate_policy)
        self.assertEqual(outcome.candidate_policy.as_mapping(), {NEW_UNIT: "INSTALL_ONLY"})

    def test_valid_bundle_without_policy_file_ok_but_no_policy(self):
        descriptor_bytes = self._stage(include_policy=False)
        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertTrue(outcome.ok, outcome.reasons)
        self.assertIsNone(outcome.candidate_policy)

    def test_bad_signature_fails_and_policy_never_trusted(self):
        descriptor_bytes = self._stage(include_policy=True)
        # Corrupt the staged attestation after signing.
        path = self.attestations_dir / "00.json"
        record = json.loads(path.read_text())
        signature = bytearray(base64.b64decode(record["signature_base64"]))
        signature[0] ^= 0xFF
        record["signature_base64"] = base64.b64encode(bytes(signature)).decode()
        path.write_text(json.dumps(record))
        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(outcome.ok)
        self.assertIsNone(outcome.candidate_policy)

    def test_replayed_generation_fails(self):
        descriptor_bytes = self._stage(generation=1, include_policy=True)
        outcome = verify_candidate_independently(
            trust_policy=self.trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=self.bundle_root,
            attestations_dir=self.attestations_dir, release_id="r0004", previous_release_id="r0003",
            previous_generation=1, current_bootstrap_protocol_version=1, current_wire_protocol_version=3,
        )
        self.assertFalse(outcome.ok)
        self.assertTrue(any("generation" in r for r in outcome.reasons))
