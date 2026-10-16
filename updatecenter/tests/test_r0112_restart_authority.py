"""r0112 generation-14 bridge: explicit spoken-worker restart authority only."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401
from isadoraair_updater import (
    MANIFEST_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
    RUNTIME_VERSION,
    SUPPORTED_WIRE_PROTOCOLS,
)
from isadoraair_updater.release import CORE_SERVICES, RESTART_ORDER
from protected_bootstrap.descriptor import parse_descriptor_dict
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import planner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "deploy" / "releases" / "r0112.json"
DESCRIPTOR = PROJECT_ROOT / "deploy" / "updater_runtime" / "protected-runtime-descriptor.json"
POLICY = PROJECT_ROOT / "deploy" / "updater_runtime" / "protected-policy.json"
ATTESTATION = PROJECT_ROOT / "deploy" / "updater_attestations" / "r0112-primary.json"
BASE = "65cf850d35aee7d6a3f94c51f3f0c84f4260aa6f"
R0109 = "6694b15672f520815b73e2b0e054639f9263d2ba"
DESCRIPTOR_SHA256 = "c892e2f08fe90b302dc9be687b127a02a161c53f4f61843320c5db0e0d1d9f11"
EXPECTED_SURFACE = {
    "deploy/releases/r0112.json",
    "deploy/updater_attestations/r0112-primary.json",
    "deploy/updater_runtime/README.md",
    "deploy/updater_runtime/isadoraair_updater/__init__.py",
    "deploy/updater_runtime/isadoraair_updater/release.py",
    "deploy/updater_runtime/protected-runtime-descriptor.json",
    "docs/releases/r0112.md",
    "updatecenter/manifest.py",
    "updatecenter/planner.py",
    "updatecenter/tests/test_manifest.py",
    "updatecenter/tests/test_phase_b_release_staging.py",
    "updatecenter/tests/test_phase_b_systemd_daemon.py",
    "updatecenter/tests/test_phase_b_managed_units.py",
    "updatecenter/tests/test_phase_b_protocol.py",
    "updatecenter/tests/test_r0107_protected_policy.py",
    "updatecenter/tests/test_planner.py",
    "updatecenter/tests/test_r0109_protected_policy.py",
    "updatecenter/tests/test_r0112_restart_authority.py",
    "updatecenter/tests/test_security_contract.py",
    "updatecenter/tests/test_runtime11_bootstrap.py",
}


def _git(*args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), *args],
        check=True,
        capture_output=True,
        text=not binary,
    )
    return result.stdout if binary else result.stdout.strip()


def _release_commit() -> str | None:
    commits = _git(
        "log", "--full-history", "--no-renames", "--diff-filter=A",
        "--format=%H", "HEAD", "--", "deploy/releases/r0112.json",
    ).split()
    if not commits:
        return None
    if len(commits) != 1:
        raise AssertionError(f"r0112 manifest introduction is not unique: {commits!r}")
    return commits[0]


class R0112RestartAuthorityTests(SimpleTestCase):
    def setUp(self):
        self.raw = MANIFEST.read_bytes()
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(
            self.data, source_label="r0112.json"
        )

    def test_manifest_is_the_exact_generation_14_bridge(self):
        m = self.manifest
        self.assertEqual(
            (m.release_id, m.previous_release_id, m.minimum_updater_protocol_version),
            ("r0112", "r0111", 5),
        )
        self.assertEqual(list(m.migrations_required), [])
        self.assertIsNone(m.migration_compatibility)
        self.assertFalse(m.manual_bootstrap_required)
        self.assertFalse(m.python_requirements_changed)
        self.assertIsNone(m.requirements_sha256)
        self.assertEqual(list(m.apt_packages_new), [])
        self.assertEqual(list(m.systemd_units_changed), [])
        self.assertEqual(list(m.systemd_units_new_required), [])
        self.assertEqual(list(m.systemd_units_new_optional), [])
        self.assertEqual(list(m.systemd_units_removed_or_renamed), [])
        self.assertFalse(m.collectstatic_required)
        self.assertEqual(list(m.services_requiring_restart), ["isadoraair-gunicorn"])
        self.assertFalse(m.nginx_changed)
        self.assertFalse(m.runtime_components_changed)
        self.assertEqual(m.minimum_supported_release_id, "r0081")
        field = m.protected_runtime
        self.assertIsNotNone(field)
        self.assertEqual(
            (
                field.generation,
                field.runtime_version,
                field.manifest_protocol_version,
                field.supported_wire_protocols,
                field.minimum_bootstrap_protocol_version,
            ),
            (14, 12, 5, (3, 4), 1),
        )
        self.assertEqual(
            field.attestations,
            ("deploy/updater_attestations/r0112-primary.json",),
        )
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())

    def test_descriptor_is_canonical_exact_and_matches_runtime_identity(self):
        raw = DESCRIPTOR.read_bytes()
        value = json.loads(raw)
        descriptor = parse_descriptor_dict(value, label="r0112 descriptor")
        self.assertEqual(raw, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), DESCRIPTOR_SHA256)
        self.assertEqual(
            (
                descriptor.generation,
                descriptor.runtime_version,
                descriptor.manifest_protocol_version,
                descriptor.supported_wire_protocols,
            ),
            (14, 12, 5, (3, 4)),
        )
        self.assertEqual(self.manifest.protected_runtime.descriptor_sha256, DESCRIPTOR_SHA256)

    def test_application_and_protected_restart_contracts_match(self):
        expected_order = (
            "isadoraair-gunicorn",
            "isadoraair-engine",
            "isadoraair-monitoring",
            "isadoraair-spoken-render-worker",
            "isadoraair-encoders",
            "isadoraair-rbds",
        )
        self.assertEqual(manifest_mod.CORE_RESTARTABLE_SERVICES, CORE_SERVICES)
        self.assertEqual(planner.RESTART_ORDER, RESTART_ORDER)
        self.assertEqual(RESTART_ORDER, expected_order)
        self.assertEqual(RUNTIME_VERSION, 12)
        self.assertEqual(MANIFEST_PROTOCOL_VERSION, 5)
        self.assertEqual(PROTOCOL_VERSION, 4)
        self.assertEqual(SUPPORTED_WIRE_PROTOCOLS, (3, 4))

    def test_signed_policy_bytes_are_unchanged_and_do_not_define_restartability(self):
        previous = _git(
            "show", f"{R0109}:deploy/updater_runtime/protected-policy.json",
            binary=True,
        )
        self.assertEqual(POLICY.read_bytes(), previous)
        policy = parse_policy_dict(json.loads(previous), label="generation 13 policy")
        self.assertEqual(
            policy.as_mapping()["isadoraair-spoken-render-worker.service"],
            "ENABLE_NOW",
        )
        self.assertEqual(policy.as_mapping()["isadoraair-render.service"], "ENABLE_NOW")
        self.assertIn("isadoraair-spoken-render-worker", CORE_SERVICES)
        self.assertNotIn("isadoraair-render", CORE_SERVICES)

    def test_primary_attestation_shape_is_exact_when_present(self):
        if not ATTESTATION.exists():
            self.skipTest("awaiting operator signing with the root-owned release key")
        raw = ATTESTATION.read_bytes()
        value = json.loads(raw)
        self.assertEqual(set(value), {"schema_version", "signer_id", "signature_base64"})
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["signer_id"], "primary-release")
        self.assertEqual(raw, (json.dumps(value, indent=2) + "\n").encode())

    def test_release_surface_is_exact_and_contains_no_d3c_or_product_change(self):
        release = _release_commit()
        if release is None:
            self.skipTest("historical surface is asserted after the atomic release commit")
        self.assertEqual(_git("rev-parse", f"{release}^"), BASE)
        changed = set(_git("diff", "--name-only", BASE, release).split())
        self.assertEqual(changed, EXPECTED_SURFACE)
        forbidden_prefixes = (
            "produced_content/",
            "active_messages/",
            "spoken/management/commands/",
            "production/services/render/",
            "ogremote/",
        )
        self.assertFalse(
            [path for path in changed if path.startswith(forbidden_prefixes)]
        )
        self.assertFalse(
            [path for path in changed if path.endswith((".service", ".timer"))]
        )
        self.assertNotIn("deploy/updater_runtime/protected-policy.json", changed)
        self.assertNotIn("requirements.txt", changed)
        self.assertNotIn("deploy/isadoraair.nginx", changed)

    def test_no_migration_authorization_companion_exists(self):
        companion = PROJECT_ROOT / "deploy" / "migration_authorizations" / "r0112.json"
        self.assertFalse(companion.exists())
        history = _git(
            "log", "--full-history", "--format=%H", "HEAD", "--",
            "deploy/migration_authorizations/r0112.json",
        )
        self.assertEqual(history, "")

    def test_native_airing_remains_closed(self):
        source = (PROJECT_ROOT / "active_messages" / "services.py").read_text()
        self.assertIn("\nNATIVE_AIRING_AVAILABLE = False\n", source)
        from active_messages import services

        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
