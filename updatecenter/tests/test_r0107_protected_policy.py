"""r0107: protected-runtime generation 12 authorizes exactly one new managed
unit -- isadoraair-validation.service, ENABLE_NOW -- through the signed policy
only (the r0082/generation-4 pattern: no protected-runtime Python change).

The policy must be generation 11's (as shipped by r0105, unchanged through
r0106) plus exactly that one entry: no other unit gained, lost or changed
policy, so it cannot grant authority over anything unintended.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401  (puts the protected runtime on sys.path)

from isadoraair_updater.release import (
    CORE_SERVICES, MANAGED_UNIT_POLICIES, RESTART_ORDER, UnitActivationPolicy,
    resolve_known_managed_units, resolve_unit_policy,
)
from protected_bootstrap.policy import parse_policy_dict

from updatecenter import manifest as manifest_mod

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POLICY = PROJECT_ROOT / "deploy" / "updater_runtime" / "protected-policy.json"
R0106 = "f65652a2830d2eb721defc05adfeb533acc6ef8c"
# Pinned to what r0107 shipped: later generations (r0109's 13) extend the
# working-tree policy and are covered by their own release tests.
R0107 = "e3f51c0f5616f28d140fea2014ed4e8c9de0dc6e"
NEW_UNIT = "isadoraair-validation.service"


def _policy_at(commit: str) -> dict:
    raw = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:deploy/updater_runtime/protected-policy.json"],
        check=True, capture_output=True,
    ).stdout
    return json.loads(raw)


def _git_show(commit: str, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:{path}"], check=True, capture_output=True,
    ).stdout


def _exists_at(commit: str, path: str) -> bool:
    return subprocess.run(["git", "-C", str(PROJECT_ROOT), "cat-file", "-e", f"{commit}:{path}"],
                          capture_output=True).returncode == 0


def _runtime_changes(previous: str, release: str) -> set:
    return set(subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "diff", "--name-only", previous, release, "--", "deploy/updater_runtime"],
        check=True, capture_output=True, text=True,
    ).stdout.split())


RELEASE_PY = "deploy/updater_runtime/isadoraair_updater/release.py"
POLICY_ONLY = {"deploy/updater_runtime/protected-policy.json", "deploy/updater_runtime/protected-runtime-descriptor.json"}


class R0107ProtectedPolicyTests(SimpleTestCase):
    def setUp(self):
        self.raw = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "show", f"{R0107}:deploy/updater_runtime/protected-policy.json"],
            check=True, capture_output=True,
        ).stdout
        self.value = json.loads(self.raw)
        self.document = parse_policy_dict(self.value, label="protected-policy.json")

    def test_the_policy_is_canonical_and_parses(self):
        self.assertEqual(self.raw, (json.dumps(self.value, indent=2, sort_keys=True) + "\n").encode())
        units = [entry["unit"] for entry in self.value["managed_units"]]
        self.assertEqual(units, sorted(units))

    def test_exactly_one_unit_is_added_to_generation_11(self):
        previous = parse_policy_dict(_policy_at(R0106), label="generation 11").as_mapping()
        current = self.document.as_mapping()
        self.assertNotIn(NEW_UNIT, previous)
        self.assertEqual({unit: policy for unit, policy in current.items() if unit != NEW_UNIT}, previous)
        self.assertEqual(set(current) - set(previous), {NEW_UNIT})
        self.assertEqual(current[NEW_UNIT], "ENABLE_NOW")

    def test_the_exact_managed_unit_inventory(self):
        self.assertEqual(self.document.as_mapping(), {
            "isadoraair-aircheck-buffer.service": "INSTALL_ONLY",
            "isadoraair-aircheck-buffer.timer": "ENABLE_NOW",
            "isadoraair-aircheck-recovery.service": "INSTALL_ONLY",
            "isadoraair-aircheck-recovery.timer": "ENABLE_NOW",
            "isadoraair-encoders.service": "ENABLE_NOW",
            "isadoraair-engine.service": "ENABLE_NOW",
            "isadoraair-generate-road-condition-audio.service": "INSTALL_ONLY",
            "isadoraair-generate-road-condition-audio.timer": "ENABLE_NOW",
            "isadoraair-gunicorn.service": "ENABLE_NOW",
            "isadoraair-monitoring.service": "ENABLE_NOW",
            "isadoraair-rbds.service": "ENABLE_NOW",
            "isadoraair-sync-road-conditions.service": "INSTALL_ONLY",
            "isadoraair-sync-road-conditions.timer": "ENABLE_NOW",
            "isadoraair-validation.service": "ENABLE_NOW",
            "wx-forecast-1day-day.service": "INSTALL_ONLY",
            "wx-forecast-1day-night.service": "INSTALL_ONLY",
            "wx-forecast-3day-day.service": "INSTALL_ONLY",
            "wx-forecast-3day-night.service": "INSTALL_ONLY",
        })

    def test_every_policy_unit_has_a_real_template(self):
        for unit in self.document.as_mapping():
            with self.subTest(unit=unit):
                self.assertTrue(_exists_at(R0107, f"deploy/{unit}"))          # at r0107 itself

    def test_the_candidate_runtime_recognizes_and_enables_the_new_unit(self):
        known = resolve_known_managed_units(active_policy=self.document)
        self.assertIn(NEW_UNIT, known)
        self.assertIs(resolve_unit_policy(NEW_UNIT, signed_policy=self.document), UnitActivationPolicy.ENABLE_NOW)
        # generation 11's runtime, by contrast, never heard of it
        previous = parse_policy_dict(_policy_at(R0106), label="generation 11")
        self.assertNotIn(NEW_UNIT, resolve_known_managed_units(active_policy=previous))

    def test_no_protected_runtime_python_authority_changed_at_r0107(self):
        """Signed-policy-only transition, bounded by r0107's OWN release commit
        (never the moving tree): r0107 changed only the policy and descriptor in
        the protected runtime; its compiled fallback map, core service set and
        restart allowlist (release.py) are r0106's, and never name the
        validation service."""
        self.assertEqual(_runtime_changes(R0106, R0107), POLICY_ONLY)
        release_py = _git_show(R0107, RELEASE_PY)
        self.assertEqual(release_py, _git_show(R0106, RELEASE_PY))
        self.assertNotIn(b"isadoraair-validation", release_py)
        self.assertNotIn(b"isadoraair-validation", _git_show(R0107, "updatecenter/manifest.py"))

    def test_validation_is_still_not_restartable_now(self):
        """Current behavior: later explicit restart additions (r0112) never
        derive authority from policy."""
        self.assertNotIn(NEW_UNIT, MANAGED_UNIT_POLICIES)
        self.assertNotIn("isadoraair-validation", CORE_SERVICES)
        self.assertNotIn("isadoraair-validation", RESTART_ORDER)
        self.assertNotIn("isadoraair-validation", manifest_mod.CORE_RESTARTABLE_SERVICES)
