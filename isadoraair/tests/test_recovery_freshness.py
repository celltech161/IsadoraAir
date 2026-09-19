from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from isadoraair.recovery_freshness import compare_protected_updater_to_product


class ProtectedUpdaterFreshnessTests(TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.component = self.root / "protected-updater"
        self.component.mkdir()
        descriptor_dir = self.repo / "deploy/updater_runtime"
        descriptor_dir.mkdir(parents=True)
        self.descriptor = descriptor_dir / "protected-runtime-descriptor.json"
        self.descriptor_bytes = (
            json.dumps(
                {
                    "schema_version": 1,
                    "generation": 5,
                    "runtime_version": 6,
                    "manifest_protocol_version": 5,
                    "supported_wire_protocols": [3],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        self.descriptor.write_bytes(self.descriptor_bytes)
        self.digest = hashlib.sha256(self.descriptor_bytes).hexdigest()

    def evidence(self, *, generation=5, descriptor=None):
        return {
            "result": "pass",
            "active_generation": generation,
            "active_descriptor_sha256": descriptor or self.digest,
        }

    @patch("isadoraair.recovery_freshness.validate_phase_d_component")
    def test_matching_generation_and_descriptor_are_current(self, validate):
        validate.return_value = self.evidence()
        result = compare_protected_updater_to_product(
            component_root=self.component, repository_root=self.repo
        )
        self.assertTrue(result.checked)
        self.assertTrue(result.current)
        self.assertEqual(result.expected_generation, 5)
        self.assertEqual(result.observed_generation, 5)
        self.assertEqual(result.expected_descriptor_sha256, self.digest)
        self.assertEqual(result.observed_descriptor_sha256, self.digest)

    @patch("isadoraair.recovery_freshness.validate_phase_d_component")
    def test_stale_generation_is_rejected(self, validate):
        validate.return_value = self.evidence(generation=2, descriptor="b" * 64)
        result = compare_protected_updater_to_product(
            component_root=self.component, repository_root=self.repo
        )
        self.assertTrue(result.checked)
        self.assertFalse(result.current)
        self.assertIn("expected generation 5", result.diagnostic)
        self.assertIn("observed generation 2", result.diagnostic)

    @patch("isadoraair.recovery_freshness.validate_phase_d_component")
    def test_matching_generation_with_wrong_descriptor_is_rejected(self, validate):
        validate.return_value = self.evidence(generation=5, descriptor="c" * 64)
        result = compare_protected_updater_to_product(
            component_root=self.component, repository_root=self.repo
        )
        self.assertTrue(result.checked)
        self.assertFalse(result.current)
        self.assertEqual(result.observed_descriptor_sha256, "c" * 64)

    @patch("isadoraair.recovery_freshness.validate_phase_d_component")
    def test_invalid_component_fails_closed(self, validate):
        validate.side_effect = ValueError("bad signature")
        result = compare_protected_updater_to_product(
            component_root=self.component, repository_root=self.repo
        )
        self.assertFalse(result.checked)
        self.assertFalse(result.current)
        self.assertIn("could not be validated", result.diagnostic)

    @patch("isadoraair.recovery_freshness.validate_phase_d_component")
    def test_missing_product_descriptor_fails_closed(self, validate):
        validate.return_value = self.evidence()
        self.descriptor.unlink()
        result = compare_protected_updater_to_product(
            component_root=self.component, repository_root=self.repo
        )
        self.assertFalse(result.checked)
        self.assertFalse(result.current)
        self.assertIn("descriptor is unavailable or invalid", result.diagnostic)
