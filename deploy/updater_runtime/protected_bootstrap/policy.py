"""D1-C: the signed protected managed-unit policy -- a strict data
contract intended to eventually let a station learn a new managed unit
(e.g. a new Weather forecast service) through a signed data file inside
a protected-runtime bundle, rather than a Python source edit to
isadoraair_updater.release.MANAGED_UNIT_POLICIES.

Deliberately deviates from the workorder's own literal example schema
(a bare `{"unit-name": "POLICY"}` JSON object) in one specific way: a
bare JSON object cannot express "duplicate key" once parsed -- Python's
json.loads (like every conformant JSON parser) silently keeps only the
LAST value for a repeated key, so "duplicates impossible/rejected" and
"sorted deterministic representation" cannot be genuinely enforced
against already-parsed dict data; the duplicate has already vanished
before any validator ever sees it. Below, `managed_units` is instead an
ORDERED LIST of exactly two-field {"unit": ..., "policy": ...} objects,
which makes real duplicate-unit detection possible and lets "canonical
order" be an actual, checkable property of the wire representation, not
just an incidental fact about Python dict iteration.

Worker code (isadoraair_updater.release) remains the sole authority for
what ENABLE_NOW/INSTALL_ONLY actually DO -- this module only validates
that a policy string is one of that closed, already-existing set; it
can never introduce a new activation behavior, a wildcard/glob/regex
unit match, or an arbitrary systemctl verb. See this package's own
verify_candidate_bundle() (verification.py) for how a policy file's
signed inclusion in a runtime descriptor is what actually gives it
authority -- this module alone only checks internal well-formedness."""
from __future__ import annotations

import dataclasses
import re

SCHEMA_VERSION = 1

# Mirrors isadoraair_updater.release.UnitActivationPolicy's two values
# EXACTLY -- deliberately NOT imported from there (this package stays
# import-clean of the worker's own module), so this is the one place
# that pairing must be kept in lockstep; see this package's own tests
# for a runtime cross-check that the two enums have not silently
# diverged.
ALLOWED_POLICIES = frozenset({"ENABLE_NOW", "INSTALL_ONLY"})

# Exact systemd unit basename only -- one dot-suffix, no path
# separator, no wildcard/glob metacharacter of any kind ('*', '?',
# '[', ']'), no leading dot. The same shape discipline
# updatecenter/manifest.py's own UNIT_NAME_PATTERN already applies to
# manifest-declared unit names -- restated here independently rather
# than imported, for the same "no cross-package dependency into the
# Django app" reason as everywhere else in this package.
UNIT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(service|timer)$")

MAX_ENTRIES = 128


class PolicyError(ValueError):
    """Raised for any structurally-invalid protected policy document."""


@dataclasses.dataclass(frozen=True)
class ManagedUnitPolicy:
    unit: str
    policy: str


@dataclasses.dataclass(frozen=True)
class ProtectedPolicyDocument:
    schema_version: int
    entries: tuple[ManagedUnitPolicy, ...]

    def as_mapping(self) -> dict[str, str]:
        """Convenience view for a caller that just wants unit -> policy,
        e.g. to compare against isadoraair_updater.release.
        MANAGED_UNIT_POLICIES's own shape in a test."""
        return {entry.unit: entry.policy for entry in self.entries}


def parse_policy_dict(data: dict, *, label: str = "<protected-policy>") -> ProtectedPolicyDocument:
    """The one entry point. Strict: unknown top-level keys, an
    unsorted/duplicate/malformed unit entry, a unit name with any path
    separator or wildcard character, or a policy value outside the
    closed ALLOWED_POLICIES set all raise PolicyError."""
    if not isinstance(data, dict):
        raise PolicyError(f"{label}: policy document must be a JSON object")

    known_top = {"schema_version", "managed_units"}
    unknown_top = set(data) - known_top
    if unknown_top:
        raise PolicyError(f"{label}: unrecognized field(s) {sorted(unknown_top)!r}")
    missing_top = known_top - set(data)
    if missing_top:
        raise PolicyError(f"{label}: missing required field(s) {sorted(missing_top)!r}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise PolicyError(f"{label}: schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise PolicyError(f"{label}: unsupported schema_version {schema_version} (expected {SCHEMA_VERSION})")

    raw_entries = data["managed_units"]
    if not isinstance(raw_entries, list):
        raise PolicyError(f"{label}: managed_units must be a list")
    if not raw_entries:
        raise PolicyError(f"{label}: managed_units must not be empty")
    if len(raw_entries) > MAX_ENTRIES:
        raise PolicyError(f"{label}: managed_units exceeds {MAX_ENTRIES} entries")

    known_entry_keys = {"unit", "policy"}
    entries: list[ManagedUnitPolicy] = []
    seen_units: set[str] = set()
    for index, raw in enumerate(raw_entries):
        item_label = f"{label}: managed_units[{index}]"
        if not isinstance(raw, dict):
            raise PolicyError(f"{item_label}: must be a JSON object")
        unknown_keys = set(raw) - known_entry_keys
        if unknown_keys:
            raise PolicyError(f"{item_label}: unrecognized field(s) {sorted(unknown_keys)!r}")
        missing_keys = known_entry_keys - set(raw)
        if missing_keys:
            raise PolicyError(f"{item_label}: missing field(s) {sorted(missing_keys)!r}")

        unit = raw["unit"]
        if not isinstance(unit, str) or not UNIT_NAME_RE.match(unit):
            raise PolicyError(
                f"{item_label}: unit must be an exact '<name>.service'/'<name>.timer' "
                "basename with no path separator or wildcard"
            )
        if unit in seen_units:
            raise PolicyError(f"{item_label}: duplicate unit {unit!r}")
        seen_units.add(unit)

        policy = raw["policy"]
        if not isinstance(policy, str) or policy not in ALLOWED_POLICIES:
            raise PolicyError(f"{item_label}: policy must be one of {sorted(ALLOWED_POLICIES)!r}")

        entries.append(ManagedUnitPolicy(unit=unit, policy=policy))

    unit_order = [entry.unit for entry in entries]
    if unit_order != sorted(unit_order):
        raise PolicyError(f"{label}: managed_units must be listed in canonical ascending unit-name order")

    return ProtectedPolicyDocument(schema_version=schema_version, entries=tuple(entries))
