#!/usr/bin/env python3
"""IsadoraAir 1.2 Phase 6 -- durable, nonsecret disaster-recovery
assurance receipts.

Stdlib-only, deliberately dependency-light (like
deploy/restore/runtime_recovery_archive.py, which this module follows
in spirit): usable both as a library import from in-process Django code
(monitoring/services/probes.py's read-only "backup" probe) AND as a
standalone CLI invoked directly from deploy/backup_isadoraair.sh and
deploy/verify_backup_roundtrip.sh via plain `python3`, never through
manage.py -- nothing here touches the database or Django settings.

What this is NOT: a place to ever put a secret. Every receipt is a
small, bounded, nonsecret JSON document -- no passwords, no `.env`/
`.pgpass` contents, no SFTP host/path, no private-key material, no raw
`git status` output or dirty filenames, no command stderr. See each
schema's own docstring below for the exact field list; a strict
validator rejects anything else on read.

State root: `$BACKUP_ASSURANCE_STATE_DIR` if set (used by tests for a
deterministic, disposable location), else
`$HOME/.local/state/isadoraair/backup-assurance`. The directory is
created mode 0700; every receipt file is written mode 0600, atomically
(temp file in the same directory, fsync'd, then `os.replace`), with the
containing directory fsync'd afterward where the platform allows it.

Two independent receipt pairs live here:

  * last-attempt.json / last-success.json -- the normal nightly backup
    (deploy/backup_isadoraair.sh).
  * roundtrip-last-attempt.json / roundtrip-last-success.json -- the
    weekly real remote round-trip verifier
    (deploy/verify_backup_roundtrip.sh).

`evaluate_backup_health()` at the bottom is the one place Monitoring's
read-only "backup" probe (monitoring/services/probes.py) delegates to --
matching probe_weather's own delegation to weather.diagnostics. It only
ever reads receipts already on disk; it never touches SFTP, the
database, or the archive itself.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

STATE_DIR_ENV = "BACKUP_ASSURANCE_STATE_DIR"

ATTEMPT_FILE = "last-attempt.json"
SUCCESS_FILE = "last-success.json"
ROUNDTRIP_ATTEMPT_FILE = "roundtrip-last-attempt.json"
ROUNDTRIP_SUCCESS_FILE = "roundtrip-last-success.json"

STATE_DIR_MODE = 0o700
RECEIPT_FILE_MODE = 0o600

GIT_TIMEOUT_SECONDS = 5

ATTEMPT_OUTCOMES = ("running", "success", "failed")
TRIBOOL_STRINGS = ("true", "false", "unknown")


class AssuranceError(ValueError):
    """Malformed/unsupported receipt content, or an invalid call into
    this module's write API (e.g. claiming success before remote
    promotion). Callers that need to distinguish "no receipt yet" from
    "a receipt exists but is corrupt/unsupported" should catch this
    specifically -- see read_last_attempt()/read_last_success()."""


# --------------------------------------------------------------------
# State directory + atomic JSON I/O
# --------------------------------------------------------------------

def resolve_state_dir(override=None):
    """Directory receipts live in. `override` (a str/Path or None) wins
    first, then $BACKUP_ASSURANCE_STATE_DIR, then the durable default.
    Does not create it -- see ensure_state_dir()."""
    if override:
        return Path(override)
    env_value = os.environ.get(STATE_DIR_ENV)
    if env_value:
        return Path(env_value)
    return Path.home() / ".local" / "state" / "isadoraair" / "backup-assurance"


def ensure_state_dir(state_dir):
    """Create the receipt directory (and its ancestors) if needed, and
    make sure the LEAF directory is mode 0700 -- self-healing on every
    call, not just at first creation, so a host where the directory was
    created with a looser umask before this discipline existed still
    gets tightened the next time a receipt is written."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(state_dir, STATE_DIR_MODE)
    return state_dir


