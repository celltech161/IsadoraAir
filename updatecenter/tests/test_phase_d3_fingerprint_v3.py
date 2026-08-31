"""Update Center Phase D, D3-J: fingerprint contract v3 cross-boundary
parity -- Django's own independent execution_contract.py copy and the
worker's own independent release.py copy must compute BYTE-IDENTICAL
v3 fingerprints for the same authorization facts, never merely "close
enough." Also proves v2 stays untouched for an ordinary release, and
that v2 and v3 payloads for otherwise-identical facts are never
accidentally equal (contract_version alone, plus the added block,
must actually change the hash)."""
from __future__ import annotations

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401

from updatecenter.execution_contract import (
    execution_fingerprint, execution_fingerprint_payload,
    protected_runtime_execution_fingerprint, protected_runtime_fingerprint_payload,
)
from isadoraair_updater.release import (
    execution_fingerprint_payload as worker_execution_fingerprint_payload,
    fingerprint as worker_fingerprint,
    protected_runtime_fingerprint_payload as worker_protected_runtime_fingerprint_payload,
)


_BASE_VALUES = dict(
    installed_release_id="r0003", installed_commit="a" * 40,
    target_release_id="r0004", target_commit="b" * 40,
    releases_in_plan=("r0004",), migrations_required=(),
    migration_compatibility=None, python_requirements_changed=False,
    apt_packages_new=(), systemd_units_changed=(), systemd_units_new_required=(),
    systemd_units_new_optional=(), systemd_units_removed_or_renamed=(),
    collectstatic_required=False, services_requiring_restart=(),
    nginx_changed=False, runtime_components_changed=False,
    minimum_updater_protocol_version=5, manual_bootstrap_required=False,
)
_PROTECTED_RUNTIME_VALUES = dict(
    protected_runtime_generation=2, protected_runtime_descriptor_sha256="c" * 64,
    protected_runtime_minimum_bootstrap_protocol_version=1, protected_runtime_runtime_version=5,
    protected_runtime_manifest_protocol_version=5, protected_runtime_supported_wire_protocols=(3,),
)


class FingerprintV3CrossBoundaryParityTests(SimpleTestCase):
    def test_v2_payloads_agree_across_django_and_worker(self):
        self.assertEqual(
            execution_fingerprint_payload(**_BASE_VALUES),
            worker_execution_fingerprint_payload(**_BASE_VALUES),
        )

    def test_v2_fingerprints_agree_across_django_and_worker(self):
        self.assertEqual(
            execution_fingerprint(**_BASE_VALUES),
            worker_fingerprint(worker_execution_fingerprint_payload(**_BASE_VALUES)),
        )

    def test_v3_payloads_agree_across_django_and_worker(self):
        django_payload = protected_runtime_fingerprint_payload(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        worker_payload = worker_protected_runtime_fingerprint_payload(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        self.assertEqual(django_payload, worker_payload)
        self.assertEqual(django_payload["contract_version"], 3)

    def test_v3_fingerprints_agree_across_django_and_worker(self):
        django_fp = protected_runtime_execution_fingerprint(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        worker_fp = worker_fingerprint(worker_protected_runtime_fingerprint_payload(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES))
        self.assertEqual(django_fp, worker_fp)

    def test_v2_and_v3_fingerprints_differ_for_the_same_underlying_facts(self):
        v2 = execution_fingerprint(**_BASE_VALUES)
        v3 = protected_runtime_execution_fingerprint(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        self.assertNotEqual(v2, v3)

    def test_v3_payload_preserves_every_v2_fact_unchanged(self):
        # D1-H's own invariant, restated as a direct assertion: a
        # candidate worker generation must never be able to
        # substitute a different release plan just because contract
        # v3 is in play -- every v2-era key/value survives into v3
        # untouched except contract_version, plus the new block.
        v2_payload = execution_fingerprint_payload(**_BASE_VALUES)
        v3_payload = protected_runtime_fingerprint_payload(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        for key, value in v2_payload.items():
            if key == "contract_version":
                continue
            self.assertEqual(v3_payload[key], value)
        self.assertEqual(set(v3_payload) - set(v2_payload), {"protected_runtime"})

    def test_different_protected_runtime_generation_changes_the_fingerprint(self):
        base = protected_runtime_execution_fingerprint(**_BASE_VALUES, **_PROTECTED_RUNTIME_VALUES)
        changed_values = dict(_PROTECTED_RUNTIME_VALUES)
        changed_values["protected_runtime_generation"] = 3
        changed = protected_runtime_execution_fingerprint(**_BASE_VALUES, **changed_values)
        self.assertNotEqual(base, changed)
