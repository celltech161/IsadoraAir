"""Release manifest schema + structural validation -- [P0] 1.1 Phase A.

Pure Python. No Django ORM import, no `git`/`subprocess` call, no
filesystem write, ever. This is deliberate, not just tidy: per the
architecture-review correction (`ARCHITECTURE_REPORT.md` addendum,
"2.1 privileged updater design"), the future Phase B privileged
executor must run from a root-owned, application-*unwritable*
installation -- it cannot import `updatecenter.*` out of the
Gunicorn-writable checkout/venv at all. Keeping this module free of
any Django/ORM/web dependency means its logic (what a valid manifest
looks like) can be vendored or reimplemented independently on the
privileged side without dragging Django in, and means Phase A's own
tests can exercise every validation rule with zero database, zero
filesystem fixture beyond a literal dict, and zero mocking.

This module answers ONLY "is this manifest, taken alone, internally
well-formed" -- syntax, types, enums, cross-field consistency within
one manifest. It deliberately does NOT check the manifest against the
actual repository (does the named migration exist? does the named
systemd unit have a `deploy/` template? does the requirements hash
match `requirements.txt`?) -- that's `release_chain.py`'s job
(release-CHAIN-level structural checks: duplicate/missing/cyclic
release ids) and `cross_check.py`'s job (repo-REALITY checks). See
`docs/UPDATE_CENTER.md`'s "machine-verifiable facts vs. release-author
intent" section for why this three-way split exists rather than one
big validator.

## Why there is no self-referential commit-SHA field

A manifest committed as part of commit X cannot embed X's own SHA --
the SHA is a hash of the commit's content, which would include the
embedded SHA, which would change the SHA. This is not a subtle
implementation detail; it's a genuine impossibility, and earlier design
work briefly proposed exactly this before catching it.

The fix is not "compute it some other way" -- it's that this manifest
format never needs the association at all. A release's commit identity
is discovered EXTERNALLY, by whichever commit's tree first introduces
(or currently contains) `deploy/releases/<release_id>.json` --
`release_chain.py`'s `resolve_release_commit()` does this via `git log
--diff-filter=A`, never by reading a field out of the JSON.

The one narrow exception is `bootstrap_commit`, valid ONLY on the
single release whose `previous_release_id` is null. That field names a
DIFFERENT, already-immutable, pre-manifest-era commit (this project's
actual production baseline the day manifests were introduced) -- not
the commit the manifest itself lives in. See `BOOTSTRAP_RELEASE`
validation below for exactly what's enforced.
"""
from __future__ import annotations

import dataclasses
import hashlib
import re

SCHEMA_VERSION = 1

# Bumped only if a manifest's *meaning* changes in a way old planning
# code could misinterpret (not just "a new optional field was added").
# A manifest whose minimum_updater_protocol_version exceeds what THIS
# code understands must be refused, never best-effort-interpreted --
# see validate_manifest_dict's UNSUPPORTED_PROTOCOL check.
#
# 3 -> 4: systemd_units_new_required's EXECUTION semantics changed --
# a required unit is no longer unconditionally `enable --now`d. The
# protected updater now looks up each declared unit's activation
# policy in a closed, protected-runtime-compiled map
# (isadoraair_updater.release.MANAGED_UNIT_POLICIES): ENABLE_NOW
# (existing behavior, unchanged) or INSTALL_ONLY (installed + daemon-
# reloaded, but never enabled/started -- for a timer-triggered
# companion .service). The manifest schema/fields are unchanged; a
# release requiring this new interpretation must declare
# minimum_updater_protocol_version=4 so an updater still running
# protocol 3 code refuses it (UPDATER_UPGRADE_REQUIRED) instead of
# silently enable --now-ing a companion .service that was only ever
# meant to be installed. See docs/UPDATE_CENTER.md.
#
# 4 -> 5 (Update Center Phase D, D3): protected_runtime (already
# parseable since D1) now has real execution meaning -- a release that
# declares it requires the D3 runtime-handoff pipeline, which a
# protocol-4 worker cannot perform. Independently mirrors deploy/
# updater_runtime/isadoraair_updater/__init__.py's own MANIFEST_
# PROTOCOL_VERSION bump; see that module's own docstring for the full
# reasoning and test_phase_d3_version_bridge.py for the cross-copy
# lockstep proof.
UPDATER_PROTOCOL_VERSION = 5