def _fsync_dir(path):
    """Best-effort directory fsync -- not supported on every platform/
    filesystem (e.g. some overlay/network filesystems reject O_RDONLY
    fsync on a directory); never fatal, this is belt-and-suspenders
    beyond the atomic os.replace() itself."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path, value):
    """Write `value` to `path` atomically: temp file in the same
    directory (so os.replace() is a same-filesystem rename, never a
    cross-device copy), fsync'd and chmod'd 0600 before the replace,
    then the containing directory is fsync'd. Never leaves a partial
    file at `path` itself -- either the old content or the new content,
    never a mix."""
    path = Path(path)
    directory = ensure_state_dir(path.parent)
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp_path, RECEIPT_FILE_MODE)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    _fsync_dir(directory)


def _read_json(path):
    """None if the file doesn't exist; the parsed object if it does.
    Raises AssuranceError for anything that isn't valid, readable JSON
    -- callers must not treat a corrupt receipt the same as a missing
    one (see evaluate_backup_health's UNKNOWN-vs-CRITICAL distinction)."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssuranceError(f"could not read {path.name}: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise AssuranceError(f"{path.name} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------
# Small shared field validators -- strict on read, so a malformed or
# hand-edited receipt is never silently trusted as healthy.
# --------------------------------------------------------------------

def _require_keys(obj, required, name):
    if not isinstance(obj, dict):
        raise AssuranceError(f"{name} must be a JSON object")
    missing = required - set(obj)
    if missing:
        raise AssuranceError(f"{name} is missing required field(s): {sorted(missing)}")


def _require_schema_version(obj, name):
    if obj.get("schema_version") != SCHEMA_VERSION:
        raise AssuranceError(f"{name} has unsupported schema_version {obj.get('schema_version')!r}")


def _require_str_or_none(obj, field, name):
    value = obj.get(field)
    if value is not None and not isinstance(value, str):
        raise AssuranceError(f"{name}.{field} must be a string or null")


def _require_bool_or_none(obj, field, name):
    value = obj.get(field)
    if value is not None and not isinstance(value, bool):
        raise AssuranceError(f"{name}.{field} must be true/false/null")


def _require_bool(obj, field, name):
    if not isinstance(obj.get(field), bool):
        raise AssuranceError(f"{name}.{field} must be true/false")


def _require_int(obj, field, name):
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssuranceError(f"{name}.{field} must be an integer")


def _require_nonempty_str(obj, field, name):
    value = obj.get(field)
    if not isinstance(value, str) or not value:
        raise AssuranceError(f"{name}.{field} must be a non-empty string")


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_attempt_id(now):
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _tribool_from_str(value):
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _tribool_to_str(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


# --------------------------------------------------------------------
# Git cleanliness -- exact HEAD, branch-or-detached, dirty tri-state.
# Bounded, fixed argument lists, never shell=True; fails to all-None on
# any error (git missing, not a repo, timeout) -- the caller then
# records "unknown", never a guess. NEVER returns/logs raw `git status`
# output -- only the derived boolean.
# --------------------------------------------------------------------

def _run_git(repo_root, *args):
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def collect_git_state(repo_root):
    """{"sha": str|None, "branch": str|None, "detached": bool|None,
    "dirty": bool|None}. `branch` is None when detached (check
    `detached` to tell "detached" apart from "unknown" -- both leave
    branch None, but detached=False/True is only meaningful once sha is
    known; detached is None when sha itself could not be determined)."""
    sha = _run_git(repo_root, "rev-parse", "HEAD")
    if not sha:
        return {"sha": None, "branch": None, "detached": None, "dirty": None}
    branch = _run_git(repo_root, "symbolic-ref", "-q", "--short", "HEAD")
    detached = not bool(branch)
    porcelain = _run_git(repo_root, "status", "--porcelain")
    dirty = None if porcelain is None else bool(porcelain)
    return {"sha": sha, "branch": (branch or None), "detached": detached, "dirty": dirty}


# --------------------------------------------------------------------
# last-attempt.json
# --------------------------------------------------------------------

_ATTEMPT_FIELDS = {
    "schema_version", "attempt_id", "started_at", "completed_at", "outcome",
    "stage", "exit_code", "git_sha", "git_branch", "git_detached", "git_dirty",
}


def _validate_attempt(obj):
    _require_keys(obj, _ATTEMPT_FIELDS, "last-attempt")
    _require_schema_version(obj, "last-attempt")
    _require_nonempty_str(obj, "attempt_id", "last-attempt")
    _require_nonempty_str(obj, "started_at", "last-attempt")
    _require_str_or_none(obj, "completed_at", "last-attempt")
    if obj.get("outcome") not in ATTEMPT_OUTCOMES:
        raise AssuranceError(f"last-attempt.outcome must be one of {ATTEMPT_OUTCOMES}")
    _require_nonempty_str(obj, "stage", "last-attempt")
    exit_code = obj.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise AssuranceError("last-attempt.exit_code must be an integer or null")
    _require_str_or_none(obj, "git_sha", "last-attempt")
    _require_str_or_none(obj, "git_branch", "last-attempt")
    _require_bool_or_none(obj, "git_detached", "last-attempt")
    _require_bool_or_none(obj, "git_dirty", "last-attempt")
    return obj


def record_attempt_start(stage, git_state=None, state_dir=None, dry_run=False, now=None):
    """Write last-attempt.json with outcome="running". Returns the
    record that was (or, under dry_run, WOULD have been) written --
    callers must keep the returned attempt_id to pass to
    record_attempt_result() later. DRY_RUN never touches disk."""
    now = now or _utcnow()
    git_state = git_state or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": _new_attempt_id(now),
        "started_at": _iso(now),
        "completed_at": None,
        "outcome": "running",
        "stage": stage,
        "exit_code": None,
        "git_sha": git_state.get("sha"),
        "git_branch": git_state.get("branch"),
        "git_detached": git_state.get("detached"),
        "git_dirty": git_state.get("dirty"),
    }
    _validate_attempt(record)
    if not dry_run:
        _atomic_write_json(Path(resolve_state_dir(state_dir)) / ATTEMPT_FILE, record)
    return record


def record_attempt_result(attempt_id, outcome, stage, exit_code=None, state_dir=None,
                           dry_run=False, now=None):
    """Update last-attempt.json to a terminal outcome ("success" or
    "failed"). Preserves the original started_at/git_* fields from the
    matching "running" record when one is present and its attempt_id
    matches; otherwise (missing/corrupt/mismatched -- must never happen
    in normal operation, but the ORIGINAL failure must never be lost
    over a receipt-bookkeeping problem) writes a best-effort standalone
    terminal record instead of raising."""
    if outcome not in ("success", "failed"):
        raise AssuranceError('record_attempt_result outcome must be "success" or "failed"')
    now = now or _utcnow()
    state_dir = resolve_state_dir(state_dir)
    started_at = _iso(now)
    git_sha = git_branch = git_detached = git_dirty = None
    if not dry_run:
        try:
            existing = _read_json(Path(state_dir) / ATTEMPT_FILE)
        except AssuranceError:
            existing = None
        if isinstance(existing, dict) and existing.get("attempt_id") == attempt_id:
            started_at = existing.get("started_at", started_at)
            git_sha = existing.get("git_sha")
            git_branch = existing.get("git_branch")
            git_detached = existing.get("git_detached")
            git_dirty = existing.get("git_dirty")
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "started_at": started_at,
        "completed_at": _iso(now),
        "outcome": outcome,
        "stage": stage,
        "exit_code": exit_code,
        "git_sha": git_sha,
        "git_branch": git_branch,
        "git_detached": git_detached,
        "git_dirty": git_dirty,
    }
    _validate_attempt(record)
    if not dry_run:
        _atomic_write_json(Path(state_dir) / ATTEMPT_FILE, record)
    return record


