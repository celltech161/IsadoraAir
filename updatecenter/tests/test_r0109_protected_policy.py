"""r0109: protected-runtime generation 13 authorizes exactly the two Phase C
managed units -- isadoraair-render.service and
isadoraair-spoken-render-worker.service, both ENABLE_NOW -- through the signed
policy only (the r0082/r0107 pattern: no protected-runtime Python change,
runtime version 11, manifest protocol 5).

The policy must be generation 12's (as shipped by r0107, unchanged through
r0108) plus exactly those two entries: no other unit gained, lost or changed
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
R0108 = "4703a8648e0e0e3c02620d46c7684e2c0a569a18"          # generation 12 policy, as deployed
R0109 = "6694b15672f520815b73e2b0e054639f9263d2ba"          # generation 13 policy, as deployed
NEW_UNITS = {
    "isadoraair-render.service": "ENABLE_NOW",
    "isadoraair-spoken-render-worker.service": "ENABLE_NOW",
}


def _policy_at(commit: str) -> dict:
    raw = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "show", f"{commit}:deploy/updater_runtime/protected-policy.json"],
        check=True, capture_output=True,
    ).stdout
    return json.loads(raw)


class R0109ProtectedPolicyTests(SimpleTestCase):
    def setUp(self):
        self.raw = POLICY.read_bytes()
        self.value = json.loads(self.raw)
        self.document = parse_policy_dict(self.value, label="protected-policy.json")

    def test_the_policy_is_canonical_and_parses(self):
        self.assertEqual(self.raw, (json.dumps(self.value, indent=2, sort_keys=True) + "\n").encode())
        units = [entry["unit"] for entry in self.value["managed_units"]]
        self.assertEqual(units, sorted(units))

    def test_exactly_the_two_phase_c_units_are_added_to_generation_12(self):
        previous = parse_policy_dict(_policy_at(R0108), label="generation 12").as_mapping()
        current = self.document.as_mapping()
        for unit in NEW_UNITS:
            self.assertNotIn(unit, previous)
        self.assertEqual({unit: policy for unit, policy in current.items() if unit not in NEW_UNITS}, previous)
        self.assertEqual({unit: current[unit] for unit in set(current) - set(previous)}, NEW_UNITS)

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
            "isadoraair-render.service": "ENABLE_NOW",
            "isadoraair-spoken-render-worker.service": "ENABLE_NOW",
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

    def test_the_candidate_runtime_recognizes_and_enables_the_new_units(self):
        known = resolve_known_managed_units(active_policy=self.document)
        previous = parse_policy_dict(_policy_at(R0108), label="generation 12")
        for unit in NEW_UNITS:
            with self.subTest(unit=unit):
                self.assertIn(unit, known)
                self.assertIs(resolve_unit_policy(unit, signed_policy=self.document),
                              UnitActivationPolicy.ENABLE_NOW)
                # generation 12's runtime, by contrast, never heard of them
                self.assertNotIn(unit, resolve_known_managed_units(active_policy=previous))

    def test_r0112_restart_authority_is_explicit_and_policy_bytes_stay_r0109(self):
        self.assertEqual(self.value, _policy_at(R0109))

        worker = "isadoraair-spoken-render-worker"
        render = "isadoraair-render"
        mapping = self.document.as_mapping()
        self.assertEqual(mapping[f"{worker}.service"], "ENABLE_NOW")
        self.assertEqual(mapping[f"{render}.service"], "ENABLE_NOW")

        self.assertIn(worker, CORE_SERVICES)
        self.assertIn(worker, RESTART_ORDER)
        self.assertIn(worker, manifest_mod.CORE_RESTARTABLE_SERVICES)
        self.assertIn(f"{worker}.service", MANAGED_UNIT_POLICIES)

        self.assertNotIn(f"{render}.service", MANAGED_UNIT_POLICIES)
        self.assertNotIn(render, CORE_SERVICES)
        self.assertNotIn(render, RESTART_ORDER)
        self.assertNotIn(render, manifest_mod.CORE_RESTARTABLE_SERVICES)
