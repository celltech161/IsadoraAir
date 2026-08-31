from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import shutil
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy.updater_bootstrap.tools.protected_runtime_release import (
    OPENSSL_BINARY,
    ReleaseAuthoringError,
    build_descriptor,
    build_statement,
    generation_one_policy_bytes,
    sign_statement,
    validate_descriptor_inventory,
)

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from isadoraair_updater.release import GENERATION_1_POLICY_DOCUMENT, MANAGED_UNIT_POLICIES
from protected_bootstrap.policy import parse_policy_dict
from updatecenter.protected_release_validator import validate_protected_release


class ProtectedRuntimeReleaseToolTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        (self.runtime / "isadoraair_updater").mkdir(parents=True)
        (self.runtime / "protected_bootstrap").mkdir()
        files = {
            "README.md": b"reviewed runtime\n",
            "updaterctl.py": b"def main(): return 0\n",
            "updaterd.py": b"def main(): return 0\n",
            "isadoraair_updater/__init__.py": b"RUNTIME_VERSION = 5\n",
            "protected_bootstrap/__init__.py": b"BOOTSTRAP_PROTOCOL_VERSION = 1\n",
        }
        for relative, content in files.items():
            (self.runtime / relative).write_bytes(content)
        (self.runtime / "protected-policy.json").write_bytes(generation_one_policy_bytes())

    def _descriptor(self):
        return build_descriptor(
            runtime_root=self.runtime, generation=2, runtime_version=5,
            manifest_protocol_version=5, supported_wire_protocols=(3,),
        )

    def test_builder_is_byte_deterministic_and_round_trips_inventory(self):
        first = self._descriptor()
        second = self._descriptor()
        self.assertEqual(first, second)
        descriptor = validate_descriptor_inventory(
            descriptor_bytes=first, runtime_root=self.runtime,
        )
        self.assertEqual(descriptor.generation, 2)
        self.assertIn("protected-policy.json", descriptor.file_by_path())

    def test_builder_rejects_unexpected_symlink_special_and_cache_payload(self):
        unexpected = self.runtime / "secret.key"
        unexpected.write_text("not a real key")
        with self.assertRaises(ReleaseAuthoringError):
            self._descriptor()
        unexpected.unlink()
        (self.runtime / "cache").mkdir()
        with self.assertRaises(ReleaseAuthoringError):
            self._descriptor()

    def test_fixed_openssl_signer_requires_explicit_private_key_and_verifies(self):
        private = self.root / "release.key"
        public = self.root / "release.pem"
        subprocess.run(
            [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        private.chmod(0o600)
        subprocess.run(
            [OPENSSL_BINARY, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        statement = build_statement(
            descriptor_bytes=self._descriptor(), release_id="r0027",
            previous_release_id="r0026", generation=2,
        )
        signature = sign_statement(
            statement=statement, private_key_path=private, public_key_path=public,
        )
        self.assertEqual(len(signature), 64)
        private.chmod(0o640)
        with self.assertRaises(ReleaseAuthoringError):
            sign_statement(statement=statement, private_key_path=private, public_key_path=public)

    def test_generation_one_policy_is_exact_compiled_parity(self):
        policy = parse_policy_dict(json.loads(generation_one_policy_bytes()), label="D0")
        expected = {unit: value.name for unit, value in MANAGED_UNIT_POLICIES.items()}
        self.assertEqual(policy.as_mapping(), expected)
        self.assertEqual(policy.as_mapping(), GENERATION_1_POLICY_DOCUMENT.as_mapping())
        self.assertEqual(list(policy.as_mapping()), sorted(policy.as_mapping()))

    def test_statement_binds_descriptor_release_predecessor_and_generation(self):
        descriptor = self._descriptor()
        statement = build_statement(
            descriptor_bytes=descriptor, release_id="r0027",
            previous_release_id="r0026", generation=2,
        )
        self.assertIn(hashlib.sha256(descriptor).hexdigest().encode(), statement)
        self.assertIn(b"release_id=r0027", statement)
        self.assertIn(b"previous_release_id=r0026", statement)
        self.assertIn(b"generation=2", statement)
        self.assertNotIn(base64.b64encode(b"private material"), statement)

    def test_release_authoring_validator_checks_manifest_inventory_policy_and_threshold(self):
        checkout = self.root / "checkout"
        runtime = checkout / "deploy" / "updater_runtime"
        runtime.parent.mkdir(parents=True)
        shutil.copytree(self.runtime, runtime)
        descriptor = build_descriptor(
            runtime_root=runtime, generation=2, runtime_version=5,
            manifest_protocol_version=5, supported_wire_protocols=(3,),
        )
        descriptor_path = runtime / "protected-runtime-descriptor.json"
        descriptor_path.write_bytes(descriptor)
        digest = hashlib.sha256(descriptor).hexdigest()
        signers = self.root / "release-signers"
        signers.mkdir()
        private = self.root / "validator-private.key"
        public = signers / "primary.pem"
        subprocess.run(
            [OPENSSL_BINARY, "genpkey", "-algorithm", "ed25519", "-out", str(private)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        private.chmod(0o600)
        subprocess.run(
            [OPENSSL_BINARY, "pkey", "-in", str(private), "-pubout", "-out", str(public)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        statement = build_statement(
            descriptor_bytes=descriptor, release_id="r0027",
            previous_release_id="r0026", generation=2,
        )
        signature = sign_statement(
            statement=statement, private_key_path=private, public_key_path=public,
        )
        attestations = checkout / "deploy" / "updater_attestations"
        attestations.mkdir()
        (attestations / "r0027-primary.json").write_text(json.dumps({
            "schema_version": 1, "signer_id": "primary-release",
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        }))
        releases = checkout / "deploy" / "releases"
        releases.mkdir()
        manifest = {
            "schema_version": 1, "release_id": "r0027", "previous_release_id": "r0026",
            "minimum_updater_protocol_version": 5, "summary": "fixture",
            "migrations_required": [], "migration_compatibility": None,
            "manual_bootstrap_required": False,
            "python_requirements_changed": False, "requirements_sha256": None,
            "apt_packages_new": [], "systemd_units_changed": [],
            "systemd_units_new_required": [], "systemd_units_new_optional": [],
            "systemd_units_removed_or_renamed": [], "collectstatic_required": False,
            "services_requiring_restart": [], "nginx_changed": False,
            "runtime_components_changed": False, "minimum_supported_release_id": "r0007",
            "protected_runtime": {
                "generation": 2,
                "descriptor_path": "deploy/updater_runtime/protected-runtime-descriptor.json",
                "descriptor_sha256": digest,
                "minimum_bootstrap_protocol_version": 1,
                "runtime_version": 5, "manifest_protocol_version": 5,
                "supported_wire_protocols": [3],
                "attestations": ["deploy/updater_attestations/r0027-primary.json"],
            },
        }
        manifest_path = releases / "r0027.json"
        manifest_path.write_text(json.dumps(manifest))
        trust_path = self.root / "release-trust.json"
        trust_path.write_text(json.dumps({
            "schema_version": 1, "signature_algorithm": "ed25519", "threshold": 1,
            "signers": [{"id": "primary-release", "public_key_path": str(public)}],
        }))
        previous_policy = self.root / "previous-policy.json"
        previous_policy.write_bytes(generation_one_policy_bytes())
        evidence = validate_protected_release(
            checkout_root=checkout, manifest_path=manifest_path,
            trust_policy_path=trust_path, signer_directory=signers,
            previous_generation=1, previous_policy_path=previous_policy,
        )
        self.assertEqual(evidence["release_id"], "r0027")
        self.assertEqual(evidence["generation"], 2)
        self.assertEqual(evidence["verified_signers"], ["primary-release"])

        with patch(
            "updatecenter.protected_release_validator.git_adapter.changed_paths_between",
            return_value=("deploy/updater_runtime/release.py",),
        ) as changed_paths:
            evidence = validate_protected_release(
                checkout_root=checkout, manifest_path=manifest_path,
                trust_policy_path=trust_path, signer_directory=signers,
                previous_generation=1, previous_policy_path=previous_policy,
                previous_commit="before", target_commit="after",
            )
        changed_paths.assert_called_once_with(checkout, "before", "after", "deploy")
        self.assertEqual(
            evidence["changed_paths"],
            ["deploy/updater_runtime/release.py"],
        )
