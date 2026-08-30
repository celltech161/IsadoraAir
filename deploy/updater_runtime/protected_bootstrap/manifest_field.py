"""D1-A: the optional release-manifest `protected_runtime` field.

`"protected_runtime": null` (or the field simply absent) is what every
ordinary release declares -- true for every release through r0025 today,
and true for r0026 (see this package's own docstring / docs/
UPDATE_CENTER_PHASE_D.md's "D0 final bootstrap bridge" section: r0026
must remain parseable by the CURRENT, pre-Phase-D manifest schema, which
does not know this field exists at all -- so r0026 must not rely on it
being present, only on `manual_bootstrap_required: true`). The first
release legitimately allowed to populate this field with a real object
is r0027 or later, once r0026's manually-bootstrapped planner/worker
already understands it.

Version numbers below are PROPOSED BRIDGE VALUES, inspected against this
codebase's real current constants (isadoraair_updater/__init__.py:
PROTOCOL_VERSION=3, RUNTIME_VERSION=4, MANIFEST_PROTOCOL_VERSION=4) --
not blindly copied from the workorder's own conceptual example:
  - generation starts at 1 -- a NEW counter Phase D introduces,
    independent of RUNTIME_VERSION's own numbering (see verification.
    py's own "first-ever generation must be exactly 1" rule).
  - runtime_version/manifest_protocol_version inside a real future
    protected_runtime block should equal whatever RUNTIME_VERSION/
    MANIFEST_PROTOCOL_VERSION actually are in that release's own
    worker source -- 4 today were this cut immediately; genuinely a
    later, real number once Phase D's own worker code changes ship.
  - supported_wire_protocols should include today's PROTOCOL_VERSION=3
    at minimum, per D1-I's bridge rule (a new runtime must keep
    understanding the old wire protocol until every client has moved).
  - minimum_bootstrap_protocol_version references a protocol that does
    not exist before Phase D at all -- see this package's own addition
    of BOOTSTRAP_PROTOCOL_VERSION=1 to isadoraair_updater/__init__.py's
    four-protocol-concept documentation (D1-I)."""
from __future__ import annotations

import dataclasses

from .descriptor import MAX_GENERATION, SHA256_RE, validate_relative_path

SCHEMA_VERSION = 1

# The exact, closed repository-relative prefixes a descriptor_path/
# attestation path may live under -- never an arbitrary relative path
# elsewhere in the trusted repository. Kept deliberately narrow; a
# future need for a third prefix is a real, reviewable schema change,
# not something this validator infers.
DESCRIPTOR_PATH_PREFIX = "deploy/updater_runtime/"
ATTESTATION_PATH_PREFIX = "deploy/updater_attestations/"

MAX_WIRE_PROTOCOLS = 8
MAX_ATTESTATIONS = 16


class ProtectedRuntimeFieldError(ValueError):
    """Raised for a malformed protected_runtime manifest field."""


@dataclasses.dataclass(frozen=True)
class ProtectedRuntimeField:
    generation: int
    descriptor_path: str
    descriptor_sha256: str
    minimum_bootstrap_protocol_version: int
    runtime_version: int
    manifest_protocol_version: int
    supported_wire_protocols: tuple[int, ...]
    attestations: tuple[str, ...]


def _require_prefixed_path(value, *, field: str, prefix: str) -> str:
    path = validate_relative_path(value, field=field)
    if not path.startswith(prefix):
        raise ProtectedRuntimeFieldError(f"{field}: must live under {prefix!r}, got {path!r}")
    return path