def read_last_attempt(state_dir=None):
    obj = _read_json(Path(resolve_state_dir(state_dir)) / ATTEMPT_FILE)
    if obj is None:
        return None
    return _validate_attempt(obj)


# --------------------------------------------------------------------
# last-success.json
# --------------------------------------------------------------------

_SUCCESS_VALIDATION_KEYS = (
    "database_catalog", "archive_integrity", "runtime_extractable",
    "recovery_policy_satisfied", "remote_promotion",
)

_SUCCESS_FIELDS = {
    "schema_version", "completed_at", "remote_filename", "archive_bytes",
    "archive_sha256", "backup_script_version", "archive_format_version",
    "recovery_class", "git_sha", "git_branch", "git_detached", "git_dirty",
    "product_contract_sha256", "retention_days", "validations",
}


def _validate_success(obj):
    _require_keys(obj, _SUCCESS_FIELDS, "last-success")
    _require_schema_version(obj, "last-success")
    _require_nonempty_str(obj, "completed_at", "last-success")
    _require_nonempty_str(obj, "remote_filename", "last-success")
    _require_int(obj, "archive_bytes", "last-success")
    if obj["archive_bytes"] < 0:
        raise AssuranceError("last-success.archive_bytes must not be negative")
    _require_nonempty_str(obj, "archive_sha256", "last-success")
    _require_nonempty_str(obj, "backup_script_version", "last-success")
    _require_nonempty_str(obj, "archive_format_version", "last-success")
    _require_nonempty_str(obj, "recovery_class", "last-success")
    _require_str_or_none(obj, "git_sha", "last-success")
    _require_str_or_none(obj, "git_branch", "last-success")
    _require_bool_or_none(obj, "git_detached", "last-success")
    _require_bool_or_none(obj, "git_dirty", "last-success")
    _require_str_or_none(obj, "product_contract_sha256", "last-success")
    _require_int(obj, "retention_days", "last-success")
    validations = obj.get("validations")
    if not isinstance(validations, dict) or set(validations) != set(_SUCCESS_VALIDATION_KEYS):
        raise AssuranceError(f"last-success.validations must have exactly {_SUCCESS_VALIDATION_KEYS}")
    for key in _SUCCESS_VALIDATION_KEYS:
        if not isinstance(validations[key], bool):
            raise AssuranceError(f"last-success.validations.{key} must be true/false")
    if not validations["remote_promotion"]:
        raise AssuranceError(
            "last-success.validations.remote_promotion must be true -- a success "
            "receipt can only be written after the remote .partial -> final "
            "promotion (and same-session pruning) has actually completed"
        )
    return obj