RELEASE_ID_PATTERN = re.compile(r"^r[0-9]{4,}$")
# "app_label.migration_name", matching Django's own migration-name
# shape (a leading zero-padded sequence number, then a slug) closely
# enough to catch typos without hardcoding every real migration name
# here -- exact existence is release_chain.py's/cross_check.py's job,
# not this module's.
MIGRATION_REF_PATTERN = re.compile(
    r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$"
)
# Debian/apt source-package-name shape (lowercase alnum + . + - +).
APT_PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
# Matches this project's actual unit-file basenames on disk
# (`deploy/isadoraair-engine.service`, `deploy/wx-alert-beep.timer`,
# `deploy/syndicated-fsn.service`, ...) -- deliberately permissive
# about the PREFIX (this repo owns several non-`isadoraair-`-prefixed
# families: `syndicated-*`, `wx-*`, `ogremote-*`) but strict about the
# SUFFIX (`.service` or `.timer` only -- nothing else is a systemd
# unit type this project ever installs) and shape (no path separators,
# no `..`, no leading dot) so a manifest can never smuggle an
# arbitrary filesystem path or unit outside this project's own naming
# through this field. Existence against a REAL `deploy/<name>` file is
# cross_check.py's job -- this pattern alone does not prove the unit is
# real, only that its name has the right shape to possibly be one.
UNIT_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(service|timer)$")

MIGRATION_COMPATIBILITY_VALUES = frozenset({"additive", "destructive"})

# Fixed, closed set -- NOT "anything named services_requiring_restart
# says," matching the architecture report's explicit "known service
# names" requirement. Adding a 6th core service to this project is a
# real, rare, reviewable event; this set is deliberately not derived
# from scanning `deploy/*.service` (that would accept any of the 100+
# optional/companion units here too, which must never be silently
# eligible for an unattended restart).
CORE_RESTARTABLE_SERVICES = frozenset({
    "isadoraair-gunicorn",
    "isadoraair-engine",
    "isadoraair-encoders",
    "isadoraair-monitoring",
    "isadoraair-rbds",
})

# Fields that look like they might be "helpful" additions but were
# explicitly evaluated and rejected in architecture review -- listed
# individually (not just "unknown field") so a manifest author gets a
# specific, actionable error ("hooks are not supported by design," not
# a generic "unknown key") rather than having to reverse-engineer why.
FORBIDDEN_FIELDS = {
    "pre_update_hooks": "arbitrary executable hooks are rejected by design (see ARCHITECTURE_REPORT.md addendum §3 'manifest philosophy') -- a release cannot ship code for the updater to run, only declarative facts",
    "post_update_hooks": "arbitrary executable hooks are rejected by design (same as pre_update_hooks)",
    "hooks": "arbitrary executable hooks are rejected by design (same as pre_update_hooks)",
    "commands": "arbitrary command arrays are rejected by design (same as pre_update_hooks)",
    "shell": "arbitrary shell invocation is rejected by design (same as pre_update_hooks)",
    "script": "arbitrary script paths are rejected by design (same as pre_update_hooks)",
    "exec": "arbitrary exec targets are rejected by design (same as pre_update_hooks)",
    "release_commit": "a manifest must never embed the SHA of the commit it is part of (self-referential -- the SHA depends on the file's own content); see this module's own docstring",
    "commit": "same as release_commit -- see this module's own docstring",
    "sha": "same as release_commit -- see this module's own docstring",
    "git_sha": "same as release_commit -- see this module's own docstring",
}

