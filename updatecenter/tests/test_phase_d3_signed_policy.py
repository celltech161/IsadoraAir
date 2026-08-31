"""Update Center Phase D, D3-C: signed protected managed-unit policy
actually consumed by real worker behavior -- resolve_unit_policy()'s
signed-document-first/compiled-fallback resolution, and the D0
generation-1 policy document's exact parity with today's compiled
MANAGED_UNIT_POLICIES."""
from __future__ import annotations

import json

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from isadoraair_updater.release import (
    GENERATION_1_POLICY_DOCUMENT, MANAGED_UNIT_POLICIES, UnitActivationPolicy, resolve_unit_policy,
)
from protected_bootstrap.policy import ManagedUnitPolicy, ProtectedPolicyDocument, parse_policy_dict


class Generation1PolicyDocumentParityTests(SimpleTestCase):
    def test_generation_1_document_is_well_formed_per_policy_py(self):
        # Round-trips through the REAL wire encoding (dict -> JSON ->
        # parse_policy_dict) rather than trusting the in-memory
        # dataclass alone -- proves it would actually survive being
        # signed/shipped as deploy/updater_attestations-adjacent data.
        as_dict = {
            "schema_version": GENERATION_1_POLICY_DOCUMENT.schema_version,
            "managed_units": [
                {"unit": e.unit, "policy": e.policy} for e in GENERATION_1_POLICY_DOCUMENT.entries
            ],
        }
        raw = json.dumps(as_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        reparsed = parse_policy_dict(json.loads(raw), label="generation-1")
        self.assertEqual(reparsed, GENERATION_1_POLICY_DOCUMENT)

    def test_generation_1_document_matches_compiled_defaults_exactly(self):
        expected = {unit: policy.name for unit, policy in MANAGED_UNIT_POLICIES.items()}
        self.assertEqual(GENERATION_1_POLICY_DOCUMENT.as_mapping(), expected)

    def test_generation_1_document_covers_every_known_unit_no_more_no_less(self):
        self.assertEqual(set(GENERATION_1_POLICY_DOCUMENT.as_mapping()), set(MANAGED_UNIT_POLICIES))


class ResolveUnitPolicyTests(SimpleTestCase):
    def test_none_signed_policy_is_byte_for_byte_todays_behavior(self):
        for unit, expected in MANAGED_UNIT_POLICIES.items():
            self.assertIs(resolve_unit_policy(unit, signed_policy=None), expected)
        self.assertIsNone(resolve_unit_policy("unknown-unit.service", signed_policy=None))

    def test_signed_generation_1_document_agrees_with_compiled_defaults(self):
        for unit, expected in MANAGED_UNIT_POLICIES.items():
            self.assertIs(
                resolve_unit_policy(unit, signed_policy=GENERATION_1_POLICY_DOCUMENT), expected,
            )

    def test_signed_policy_can_override_an_existing_units_activation_behavior(self):
        # Exactly the "let a station learn new behavior for an
        # EXISTING known unit through signed data, not a Python edit"
        # case D3-C's own product goal describes -- flips one real
        # unit's policy via signed data alone.
        sample_unit = "isadoraair-sync-road-conditions.service"
        self.assertIs(MANAGED_UNIT_POLICIES[sample_unit], UnitActivationPolicy.INSTALL_ONLY)
        flipped = ProtectedPolicyDocument(
            schema_version=1,
            entries=tuple(sorted((
                ManagedUnitPolicy(unit=sample_unit, policy="ENABLE_NOW"),
                *(e for e in GENERATION_1_POLICY_DOCUMENT.entries if e.unit != sample_unit),
            ), key=lambda e: e.unit)),
        )
        self.assertIs(resolve_unit_policy(sample_unit, signed_policy=flipped), UnitActivationPolicy.ENABLE_NOW)
        # Every OTHER unit is untouched by the override.
        other_unit = "isadoraair-gunicorn.service"
        self.assertIs(
            resolve_unit_policy(other_unit, signed_policy=flipped),
            MANAGED_UNIT_POLICIES[other_unit],
        )

    def test_signed_policy_silent_on_a_unit_falls_back_to_compiled_default(self):
        partial = ProtectedPolicyDocument(
            schema_version=1,
            entries=(ManagedUnitPolicy(unit="isadoraair-gunicorn.service", policy="ENABLE_NOW"),),
        )
        untouched_unit = "isadoraair-sync-road-conditions.service"
        self.assertIs(
            resolve_unit_policy(untouched_unit, signed_policy=partial),
            MANAGED_UNIT_POLICIES[untouched_unit],
        )

    def test_unknown_unit_in_neither_source_returns_none(self):
        partial = ProtectedPolicyDocument(
            schema_version=1,
            entries=(ManagedUnitPolicy(unit="isadoraair-gunicorn.service", policy="ENABLE_NOW"),),
        )
        self.assertIsNone(resolve_unit_policy("wx-forecast-1day-day.service", signed_policy=partial))


class SystemdManagerSignedPolicyWiringTests(SimpleTestCase):
    """Confirms SystemdManager itself actually reaches
    resolve_unit_policy() -- not merely that the pure function works
    in isolation."""

    def test_default_signed_policy_is_none_parity_preserved(self):
        import tempfile
        from pathlib import Path
        from .phase_b_helpers import config_dict
        from isadoraair_updater.config import validate_config_dict
        from isadoraair_updater.process import CommandRunner
        from isadoraair_updater.systemd import SystemdManager

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = validate_config_dict(config_dict(root, str(root / "upstream.git")), allow_local_repository=True)
            manager = SystemdManager(config, CommandRunner())
            self.assertIsNone(manager.signed_policy)
