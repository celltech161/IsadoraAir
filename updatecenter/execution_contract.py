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
