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
UPDATER_PROTOCOL_VERSION = 1

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
})

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
    )


def sha256_hex(content: bytes) -> str:
    """Shared helper so every caller that needs to compute/compare a
    requirements-file digest (manifest authoring tooling, cross_check.py)
    uses the identical algorithm -- sha256 of the raw file bytes,
    nothing normalized/stripped, so authoring and checking can never
    silently disagree about whitespace handling."""
    return hashlib.sha256(content).hexdigest()