# Every field a manifest is allowed to declare. Anything else -- not
# just the FORBIDDEN_FIELDS above, ANY unrecognized key -- is rejected.
# "Prefer rejecting an unsupported schema over best-effort
# interpretation" is a direct instruction, not just a nice-to-have.
KNOWN_FIELDS = frozenset({
    "schema_version",
    "release_id",
    "previous_release_id",
    "bootstrap_commit",
    "minimum_updater_protocol_version",
    "summary",
    "migrations_required",
    "migration_compatibility",
    "python_requirements_changed",
    "requirements_sha256",
    "apt_packages_new",
    "systemd_units_changed",
    "systemd_units_new_required",
    "systemd_units_new_optional",
    "systemd_units_removed_or_renamed",
    "collectstatic_required",
    "services_requiring_restart",
    "nginx_changed",
    "runtime_components_changed",
    "minimum_supported_release_id",
    "manual_bootstrap_required",
    "protected_runtime",
})

# D1-A (Update Center Phase D): the optional protected-runtime bundle
# reference. Every release through r0025, and r0026's own final manual-
# bootstrap bridge release, must declare this null (or omit it) -- see
# deploy/updater_runtime/protected_bootstrap/manifest_field.py's own
# docstring for the full D0 bridge reasoning. This is an INDEPENDENTLY
# maintained mirror of that module's parse_protected_runtime_field(),
# not an import of it -- this module's own top docstring already
# explains why (no Django-app-side import may ever reach into the
# separately-installed, root-owned protected tree); the two are kept in
# lockstep by test_phase_d_contracts.py's own cross-check, not by
# sharing code.
PROTECTED_RUNTIME_DESCRIPTOR_PREFIX = "deploy/updater_runtime/"
PROTECTED_RUNTIME_ATTESTATION_PREFIX = "deploy/updater_attestations/"
PROTECTED_RUNTIME_MAX_GENERATION = 1_000_000
PROTECTED_RUNTIME_MAX_WIRE_PROTOCOLS = 8
PROTECTED_RUNTIME_MAX_ATTESTATIONS = 16
PROTECTED_RUNTIME_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Same strict relative-path rule protected_bootstrap.descriptor.
# validate_relative_path enforces -- restated independently here for
# the same no-cross-import reason as everything else in this block.
PROTECTED_RUNTIME_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@dataclasses.dataclass(frozen=True)
class ProtectedRuntimeManifestField:
    generation: int
    descriptor_path: str
    descriptor_sha256: str
    minimum_bootstrap_protocol_version: int
    runtime_version: int
    manifest_protocol_version: int
    supported_wire_protocols: tuple[int, ...]
    attestations: tuple[str, ...]