def record_success(*, remote_filename, archive_bytes, archive_sha256,
                    backup_script_version, archive_format_version, recovery_class,
                    retention_days, validations, git_state=None,
                    product_contract_sha256=None, state_dir=None, dry_run=False, now=None):
    """Write last-success.json. Only call this AFTER the normal SFTP
    session has fully completed its final .partial -> final rename and
    any same-session pruning -- enforced here by requiring
    validations["remote_promotion"] is True (see _validate_success);
    passing False/missing raises AssuranceError rather than writing a
    receipt that overstates what actually happened."""
    now = now or _utcnow()
    git_state = git_state or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "completed_at": _iso(now),
        "remote_filename": remote_filename,
        "archive_bytes": archive_bytes,
        "archive_sha256": archive_sha256,
        "backup_script_version": backup_script_version,
        "archive_format_version": archive_format_version,
        "recovery_class": recovery_class,
        "git_sha": git_state.get("sha"),
        "git_branch": git_state.get("branch"),
        "git_detached": git_state.get("detached"),
        "git_dirty": git_state.get("dirty"),
        "product_contract_sha256": product_contract_sha256,
        "retention_days": retention_days,
        "validations": dict(validations),
    }
    _validate_success(record)
    if not dry_run:
        _atomic_write_json(Path(resolve_state_dir(state_dir)) / SUCCESS_FILE, record)
    return record


def read_last_success(state_dir=None):
    obj = _read_json(Path(resolve_state_dir(state_dir)) / SUCCESS_FILE)
    if obj is None:
        return None
    return _validate_success(obj)


# --------------------------------------------------------------------
# roundtrip-last-attempt.json / roundtrip-last-success.json
# --------------------------------------------------------------------

_ROUNDTRIP_ATTEMPT_FIELDS = {
    "schema_version", "attempt_id", "started_at", "completed_at", "outcome",
    "stage", "exit_code", "remote_filename", "backup_git_sha",
}


def _validate_roundtrip_attempt(obj):
    _require_keys(obj, _ROUNDTRIP_ATTEMPT_FIELDS, "roundtrip-last-attempt")
    _require_schema_version(obj, "roundtrip-last-attempt")
    _require_nonempty_str(obj, "attempt_id", "roundtrip-last-attempt")
    _require_nonempty_str(obj, "started_at", "roundtrip-last-attempt")
    _require_str_or_none(obj, "completed_at", "roundtrip-last-attempt")
    if obj.get("outcome") not in ATTEMPT_OUTCOMES:
        raise AssuranceError(f"roundtrip-last-attempt.outcome must be one of {ATTEMPT_OUTCOMES}")
    _require_nonempty_str(obj, "stage", "roundtrip-last-attempt")
    exit_code = obj.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise AssuranceError("roundtrip-last-attempt.exit_code must be an integer or null")
    _require_str_or_none(obj, "remote_filename", "roundtrip-last-attempt")
    _require_str_or_none(obj, "backup_git_sha", "roundtrip-last-attempt")
    return obj


def record_roundtrip_attempt_start(stage, state_dir=None, dry_run=False, now=None):
    now = now or _utcnow()
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": _new_attempt_id(now),
        "started_at": _iso(now),
        "completed_at": None,
        "outcome": "running",
        "stage": stage,
        "exit_code": None,
        "remote_filename": None,
        "backup_git_sha": None,
    }
    _validate_roundtrip_attempt(record)
    if not dry_run:
        _atomic_write_json(Path(resolve_state_dir(state_dir)) / ROUNDTRIP_ATTEMPT_FILE, record)
    return record


def record_roundtrip_attempt_result(attempt_id, outcome, stage, exit_code=None,
                                     remote_filename=None, backup_git_sha=None,
                                     state_dir=None, dry_run=False, now=None):
    if outcome not in ("success", "failed"):
        raise AssuranceError('record_roundtrip_attempt_result outcome must be "success" or "failed"')
    now = now or _utcnow()
    state_dir = resolve_state_dir(state_dir)
    started_at = _iso(now)
    if not dry_run:
        try:
            existing = _read_json(Path(state_dir) / ROUNDTRIP_ATTEMPT_FILE)
        except AssuranceError:
            existing = None
        if isinstance(existing, dict) and existing.get("attempt_id") == attempt_id:
            started_at = existing.get("started_at", started_at)
            remote_filename = remote_filename or existing.get("remote_filename")
            backup_git_sha = backup_git_sha or existing.get("backup_git_sha")
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "started_at": started_at,
        "completed_at": _iso(now),
        "outcome": outcome,
        "stage": stage,
        "exit_code": exit_code,
        "remote_filename": remote_filename,
        "backup_git_sha": backup_git_sha,
    }
    _validate_roundtrip_attempt(record)
    if not dry_run:
        _atomic_write_json(Path(state_dir) / ROUNDTRIP_ATTEMPT_FILE, record)
    return record


