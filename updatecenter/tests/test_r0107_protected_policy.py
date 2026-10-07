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
NEW_UNIT = "isadoraair-validation.service"


def _policy_at(commit: str) -> dict:
    raw = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:deploy/updater_runtime/protected-policy.json"],
        check=True, capture_output=True,
    ).stdout
    return json.loads(raw)


class R0107ProtectedPolicyTests(SimpleTestCase):
    def setUp(self):
        self.raw = POLICY.read_bytes()
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
                self.assertTrue((PROJECT_ROOT / "deploy" / unit).is_file())

    def test_the_candidate_runtime_recognizes_and_enables_the_new_unit(self):
        known = resolve_known_managed_units(active_policy=self.document)
        self.assertIn(NEW_UNIT, known)
        self.assertIs(resolve_unit_policy(NEW_UNIT, signed_policy=self.document), UnitActivationPolicy.ENABLE_NOW)
        # generation 11's runtime, by contrast, never heard of it
        previous = parse_policy_dict(_policy_at(R0106), label="generation 11")
        self.assertNotIn(NEW_UNIT, resolve_known_managed_units(active_policy=previous))

    def test_no_protected_runtime_python_authority_changed(self):
        """Signed-policy-only transition: the compiled fallback map, the core
        service set and the restart allowlist are untouched -- the validation
        service is never restartable through services_requiring_restart."""
        self.assertNotIn(NEW_UNIT, MANAGED_UNIT_POLICIES)
        self.assertNotIn("isadoraair-validation", CORE_SERVICES)
        self.assertNotIn("isadoraair-validation", RESTART_ORDER)
        self.assertNotIn("isadoraair-validation", manifest_mod.CORE_RESTARTABLE_SERVICES)
        changed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "diff", "--name-only", R0106, "--", "deploy/updater_runtime"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
        self.assertTrue(set(changed) <= {
            "deploy/updater_runtime/protected-policy.json",
            "deploy/updater_runtime/protected-runtime-descriptor.json",
        }, changed)
