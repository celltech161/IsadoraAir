"""Trusted companion migration authorization (P1 1.17, protected runtime 11).

Release MANIFESTS never reference this. A runtime-10 worker parses only
deploy/releases/*.json and its predecessor-diff rules never match this
directory, so it neither needs nor sees these files: every manifest stays
manifest-protocol 5 and the whole chain stays runtime-10 parseable.

Runtime 11 discovers, by path convention, at most one companion per release:

    deploy/migration_authorizations/<release_id>.json

A companion is a later, metadata-only, immutable artifact. It must be:

* added exactly once on the trusted tip's history, strictly after the
  release's own introducing commit, and never modified, deleted or
  re-added afterwards (any such history fails closed);
* introduced by a single-parent commit that changes nothing outside
  deploy/migration_authorizations/;
* a closed schema binding release_id, that release's target_commit, and
  the SHA-256 of that release's manifest bytes (computed from Git);
* RELEASE-LOCAL: every authorized operation belongs to a migration that
  release declares in migrations_required, pinned to that migration's exact
  file bytes at the release commit.

It inherits the trust of the root-configured, fast-forward-only trusted
repository; it carries no signature of its own. Companions are never part
of the trusted-plan fingerprint (a runtime-10 initiator knows nothing about
them) and never add a migration or operation to any plan: they can only
satisfy the manual-operation approval gate for operations the station's own
plan already contains. updatecenter/migration_authorization.py is Django's
independent mirror of these rules.
"""
from __future__ import annotations

import hashlib
import json
import re

from .release import (
    APP_MIGRATION_PATHS, MIGRATION_RE, RELEASE_DIR, ChainEntry, ReleaseError, TrustedPlan,
    TrustedRepository, load_chain,
)

AUTHORIZATION_DIR = "deploy/migration_authorizations"
MAX_COMPANION_BYTES = 262144
MAX_AUTHORIZED_OPERATIONS = 1000
_FIELDS = frozenset({
    "schema_version", "release_id", "target_commit", "manifest_sha256", "authorized_manual_operations",
})
_OPERATION_FIELDS = frozenset({
    "ref", "migration_file_sha256", "operation_index", "operation", "classification",
})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")


def companion_path(release_id: str) -> str:
    return f"{AUTHORIZATION_DIR}/{release_id}.json"


def migration_file_path(ref: str) -> str:
    app, name = ref.split(".", 1)
    return f"{APP_MIGRATION_PATHS.get(app, app)}/migrations/{name}.py"


def manual_operation_identity(operation: dict, migration_file_sha256: str) -> tuple:
    """Exact reviewed identity: migration ref, exact source bytes, exact
    operation position, operation type, classification. Free-text detail
    is derived from those bytes and is never matched."""
    return (
        operation["ref"], migration_file_sha256, operation["operation_index"],
        operation["operation"], operation["classification"],
    )


def parse_companion(raw: bytes) -> dict:
    """Closed-schema parse. Returns {"release_id", "target_commit",
    "manifest_sha256", "operations": [entry, ...]}; raises ReleaseError."""
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReleaseError("migration authorization companion is not valid UTF-8 JSON") from exc
    if not isinstance(record, dict) or set(record) != _FIELDS or record.get("schema_version") != 1:
        raise ReleaseError("migration authorization companion has an invalid closed schema")
    if (not isinstance(record["release_id"], str) or not isinstance(record["target_commit"], str)
            or not _HEX40.fullmatch(record["target_commit"])
            or not isinstance(record["manifest_sha256"], str) or not _HEX64.fullmatch(record["manifest_sha256"])):
        raise ReleaseError("migration authorization companion identity fields are invalid")
    operations = record["authorized_manual_operations"]
    if not isinstance(operations, list) or not operations or len(operations) > MAX_AUTHORIZED_OPERATIONS:
        raise ReleaseError("migration authorization companion must list 1..1000 manual operations")
    seen = set()
    for entry in operations:
        if not isinstance(entry, dict) or set(entry) != _OPERATION_FIELDS:
            raise ReleaseError("migration authorization operation has an invalid closed schema")
        index = entry["operation_index"]
        if (not isinstance(entry["ref"], str) or not MIGRATION_RE.fullmatch(entry["ref"])
                or not isinstance(entry["migration_file_sha256"], str)
                or not _HEX64.fullmatch(entry["migration_file_sha256"])
                or isinstance(index, bool) or not isinstance(index, int) or index < 0
                or not isinstance(entry["operation"], str) or not _OPERATION_NAME_RE.fullmatch(entry["operation"])
                or entry["classification"] != "manual"):
            raise ReleaseError("migration authorization operation identity is invalid")
        identity = manual_operation_identity(entry, entry["migration_file_sha256"])
        if identity in seen:
            raise ReleaseError("migration authorization companion lists a duplicate operation")
        seen.add(identity)
    return {
        "release_id": record["release_id"], "target_commit": record["target_commit"],
        "manifest_sha256": record["manifest_sha256"], "operations": operations,
    }