def read_roundtrip_last_attempt(state_dir=None):
    obj = _read_json(Path(resolve_state_dir(state_dir)) / ROUNDTRIP_ATTEMPT_FILE)
    if obj is None:
        return None
    return _validate_roundtrip_attempt(obj)


_ROUNDTRIP_RESULT_VALUES = ("pass", "fail")

_ROUNDTRIP_SUCCESS_FIELDS = {
    "schema_version", "completed_at", "remote_filename", "expected_sha256",
    "observed_sha256", "backup_git_sha", "inspector_result",
    "pg_restore_catalog_result", "main_ancestry_result",
}


def _validate_roundtrip_success(obj):
    _require_keys(obj, _ROUNDTRIP_SUCCESS_FIELDS, "roundtrip-last-success")
    _require_schema_version(obj, "roundtrip-last-success")
    _require_nonempty_str(obj, "completed_at", "roundtrip-last-success")
    _require_nonempty_str(obj, "remote_filename", "roundtrip-last-success")
    _require_nonempty_str(obj, "expected_sha256", "roundtrip-last-success")
    _require_nonempty_str(obj, "observed_sha256", "roundtrip-last-success")
    if obj["expected_sha256"] != obj["observed_sha256"]:
        raise AssuranceError(
            "roundtrip-last-success can only record a hash MATCH -- expected_sha256 "
            "and observed_sha256 differ"
        )
    _require_nonempty_str(obj, "backup_git_sha", "roundtrip-last-success")
    for field in ("inspector_result", "pg_restore_catalog_result", "main_ancestry_result"):
        if obj.get(field) not in _ROUNDTRIP_RESULT_VALUES:
            raise AssuranceError(f"roundtrip-last-success.{field} must be one of {_ROUNDTRIP_RESULT_VALUES}")
        if obj[field] != "pass":
            raise AssuranceError(
                f"roundtrip-last-success.{field} must be 'pass' -- a success receipt "
                "can only be written once every verification stage has actually passed"
            )
    return obj


def record_roundtrip_success(*, remote_filename, expected_sha256, observed_sha256,
                              backup_git_sha, inspector_result, pg_restore_catalog_result,
                              main_ancestry_result, state_dir=None, dry_run=False, now=None):
    now = now or _utcnow()
    record = {
        "schema_version": SCHEMA_VERSION,
        "completed_at": _iso(now),
        "remote_filename": remote_filename,
        "expected_sha256": expected_sha256,
        "observed_sha256": observed_sha256,
        "backup_git_sha": backup_git_sha,
        "inspector_result": inspector_result,
        "pg_restore_catalog_result": pg_restore_catalog_result,
        "main_ancestry_result": main_ancestry_result,
    }
    _validate_roundtrip_success(record)
    if not dry_run:
        _atomic_write_json(Path(resolve_state_dir(state_dir)) / ROUNDTRIP_SUCCESS_FILE, record)
    return record


def read_roundtrip_last_success(state_dir=None):
    obj = _read_json(Path(resolve_state_dir(state_dir)) / ROUNDTRIP_SUCCESS_FILE)
    if obj is None:
        return None
    return _validate_roundtrip_success(obj)


# --------------------------------------------------------------------
# Monitoring probe authority -- read-only, local, no SFTP/archive
# access. Default policy constants (Phase 6, first implementation).
# --------------------------------------------------------------------

NIGHTLY_FRESH_HOURS = 28
NIGHTLY_WARNING_HOURS = 28
NIGHTLY_CRITICAL_HOURS = 36
RUNNING_STUCK_HOURS = 2

ROUNDTRIP_HEALTHY_DAYS = 8
ROUNDTRIP_WARNING_DAYS = 8
ROUNDTRIP_CRITICAL_DAYS = 14

# How far into the future a timestamp may plausibly sit before it's
# treated as corrupt/clock-skewed rather than genuinely fresh -- same
# reasoning as monitoring/services/probes.py's own
# CLOCK_SKEW_TOLERANCE_SECONDS, kept separate (a much smaller value is
# appropriate for a once-a-day event) rather than importing that
# module's constant, so this stays independently reusable.
FUTURE_TIMESTAMP_TOLERANCE_SECONDS = 300


