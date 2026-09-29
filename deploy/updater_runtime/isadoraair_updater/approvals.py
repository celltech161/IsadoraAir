"""Root-owned, schema-independent reviewed-migration approvals.

The application database cannot store the approval for the migration which
creates its own approval table.  This store deliberately lives beside the
protected updater's job state and derives every approved identity field from a
terminal root-owned Job A record.  Operator input is limited to a confirmation
digest, identity, and audit reason.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .jobs import JobError, JobStore
from .security import assert_root_protected, assert_root_protected_parents


MAX_APPROVAL_RECORDS = 1000
MAX_APPROVAL_BYTES = 256 * 1024
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RELEASE = re.compile(r"^r[0-9]{4,}$")
_MIGRATION = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
_OPERATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,149}$")
_IDENTITY_FIELDS = (
    "target_release_id",
    "target_commit",
    "target_manifest_sha256",
    "migration_plan_digest",
    "trusted_plan_fingerprint",
)
_REVIEW_FIELDS = frozenset({
    "release_id", "target_commit", "manifest_sha256",
    "migration_plan_digest", "trusted_plan_fingerprint",
    "manual_operations",
})
_RECORD_FIELDS = frozenset({
    "schema_version", "approval_id", *_IDENTITY_FIELDS,
    "source_job_id", "manual_operations_snapshot",
    "releases_in_plan_snapshot", "migrations_required_snapshot",
    "approved_by_username", "approved_at", "reason",
})


class ApprovalError(ValueError):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_uuid(value, field: str) -> str:
    if not isinstance(value, str):
        raise ApprovalError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ApprovalError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ApprovalError(f"{field} must use canonical lowercase UUID form")
    return value


def _validate_identity(identity: dict) -> dict:
    if not isinstance(identity, dict) or tuple(identity.keys()) != _IDENTITY_FIELDS:
        # Callers construct this in the canonical order; a closed shape prevents
        # accidental omission when the identity contract evolves.
        raise ApprovalError("approval identity has an invalid field set or order")
    release = identity["target_release_id"]
    commit = identity["target_commit"]
    if not isinstance(release, str) or not _RELEASE.fullmatch(release):
        raise ApprovalError("target_release_id has an invalid shape")
    if not isinstance(commit, str) or not _HEX40.fullmatch(commit):
        raise ApprovalError("target_commit must be lowercase Git SHA-1")
    for field in _IDENTITY_FIELDS[2:]:
        value = identity[field]
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise ApprovalError(f"{field} must be lowercase SHA-256")
    return dict(identity)


def approval_identity(*, target_release_id: str, target_commit: str,
                      target_manifest_sha256: str, migration_plan_digest: str,
                      trusted_plan_fingerprint: str) -> dict:
    return _validate_identity({
        "target_release_id": target_release_id,
        "target_commit": target_commit,
        "target_manifest_sha256": target_manifest_sha256,
        "migration_plan_digest": migration_plan_digest,
        "trusted_plan_fingerprint": trusted_plan_fingerprint,
    })


def _identity_key(identity: dict) -> str:
    canonical = json.dumps(_validate_identity(identity), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_manual_operations(value) -> list[dict]:
    if not isinstance(value, list) or not value or len(value) > 256:
        raise ApprovalError("manual_operations must be a non-empty list of at most 256 entries")
    result = []
    expected = {"ref", "operation_index", "operation", "classification", "detail"}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != expected:
            raise ApprovalError("manual operation has an invalid field set")
        ref = entry["ref"]
        index = entry["operation_index"]
        operation = entry["operation"]
        detail = entry["detail"]
        if not isinstance(ref, str) or not _MIGRATION.fullmatch(ref):
            raise ApprovalError("manual operation migration reference is invalid")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= 4096:
            raise ApprovalError("manual operation index is invalid")
        if entry["classification"] != "manual":
            raise ApprovalError("approval may contain only manual operations")
        if (not isinstance(operation, str) or not operation or len(operation) > 128
                or not isinstance(detail, str) or not detail or len(detail) > 1000
                or any(ch in operation + detail for ch in ("\x00", "\r"))):
            raise ApprovalError("manual operation text is invalid or exceeds its bound")
        result.append(dict(entry))
    return result


def _validate_string_list(value, field: str, *, item_pattern=None, maximum=256) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > maximum:
        raise ApprovalError(f"{field} must be a bounded list")
    result = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 256:
            raise ApprovalError(f"{field} contains an invalid item")
        if item_pattern is not None and not item_pattern.fullmatch(item):
            raise ApprovalError(f"{field} contains an invalid item")
        result.append(item)
    return result


def _validate_record(record: dict, expected_identity: dict | None = None) -> dict:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS or record.get("schema_version") != 1:
        raise ApprovalError("approval record identity/schema mismatch")
    _canonical_uuid(record["approval_id"], "approval_id")
    _canonical_uuid(record["source_job_id"], "source_job_id")
    identity = _validate_identity({field: record[field] for field in _IDENTITY_FIELDS})
    if expected_identity is not None and identity != _validate_identity(expected_identity):
        raise ApprovalError("approval record does not match the independently recomputed identity")
    _validate_manual_operations(record["manual_operations_snapshot"])
    _validate_string_list(record["releases_in_plan_snapshot"], "releases_in_plan_snapshot", item_pattern=_RELEASE)
    _validate_string_list(record["migrations_required_snapshot"], "migrations_required_snapshot", item_pattern=_MIGRATION)
    operator = record["approved_by_username"]
    reason = record["reason"]
    if not isinstance(operator, str) or not _OPERATOR.fullmatch(operator):
        raise ApprovalError("approved_by_username has an invalid shape")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
            or any(ch in reason for ch in ("\x00", "\r"))):
        raise ApprovalError("approval reason is empty, invalid, or exceeds 2000 characters")
    approved_at = record["approved_at"]
    if not isinstance(approved_at, str) or len(approved_at) > 64:
        raise ApprovalError("approved_at is invalid")
    try:
        parsed = dt.datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise ApprovalError("approved_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ApprovalError("approved_at must carry a timezone")
    return record


class ApprovalStore:
    """Durable exact-match approvals derived from protected job evidence."""

    def __init__(self, approvals_root: Path, job_store: JobStore):
        self.approvals_root = Path(approvals_root)
        self.job_store = job_store
        assert_root_protected_parents(self.approvals_root)
        self.approvals_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_root_protected(self.approvals_root)
        os.chmod(self.approvals_root, 0o700)

    def _path(self, identity: dict) -> Path:
        return self.approvals_root / f"{_identity_key(identity)}.json"

    def _load_path(self, path: Path, *, expected_identity: dict | None = None) -> dict:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ApprovalError("approval does not exist") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (os.geteuid() == 0 and info.st_uid != 0)):
                raise ApprovalError("approval record protection is invalid")
            raw = os.read(fd, MAX_APPROVAL_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_APPROVAL_BYTES:
            raise ApprovalError("approval record exceeds its size limit")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApprovalError("approval record is corrupt") from exc
        return _validate_record(record, expected_identity)

    def find(self, identity: dict) -> dict | None:
        identity = _validate_identity(identity)
        path = self._path(identity)
        if not path.exists():
            return None
        return self._load_path(path, expected_identity=identity)

    def create_from_job(self, source_job_id: str, *, confirmed_migration_plan_digest: str,
                        approved_by_username: str, reason: str) -> tuple[dict, bool]:
        source_job_id = _canonical_uuid(source_job_id, "source_job_id")
        if not isinstance(confirmed_migration_plan_digest, str) or not _HEX64.fullmatch(confirmed_migration_plan_digest):
            raise ApprovalError("confirmed_migration_plan_digest must be lowercase SHA-256")
        if not isinstance(approved_by_username, str) or not _OPERATOR.fullmatch(approved_by_username):
            raise ApprovalError("approved_by_username has an invalid shape")
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 2000
                or any(ch in reason for ch in ("\x00", "\r"))):
            raise ApprovalError("approval reason is empty, invalid, or exceeds 2000 characters")
        try:
            job = self.job_store.load(source_job_id)
        except JobError as exc:
            raise ApprovalError(str(exc)) from exc
        if (job.get("state") != "manual_intervention_required"
                or job.get("failure_classification") != "MIGRATION_OPERATION_MANUAL"):
            raise ApprovalError("source job is not a terminal reviewed-migration decision point")
        review = job.get("migration_plan_review")
        plan = job.get("trusted_plan")
        if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS or not isinstance(plan, dict):
            raise ApprovalError("source job lacks complete protected review evidence")
        manual = _validate_manual_operations(review["manual_operations"])
        if review["migration_plan_digest"] != confirmed_migration_plan_digest:
            raise ApprovalError("confirmation digest does not match the protected review evidence")
        identity = approval_identity(
            target_release_id=review["release_id"],
            target_commit=review["target_commit"],
            target_manifest_sha256=review["manifest_sha256"],
            migration_plan_digest=review["migration_plan_digest"],
            trusted_plan_fingerprint=review["trusted_plan_fingerprint"],
        )
        if (plan.get("target_release_id") != identity["target_release_id"]
                or plan.get("target_commit") != identity["target_commit"]
                or plan.get("fingerprint") != identity["trusted_plan_fingerprint"]):
            raise ApprovalError("review evidence does not match the protected trusted plan")
        path = self._path(identity)
        if path.exists():
            return self._load_path(path, expected_identity=identity), False
        if len(list(self.approvals_root.glob("*.json"))) >= MAX_APPROVAL_RECORDS:
            raise ApprovalError("approval retention limit reached; operator review is required")
        record = {
            "schema_version": 1,
            "approval_id": str(uuid.uuid4()),
            **identity,
            "source_job_id": source_job_id,
            "manual_operations_snapshot": manual,
            "releases_in_plan_snapshot": _validate_string_list(
                plan.get("releases_in_plan"), "releases_in_plan_snapshot", item_pattern=_RELEASE,
            ),
            "migrations_required_snapshot": _validate_string_list(
                plan.get("migrations_required"), "migrations_required_snapshot", item_pattern=_MIGRATION,
            ),
            "approved_by_username": approved_by_username,
            "approved_at": _now(),
            "reason": reason.strip(),
        }
        _validate_record(record, identity)
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_APPROVAL_BYTES:
            raise ApprovalError("approval record exceeds its size limit")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            written = 0
            while written < len(raw):
                written += os.write(fd, raw[written:])
            os.fsync(fd)
        finally:
            os.close(fd)
        # A concurrent duplicate cannot overwrite the first audit record.
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            os.unlink(temporary)
            return self._load_path(path, expected_identity=identity), False
        os.unlink(temporary)
        directory_fd = os.open(self.approvals_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return record, True
