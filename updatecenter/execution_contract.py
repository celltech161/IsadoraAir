"""Pure application-side copy of fingerprint contract v2 used by protocol v3."""
from __future__ import annotations

import hashlib
import json


def execution_fingerprint_payload(**values) -> dict:
    return {
        "contract_version": 2,
        "installed_release_id": values["installed_release_id"],
        "installed_commit": values["installed_commit"],
        "target_release_id": values["target_release_id"],
        "target_commit": values["target_commit"],
        "releases_in_plan": list(values["releases_in_plan"]),
        "migrations_required": list(values["migrations_required"]),
        "migration_compatibility": values["migration_compatibility"],
        "python_requirements_changed": values["python_requirements_changed"],
        "apt_packages_new": list(values["apt_packages_new"]),
        "systemd_units_changed": list(values["systemd_units_changed"]),
        "systemd_units_new_required": list(values["systemd_units_new_required"]),
        "systemd_units_new_optional": list(values["systemd_units_new_optional"]),
        "systemd_units_removed_or_renamed": list(values["systemd_units_removed_or_renamed"]),
        "collectstatic_required": values["collectstatic_required"],
        "services_requiring_restart": list(values["services_requiring_restart"]),
        "nginx_changed": values["nginx_changed"],
        "runtime_components_changed": values["runtime_components_changed"],
        "minimum_updater_protocol_version": values["minimum_updater_protocol_version"],
        "manual_bootstrap_required": values["manual_bootstrap_required"],
    }


def execution_fingerprint(**values) -> str:
    payload = execution_fingerprint_payload(**values)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def protected_runtime_fingerprint_payload(**values) -> dict:
    """Update Center Phase D, D3-J: Django's own independently-
    maintained mirror of deploy/updater_runtime/isadoraair_updater/
    release.py's protected_runtime_fingerprint_payload() -- byte-for-
    byte the SAME contract (fingerprint v3, D1-H), built the SAME way
    (v2's exact existing-release-actions payload, contract_version
    overwritten, protected_runtime facts added), kept in lockstep by
    test_phase_d3_fingerprint_v3.py's own cross-boundary parity test
    rather than by importing the worker's tree (this module's own top
    docstring already explains why Django never imports deploy/
    updater_runtime/**)."""
    base = execution_fingerprint_payload(**values)
    return {
        **{key: value for key, value in base.items() if key != "contract_version"},
        "contract_version": 3,
        "protected_runtime": {
            "generation": values["protected_runtime_generation"],
            "descriptor_sha256": values["protected_runtime_descriptor_sha256"],
            "minimum_bootstrap_protocol_version": values["protected_runtime_minimum_bootstrap_protocol_version"],
            "runtime_version": values["protected_runtime_runtime_version"],
            "manifest_protocol_version": values["protected_runtime_manifest_protocol_version"],
            "supported_wire_protocols": list(values["protected_runtime_supported_wire_protocols"]),
        },
    }


def protected_runtime_execution_fingerprint(**values) -> str:
    payload = protected_runtime_fingerprint_payload(**values)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
