"""Django's independent mirror of protected runtime 11's companion rules
(deploy/updater_runtime/isadoraair_updater/migration_authorization.py).

Used by `manage.py validate_release_manifests` so an unsafe companion is
caught before publication. The two implementations must agree on every
accept/reject decision (see test_migration_authorization_companions.py).

    deploy/migration_authorizations/<release_id>.json

* exact path for a release in the chain; nothing else may live there;
* added exactly once, strictly after that release's introducing commit, on
  the canonical history; never modified, deleted or re-added;
* introduced by a single-parent, metadata-only commit;
* closed schema binding release_id, the release commit, and the SHA-256 of
  the release manifest's committed bytes;
* release-local: each authorized operation's migration is declared by that
  release and pinned to its exact bytes at the release commit.

Release manifests never reference a companion and stay manifest-protocol 5.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import git_adapter

AUTHORIZATION_DIR = "deploy/migration_authorizations"
MAX_AUTHORIZED_OPERATIONS = 1000
_FIELDS = frozenset({
    "schema_version", "release_id", "target_commit", "manifest_sha256", "authorized_manual_operations",
})
_OPERATION_FIELDS = frozenset({
    "ref", "migration_file_sha256", "operation_index", "operation", "classification",
})
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_MIGRATION_REF = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
_OPERATION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_COMPANION_NAME = re.compile(r"^(r[0-9]{4,})\.json$")


class CompanionError(ValueError):
    """A companion artifact violates the trusted companion contract."""


def companion_path(release_id: str) -> str:
    return f"{AUTHORIZATION_DIR}/{release_id}.json"


def _lines(checkout_root: Path, args: list[str]) -> list[str]:
    result = git_adapter.run_git(args, checkout_root)
    if not result.ok:
        raise CompanionError(f"git {args[0]} failed while validating a migration authorization companion")
    return [line for line in result.stdout.splitlines() if line]


def parse_companion(raw: bytes) -> dict:
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CompanionError("companion is not valid UTF-8 JSON") from exc
    if not isinstance(record, dict) or set(record) != _FIELDS or record.get("schema_version") != 1:
        raise CompanionError("companion has an invalid closed schema")
    if (not isinstance(record["release_id"], str) or not isinstance(record["target_commit"], str)
            or not _HEX40.fullmatch(record["target_commit"])
            or not isinstance(record["manifest_sha256"], str) or not _HEX64.fullmatch(record["manifest_sha256"])):
        raise CompanionError("companion identity fields are invalid")
    operations = record["authorized_manual_operations"]
    if not isinstance(operations, list) or not operations or len(operations) > MAX_AUTHORIZED_OPERATIONS:
        raise CompanionError("companion must list 1..1000 manual operations")
    seen = set()
    for entry in operations:
        if not isinstance(entry, dict) or set(entry) != _OPERATION_FIELDS:
            raise CompanionError("companion operation has an invalid closed schema")
        index = entry["operation_index"]
        if (not isinstance(entry["ref"], str) or not _MIGRATION_REF.fullmatch(entry["ref"])
                or not isinstance(entry["migration_file_sha256"], str)
                or not _HEX64.fullmatch(entry["migration_file_sha256"])
                or isinstance(index, bool) or not isinstance(index, int) or index < 0
                or not isinstance(entry["operation"], str) or not _OPERATION_NAME.fullmatch(entry["operation"])
                or entry["classification"] != "manual"):
            raise CompanionError("companion operation identity is invalid")
        identity = (entry["ref"], entry["migration_file_sha256"], index, entry["operation"], entry["classification"])
        if identity in seen:
            raise CompanionError("companion lists a duplicate operation")
        seen.add(identity)
    return record


def validate_release_companion(checkout_root: Path, tip: str, *, release_id: str, release_commit: str,
                               migrations_required, app_label_paths: dict | None = None) -> frozenset | None:
    """Authorized identities, None when the release has no companion, or
    CompanionError for any violation (mirrors runtime 11 exactly)."""
    path = companion_path(release_id)
    history = _lines(checkout_root, ["log", "--full-history", "--format=%H", tip, "--", path])
    if not history:
        return None
    additions = _lines(checkout_root, [
        "log", "--full-history", "--no-renames", "--diff-filter=A", "--format=%H", tip, "--", path,
    ])
    if len(additions) != 1 or history != additions:
        raise CompanionError(f"{path} was modified, deleted or re-added after introduction")
    introducing = additions[0]
    if (git_adapter.is_ancestor(checkout_root, release_commit, introducing) is not True
            or introducing == release_commit
            or git_adapter.is_ancestor(checkout_root, introducing, tip) is not True):
        raise CompanionError(f"{path} is not introduced strictly after {release_id}'s commit on trusted history")
    parents = _lines(checkout_root, ["rev-list", "--parents", "-n", "1", introducing])
    if len(parents) != 1 or len(parents[0].split()) != 2:
        raise CompanionError(f"{path} must be introduced by a single-parent commit")
    changed = _lines(checkout_root, ["show", "--pretty=format:", "--name-only", "--no-renames", introducing])
    if not changed or any(not item.startswith(f"{AUTHORIZATION_DIR}/") for item in changed):
        raise CompanionError(f"{path} must be introduced by a metadata-only commit")
    raw = git_adapter.read_bytes_at_commit(checkout_root, tip, path)
    if raw is None:
        raise CompanionError(f"{path} could not be read at the canonical tip")
    record = parse_companion(raw)
    manifest = git_adapter.read_bytes_at_commit(checkout_root, release_commit, f"deploy/releases/{release_id}.json")
    if (manifest is None or record["release_id"] != release_id or record["target_commit"] != release_commit
            or record["manifest_sha256"] != hashlib.sha256(manifest).hexdigest()):
        raise CompanionError(f"{path} does not bind {release_id}'s exact release commit and manifest")
    declared = set(migrations_required)
    paths = app_label_paths or {}
    identities = set()
    for entry in record["authorized_manual_operations"]:
        if entry["ref"] not in declared:
            raise CompanionError(f"{path} authorizes {entry['ref']}, which {release_id} did not introduce")
        app, name = entry["ref"].split(".", 1)
        source = git_adapter.read_bytes_at_commit(
            checkout_root, release_commit, f"{paths.get(app, app)}/migrations/{name}.py",
        )
        if source is None or hashlib.sha256(source).hexdigest() != entry["migration_file_sha256"]:
            raise CompanionError(f"{path} does not match {entry['ref']}'s exact bytes at {release_id}")
        identities.add((entry["ref"], entry["migration_file_sha256"], entry["operation_index"],
                        entry["operation"], entry["classification"]))
    return frozenset(identities)


def orphan_companions(checkout_root: Path, tip: str, release_ids) -> list[str]:
    """Files under the companion directory that are not <release_id>.json
    for a release in the chain."""
    listed = git_adapter.list_files_at_commit(checkout_root, tip, AUTHORIZATION_DIR) or []
    known = set(release_ids)
    orphans = []
    for name in listed:     # basenames; a subdirectory appears by name and is an orphan
        match = _COMPANION_NAME.fullmatch(name)
        if not match or match.group(1) not in known:
            orphans.append(name)
    return orphans