def _parse_iso(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _age_seconds(dt, now):
    return (now - dt).total_seconds()


def _short(sha):
    return sha[:7] if sha else "unknown"


def evaluate_backup_health(state_dir=None, now=None):
    """Returns (status, detail) -- status in "ok"/"warning"/"critical"/
    "unknown", detail a small dict with a concise, nonsecret "message"
    plus the raw evidence fields the dashboard/tests may want. Never
    raises: any exception reading/validating a receipt is treated as a
    fail-closed CRITICAL (an initialized-but-malformed receipt), never
    silently swallowed into "ok". A wholly uninitialized install (no
    receipts at all) reports UNKNOWN, not a misleading success."""
    now = now or _utcnow()

    try:
        attempt = read_last_attempt(state_dir)
    except AssuranceError as exc:
        return "critical", {"message": f"last-attempt.json is malformed: {exc}"}
    try:
        success = read_last_success(state_dir)
    except AssuranceError as exc:
        return "critical", {"message": f"last-success.json is malformed: {exc}"}
    try:
        rt_attempt = read_roundtrip_last_attempt(state_dir)
    except AssuranceError as exc:
        return "critical", {"message": f"roundtrip-last-attempt.json is malformed: {exc}"}
    try:
        rt_success = read_roundtrip_last_success(state_dir)
    except AssuranceError as exc:
        return "critical", {"message": f"roundtrip-last-success.json is malformed: {exc}"}

    if attempt is None and success is None:
        return "unknown", {
            "message": "no backup-assurance receipts yet -- awaiting first normal "
                       "backup run and operator acceptance",
        }

    worst = "ok"
    reasons = []

    def _escalate(new_status):
        nonlocal worst
        order = {"ok": 0, "warning": 1, "critical": 2, "unknown": 1}
        if order[new_status] > order[worst]:
            worst = new_status

    success_dt = _parse_iso(success["completed_at"]) if success else None
    if success is not None and success_dt is None:
        return "critical", {"message": "last-success.json has an unparseable completed_at timestamp"}
    if success_dt is not None:
        success_age = _age_seconds(success_dt, now)
        if success_age < -FUTURE_TIMESTAMP_TOLERANCE_SECONDS:
            return "critical", {"message": "last-success.json's completed_at is materially in the future"}

    # Newest COMPLETED normal attempt, newer than last-success, that
    # failed -> immediate critical (a later success clears this
    # naturally, since it advances last-success past the failure).
    if attempt is not None and attempt["outcome"] == "failed":
        attempt_completed = _parse_iso(attempt.get("completed_at"))
        newer_than_success = success_dt is None or (
            attempt_completed is not None and attempt_completed > success_dt
        )
        if newer_than_success:
            _escalate("critical")
            reasons.append(
                f"last backup attempt failed at stage '{attempt.get('stage', 'unknown')}'"
                + (f" (exit {attempt['exit_code']})" if attempt.get("exit_code") is not None else "")
            )

    if attempt is not None and attempt["outcome"] == "running":
        started = _parse_iso(attempt.get("started_at"))
        if started is None:
            _escalate("critical")
            reasons.append("current backup attempt has an unparseable started_at timestamp")
        else:
            running_age_hours = _age_seconds(started, now) / 3600.0
            if running_age_hours < -(FUTURE_TIMESTAMP_TOLERANCE_SECONDS / 3600.0):
                _escalate("critical")
                reasons.append("current backup attempt's started_at is materially in the future")
            elif running_age_hours > RUNNING_STUCK_HOURS:
                _escalate("critical")
                reasons.append(f"backup attempt has been 'running' for {running_age_hours:.1f}h -- likely stuck")
            # else: a fresh running attempt with a still-valid prior
            # success is NOT itself a problem -- reported informationally
            # below, never escalated.

    if success is not None:
        success_age_hours = _age_seconds(success_dt, now) / 3600.0
        if success_age_hours > NIGHTLY_CRITICAL_HOURS:
            _escalate("critical")
            reasons.append(f"last successful backup was {success_age_hours / 24.0:.1f}d ago")
        elif success_age_hours > NIGHTLY_WARNING_HOURS:
            _escalate("warning")
            reasons.append(f"last successful backup was {success_age_hours:.1f}h ago")

        dirty = success.get("git_dirty")
        if dirty is True:
            _escalate("warning")
            reasons.append("last successful backup ran against an uncommitted (dirty) checkout")
        elif dirty is None:
            _escalate("warning")
            reasons.append("last successful backup's checkout cleanliness could not be determined")
    else:
        _escalate("critical")
        reasons.append("no successful backup has ever completed")

    # Weekly remote round-trip.
    if rt_attempt is not None and rt_attempt["outcome"] == "failed":
        rt_attempt_completed = _parse_iso(rt_attempt.get("completed_at"))
        rt_success_dt = _parse_iso(rt_success["completed_at"]) if rt_success else None
        newer_than_rt_success = rt_success_dt is None or (
            rt_attempt_completed is not None and rt_attempt_completed > rt_success_dt
        )
        if newer_than_rt_success:
            _escalate("critical")
            reasons.append(f"weekly remote round-trip verification failed at stage '{rt_attempt.get('stage', 'unknown')}'")

    if rt_success is not None:
        rt_dt = _parse_iso(rt_success["completed_at"])
        if rt_dt is None:
            return "critical", {"message": "roundtrip-last-success.json has an unparseable completed_at timestamp"}
        rt_age_days = _age_seconds(rt_dt, now) / 86400.0
        if rt_age_days < -(FUTURE_TIMESTAMP_TOLERANCE_SECONDS / 86400.0):
            return "critical", {"message": "roundtrip-last-success.json's completed_at is materially in the future"}
        if rt_age_days > ROUNDTRIP_CRITICAL_DAYS:
            _escalate("critical")
            reasons.append(f"weekly remote round-trip last verified {rt_age_days:.1f}d ago")
        elif rt_age_days > ROUNDTRIP_WARNING_DAYS:
            _escalate("warning")
            reasons.append(f"weekly remote round-trip last verified {rt_age_days:.1f}d ago")
    elif success is not None:
        # A normal backup exists but no weekly round-trip has ever
        # completed -- informational warning (not a hard requirement
        # for a freshly-enabled check), never critical on its own.
        _escalate("warning")
        reasons.append("no weekly remote round-trip verification has completed yet")

    message_parts = []
    if success_dt is not None:
        message_parts.append(f"Last backup {success_age_hours:.0f}h ago")
    if success is not None and success.get("git_sha"):
        message_parts.append(f"SHA {_short(success['git_sha'])}")
        message_parts.append("checkout clean" if success.get("git_dirty") is False else "checkout dirty/unknown")
    if rt_success is not None:
        message_parts.append(f"remote round-trip {rt_age_days:.0f}d ago")
    if reasons:
        message_parts.append("; ".join(reasons))
    detail = {
        "message": "; ".join(message_parts) or "backup assurance state incomplete",
        "reasons": reasons,
    }
    return worst, detail


# --------------------------------------------------------------------
# CLI -- thin argparse wrapper so deploy/backup_isadoraair.sh and
# deploy/verify_backup_roundtrip.sh can call this module directly via
# plain `python3`, exactly like deploy/restore/runtime_recovery_archive.py.
# --------------------------------------------------------------------

def _add_git_state_args(parser):
    parser.add_argument("--git-sha", default=None)
    parser.add_argument("--git-branch", default=None)
    parser.add_argument("--git-detached", choices=TRIBOOL_STRINGS, default="unknown")
    parser.add_argument("--git-dirty", choices=TRIBOOL_STRINGS, default="unknown")


def _git_state_from_args(args):
    return {
        "sha": args.git_sha or None,
        "branch": args.git_branch or None,
        "detached": _tribool_from_str(args.git_detached),
        "dirty": _tribool_from_str(args.git_dirty),
    }


def _cmd_git_state(args):
    print(json.dumps(collect_git_state(args.repo_root), sort_keys=True))
    return 0


def _cmd_attempt_start(args):
    record = record_attempt_start(
        args.stage, git_state=_git_state_from_args(args),
        state_dir=args.state_dir, dry_run=args.dry_run,
    )
    print(record["attempt_id"])
    return 0


def _cmd_attempt_finish(args):
    record_attempt_result(
        args.attempt_id, args.outcome, args.stage, exit_code=args.exit_code,
        state_dir=args.state_dir, dry_run=args.dry_run,
    )
    return 0


def _cmd_record_success(args):
    validations = {
        "database_catalog": args.database_catalog == "true",
        "archive_integrity": args.archive_integrity == "true",
        "runtime_extractable": args.runtime_extractable == "true",
        "recovery_policy_satisfied": args.recovery_policy_satisfied == "true",
        "remote_promotion": args.remote_promotion == "true",
    }
    record_success(
        remote_filename=args.remote_filename,
        archive_bytes=args.archive_bytes,
        archive_sha256=args.archive_sha256,
        backup_script_version=args.backup_script_version,
        archive_format_version=args.archive_format_version,
        recovery_class=args.recovery_class,
        retention_days=args.retention_days,
        validations=validations,
        git_state=_git_state_from_args(args),
        product_contract_sha256=args.product_contract_sha256,
        state_dir=args.state_dir, dry_run=args.dry_run,
    )
    return 0


def _cmd_roundtrip_attempt_start(args):
    record = record_roundtrip_attempt_start(args.stage, state_dir=args.state_dir, dry_run=args.dry_run)
    print(record["attempt_id"])
    return 0


def _cmd_roundtrip_attempt_finish(args):
    record_roundtrip_attempt_result(
        args.attempt_id, args.outcome, args.stage, exit_code=args.exit_code,
        remote_filename=args.remote_filename, backup_git_sha=args.backup_git_sha,
        state_dir=args.state_dir, dry_run=args.dry_run,
    )
    return 0


def _cmd_roundtrip_record_success(args):
    record_roundtrip_success(
        remote_filename=args.remote_filename,
        expected_sha256=args.expected_sha256,
        observed_sha256=args.observed_sha256,
        backup_git_sha=args.backup_git_sha,
        inspector_result=args.inspector_result,
        pg_restore_catalog_result=args.pg_restore_catalog_result,
        main_ancestry_result=args.main_ancestry_result,
        state_dir=args.state_dir, dry_run=args.dry_run,
    )
    return 0


def _cmd_evaluate_health(args):
    status, detail = evaluate_backup_health(state_dir=args.state_dir)
    print(json.dumps({"status": status, "detail": detail}, sort_keys=True))
    return 0


def _cmd_read_last_success(args):
    """For deploy/verify_backup_roundtrip.sh: prints last-success.json
    as compact JSON on stdout, or nothing (exit 0, empty stdout) if no
    valid receipt exists yet -- the caller distinguishes "not yet
    initialized" from a real receipt by checking for empty output,
    matching read_last_success()'s own None-vs-dict contract."""
    try:
        success = read_last_success(state_dir=args.state_dir)
    except AssuranceError as exc:
        print(f"backup-assurance error: {exc}", file=sys.stderr)
        return 1
    if success is not None:
        print(json.dumps(success, sort_keys=True))
    return 0


def build_parser():
    root = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = root.add_subparsers(dest="command", required=True)

    def _with_state_dir(p):
        p.add_argument("--state-dir", default=None)
        p.add_argument("--dry-run", action="store_true")
        return p

    git_state = sub.add_parser("git-state", allow_abbrev=False)
    git_state.add_argument("--repo-root", required=True)
    git_state.set_defaults(handler=_cmd_git_state)

    start = _with_state_dir(sub.add_parser("attempt-start", allow_abbrev=False))
    start.add_argument("--stage", required=True)
    _add_git_state_args(start)
    start.set_defaults(handler=_cmd_attempt_start)

    finish = _with_state_dir(sub.add_parser("attempt-finish", allow_abbrev=False))
    finish.add_argument("--attempt-id", required=True)
    finish.add_argument("--outcome", required=True, choices=("success", "failed"))
    finish.add_argument("--stage", required=True)
    finish.add_argument("--exit-code", type=int, default=None)
    finish.set_defaults(handler=_cmd_attempt_finish)

    success = _with_state_dir(sub.add_parser("record-success", allow_abbrev=False))
    success.add_argument("--remote-filename", required=True)
    success.add_argument("--archive-bytes", required=True, type=int)
    success.add_argument("--archive-sha256", required=True)
    success.add_argument("--backup-script-version", required=True)
    success.add_argument("--archive-format-version", required=True)
    success.add_argument("--recovery-class", required=True)
    success.add_argument("--retention-days", required=True, type=int)
    success.add_argument("--product-contract-sha256", default=None)
    _add_git_state_args(success)
    for flag in ("database-catalog", "archive-integrity", "runtime-extractable",
                 "recovery-policy-satisfied", "remote-promotion"):
        success.add_argument(f"--{flag}", required=True, choices=("true", "false"))
    success.set_defaults(handler=_cmd_record_success)

    rt_start = _with_state_dir(sub.add_parser("roundtrip-attempt-start", allow_abbrev=False))
    rt_start.add_argument("--stage", required=True)
    rt_start.set_defaults(handler=_cmd_roundtrip_attempt_start)

    rt_finish = _with_state_dir(sub.add_parser("roundtrip-attempt-finish", allow_abbrev=False))
    rt_finish.add_argument("--attempt-id", required=True)
    rt_finish.add_argument("--outcome", required=True, choices=("success", "failed"))
    rt_finish.add_argument("--stage", required=True)
    rt_finish.add_argument("--exit-code", type=int, default=None)
    rt_finish.add_argument("--remote-filename", default=None)
    rt_finish.add_argument("--backup-git-sha", default=None)
    rt_finish.set_defaults(handler=_cmd_roundtrip_attempt_finish)

    rt_success = _with_state_dir(sub.add_parser("roundtrip-record-success", allow_abbrev=False))
    rt_success.add_argument("--remote-filename", required=True)
    rt_success.add_argument("--expected-sha256", required=True)
    rt_success.add_argument("--observed-sha256", required=True)
    rt_success.add_argument("--backup-git-sha", required=True)
    rt_success.add_argument("--inspector-result", required=True, choices=_ROUNDTRIP_RESULT_VALUES)
    rt_success.add_argument("--pg-restore-catalog-result", required=True, choices=_ROUNDTRIP_RESULT_VALUES)
    rt_success.add_argument("--main-ancestry-result", required=True, choices=_ROUNDTRIP_RESULT_VALUES)
    rt_success.set_defaults(handler=_cmd_roundtrip_record_success)

    health = sub.add_parser("evaluate-health", allow_abbrev=False)
    health.add_argument("--state-dir", default=None)
    health.set_defaults(handler=_cmd_evaluate_health)

    read_success = sub.add_parser("read-last-success", allow_abbrev=False)
    read_success.add_argument("--state-dir", default=None)
    read_success.set_defaults(handler=_cmd_read_last_success)

    return root


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except AssuranceError as exc:
        print(f"backup-assurance error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