def _git_lines(repository: TrustedRepository, args: list[str]) -> list[str]:
    result = repository._run(args)
    if not result.ok:
        raise ReleaseError("trusted repository query for a migration authorization companion failed")
    try:
        return [line for line in result.stdout.decode("ascii", "strict").splitlines() if line]
    except UnicodeDecodeError as exc:
        raise ReleaseError("trusted repository returned a non-ASCII history") from exc


def validate_release_companion(repository: TrustedRepository, trusted_tip: str, entry: ChainEntry) -> frozenset | None:
    """The authorized identities for one chain release, None when it has
    no companion at all, or ReleaseError (fail closed) for any violation."""
    release_id = entry.manifest.release_id
    path = companion_path(release_id)
    if not _HEX40.fullmatch(trusted_tip):
        raise ReleaseError("trusted tip is not a commit SHA")
    # --full-history: a modification on any merged side branch still counts.
    history = _git_lines(repository, ["log", "--full-history", "--format=%H", trusted_tip, "--", path])
    if not history:
        return None
    additions = _git_lines(repository, [
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", trusted_tip, "--", path,
    ])
    if len(additions) != 1 or history != additions:
        raise ReleaseError(f"{path} was modified, deleted or re-added after introduction")
    introducing = additions[0]
    if (repository.is_ancestor(entry.commit, introducing) is not True
            or introducing == entry.commit
            or repository.is_ancestor(introducing, trusted_tip) is not True):
        raise ReleaseError(f"{path} is not introduced strictly after {release_id}'s commit on trusted history")
    parents = _git_lines(repository, ["rev-list", "--parents", "-n", "1", introducing])
    if len(parents) != 1 or len(parents[0].split()) != 2:
        raise ReleaseError(f"{path} must be introduced by a single-parent commit")
    changed = _git_lines(repository, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", introducing])
    if not changed or any(not item.startswith(f"{AUTHORIZATION_DIR}/") for item in changed):
        raise ReleaseError(f"{path} must be introduced by a metadata-only commit")
    raw = repository.read_file(trusted_tip, path, maximum=MAX_COMPANION_BYTES)
    if raw is None:
        raise ReleaseError(f"{path} could not be read from the trusted tip")
    record = parse_companion(raw)
    manifest = repository.read_file(entry.commit, f"{RELEASE_DIR}/{release_id}.json", maximum=65536)
    if (manifest is None or record["release_id"] != release_id or record["target_commit"] != entry.commit
            or record["manifest_sha256"] != hashlib.sha256(manifest).hexdigest()):
        raise ReleaseError(f"{path} does not bind {release_id}'s exact release commit and manifest")
    declared = set(entry.manifest.migrations_required)
    identities = set()
    for operation in record["operations"]:
        if operation["ref"] not in declared:
            raise ReleaseError(f"{path} authorizes {operation['ref']}, which {release_id} did not introduce")
        source = repository.read_file(entry.commit, migration_file_path(operation["ref"]))
        if source is None or hashlib.sha256(source).hexdigest() != operation["migration_file_sha256"]:
            raise ReleaseError(f"{path} does not match {operation['ref']}'s exact bytes at {release_id}")
        identities.add(manual_operation_identity(operation, operation["migration_file_sha256"]))
    return frozenset(identities)


def load_plan_authorizations(repository: TrustedRepository, trusted_tip: str, plan: TrustedPlan) -> dict[str, frozenset]:
    """Validated companions of every release in the plan, keyed by release."""
    chain = {entry.manifest.release_id: entry for entry in load_chain(repository, trusted_tip)}
    authorizations = {}
    for release_id in plan.releases_in_plan:
        entry = chain.get(release_id)
        if entry is None:
            raise ReleaseError(f"plan release {release_id} is absent from the trusted chain")
        identities = validate_release_companion(repository, trusted_tip, entry)
        if identities is not None:
            authorizations[release_id] = identities
    return authorizations


def central_authorization_covers(authorizations: dict[str, frozenset], *, plan_items: list[dict],
                                 manual_operations: list[dict]) -> bool:
    """True only when EVERY manual operation in the station's own derived
    plan is in the union of the plan releases' authorized sets."""
    if not manual_operations or not authorizations:
        return False
    union = frozenset().union(*authorizations.values())
    file_digests = {item["ref"]: item["migration_file_sha256"] for item in plan_items}
    for operation in manual_operations:
        digest = file_digests.get(operation["ref"])
        if digest is None or manual_operation_identity(operation, digest) not in union:
            return False
    return True