def parse_protected_runtime_field(value, *, label: str = "<manifest>.protected_runtime") -> ProtectedRuntimeField | None:
    """`value` is whatever `data.get("protected_runtime")` produced --
    None (the ordinary-release case, including every release through
    the r0026 bridge) returns None immediately. Any non-None value is
    validated strictly: unknown keys, wrong types, out-of-bound
    generation, a malformed/wrongly-prefixed descriptor_path, a
    descriptor_sha256 that isn't exactly 64 lowercase hex, a duplicate/
    wrongly-prefixed/excessive attestations list, or a malformed
    supported_wire_protocols list all raise ProtectedRuntimeFieldError."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ProtectedRuntimeFieldError(f"{label}: must be a JSON object or null")

    known = {
        "generation", "descriptor_path", "descriptor_sha256",
        "minimum_bootstrap_protocol_version", "runtime_version",
        "manifest_protocol_version", "supported_wire_protocols", "attestations",
    }
    unknown = set(value) - known
    if unknown:
        raise ProtectedRuntimeFieldError(f"{label}: unrecognized field(s) {sorted(unknown)!r}")
    missing = known - set(value)
    if missing:
        raise ProtectedRuntimeFieldError(f"{label}: missing required field(s) {sorted(missing)!r}")

    def require_positive_int(field_name, *, maximum=None):
        raw = value[field_name]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            raise ProtectedRuntimeFieldError(f"{label}.{field_name}: must be a positive integer")
        if maximum is not None and raw > maximum:
            raise ProtectedRuntimeFieldError(f"{label}.{field_name}: exceeds maximum {maximum}")
        return raw

    generation = require_positive_int("generation", maximum=MAX_GENERATION)
    minimum_bootstrap_protocol_version = require_positive_int("minimum_bootstrap_protocol_version")
    runtime_version = require_positive_int("runtime_version")
    manifest_protocol_version = require_positive_int("manifest_protocol_version")

    descriptor_path = _require_prefixed_path(
        value["descriptor_path"], field=f"{label}.descriptor_path", prefix=DESCRIPTOR_PATH_PREFIX,
    )

    descriptor_sha256 = value["descriptor_sha256"]
    if not isinstance(descriptor_sha256, str) or not SHA256_RE.match(descriptor_sha256):
        raise ProtectedRuntimeFieldError(f"{label}.descriptor_sha256: must be exactly 64 lowercase hex characters")

    wire = value["supported_wire_protocols"]
    if not isinstance(wire, list) or not wire:
        raise ProtectedRuntimeFieldError(f"{label}.supported_wire_protocols: must be a non-empty list")
    if len(wire) > MAX_WIRE_PROTOCOLS:
        raise ProtectedRuntimeFieldError(f"{label}.supported_wire_protocols: exceeds {MAX_WIRE_PROTOCOLS} entries")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in wire):
        raise ProtectedRuntimeFieldError(f"{label}.supported_wire_protocols: must contain only positive integers")
    if len(set(wire)) != len(wire):
        raise ProtectedRuntimeFieldError(f"{label}.supported_wire_protocols: contains a duplicate")
    if list(wire) != sorted(wire):
        raise ProtectedRuntimeFieldError(f"{label}.supported_wire_protocols: must be in canonical ascending order")

    raw_attestations = value["attestations"]
    if not isinstance(raw_attestations, list) or not raw_attestations:
        raise ProtectedRuntimeFieldError(f"{label}.attestations: must be a non-empty list")
    if len(raw_attestations) > MAX_ATTESTATIONS:
        raise ProtectedRuntimeFieldError(f"{label}.attestations: exceeds {MAX_ATTESTATIONS} entries")
    attestations: list[str] = []
    for index, raw in enumerate(raw_attestations):
        path = _require_prefixed_path(
            raw, field=f"{label}.attestations[{index}]", prefix=ATTESTATION_PATH_PREFIX,
        )
        attestations.append(path)
    if len(set(attestations)) != len(attestations):
        raise ProtectedRuntimeFieldError(f"{label}.attestations: contains a duplicate path")

    return ProtectedRuntimeField(
        generation=generation,
        descriptor_path=descriptor_path,
        descriptor_sha256=descriptor_sha256,
        minimum_bootstrap_protocol_version=minimum_bootstrap_protocol_version,
        runtime_version=runtime_version,
        manifest_protocol_version=manifest_protocol_version,
        supported_wire_protocols=tuple(wire),
        attestations=tuple(attestations),
    )
