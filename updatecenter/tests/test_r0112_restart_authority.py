"""r0112 generation-14 bridge: explicit spoken-worker restart authority only.

Historical artifact identity is read from r0112's immutable release commit
(R0112), never the moving tree, so a later protected-runtime release that
legitimately advances the descriptor, policy or runtime cannot break this
release's own proof. The CURRENT-behavior tests at the end (application and
protected runtime in lockstep; the worker restartable; the renderer not) are
deliberately about the code as it is now.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401
from isadoraair_updater.release import CORE_SERVICES, RESTART_ORDER
from protected_bootstrap.descriptor import parse_descriptor_dict
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod
from updatecenter import planner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
R0112 = "02b10fe43728acde954dd2e151dde2ea06ae1ad0"          # the immutable r0112 release commit
BASE = "65cf850d35aee7d6a3f94c51f3f0c84f4260aa6f"           # r0111 (its parent)
R0109 = "6694b15672f520815b73e2b0e054639f9263d2ba"          # generation 13 policy, as deployed
MANIFEST_PATH = "deploy/releases/r0112.json"
DESCRIPTOR_PATH = "deploy/updater_runtime/protected-runtime-descriptor.json"
POLICY_PATH = "deploy/updater_runtime/protected-policy.json"
ATTESTATION_PATH = "deploy/updater_attestations/r0112-primary.json"
DESCRIPTOR_SHA256 = "c892e2f08fe90b302dc9be687b127a02a161c53f4f61843320c5db0e0d1d9f11"
BUNDLE_SHA256 = "6770b21c1265a8b64bc623e78e2b078b88f9cfdd7b2fb2ce2a752b07429f0cd3"
POLICY_SHA256 = "085c8d57ca7110efd12f1d1ccdea96531c0b8ffce3faaac3738802e069b0dcce"
WORKER, RENDER = "isadoraair-spoken-render-worker", "isadoraair-render"
R0112_RESTART_ORDER = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
    WORKER, "isadoraair-encoders", "isadoraair-rbds",
)
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


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _show(path: str, commit: str = R0112) -> bytes:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True,
                          capture_output=True).stdout


class R0112ReleaseArtifactTests(SimpleTestCase):
    """r0112's own artifacts, exactly as committed at R0112."""

    def setUp(self):
        self.raw = _show(MANIFEST_PATH)
        self.data = json.loads(self.raw)
        self.manifest = manifest_mod.validate_manifest_dict(self.data, source_label="r0112.json")

    def test_the_manifest_is_introduced_uniquely_by_the_release_commit(self):
        commits = _git("log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", "HEAD", "--",
                       MANIFEST_PATH).split()
        self.assertEqual(commits, [R0112])
        self.assertEqual(_git("rev-parse", f"{R0112}^"), BASE)

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
            (field.generation, field.runtime_version, field.manifest_protocol_version,
             field.supported_wire_protocols, field.minimum_bootstrap_protocol_version),
            (14, 12, 5, (3, 4), 1),
        )
        self.assertEqual(field.descriptor_sha256, DESCRIPTOR_SHA256)
        self.assertEqual(field.attestations, (ATTESTATION_PATH,))
        self.assertEqual(self.raw, (json.dumps(self.data, indent=2) + "\n").encode())

    def test_descriptor_is_canonical_exact_and_names_generation_14_runtime_12(self):
        raw = _show(DESCRIPTOR_PATH)
        value = json.loads(raw)
        descriptor = parse_descriptor_dict(value, label="r0112 descriptor")
        self.assertEqual(raw, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())
        self.assertEqual(hashlib.sha256(raw).hexdigest(), DESCRIPTOR_SHA256)
        self.assertEqual(value["bundle_sha256"], BUNDLE_SHA256)
        self.assertEqual(
            (descriptor.generation, descriptor.runtime_version, descriptor.manifest_protocol_version,
             descriptor.supported_wire_protocols),
            (14, 12, 5, (3, 4)),
        )
        # the runtime source it describes says the same (at R0112)
        package = _show("deploy/updater_runtime/isadoraair_updater/__init__.py").decode()
        self.assertIn("\nRUNTIME_VERSION = 12\n", package)
        self.assertIn("\nMANIFEST_PROTOCOL_VERSION = 5\n", package)

    def test_the_restart_contract_at_r0112_is_exact_and_in_lockstep(self):
        release_py = _show("deploy/updater_runtime/isadoraair_updater/release.py").decode()
        manifest_py = _show("updatecenter/manifest.py").decode()
        planner_py = _show("updatecenter/planner.py").decode()
        order = ('RESTART_ORDER = (\n'
                 '    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",\n'
                 '    "isadoraair-spoken-render-worker", "isadoraair-encoders", "isadoraair-rbds",\n'
                 ')\n')
        self.assertEqual(release_py.count(order), 1)              # protected runtime
        self.assertEqual(planner_py.count(order), 1)              # application planner -- the same order
        self.assertEqual(release_py.count(
            'CORE_SERVICES = frozenset({\n'
            '    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-encoders",\n'
            '    "isadoraair-monitoring", "isadoraair-spoken-render-worker", "isadoraair-rbds",\n'
            '})\n'), 1)
        self.assertEqual(manifest_py.count(
            'CORE_RESTARTABLE_SERVICES = frozenset({\n'
            '    "isadoraair-gunicorn",\n'
            '    "isadoraair-engine",\n'
            '    "isadoraair-spoken-render-worker",\n'
            '    "isadoraair-encoders",\n'
            '    "isadoraair-monitoring",\n'
            '    "isadoraair-rbds",\n'
            '})\n'), 1)
        for source in (release_py, manifest_py, planner_py):
            self.assertNotIn(f'"{RENDER}"', source)
        self.assertNotIn("RESTART_ORDER", manifest_py)
        self.assertEqual(R0112_RESTART_ORDER[3], WORKER)

    def test_signed_policy_bytes_are_generation_13s_and_do_not_define_restartability(self):
        raw = _show(POLICY_PATH)
        self.assertEqual(raw, _show(POLICY_PATH, R0109))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), POLICY_SHA256)
        mapping = parse_policy_dict(json.loads(raw), label="generation 14 policy").as_mapping()
        self.assertEqual(mapping[f"{WORKER}.service"], "ENABLE_NOW")
        self.assertEqual(mapping[f"{RENDER}.service"], "ENABLE_NOW")    # managed, yet never restartable

    def test_primary_attestation_shape_is_exact(self):
        raw = _show(ATTESTATION_PATH)
        value = json.loads(raw)
        self.assertEqual(set(value), {"schema_version", "signer_id", "signature_base64"})
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["signer_id"], "primary-release")
        self.assertEqual(raw, (json.dumps(value, indent=2) + "\n").encode())

    def test_release_surface_is_exact_and_contains_no_d3c_or_product_change(self):
        changed = set(_git("diff", "--name-only", BASE, R0112).split())
        self.assertEqual(changed, EXPECTED_SURFACE)
        forbidden_prefixes = (
            "produced_content/", "active_messages/", "spoken/", "production/", "library/", "ogremote/",
        )
        self.assertFalse([path for path in changed if path.startswith(forbidden_prefixes)])
        self.assertFalse([path for path in changed if "/migrations/" in path])
        self.assertFalse([path for path in changed if path.endswith((".service", ".timer"))])
        self.assertNotIn(POLICY_PATH, changed)
        self.assertNotIn("requirements.txt", changed)
        self.assertNotIn("deploy/isadoraair.nginx", changed)

    def test_no_migration_authorization_companion_exists(self):
        self.assertEqual(_git("log", "--full-history", "--format=%H", "HEAD", "--",
                              "deploy/migration_authorizations/r0112.json"), "")

    def test_native_airing_is_closed_at_r0112(self):
        self.assertIn(b"\nNATIVE_AIRING_AVAILABLE = False\n", _show("active_messages/services.py"))


class CurrentRestartAuthorityTests(SimpleTestCase):
    """The code as it is now: application and protected runtime agree, the
    worker is explicitly restartable, the policy-only renderer is not."""

    def test_application_and_protected_restart_contracts_match(self):
        self.assertEqual(manifest_mod.CORE_RESTARTABLE_SERVICES, CORE_SERVICES)
        self.assertEqual(planner.RESTART_ORDER, RESTART_ORDER)
        self.assertEqual(set(RESTART_ORDER), CORE_SERVICES)
        self.assertIn(WORKER, CORE_SERVICES)
        self.assertNotIn(RENDER, CORE_SERVICES)