def _validate_protected_runtime_relative_path(value, *, field: str, prefix: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{field}: must be a non-empty string")
    if len(value) > 255:
        raise ManifestError(f"{field}: exceeds 255 characters")
    if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        raise ManifestError(f"{field}: contains an unsupported character")
    if value.startswith("/") or value.endswith("/"):
        raise ManifestError(f"{field}: must be a relative path with no leading/trailing slash")
    segments = value.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ManifestError(f"{field}: contains an empty, '.', or '..' path segment")
    for segment in segments:
        if not PROTECTED_RUNTIME_PATH_SEGMENT_RE.match(segment):
            raise ManifestError(f"{field}: path segment {segment!r} contains an unsupported character")
    if not value.startswith(prefix):
        raise ManifestError(f"{field}: must live under {prefix!r}, got {value!r}")
    return value


def _validate_protected_runtime(value, source_label: str) -> ProtectedRuntimeManifestField | None:
    """value is data.get("protected_runtime") -- None (absent or
    explicit null) is the ordinary-release case and returns None
    immediately, exactly as every release through today's schema
    already behaves with this field simply not existing."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ManifestError(f"{source_label}: protected_runtime must be a JSON object or null")

    known = {
        "generation", "descriptor_path", "descriptor_sha256",
        "minimum_bootstrap_protocol_version", "runtime_version",
        "manifest_protocol_version", "supported_wire_protocols", "attestations",
    }
    unknown = set(value) - known
    if unknown:
        raise ManifestError(f"{source_label}: protected_runtime has unrecognized field(s) {sorted(unknown)!r}")
    missing = known - set(value)
    if missing:
        raise ManifestError(f"{source_label}: protected_runtime is missing field(s) {sorted(missing)!r}")

    def positive_int(field_name, *, maximum=None):
        raw = value[field_name]
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
            raise ManifestError(f"{source_label}: protected_runtime.{field_name} must be a positive integer")
        if maximum is not None and raw > maximum:
            raise ManifestError(f"{source_label}: protected_runtime.{field_name} exceeds maximum {maximum}")
        return raw

    generation = positive_int("generation", maximum=PROTECTED_RUNTIME_MAX_GENERATION)
    minimum_bootstrap_protocol_version = positive_int("minimum_bootstrap_protocol_version")
    runtime_version = positive_int("runtime_version")
    manifest_protocol_version = positive_int("manifest_protocol_version")

    descriptor_path = _validate_protected_runtime_relative_path(
        value["descriptor_path"], field=f"{source_label}: protected_runtime.descriptor_path",
        prefix=PROTECTED_RUNTIME_DESCRIPTOR_PREFIX,
    )

    descriptor_sha256 = value["descriptor_sha256"]
    if not isinstance(descriptor_sha256, str) or not PROTECTED_RUNTIME_SHA256_RE.match(descriptor_sha256):
        raise ManifestError(f"{source_label}: protected_runtime.descriptor_sha256 must be exactly 64 lowercase hex characters")

    wire = value["supported_wire_protocols"]
    if not isinstance(wire, list) or not wire:
        raise ManifestError(f"{source_label}: protected_runtime.supported_wire_protocols must be a non-empty list")
    if len(wire) > PROTECTED_RUNTIME_MAX_WIRE_PROTOCOLS:
        raise ManifestError(f"{source_label}: protected_runtime.supported_wire_protocols exceeds {PROTECTED_RUNTIME_MAX_WIRE_PROTOCOLS} entries")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in wire):
        raise ManifestError(f"{source_label}: protected_runtime.supported_wire_protocols must contain only positive integers")
    if len(set(wire)) != len(wire):
        raise ManifestError(f"{source_label}: protected_runtime.supported_wire_protocols contains a duplicate")
    if list(wire) != sorted(wire):
        raise ManifestError(f"{source_label}: protected_runtime.supported_wire_protocols must be in canonical ascending order")

    raw_attestations = value["attestations"]
    if not isinstance(raw_attestations, list) or not raw_attestations:
        raise ManifestError(f"{source_label}: protected_runtime.attestations must be a non-empty list")
    if len(raw_attestations) > PROTECTED_RUNTIME_MAX_ATTESTATIONS:
        raise ManifestError(f"{source_label}: protected_runtime.attestations exceeds {PROTECTED_RUNTIME_MAX_ATTESTATIONS} entries")
    attestations = tuple(
        _validate_protected_runtime_relative_path(
            raw, field=f"{source_label}: protected_runtime.attestations[{index}]",
            prefix=PROTECTED_RUNTIME_ATTESTATION_PREFIX,
        )
        for index, raw in enumerate(raw_attestations)
    )
    if len(set(attestations)) != len(attestations):
        raise ManifestError(f"{source_label}: protected_runtime.attestations contains a duplicate path")

    return ProtectedRuntimeManifestField(
        generation=generation,
        descriptor_path=descriptor_path,
        descriptor_sha256=descriptor_sha256,
        minimum_bootstrap_protocol_version=minimum_bootstrap_protocol_version,
        runtime_version=runtime_version,
        manifest_protocol_version=manifest_protocol_version,
        supported_wire_protocols=tuple(wire),
        attestations=attestations,
    )


SUMMARY_MAX_LEN = 500


class ManifestError(ValueError):
    """Raised for any structurally-invalid manifest. Always carries a
    human-readable, non-secret message safe to show an operator or log
    verbatim -- see docs/UPDATE_CENTER.md's logging-sanitization note."""


@dataclasses.dataclass(frozen=True)
class ReleaseManifest:
    """Validated, immutable in-memory representation of one release
    manifest. Only ever constructed by validate_manifest_dict() below
    -- there is no public constructor that skips validation, so
    holding a ReleaseManifest instance anywhere in this codebase is
    itself proof it passed every structural check."""
    schema_version: int
    release_id: str
    previous_release_id: str | None
    bootstrap_commit: str | None
    minimum_updater_protocol_version: int
    summary: str
    migrations_required: tuple[str, ...]
    migration_compatibility: str | None
    python_requirements_changed: bool
    requirements_sha256: str | None
    apt_packages_new: tuple[str, ...]
    systemd_units_changed: tuple[str, ...]
    systemd_units_new_required: tuple[str, ...]
    systemd_units_new_optional: tuple[str, ...]
    systemd_units_removed_or_renamed: tuple[str, ...]
    collectstatic_required: bool
    services_requiring_restart: tuple[str, ...]
    nginx_changed: bool
    runtime_components_changed: bool
    minimum_supported_release_id: str | None
    manual_bootstrap_required: bool
    protected_runtime: ProtectedRuntimeManifestField | None

    @property
    def is_bootstrap(self) -> bool:
        """The single release with no predecessor -- see this module's
        own top docstring for what bootstrap_commit means for it."""
        return self.previous_release_id is None


def _require_type(value, expected_type, field_name):
    if not isinstance(value, expected_type) or isinstance(value, bool) and expected_type is not bool:
        raise ManifestError(f"{field_name!r}: expected {expected_type.__name__}, got {type(value).__name__}")


def _require_str_list(value, field_name, pattern=None):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ManifestError(f"{field_name!r}: expected a list of strings")
    if len(set(value)) != len(value):
        raise ManifestError(f"{field_name!r}: contains a duplicate entry")
    if pattern is not None:
        for v in value:
            if not pattern.match(v):
                raise ManifestError(f"{field_name!r}: {v!r} does not match the required shape")
    return tuple(value)


def validate_manifest_dict(data: dict, *, source_label: str = "<manifest>") -> ReleaseManifest:
    """The one entry point. Raises ManifestError with a specific,
    actionable message on any structural problem; returns a validated
    ReleaseManifest otherwise. `source_label` is only used to make
    error messages identify which file/object was being checked when
    several are validated in a batch (see release_chain.py) -- never
    interpreted, never a path this function itself opens."""
    if not isinstance(data, dict):
        raise ManifestError(f"{source_label}: manifest must be a JSON object, got {type(data).__name__}")

    unknown = set(data.keys()) - KNOWN_FIELDS
    for field in sorted(unknown):
        if field in FORBIDDEN_FIELDS:
            raise ManifestError(f"{source_label}: field {field!r} is rejected -- {FORBIDDEN_FIELDS[field]}")
    if unknown:
        raise ManifestError(
            f"{source_label}: unrecognized field(s) {sorted(unknown)!r} -- "
            f"unsupported schema is rejected rather than best-effort interpreted"
        )

    missing_required = {
        "schema_version", "release_id", "previous_release_id",
        "minimum_updater_protocol_version", "migrations_required",
        "python_requirements_changed", "apt_packages_new",
        "systemd_units_changed", "systemd_units_new_required",
        "systemd_units_new_optional", "collectstatic_required",
        "services_requiring_restart", "nginx_changed",
        "runtime_components_changed",
    } - set(data.keys())
    if missing_required:
        raise ManifestError(f"{source_label}: missing required field(s) {sorted(missing_required)!r}")

    schema_version = data["schema_version"]
    _require_type(schema_version, int, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ManifestError(
            f"{source_label}: schema_version {schema_version} is not the supported "
            f"version ({SCHEMA_VERSION}) -- refusing rather than guessing at an "
            f"unknown schema's meaning"
        )

    min_protocol = data["minimum_updater_protocol_version"]
    _require_type(min_protocol, int, "minimum_updater_protocol_version")
    if min_protocol > UPDATER_PROTOCOL_VERSION:
        raise ManifestError(
            f"{source_label}: requires updater protocol {min_protocol}, this "
            f"updater only understands up to {UPDATER_PROTOCOL_VERSION} -- refusing"
        )
    if min_protocol < 1:
        raise ManifestError(f"{source_label}: minimum_updater_protocol_version must be >= 1")

    release_id = data["release_id"]
    _require_type(release_id, str, "release_id")
    if not RELEASE_ID_PATTERN.match(release_id):
        raise ManifestError(f"{source_label}: release_id {release_id!r} does not match required pattern r####...")

    previous_release_id = data["previous_release_id"]
    if previous_release_id is not None:
        _require_type(previous_release_id, str, "previous_release_id")
        if not RELEASE_ID_PATTERN.match(previous_release_id):
            raise ManifestError(f"{source_label}: previous_release_id {previous_release_id!r} does not match required pattern")
        if previous_release_id == release_id:
            raise ManifestError(f"{source_label}: previous_release_id cannot equal release_id (self-predecessor)")

    bootstrap_commit = data.get("bootstrap_commit")
    if previous_release_id is None:
        # This IS the bootstrap release -- bootstrap_commit is required.
        if bootstrap_commit is None:
            raise ManifestError(
                f"{source_label}: is a bootstrap release (previous_release_id is null) "
                f"but has no bootstrap_commit -- every release chain needs exactly one "
                f"anchor to a real, already-immutable commit"
            )
        _require_type(bootstrap_commit, str, "bootstrap_commit")
        if not re.fullmatch(r"[0-9a-f]{40}", bootstrap_commit):
            raise ManifestError(f"{source_label}: bootstrap_commit must be a full 40-character lowercase hex SHA")
    else:
        # NOT the bootstrap release -- bootstrap_commit must be absent.
        # This is the enforcement half of "no self-referential commit
        # SHA": every non-bootstrap release's commit identity comes
        # ONLY from git history (release_chain.resolve_release_commit),
        # never from a field in the file.
        if bootstrap_commit is not None:
            raise ManifestError(
                f"{source_label}: bootstrap_commit is only valid on the bootstrap "
                f"release (previous_release_id null) -- a non-bootstrap release's "
                f"commit identity must be discovered from git history, never embedded"
            )

    summary = data.get("summary", "")
    _require_type(summary, str, "summary")
    if len(summary) > SUMMARY_MAX_LEN:
        raise ManifestError(f"{source_label}: summary exceeds {SUMMARY_MAX_LEN} characters")

    migrations_required = _require_str_list(data["migrations_required"], "migrations_required", MIGRATION_REF_PATTERN)

    migration_compatibility = data.get("migration_compatibility")
    if migrations_required:
        if migration_compatibility not in MIGRATION_COMPATIBILITY_VALUES:
            raise ManifestError(
                f"{source_label}: migrations_required is non-empty but "
                f"migration_compatibility is {migration_compatibility!r} -- must be "
                f"one of {sorted(MIGRATION_COMPATIBILITY_VALUES)!r}"
            )
    elif migration_compatibility is not None:
        raise ManifestError(f"{source_label}: migration_compatibility must be null when migrations_required is empty")

    python_requirements_changed = data["python_requirements_changed"]
    _require_type(python_requirements_changed, bool, "python_requirements_changed")
    requirements_sha256 = data.get("requirements_sha256")
    if python_requirements_changed:
        if not isinstance(requirements_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", requirements_sha256):
            raise ManifestError(
                f"{source_label}: python_requirements_changed is true but "
                f"requirements_sha256 is missing/malformed -- a bare boolean is not "
                f"a strong enough contract on its own (see docs/UPDATE_CENTER.md)"
            )
    elif requirements_sha256 is not None:
        raise ManifestError(f"{source_label}: requirements_sha256 must be null when python_requirements_changed is false")

    apt_packages_new = _require_str_list(data["apt_packages_new"], "apt_packages_new", APT_PACKAGE_PATTERN)

    systemd_units_changed = _require_str_list(data["systemd_units_changed"], "systemd_units_changed", UNIT_NAME_PATTERN)
    systemd_units_new_required = _require_str_list(data["systemd_units_new_required"], "systemd_units_new_required", UNIT_NAME_PATTERN)
    systemd_units_new_optional = _require_str_list(data["systemd_units_new_optional"], "systemd_units_new_optional", UNIT_NAME_PATTERN)
    systemd_units_removed_or_renamed = _require_str_list(
        data.get("systemd_units_removed_or_renamed", []), "systemd_units_removed_or_renamed", UNIT_NAME_PATTERN)

    all_declared_units = (
        set(systemd_units_changed) | set(systemd_units_new_required)
        | set(systemd_units_new_optional) | set(systemd_units_removed_or_renamed)
    )
    if len(all_declared_units) != (
        len(systemd_units_changed) + len(systemd_units_new_required)
        + len(systemd_units_new_optional) + len(systemd_units_removed_or_renamed)
    ):
        raise ManifestError(f"{source_label}: the same unit name appears in more than one systemd_units_* list")

    collectstatic_required = data["collectstatic_required"]
    _require_type(collectstatic_required, bool, "collectstatic_required")

    services_requiring_restart = _require_str_list(data["services_requiring_restart"], "services_requiring_restart")
    unknown_services = set(services_requiring_restart) - CORE_RESTARTABLE_SERVICES
    if unknown_services:
        raise ManifestError(
            f"{source_label}: services_requiring_restart contains unknown service(s) "
            f"{sorted(unknown_services)!r} -- only {sorted(CORE_RESTARTABLE_SERVICES)!r} are recognized"
        )

    nginx_changed = data["nginx_changed"]
    _require_type(nginx_changed, bool, "nginx_changed")

    runtime_components_changed = data["runtime_components_changed"]
    _require_type(runtime_components_changed, bool, "runtime_components_changed")

    minimum_supported_release_id = data.get("minimum_supported_release_id")
    if minimum_supported_release_id is not None:
        _require_type(minimum_supported_release_id, str, "minimum_supported_release_id")
        if not RELEASE_ID_PATTERN.match(minimum_supported_release_id):
            raise ManifestError(f"{source_label}: minimum_supported_release_id does not match required pattern")

    manual_bootstrap_required = data.get("manual_bootstrap_required", False)
    _require_type(manual_bootstrap_required, bool, "manual_bootstrap_required")

    protected_runtime = _validate_protected_runtime(data.get("protected_runtime"), source_label)

    return ReleaseManifest(
        schema_version=schema_version,
        release_id=release_id,
        previous_release_id=previous_release_id,
        bootstrap_commit=bootstrap_commit,
        minimum_updater_protocol_version=min_protocol,
        summary=summary,
        migrations_required=migrations_required,
        migration_compatibility=migration_compatibility,
        python_requirements_changed=python_requirements_changed,
        requirements_sha256=requirements_sha256,
        apt_packages_new=apt_packages_new,
        systemd_units_changed=systemd_units_changed,
        systemd_units_new_required=systemd_units_new_required,
        systemd_units_new_optional=systemd_units_new_optional,
        systemd_units_removed_or_renamed=systemd_units_removed_or_renamed,
        collectstatic_required=collectstatic_required,
        services_requiring_restart=services_requiring_restart,
        nginx_changed=nginx_changed,
        runtime_components_changed=runtime_components_changed,
        minimum_supported_release_id=minimum_supported_release_id,
        manual_bootstrap_required=manual_bootstrap_required,
        protected_runtime=protected_runtime,
    )


def sha256_hex(content: bytes) -> str:
    """Shared helper so every caller that needs to compute/compare a
    requirements-file digest (manifest authoring tooling, cross_check.py)
    uses the identical algorithm -- sha256 of the raw file bytes,
    nothing normalized/stripped, so authoring and checking can never
    silently disagree about whitespace handling."""
    return hashlib.sha256(content).hexdigest()
