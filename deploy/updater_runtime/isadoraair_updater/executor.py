"""Milestone-driven safe update pipeline for the protected root runtime."""
from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import re
import shutil
import stat
import time
from urllib.parse import urlsplit

from . import HANDOFF_WIRE_PROTOCOL
from .approvals import ApprovalError, ApprovalStore, approval_identity
from .checkpoint import CheckpointError, create_checkpoint, verify_checkpoint
from .config import StationConfig
from .jobs import JobError, JobStore
from .process import CommandRunner, ProcessResult
from .migration_authorization import central_authorization_covers, load_plan_authorizations
from .release import (
    GIT, ReleaseError, TrustedPlan, TrustedRepository, derive_plan, load_chain, manual_blockers,
    resolve_known_managed_units,
)
from .runtime_handoff import (
    MILESTONE_RUNTIME_ACTIVATION_REQUESTED, MILESTONE_RUNTIME_CANDIDATE_STAGED,
    MILESTONE_RUNTIME_CANDIDATE_VERIFIED, MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED,
    MILESTONE_RUNTIME_GENERATION_COMMITTED, MILESTONE_RUNTIME_ALREADY_AUTHORITATIVE,
    MUTATION_GATE_MILESTONE, SAFE_YIELD_MILESTONE, HandoffError, MutationGateError,
    attestations_staging_directory, descriptor_staging_path, handoff_required, materialize_candidate,
    mutation_gate_satisfied, new_supervisor_staging_directory, publish_to_candidate_slot,
    require_mutation_allowed,
    stage_attestations, stage_descriptor, verify_candidate_independently,
    verify_new_units_authorized_by_candidate_policy,
)
from .security import ProtectionError
from .staging import StagedSource, StagingError, cleanup, materialize
from .supervisor_client import (
    SupervisorClientError, SupervisorClient, SupervisorRejectedError, SupervisorTransportError,
)
from .systemd import SystemdError, SystemdManager

from protected_bootstrap.trust import TrustPolicyError, parse_trust_policy_dict


class ExecutionError(RuntimeError):
    def __init__(self, classification: str, detail: str, *, manual: bool = False, migration_plan_review: dict | None = None):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail
        self.manual = manual
        # Structured evidence for MIGRATION_OPERATION_MANUAL only -- see
        # JobStore.fail()'s own matching parameter and docs/UPDATE_CENTER.md's
        # "Reviewed migration approval" section. None for every other
        # ExecutionError.
        self.migration_plan_review = migration_plan_review


_ENV_KEYS = frozenset({
    "DEBUG", "SECRET_KEY", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
})
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _parse_env_file(path: Path) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ExecutionError("APPLICATION_ENV_INVALID", "application environment is not a regular file")
        raw = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if len(raw) > 1024 * 1024:
        raise ExecutionError("APPLICATION_ENV_INVALID", "application environment exceeds 1 MiB")
    result = {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExecutionError("APPLICATION_ENV_INVALID", "application environment is not UTF-8") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in _ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ExecutionError("APPLICATION_ENV_INVALID", f"application setting {key} contains controls")
        result[key] = value
    required = {"SECRET_KEY", "DB_NAME", "DB_USER", "DB_PASSWORD"}
    if required - set(result):
        raise ExecutionError("APPLICATION_ENV_INVALID", "application environment lacks required Django/database settings")
    return result


def _redact(text: str, secrets: dict[str, str]) -> str:
    sanitized = text
    for key in ("SECRET_KEY", "DB_PASSWORD"):
        value = secrets.get(key, "")
        if value:
            sanitized = sanitized.replace(value, "[REDACTED]")
    return " ".join(sanitized.split())[:4000]


def _decode(result: ProcessResult, settings: dict[str, str]) -> str:
    combined = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    return _redact(combined, settings)


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _valid_manual_operations(value, *, plan_refs: set[str]) -> bool:
    if not isinstance(value, list):
        return False
    refs = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"ref", "operation_index", "operation", "classification", "detail"}:
            return False
        if not isinstance(entry["ref"], str) or not refs.fullmatch(entry["ref"]) or entry["ref"] not in plan_refs:
            return False
        if not isinstance(entry["operation_index"], int) or isinstance(entry["operation_index"], bool) or entry["operation_index"] < 0:
            return False
        if entry["classification"] != "manual":
            return False
        if not isinstance(entry["operation"], str) or not isinstance(entry["detail"], str):
            return False
    return True


_LEGACY_PROBE_KEYS = frozenset({
    "schema_version", "status", "plan", "nodes", "applied", "conflicts", "replacements",
})
_REVIEW_PROBE_KEYS = _LEGACY_PROBE_KEYS | frozenset({
    "release_id", "target_commit", "manifest_sha256", "migration_plan_digest",
    "manual_operations", "approval",
})
_RECOVERY_PROBE_KEYS = _REVIEW_PROBE_KEYS | frozenset({
    "recovery_plan", "recovery_migration_plan_digest", "recovery_manual_operations",
})

PSQL = "/usr/bin/psql"
_MIGRATION_REF_RE = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
_APPLIED_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
# Closed runtime-11 partial-prefix evidence schema. prefix_records holds the
# exact django_migrations row identity ({"id", "applied"}) observed for every
# migration in successful_prefix, so a later unapply/re-apply of the same
# names cannot masquerade as the updater's own work. in_flight_migration is
# the ONE migration whose command is about to run / running (persisted before
# the command, cleared after its observation). in_flight_absence_proven is
# set durably only after a fresh observation, taken AFTER the marker was
# written, proved the migration still absent and the owned prefix unchanged.
# Crash finalization may extend the recorded prefix by at most the in-flight
# migration, and only when its absence was proven: anything applied before
# that proof was not done by the updater.
RECOVERY_EVIDENCE_FIELDS = frozenset({
    "schema_version", "classification", "evidence_job_id", "prior_job_id", "release_id", "target_commit",
    "manifest_sha256", "migration_plan_digest", "trusted_plan_fingerprint",
    "ordered_target_plan", "successful_prefix", "prefix_records", "in_flight_migration",
    "in_flight_absence_proven", "checkpoint",
    "failure_classification", "failure_detail", "continued_from_job_id", "authorization_source", "finalized",
    "first_remaining_migration", "permitted_action",
})
# Bounded wait for PostgreSQL to accept connections again after a power loss
# (crash recovery can take a while). ~60 s in total, then fail safe; a later
# exact retry can still finalize the evidence (see _find_partial_recovery).
OBSERVATION_READINESS_DELAYS = (1, 2, 4, 8, 15, 30)
_PREFLIGHT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
AUTHORIZATION_SOURCES = frozenset({"not_required", "central", "local"})


def _contiguous_prefix(ordered: list[str], present) -> list[str]:
    prefix = []
    for ref in ordered:
        if ref not in present:
            break
        prefix.append(ref)
    return prefix


def _strict_probe(raw: bytes, *, review_context: bool, recovery_context: bool = False) -> dict:
    """Parse and strictly validate one migration-probe response.

    `review_context` is set by the CALLER (never inferred from the
    payload itself) based on whether THIS probe invocation supplied
    --release-id/--target-commit:

    * review_context=True (the target-schema probe, which always
      supplies release_id/target_commit and runs against the staged
      candidate source): the payload MUST be exactly the full 13-key
      reviewed-plan shape -- unchanged from before, no relaxation.
    * review_context=False (the current-schema probe, which always
      runs against self.config.application_root -- the live,
      not-yet-advanced application checkout -- BEFORE any source
      advancement in this same job): the payload must be EITHER the
      exact legacy 7-key shape (an application source that predates
      the reviewed-migration-approval probe schema -- protected-
      runtime generation and ordinary application-source deployment
      are on independent cadences, so a currently-installed source
      may legitimately not know these fields exist) OR the full
      13-key shape with the six review-only fields held at their
      fixed, documented "no context requested" defaults (release_id/
      target_commit/manifest_sha256/migration_plan_digest=None,
      manual_operations=[], approval=None) -- the honest output of an
      already-upgraded script that was simply never asked for review
      context. A context-less call reporting anything else in those
      six fields is fabricating review evidence nobody asked for and
      is rejected exactly like any other schema violation.

    * recovery_context=True (protocol 6, only ever requested together
      with review context and explicit --recovery-plan-ref arguments):
      the payload MUST be exactly the 16-key recovery shape. The
      ordinary target probe never emits recovery keys, so it keeps the
      exact 13-key shape every earlier runtime generation accepts.

    In every case the accepted key set is one of the fixed, closed
    shapes the CALLER requested -- never a partial/arbitrary mix -- so a
    target probe that only emits the legacy shape is rejected, a current
    probe polluted with real (non-default) review data is equally
    rejected, and recovery evidence nobody asked for is rejected too.
    """
    if recovery_context and not review_context:
        raise ExecutionError("PROBE_INVALID", "recovery probe context requires review context")
    if len(raw) > 1024 * 1024:
        raise ExecutionError("PROBE_INVALID", "migration probe output exceeds 1 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionError("PROBE_INVALID", "migration probe did not emit strict JSON") from exc
    keys = set(payload) if isinstance(payload, dict) else set()
    if recovery_context:
        shape_ok = keys == _RECOVERY_PROBE_KEYS
        has_review_fields = True
    elif review_context:
        shape_ok = keys == _REVIEW_PROBE_KEYS
        has_review_fields = True
    else:
        shape_ok = keys in (_LEGACY_PROBE_KEYS, _REVIEW_PROBE_KEYS)
        has_review_fields = keys == _REVIEW_PROBE_KEYS
    if not isinstance(payload, dict) or not shape_ok or payload.get("schema_version") != 1 or payload.get("status") != "ok":
        raise ExecutionError("PROBE_INVALID", "migration probe schema/status mismatch")
    if not isinstance(payload["plan"], list) or not isinstance(payload["nodes"], dict) or not isinstance(payload["applied"], list):
        raise ExecutionError("PROBE_INVALID", "migration probe collection types are invalid")
    if not isinstance(payload["conflicts"], dict) or not isinstance(payload["replacements"], list):
        raise ExecutionError("PROBE_INVALID", "migration probe conflict/replacement types are invalid")
    if has_review_fields:
        for key in ("release_id", "target_commit"):
            if payload[key] is not None and not isinstance(payload[key], str):
                raise ExecutionError("PROBE_INVALID", f"migration probe {key} has an invalid type")
        for key in ("manifest_sha256", "migration_plan_digest"):
            if payload[key] is not None and (not isinstance(payload[key], str) or not _HEX64.fullmatch(payload[key])):
                raise ExecutionError("PROBE_INVALID", f"migration probe {key} is not a valid digest")
        if not isinstance(payload["manual_operations"], list):
            raise ExecutionError("PROBE_INVALID", "migration probe manual_operations has an invalid type")
        if payload["approval"] is not None:
            raise ExecutionError("PROBE_INVALID", "migration probe must not supply approval authority")
        if not review_context and (
            payload["release_id"] is not None or payload["target_commit"] is not None
            or payload["manifest_sha256"] is not None or payload["migration_plan_digest"] is not None
            or payload["manual_operations"] or payload["approval"] is not None
        ):
            raise ExecutionError("PROBE_INVALID", "migration probe reported review evidence without review context")
    refs = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
    for ref, dependencies in payload["nodes"].items():
        if not isinstance(ref, str) or not refs.fullmatch(ref) or not isinstance(dependencies, list) or any(not isinstance(dep, str) or not refs.fullmatch(dep) for dep in dependencies):
            raise ExecutionError("PROBE_INVALID", "migration graph contains an invalid node/dependency")
    if any(not isinstance(ref, str) or not refs.fullmatch(ref) for ref in payload["applied"]):
        raise ExecutionError("PROBE_INVALID", "migration applied set contains an invalid reference")
    seen = set()
    for item in payload["plan"]:
        if not isinstance(item, dict) or set(item) != {"ref", "dependencies", "operations", "migration_file_sha256"}:
            raise ExecutionError("PROBE_INVALID", "migration plan item shape is invalid")
        if item["ref"] in seen or item["ref"] not in payload["nodes"] or item["dependencies"] != payload["nodes"][item["ref"]]:
            raise ExecutionError("PROBE_INVALID", "migration plan identity/dependencies are inconsistent")
        if not isinstance(item["migration_file_sha256"], str) or not _HEX64.fullmatch(item["migration_file_sha256"]):
            raise ExecutionError("PROBE_INVALID", "migration plan item file digest is invalid")
        seen.add(item["ref"])
        if not isinstance(item["operations"], list):
            raise ExecutionError("PROBE_INVALID", "migration operations must be a list")
        for operation in item["operations"]:
            if (not isinstance(operation, dict) or set(operation) != {"operation", "classification", "detail"}
                    or operation["classification"] not in {"additive", "manual"}):
                raise ExecutionError("PROBE_INVALID", "migration operation classification is invalid")
    if review_context:
        if not _valid_manual_operations(payload["manual_operations"], plan_refs=seen):
            raise ExecutionError("PROBE_INVALID", "migration probe manual_operations shape is invalid")
        if payload["approval"] is not None:
            raise ExecutionError("PROBE_INVALID", "migration probe must not supply approval authority")
        if payload["manual_operations"] and payload["migration_plan_digest"] is None:
            raise ExecutionError("PROBE_INVALID", "migration probe has manual operations but no digest")
        if payload["approval"] is not None and not payload["manual_operations"]:
            raise ExecutionError("PROBE_INVALID", "migration probe reported an approval with no manual operations")
    if recovery_context:
        recovery_plan = payload["recovery_plan"]
        recovery_digest = payload["recovery_migration_plan_digest"]
        recovery_manual = payload["recovery_manual_operations"]
        if not isinstance(recovery_plan, list) or not recovery_plan:
            raise ExecutionError("PROBE_INVALID", "migration recovery plan is missing or empty")
        if not isinstance(recovery_digest, str) or not _HEX64.fullmatch(recovery_digest):
            raise ExecutionError("PROBE_INVALID", "migration recovery digest is invalid")
        recovery_refs = set()
        for item in recovery_plan:
            if (not isinstance(item, dict)
                    or set(item) != {"ref", "dependencies", "operations", "migration_file_sha256"}
                    or item["ref"] in recovery_refs or item["ref"] not in payload["nodes"]
                    or item["dependencies"] != payload["nodes"][item["ref"]]
                    or not isinstance(item["migration_file_sha256"], str)
                    or not _HEX64.fullmatch(item["migration_file_sha256"])
                    or not isinstance(item["operations"], list)):
                raise ExecutionError("PROBE_INVALID", "migration recovery plan item is invalid")
            for operation in item["operations"]:
                if (not isinstance(operation, dict) or set(operation) != {"operation", "classification", "detail"}
                        or operation["classification"] not in {"additive", "manual"}):
                    raise ExecutionError("PROBE_INVALID", "migration recovery operation classification is invalid")
            recovery_refs.add(item["ref"])
        if not _valid_manual_operations(recovery_manual, plan_refs=recovery_refs):
            raise ExecutionError("PROBE_INVALID", "migration recovery manual operations are invalid")
    return payload


def _dependency_closure(nodes: dict[str, list[str]], expected: tuple[str, ...]) -> set[str]:
    missing = set(expected) - set(nodes)
    if missing:
        raise ExecutionError("TARGET_MIGRATION_MISMATCH", f"expected migration(s) absent from target graph: {sorted(missing)!r}")
    closure = set()
    visiting = set()

    def visit(ref: str):
        if ref in visiting:
            raise ExecutionError("TARGET_MIGRATION_CONFLICT", "target migration dependency graph contains a cycle")
        if ref in closure:
            return
        visiting.add(ref)
        for dependency in nodes.get(ref, []):
            if dependency not in nodes:
                raise ExecutionError("TARGET_MIGRATION_MISMATCH", f"dependency {dependency} is absent from target graph")
            visit(dependency)
        visiting.remove(ref)
        closure.add(ref)

    for ref in expected:
        visit(ref)
    return closure


class Executor:
    def __init__(self, config: StationConfig, store: JobStore, runner: CommandRunner,
                 *, systemd_manager: SystemdManager | None = None,
                 approval_store: ApprovalStore | None = None,
                 expected_handoff_generation: int | None = None,
                 expected_handoff_descriptor_sha256: str | None = None,
                 expected_resumable_job_uuid: str | None = None,
                 active_policy=None):
        self.config = config
        self.store = store
        self.approval_store = approval_store or ApprovalStore(config.approvals_root, store)
        self.runner = runner
        self.repository = TrustedRepository(config.trusted_repository, config.trusted_repository_url, config.trusted_branch, runner)
        # D4-P: this worker's OWN independently-loaded active signed
        # policy (from its own slot -- see daemon.py/updaterd.py's own
        # loading, never from application/database/env-var sources) --
        # None (every pre-Phase-D and D0-bootstrap worker) means
        # resolve_known_managed_units()/SystemdManager both fall back
        # to the compiled MANAGED_UNIT_POLICIES map, byte-for-byte
        # today's behavior.
        self.active_policy = active_policy
        self.systemd = systemd_manager or SystemdManager(config, runner, signed_policy=active_policy)
        # D4-D: None/None/None (every non-candidate Executor -- every
        # ordinary worker, and the OLD worker in a handoff) means this
        # process is NEVER authorized to perform the candidate's own
        # runtime-acceptance step (_execute_candidate_acceptance
        # below), regardless of what a job's own durable milestones
        # say -- see execute()'s own three-way branch. Only a process
        # the supervisor ACTUALLY launched as a specific candidate
        # (all three populated, matching daemon.py's own
        # expected_handoff_* parameters) may take that branch. This is
        # the concrete answer to "prove old and new workers can never
        # mutate the same job concurrently": mutation authority is
        # bound to PROCESS IDENTITY the supervisor itself assigned,
        # never inferred from job state alone (job state is necessary
        # but not sufficient).
        if len({expected_handoff_generation is None, expected_handoff_descriptor_sha256 is None,
                expected_resumable_job_uuid is None}) != 1:
            raise ValueError(
                "expected_handoff_generation/descriptor_sha256/resumable_job_uuid must be all null or all present"
            )
        self.expected_handoff_generation = expected_handoff_generation
        self.expected_handoff_descriptor_sha256 = expected_handoff_descriptor_sha256
        self.last_authorization_source = "not_required"
        self._sleep = time.sleep
        self.expected_resumable_job_uuid = expected_resumable_job_uuid

    def _app_env(self, source: Path) -> tuple[dict[str, str], dict[str, str]]:
        settings = _parse_env_file(self.config.application_environment_file)
        database_identity = (
            settings.get("DB_NAME"), settings.get("DB_USER"),
            settings.get("DB_HOST", "localhost"), settings.get("DB_PORT", "5432"),
        )
        configured_identity = (
            self.config.database.name, self.config.database.user,
            self.config.database.host, str(self.config.database.port),
        )
        if database_identity != configured_identity:
            raise ExecutionError(
                "APPLICATION_DATABASE_IDENTITY_MISMATCH",
                "application environment database identity differs from root-owned station configuration",
            )
        environment = dict(settings)
        environment.update({
            "PYTHONPATH": str(source),
            "PYTHONDONTWRITEBYTECODE": "1",
            "DJANGO_SETTINGS_MODULE": "isadoraair.settings",
        })
        return environment, settings

    def _run_app(self, source: Path, arguments: list[str], *, timeout: float) -> tuple[ProcessResult, dict[str, str]]:
        environment, settings = self._app_env(source)
        result = self.runner.run_as_user(
            self.config.application_user,
            [str(self.config.application_python), str(source / "manage.py"), *arguments],
            cwd=source, env=environment, timeout=timeout,
        )
        return result, settings

    def _probe(self, source: Path, *, release_id: str | None = None, target_commit: str | None = None,
               recovery_plan_refs: tuple[str, ...] = ()) -> dict:
        review_context = release_id is not None or target_commit is not None
        arguments = ["updatecenter_probe", "--skip-checks"]
        if review_context:
            arguments += ["--release-id", release_id, "--target-commit", target_commit]
        for ref in recovery_plan_refs:
            arguments += ["--recovery-plan-ref", ref]
        result, settings = self._run_app(source, arguments, timeout=120)
        if not result.ok:
            raise ExecutionError("PROBE_FAILED", _decode(result, settings))
        payload = _strict_probe(
            result.stdout.strip(), review_context=review_context,
            recovery_context=bool(recovery_plan_refs),
        )
        if review_context and (payload["release_id"] != release_id or payload["target_commit"] != target_commit):
            raise ExecutionError("PROBE_INVALID", "migration probe echoed a different release/target than requested")
        if recovery_plan_refs and [item["ref"] for item in payload["recovery_plan"]] != list(recovery_plan_refs):
            raise ExecutionError("PROBE_INVALID", "migration recovery probe reconstructed a different plan than requested")
        return payload

    def _app_git(self, args: list[str], *, timeout: float = 60) -> ProcessResult:
        return self.runner.run_as_user(
            self.config.application_user,
            [GIT, "-C", str(self.config.application_root), "-c", "core.hooksPath=/dev/null", *args],
            timeout=timeout,
        )

    def _live_identity(self) -> dict:
        probes = (
            ("branch", "LIVE_GIT_BRANCH_FAILED", ["symbolic-ref", "-q", "--short", "HEAD"]),
            ("head", "LIVE_GIT_HEAD_FAILED", ["rev-parse", "--verify", "HEAD"]),
            ("status", "LIVE_GIT_STATUS_FAILED", ["status", "--porcelain"]),
            ("remote", "LIVE_GIT_REMOTE_FAILED", ["remote", "get-url", "origin"]),
        )
        results = {}
        for name, classification, arguments in probes:
            result = self._app_git(arguments)
            if not result.ok:
                raise ExecutionError(
                    classification,
                    f"live Git {name} probe failed "
                    f"(returncode={result.returncode!r}, timed_out={result.timed_out}, "
                    f"output_truncated={result.output_truncated})",
                )
            results[name] = result
        branch = results["branch"]
        head = results["head"]
        dirty = results["status"]
        remote = results["remote"]
        branch_value = branch.stdout.decode("utf-8").strip()
        head_value = head.stdout.decode("ascii").strip()
        remote_value = remote.stdout.decode("utf-8").strip()
        if branch_value != self.config.trusted_branch or not _SHA.fullmatch(head_value):
            raise ExecutionError("LIVE_GIT_INVALID", "live checkout branch/HEAD is not authoritative")
        if dirty.stdout.strip():
            raise ExecutionError("LIVE_CHECKOUT_DIRTY", "live checkout has uncommitted or untracked changes")
        if remote_value != self.config.trusted_repository_url:
            raise ExecutionError("LIVE_REMOTE_MISMATCH", "live origin does not match root-owned repository identity")
        return {"branch": branch_value, "head": head_value}

    def _validate_current_schema(self) -> dict:
        payload = self._probe(self.config.application_root)
        if payload["conflicts"] or payload["replacements"] or payload["plan"]:
            pending = [item["ref"] for item in payload["plan"]]
            raise ExecutionError("CURRENT_SCHEMA_UNHEALTHY", f"current source has migration conflicts/replacements/pending work: {pending!r}")
        return payload

    def _run_migration_preflights(self, source: Path, pending_refs) -> dict:
        """Run the staged TARGET code's read-only preflights for the trusted
        pending migrations. The target command owns an explicit registry
        keyed by migration ref (no manifest field, no dynamic import); refs
        with no registered check simply contribute none."""
        pending = list(pending_refs)
        if not pending:
            return {"schema_version": 1, "status": "ok", "checks": []}
        arguments = ["updatecenter_migration_preflight", "--skip-checks"]
        for ref in pending:
            arguments += ["--pending", ref]
        result, settings = self._run_app(source, arguments, timeout=300)
        if not result.ok or len(result.stdout) > 65536:
            raise ExecutionError("MIGRATION_PREFLIGHT_FAILED", _decode(result, settings), manual=True)
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ExecutionError("MIGRATION_PREFLIGHT_INVALID", "preflight did not emit strict JSON") from exc
        if (not isinstance(payload, dict) or set(payload) != {"schema_version", "status", "checks"}
                or payload.get("schema_version") != 1 or payload.get("status") not in {"ok", "failed"}
                or not isinstance(payload.get("checks"), list)):
            raise ExecutionError("MIGRATION_PREFLIGHT_INVALID", "preflight response schema is invalid")
        for item in payload["checks"]:
            if (not isinstance(item, dict) or set(item) != {"id", "migration", "status", "evidence"}
                    or not isinstance(item.get("id"), str) or not _PREFLIGHT_ID_RE.fullmatch(item["id"])
                    or item.get("migration") not in pending
                    or item.get("status") not in {"passed", "failed"}
                    or not isinstance(item.get("evidence"), dict)):
                raise ExecutionError("MIGRATION_PREFLIGHT_INVALID", "preflight check result is invalid")
        if payload["status"] != ("ok" if all(item["status"] == "passed" for item in payload["checks"]) else "failed"):
            raise ExecutionError("MIGRATION_PREFLIGHT_INVALID", "preflight overall status contradicts its checks")
        if payload["status"] != "ok":
            raise ExecutionError(
                "MIGRATION_PREFLIGHT_BLOCKED",
                json.dumps(payload["checks"], sort_keys=True, separators=(",", ":"))[:4000],
                manual=True,
            )
        return payload

    def _observe_migration_records(self, refs) -> dict[str, dict]:
        """Exact django_migrations rows for `refs`, read straight from PostgreSQL.

        Uses the same database identity as the checkpoint pg_dump, in a
        read-only session, independent of which application source is
        installed or whether a staged target still exists -- the
        interrupted-resume path has neither. Returns {ref: {"id", "applied"}}
        for every requested ref that has a row; a ref with more than one
        row is ambiguous and raises.
        """
        wanted = set(refs)
        db = self.config.database
        query = (
            "SELECT id, app, name, "
            "to_char(applied AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') "
            "FROM django_migrations ORDER BY id"
        )
        argv = self.runner.argv_as_user(self.config.application_user, [
            PSQL, "--no-psqlrc", "--no-password", "--no-align", "--tuples-only",
            "--field-separator=|", "--set=ON_ERROR_STOP=1",
            "--host", db.host, "--port", str(db.port), "--username", db.user, "--dbname", db.name,
            "--command", query,
        ])
        env = {"PGOPTIONS": "-c default_transaction_read_only=on"}
        if db.pgpass_file:
            env["PGPASSFILE"] = str(db.pgpass_file)
        result = self.runner.run(argv, timeout=60, env=env)
        if not result.ok or result.output_truncated:
            raise ExecutionError(
                "MIGRATION_OBSERVATION_FAILED",
                f"could not read django_migrations (returncode={result.returncode!r}, timed_out={result.timed_out})",
                manual=True,
            )
        records: dict[str, dict] = {}
        try:
            lines = result.stdout.decode("utf-8", "strict").splitlines()
        except UnicodeDecodeError as exc:
            raise ExecutionError("MIGRATION_OBSERVATION_FAILED", "django_migrations output is not UTF-8", manual=True) from exc
        for line in lines:
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != 4 or not parts[0].isdigit() or not _APPLIED_UTC_RE.fullmatch(parts[3]):
                raise ExecutionError("MIGRATION_OBSERVATION_FAILED", "django_migrations row is malformed", manual=True)
            ref = f"{parts[1]}.{parts[2]}"
            if ref not in wanted:
                continue
            if ref in records:
                raise ExecutionError(
                    "MIGRATION_RECORD_AMBIGUOUS", f"django_migrations holds more than one row for {ref}", manual=True,
                )
            records[ref] = {"id": int(parts[0]), "applied": parts[3]}
        return records

    def _observe_migration_records_when_ready(self, refs) -> dict[str, dict]:
        """_observe_migration_records with a bounded wait for PostgreSQL to
        accept connections (power-loss restart). Only connection-level
        observation failures are retried; ambiguity is never retried."""
        for delay in (*OBSERVATION_READINESS_DELAYS, None):
            try:
                return self._observe_migration_records(refs)
            except ExecutionError as exc:
                if exc.classification != "MIGRATION_OBSERVATION_FAILED" or delay is None:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _assert_owned_prefix(self, recovery: dict) -> None:
        """THE final ownership assertion for a continuation: directly observe
        django_migrations for the exact ordered target transition and require
        the applied set to be exactly the owned prefix, with every row's exact
        id/applied identity unchanged. Anything else fails closed."""
        self._assert_exact_prefix(
            recovery["ordered_target_plan"], recovery["successful_prefix"], recovery["prefix_records"],
        )

    def _assert_exact_prefix(self, ordered: list[str], prefix: list[str], records: dict) -> None:
        observed = self._observe_migration_records(ordered)
        if set(observed) != set(prefix):
            raise ExecutionError(
                "TARGET_MIGRATION_PREAPPLIED",
                f"applied target migrations {sorted(observed)!r} are not exactly the updater-owned prefix {prefix!r}",
                manual=True,
            )
        if any(observed[ref] != records[ref] for ref in prefix):
            raise ExecutionError(
                "TARGET_MIGRATION_PREAPPLIED",
                "an updater-owned migration row was unapplied, re-applied or rewritten outside the updater",
                manual=True,
            )

    def _recovery_evidence_problem(self, state: dict, evidence) -> str | None:
        """Why `evidence` is not a self-consistent protocol-6 record of
        `state`'s own job, or None. Checks the closed schema, the job
        identity, the trusted-plan identity recorded by that job, the
        prefix/record shape, and the checkpoint. It never proves anything
        about the live database -- callers compare against observation."""
        if not isinstance(evidence, dict) or set(evidence) != RECOVERY_EVIDENCE_FIELDS:
            return "evidence schema is not the closed protocol-6 shape"
        if (evidence.get("schema_version") != 1
                or evidence.get("classification") != "UPDATER_OWNED_PARTIAL_PREFIX"
                or evidence.get("evidence_job_id") != state.get("job_id")):
            return "evidence identity does not belong to this job"
        trusted = state.get("trusted_plan")
        if (not isinstance(trusted, dict)
                or evidence.get("release_id") != trusted.get("target_release_id")
                or evidence.get("target_commit") != trusted.get("target_commit")
                or evidence.get("trusted_plan_fingerprint") != trusted.get("fingerprint")):
            return "evidence does not match the job's own trusted plan"
        if (not isinstance(evidence.get("manifest_sha256"), str) or not _HEX64.fullmatch(evidence["manifest_sha256"])
                or not isinstance(evidence.get("migration_plan_digest"), str)
                or not _HEX64.fullmatch(evidence["migration_plan_digest"])
                or evidence.get("authorization_source") not in AUTHORIZATION_SOURCES):
            return "evidence digests or authorization source are invalid"
        plan_refs = evidence.get("ordered_target_plan")
        prefix = evidence.get("successful_prefix")
        records = evidence.get("prefix_records")
        if (not isinstance(plan_refs, list) or not plan_refs
                or any(not isinstance(ref, str) or not _MIGRATION_REF_RE.fullmatch(ref) for ref in plan_refs)
                or len(plan_refs) != len(set(plan_refs))
                or not isinstance(prefix, list) or prefix != plan_refs[:len(prefix)]):
            return "evidence plan/prefix is not an ordered contiguous prefix"
        in_flight = evidence.get("in_flight_migration")
        if in_flight is not None and (len(prefix) >= len(plan_refs) or in_flight != plan_refs[len(prefix)]):
            return "evidence in-flight migration is not the next migration after the prefix"
        proven = evidence.get("in_flight_absence_proven")
        if not isinstance(proven, bool) or (proven and in_flight is None):
            return "evidence in-flight absence proof is invalid"
        if (not isinstance(records, dict) or set(records) != set(prefix)
                or any(not isinstance(value, dict) or set(value) != {"id", "applied"}
                       or isinstance(value["id"], bool) or not isinstance(value["id"], int)
                       or not isinstance(value["applied"], str) or not _APPLIED_UTC_RE.fullmatch(value["applied"])
                       for value in records.values())):
            return "evidence prefix records are missing or malformed"
        checkpoint = evidence.get("checkpoint")
        if (not isinstance(checkpoint, dict)
                or checkpoint.get("target_release_id") != trusted.get("target_release_id")
                or checkpoint.get("target_commit") != trusted.get("target_commit")
                or checkpoint.get("installed_release_id") != trusted.get("installed_release_id")
                or checkpoint.get("installed_commit") != trusted.get("installed_commit")
                or not verify_checkpoint(self.config.checkpoint_root, checkpoint)):
            return "evidence checkpoint is missing, unverifiable, or for another transition"
        return None

    def _finalize_recovery_from_observation(self, job_id: str, classification: str, detail: str) -> None:
        """The one canonical finalization for an interrupted/failed migration job.

        Called from every exit path that can follow migration mutation:
        a migration ExecutionError, any other failure after
        migration_started, and AMBIGUOUS_INTERRUPTED_MIGRATION on resume
        (the hard-kill case: a migration committed and the worker died
        before its evidence write). The database recorder, not the last
        evidence write, decides what was applied:

        * the recorded identity must be this job's own, self-consistent;
        * the observed applied target migrations must be exactly a
          contiguous prefix of the recorded ordered plan (no gaps, no
          extras, no out-of-order application);
        * observation may only EXTEND the recorded prefix, and every
          previously recorded row must still have its exact id/applied;
        * only then is the observed prefix persisted and finalized.

        Anything else leaves the evidence unfinalized, so a later job
        keeps failing closed with TARGET_MIGRATION_PREAPPLIED. This never
        raises: a finalization problem must not mask the original failure.
        """
        try:
            state = self.store.load(job_id)
            evidence = state.get("migration_recovery")
            if not isinstance(evidence, dict) or evidence.get("finalized") is not False:
                return
            problem = self._recovery_evidence_problem(state, evidence)
            if problem is not None:
                self.store.append_log(job_id, f"recovery evidence left unfinalized: {problem}")
                return
            ordered = evidence["ordered_target_plan"]
            observed = self._observe_migration_records_when_ready(ordered)
            prefix = _contiguous_prefix(ordered, observed)
            if set(observed) != set(prefix):
                self.store.append_log(
                    job_id, "recovery evidence left unfinalized: applied target migrations are not a contiguous prefix",
                )
                return
            recorded = evidence["successful_prefix"]
            in_flight = evidence["in_flight_migration"]
            # The in-flight migration is claimable only if a post-marker
            # observation durably proved it absent before the command ran.
            claimable = in_flight is not None and evidence["in_flight_absence_proven"] is True
            allowed = [recorded] + ([[*recorded, in_flight]] if claimable else [])
            if prefix not in allowed:
                # Only the one migration the updater was running may be newly
                # claimed; anything else applied since is not provably ours.
                self.store.append_log(
                    job_id,
                    "recovery evidence left unfinalized: observed prefix extends beyond the recorded prefix "
                    "and its single in-flight migration",
                )
                return
            if any(observed[ref] != evidence["prefix_records"][ref] for ref in recorded):
                self.store.append_log(
                    job_id, "recovery evidence left unfinalized: previously recorded migration rows changed",
                )
                return
            if not prefix:
                return
            finalized = dict(evidence)
            finalized.update(
                successful_prefix=list(prefix),
                prefix_records={ref: observed[ref] for ref in prefix},
                in_flight_migration=None,
                in_flight_absence_proven=False,
                failure_classification=classification[:64],
                failure_detail=" ".join(detail.split())[:4000],
                first_remaining_migration=ordered[len(prefix)] if len(prefix) < len(ordered) else None,
                permitted_action="retry_same_exact_release",
                finalized=True,
            )
            self.store.update(job_id, migration_recovery=finalized)
            self.store.append_log(
                job_id, f"recovery evidence finalized from database observation: {len(prefix)} of {len(ordered)} applied",
            )
        except Exception as exc:  # noqa: BLE001 -- never mask the job's own failure
            try:
                self.store.append_log(job_id, f"recovery evidence left unfinalized: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _find_partial_recovery(self, plan: TrustedPlan, payload: dict, current_payload: dict) -> dict | None:
        """Prior protected evidence that proves the already-applied part of
        this exact transition was applied by the updater, or None.

        The burden of proof stays on the updater. A candidate is accepted
        only when ALL of these hold:

        * it is finalized, self-consistent evidence of a failed job (see
          _recovery_evidence_problem), for this exact release, target
          commit, manifest bytes and trusted-plan fingerprint;
        * its recorded full plan is exactly this transition's migrations
          and its recorded prefix is exactly what is applied now;
        * every prefix migration's live django_migrations row still has
          the exact id/applied identity the updater observed.

        A complete prefix (every migration applied, the job died before
        verification) is accepted too: the retry then runs no migration
        but still re-validates, verifies, and advances source normally.
        """
        if "nodes" not in current_payload:
            # Compatibility for unit-test/legacy internal callers. A real
            # probe always supplies nodes; recovery is never inferred without it.
            return None
        closure = _dependency_closure(payload["nodes"], plan.migrations_required)
        transition = closure - set(current_payload["nodes"])
        applied_transition = transition & set(payload["applied"])
        if not applied_transition:
            return None
        # A terminal job whose crash finalization could not reach PostgreSQL
        # (power-loss restart) left valid but unfinalized evidence. Retry the
        # SAME exact observation-based finalization now; it succeeds only if
        # every ownership invariant holds (identity, contiguous prefix, row
        # identity, at most the single recorded in-flight migration).
        for state in self.store.list_states():
            evidence = state.get("migration_recovery")
            if (isinstance(evidence, dict) and evidence.get("finalized") is False
                    and state.get("state") in {"failed", "manual_intervention_required"}
                    and "migration_started" in state.get("milestones", [])
                    and evidence.get("release_id") == plan.target_release_id
                    and evidence.get("target_commit") == plan.target_commit
                    and evidence.get("trusted_plan_fingerprint") == plan.fingerprint):
                self._finalize_recovery_from_observation(
                    state["job_id"], state.get("failure_classification") or "INTERRUPTED",
                    state.get("failure_detail") or "finalized by a later exact retry",
                )
        candidates = []
        for state in self.store.list_states():
            evidence = state.get("migration_recovery")
            if (not isinstance(evidence, dict) or evidence.get("finalized") is not True
                    or state.get("state") not in {"failed", "manual_intervention_required"}):
                continue
            if self._recovery_evidence_problem(state, evidence) is not None:
                continue
            plan_refs = evidence["ordered_target_plan"]
            prefix = evidence["successful_prefix"]
            if (not prefix
                    or evidence["release_id"] != plan.target_release_id
                    or evidence["target_commit"] != plan.target_commit
                    or evidence["manifest_sha256"] != payload["manifest_sha256"]
                    or evidence["trusted_plan_fingerprint"] != plan.fingerprint
                    or evidence["checkpoint"].get("installed_release_id") != plan.installed_release_id
                    or evidence["checkpoint"].get("installed_commit") != plan.installed_commit
                    or set(plan_refs) != transition
                    or set(prefix) != applied_transition):
                continue
            candidates.append((len(prefix), state.get("updated_at", ""), evidence))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        evidence = candidates[0][2]
        # Final ownership decision point #1: the live recorder, not the probe
        # read moments ago, must show exactly the owned prefix and rows.
        self._assert_owned_prefix(evidence)
        return evidence

    def _validate_target_schema(self, plan: TrustedPlan, payload: dict, current_payload: dict, job_id: str,
                                *, migration_already_started: bool,
                                recovery: dict | None = None, trusted_tip: str | None = None) -> tuple[str, ...]:
        # Which authority actually satisfied the manual-operation gate for
        # THIS validation: "not_required" (no manual operations), "central"
        # (trusted companion covered every local manual operation) or
        # "local" (exact station approval). Read by execute() for the
        # recovery evidence -- never inferred later from display state.
        self.last_authorization_source = "not_required"
        if payload["conflicts"]:
            raise ExecutionError("TARGET_MIGRATION_CONFLICT", "target migration graph reports conflicts")
        if payload["replacements"]:
            raise ExecutionError("TARGET_MIGRATION_AMBIGUOUS", "squashed/replacement migrations require manual review", manual=True)
        closure = _dependency_closure(payload["nodes"], plan.migrations_required)
        applied = set(payload["applied"])
        actual = tuple(item["ref"] for item in payload["plan"])
        expected_actual = closure - applied
        if set(actual) != expected_actual or len(actual) != len(set(actual)):
            raise ExecutionError(
                "TARGET_MIGRATION_MISMATCH",
                f"target plan differs from manifest dependency closure; expected={sorted(expected_actual)!r}, actual={list(actual)!r}",
            )
        if "nodes" in current_payload:
            transition = closure - set(current_payload["nodes"])
            already_applied_transition = transition & applied
        else:
            # Preserve the pre-protocol-6 direct-call contract; protected
            # recovery itself requires the full current probe graph above.
            already_applied_transition = set(plan.migrations_required) & set(current_payload.get("applied", []))
        if already_applied_transition and not migration_already_started and recovery is None:
            raise ExecutionError(
                "TARGET_MIGRATION_PREAPPLIED",
                f"target transition migration(s) lack exact protected-updater evidence: {sorted(already_applied_transition)!r}",
                manual=True,
            )
        if actual and plan.migration_compatibility != "additive":
            raise ExecutionError("MIGRATION_NOT_AUTOMATABLE", "manifest does not classify target migration work as additive", manual=True)
        # Reviewed migration approval: the mechanical classifier above
        # (MIGRATION_NOT_AUTOMATABLE) and everything before it in this
        # function are UNCHANGED and unconditional -- an approval can
        # never apply to a manifest declaring destructive work, a
        # conflicting/ambiguous/mismatched graph, or pre-applied
        # migrations. Only the narrow "this operation is outside the
        # mechanical automatic allowlist" gate below can be satisfied by
        # a reviewed approval, and only for the EXACT plan payload
        # (itself freshly, independently recomputed by updatecenter_probe
        # against the staged target we just fetched -- never a digest
        # supplied by Django/a browser) already reports a match for.
        # See docs/UPDATE_CENTER.md's "Reviewed migration approval".
        authorization_payload = payload
        if recovery is not None:
            if (payload.get("recovery_plan") is None
                    or [item["ref"] for item in payload["recovery_plan"]] != recovery["ordered_target_plan"]
                    or payload.get("recovery_migration_plan_digest") != recovery["migration_plan_digest"]):
                raise ExecutionError("TARGET_MIGRATION_PREAPPLIED", "reconstructed recovery plan does not match durable evidence", manual=True)
            # Final ownership decision point #2: the recovery probe may not
            # widen the applied transition beyond the owned prefix, and the
            # live recorder rows must still be exactly the owned ones.
            if already_applied_transition != set(recovery["successful_prefix"]):
                raise ExecutionError(
                    "TARGET_MIGRATION_PREAPPLIED",
                    f"applied target migrations {sorted(already_applied_transition)!r} differ from the owned prefix",
                    manual=True,
                )
            self._assert_owned_prefix(recovery)
            authorization_payload = {
                **payload,
                "plan": payload["recovery_plan"],
                "migration_plan_digest": payload["recovery_migration_plan_digest"],
                "manual_operations": payload["recovery_manual_operations"],
            }
        if authorization_payload["manual_operations"]:
            # Trusted companions (deploy/migration_authorizations/<release>.json)
            # of every release in the plan, discovered by path convention and
            # validated release-locally. Malformed or wrongly-provenanced
            # companions raise ReleaseError and fail the job closed.
            authorizations = {}
            if trusted_tip is not None:
                authorizations = load_plan_authorizations(self.repository, trusted_tip, plan)
            if central_authorization_covers(
                authorizations, plan_items=authorization_payload["plan"],
                manual_operations=authorization_payload["manual_operations"],
            ):
                self.store.append_log(
                    job_id,
                    f"trusted companion authorization(s) for {sorted(authorizations)!r} cover every one of this "
                    f"plan's {len(authorization_payload['manual_operations'])} manual operation(s); proceeding",
                )
                self.last_authorization_source = "central"
                return actual
            try:
                identity = approval_identity(
                    target_release_id=authorization_payload["release_id"],
                    target_commit=authorization_payload["target_commit"],
                    target_manifest_sha256=authorization_payload["manifest_sha256"],
                    migration_plan_digest=authorization_payload["migration_plan_digest"],
                    trusted_plan_fingerprint=plan.fingerprint,
                )
                approval = self.approval_store.find(identity)
            except ApprovalError as exc:
                raise ExecutionError(
                    "MIGRATION_APPROVAL_STORE_INVALID",
                    f"protected approval store rejected the exact plan identity: {exc}",
                    manual=True,
                ) from exc
            if approval is not None:
                self.store.append_log(
                    job_id,
                    f"reviewed migration approval {approval['approval_id']} (by {approval['approved_by_username']} at "
                    f"{approval['approved_at']}) matched digest {authorization_payload['migration_plan_digest']} -- proceeding",
                )
                self.last_authorization_source = "local"
            else:
                raise ExecutionError(
                    "MIGRATION_OPERATION_MANUAL",
                    "; ".join(
                        f"{entry['ref']} contains {entry['operation']}: {entry['detail']}"
                        for entry in authorization_payload["manual_operations"]
                    ),
                    manual=True,
                    migration_plan_review={
                        "release_id": authorization_payload["release_id"],
                        "target_commit": authorization_payload["target_commit"],
                        "manifest_sha256": authorization_payload["manifest_sha256"],
                        "migration_plan_digest": authorization_payload["migration_plan_digest"],
                        "trusted_plan_fingerprint": plan.fingerprint,
                        "manual_operations": authorization_payload["manual_operations"],
                    },
                )
        return actual

    def _advance_source(self, plan: TrustedPlan):
        before = self._live_identity()
        if before["head"] != plan.installed_commit:
            raise ExecutionError("LIVE_CHECKOUT_CHANGED", "live HEAD changed since job validation")
        fetched = self._app_git([
            "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{self.config.trusted_branch}",
        ], timeout=180)
        if not fetched.ok:
            raise ExecutionError("LIVE_FETCH_FAILED", "application-user fetch of the trusted branch failed")
        exists = self._app_git(["cat-file", "-e", f"{plan.target_commit}^{{commit}}"])
        ancestor = self._app_git(["merge-base", "--is-ancestor", plan.installed_commit, plan.target_commit])
        if not exists.ok or ancestor.returncode != 0:
            raise ExecutionError("LIVE_TARGET_NOT_FAST_FORWARD", "exact trusted target is absent or not a fast-forward")
        merged = self._app_git(["merge", "--ff-only", "--no-edit", plan.target_commit], timeout=180)
        if not merged.ok:
            raise ExecutionError("LIVE_SOURCE_ADVANCE_FAILED", "exact fast-forward failed", manual=True)
        after = self._live_identity()
        if after["head"] != plan.target_commit:
            raise ExecutionError("LIVE_SOURCE_VERIFY_FAILED", "live checkout did not land exactly on trusted target", manual=True)

    def _postflight_http(self):
        parsed = urlsplit(self.config.gunicorn_health_url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
        try:
            connection.request("GET", target, headers={"Connection": "close"})
            response = connection.getresponse()
            body = response.read(4097)
            if response.status != 200 or len(body) > 4096:
                raise ExecutionError("GUNICORN_HEALTH_FAILED", "local Gunicorn health response was invalid", manual=True)
        except ExecutionError:
            raise
        except Exception as exc:
            raise ExecutionError("GUNICORN_HEALTH_FAILED", f"local Gunicorn health request failed: {type(exc).__name__}", manual=True) from exc
        finally:
            connection.close()

    def _require_mutation_allowed(self, plan: TrustedPlan, milestones) -> None:
        """D3-K's central pre-mutation gate, called at EVERY production-
        mutating step below (checkpoint/migration, source advancement,
        collectstatic, systemd reconciliation, service restarts) --
        never once at the top of execute(), so a future refactor that
        adds a new mutating step cannot silently forget to gate it (a
        missing call here is a missing call at THAT site, not a
        globally-bypassed check). A complete no-op for an ordinary
        release (plan.protected_runtime is None) -- see runtime_
        handoff.require_mutation_allowed's own docstring for the
        parity guarantee this preserves."""
        try:
            require_mutation_allowed(plan.protected_runtime, milestones)
        except MutationGateError as exc:
            raise ExecutionError("RUNTIME_ACTIVATION_NOT_ACCEPTED", str(exc), manual=True) from exc

    def _load_phase_d_trust_policy(self):
        """D4-G/D4-P: this worker's OWN read of the SAME root-owned
        trust material the supervisor uses (config.phase_d_trust_
        policy_path/phase_d_signer_root, D3-C's own StationConfig
        extension) -- returns None (never raises) when either is
        unconfigured, matching this whole verification step's own
        UNBOOTSTRAPPED_SUPERVISOR fail-closed handling at its one call
        site."""
        path = self.config.phase_d_trust_policy_path
        signer_root = self.config.phase_d_signer_root
        if path is None or signer_root is None:
            return None
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return parse_trust_policy_dict(data, signer_directory=signer_root)
        except (OSError, ValueError, TrustPolicyError):
            return None

    def _authoritative_runtime_state(self, client: SupervisorClient) -> dict:
        state = client.get_runtime_state()
        active_slot = state.get("active_slot")
        active_generation = state.get("active_generation")
        active_descriptor = state.get("active_descriptor_sha256")
        activation_in_flight = state.get("activation_in_flight")
        if (active_slot not in {"A", "B"}
                or isinstance(active_generation, bool)
                or not isinstance(active_generation, int)
                or active_generation < 1
                or not isinstance(active_descriptor, str)
                or not re.fullmatch(r"[0-9a-f]{64}", active_descriptor)
                or not isinstance(activation_in_flight, bool)):
            raise ExecutionError(
                "RUNTIME_STATE_INVALID",
                "supervisor returned an invalid authoritative runtime identity",
                manual=True,
            )
        return state

    def _resolve_candidate_slot(self, activation_socket: Path) -> tuple[str, str, SupervisorClient, dict]:
        client = SupervisorClient(activation_socket)
        runtime_state = self._authoritative_runtime_state(client)
        active_slot = runtime_state["active_slot"]
        candidate_slot = "B" if active_slot == "A" else "A"
        return active_slot, candidate_slot, client, runtime_state

    @staticmethod
    def _already_authoritative_record(plan: TrustedPlan, transition, runtime_state: dict) -> dict:
        return {
            "mode": "already_authoritative",
            "generation": transition.field.generation,
            "descriptor_sha256": transition.field.descriptor_sha256,
            "active_slot": runtime_state["active_slot"],
            "introducing_release_id": transition.release_id,
            "introducing_commit": transition.commit,
            "target_release_id": plan.target_release_id,
            "target_commit": plan.target_commit,
            "trusted_plan_fingerprint": plan.fingerprint,
        }

    def _validate_already_authoritative_satisfaction(
        self, job_id: str, plan: TrustedPlan, transition, activation_socket: Path,
    ) -> None:
        """Re-prove a persisted exact-active gate after every restart.

        The root-owned record is narrow durable evidence, not a substitute for
        the freshly derived plan or the supervisor.  A resumed job must match
        both again before the alternate mutation gate is honored.
        """
        record = self.store.load(job_id).get("protected_runtime_satisfaction")
        client = SupervisorClient(activation_socket)
        try:
            runtime_state = self._authoritative_runtime_state(client)
        except SupervisorClientError as exc:
            raise ExecutionError("RUNTIME_HANDOFF_FAILED", str(exc), manual=False) from exc
        expected = self._already_authoritative_record(plan, transition, runtime_state)
        if (runtime_state["active_generation"] != transition.field.generation
                or runtime_state["active_descriptor_sha256"] != transition.field.descriptor_sha256
                or runtime_state["activation_in_flight"]
                or record != expected):
            raise ExecutionError(
                "RUNTIME_AUTHORITY_EVIDENCE_MISMATCH",
                "persisted already-authoritative evidence does not match the freshly trusted plan "
                "and current supervisor runtime identity",
                manual=True,
            )

    def _execute_runtime_handoff(self, job_id: str, plan: TrustedPlan, protected_runtime_field, milestones: set):
        """D3: the OLD worker's own short pipeline for a job crossing
        a protected-runtime transition -- stage+verify the
        candidate from root-trusted Git, request supervisor
        activation, then YIELD (return without raising and WITHOUT
        calling store.succeed()/store.fail()) so this job stays
        durably "running," open for whichever worker the supervisor
        next starts to resume (D3-H). Never runs a single Phase-B
        mutation call -- see execute()'s own MUTATION_GATE_MILESTONE
        branch, which this function's own milestones never reach on
        their own (mark_candidate_verified is a LOCAL sanity record,
        not runtime_activation_accepted)."""
        try:
            transition = plan.protected_runtime_transition
            if transition is None or transition.field != protected_runtime_field:
                raise ExecutionError(
                    "RUNTIME_PROVENANCE_MISSING",
                    "protected-runtime handoff lacks exact introducing-release provenance",
                    manual=True,
                )

            if MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED not in milestones:
                self.store.milestone(job_id, MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED)
                milestones.add(MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED)

            slots_root = self.config.phase_d_supervisor_slots_root
            activation_socket = self.config.phase_d_supervisor_activation_socket
            if slots_root is None or activation_socket is None:
                raise ExecutionError(
                    "UNBOOTSTRAPPED_SUPERVISOR",
                    "this station's protected_runtime transition requires a Phase-D supervisor, "
                    "but none is configured (phase_d_supervisor_slots_root/activation_socket are null)",
                    manual=True,
                )

            client = SupervisorClient(activation_socket)
            initial_runtime_state = self._authoritative_runtime_state(client)
            active_generation = initial_runtime_state["active_generation"]
            active_descriptor = initial_runtime_state["active_descriptor_sha256"]
            target_generation = protected_runtime_field.generation
            target_descriptor = protected_runtime_field.descriptor_sha256

            if initial_runtime_state["activation_in_flight"]:
                raise ExecutionError(
                    "PROTECTED_RUNTIME_ACTIVATION_IN_FLIGHT",
                    "supervisor reports a protected-runtime activation already in flight",
                    manual=True,
                )

            if active_generation == target_generation and active_descriptor == target_descriptor:
                if (MILESTONE_RUNTIME_CANDIDATE_STAGED in milestones
                        or MILESTONE_RUNTIME_CANDIDATE_VERIFIED in milestones
                        or MILESTONE_RUNTIME_ACTIVATION_REQUESTED in milestones
                        or self.store.load(job_id).get("protected_runtime_candidate") is not None):
                    raise ExecutionError(
                        "RUNTIME_HANDOFF_STATE_AMBIGUOUS",
                        "exact active runtime was observed, but this job also carries unfinished "
                        "candidate-handoff evidence",
                        manual=True,
                    )
                record = self._already_authoritative_record(plan, transition, initial_runtime_state)
                self.store.update(job_id, protected_runtime_satisfaction=record)
                if MILESTONE_RUNTIME_ALREADY_AUTHORITATIVE not in milestones:
                    self.store.milestone(job_id, MILESTONE_RUNTIME_ALREADY_AUTHORITATIVE)
                    milestones.add(MILESTONE_RUNTIME_ALREADY_AUTHORITATIVE)
                self.store.append_log(
                    job_id,
                    "exact protected runtime required by the trusted plan is already authoritative; "
                    "candidate staging and activation skipped",
                )
                return None

            if active_generation >= target_generation:
                raise ExecutionError(
                    "PROTECTED_RUNTIME_REPLAY_OR_ROLLBACK",
                    "active protected runtime does not exactly match the trusted target and the target "
                    f"is not newer (active_generation={active_generation}, "
                    f"target_generation={target_generation}, descriptor_match=false)",
                    manual=True,
                )

            if MILESTONE_RUNTIME_CANDIDATE_STAGED not in milestones:
                active_slot = initial_runtime_state["active_slot"]
                candidate_slot = "B" if active_slot == "A" else "A"
                staging = new_supervisor_staging_directory(slots_root)
                try:
                    materialized = materialize_candidate(
                        self.repository, protected_runtime_field, transition.commit, staging,
                    )
                    stage_attestations(
                        self.repository, protected_runtime_field, transition.commit, slots_root, candidate_slot,
                    )
                    stage_descriptor(materialized.descriptor_bytes, slots_root, candidate_slot)
                    trust_policy = self._load_phase_d_trust_policy()
                    if trust_policy is None:
                        raise ExecutionError(
                            "UNBOOTSTRAPPED_SUPERVISOR",
                            "this worker has no configured phase_d_trust_policy_path/phase_d_signer_root -- "
                            "cannot independently verify the staged candidate",
                            manual=True,
                        )
                    outcome = verify_candidate_independently(
                        trust_policy=trust_policy, descriptor_bytes=materialized.descriptor_bytes,
                        bundle_root=staging,
                        attestations_dir=attestations_staging_directory(slots_root, candidate_slot),
                        release_id=transition.release_id,
                        previous_release_id=transition.previous_release_id,
                        previous_generation=active_generation,
                        current_bootstrap_protocol_version=1,
                        current_wire_protocol_version=HANDOFF_WIRE_PROTOCOL,
                    )
                    if not outcome.ok:
                        raise ExecutionError(
                            "CANDIDATE_INDEPENDENT_VERIFICATION_FAILED",
                            "; ".join(outcome.reasons), manual=True,
                        )
                    needed_units = (
                        set(plan.systemd_units_changed) | set(plan.systemd_units_new_required)
                    ) - resolve_known_managed_units(active_policy=self.active_policy)
                    manifest_declared = (
                        set(plan.systemd_units_changed)
                        | set(plan.systemd_units_new_required)
                        | set(plan.systemd_units_new_optional)
                    )
                    unit_violations = verify_new_units_authorized_by_candidate_policy(
                        needed_units=frozenset(needed_units),
                        manifest_declared_units=frozenset(manifest_declared),
                        candidate_policy=outcome.candidate_policy,
                    )
                    if unit_violations:
                        raise ExecutionError(
                            "NEW_MANAGED_UNIT_NOT_AUTHORIZED", "; ".join(unit_violations), manual=True,
                        )

                    # A second authoritative read closes the window between
                    # preflight and publication.  Any concurrent state change
                    # is refused before the previous-LKG slot is replaced.
                    publish_runtime_state = self._authoritative_runtime_state(client)
                    if any(
                        publish_runtime_state[key] != initial_runtime_state[key]
                        for key in (
                            "active_slot", "active_generation", "active_descriptor_sha256",
                            "activation_in_flight",
                        )
                    ):
                        raise ExecutionError(
                            "RUNTIME_STATE_CHANGED_DURING_PREFLIGHT",
                            "authoritative runtime identity changed during candidate preflight",
                            manual=True,
                        )
                    publish_to_candidate_slot(slots_root, candidate_slot, staging, active_slot=active_slot)
                except Exception:
                    if staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)
                    descriptor_staging_path(slots_root, candidate_slot).unlink(missing_ok=True)
                    shutil.rmtree(
                        attestations_staging_directory(slots_root, candidate_slot), ignore_errors=True,
                    )
                    raise
                self.store.update(job_id, protected_runtime_candidate={
                    "generation": protected_runtime_field.generation,
                    "descriptor_sha256": materialized.descriptor_sha256,
                    "candidate_slot": candidate_slot,
                })
                self.store.milestone(job_id, MILESTONE_RUNTIME_CANDIDATE_STAGED)
                milestones.add(MILESTONE_RUNTIME_CANDIDATE_STAGED)

            if MILESTONE_RUNTIME_CANDIDATE_VERIFIED not in milestones:
                # D4-G: THIS worker's own independent re-verification
                # of the just-staged/published candidate -- defense in
                # depth alongside (never instead of) the supervisor's
                # own independent re-verification (always run server-
                # side before ACTIVATION_REQUESTED -- D3-A's own
                # "request is intent, never authorization" rule).
                # Required for THIS worker to safely reason about
                # whether the aggregated plan's new (to THIS
                # worker's active policy) managed unit is legitimately
                # authorized -- see verify_new_units_authorized_by_
                # candidate_policy below.
                record = self.store.load(job_id)["protected_runtime_candidate"]
                candidate_slot = record["candidate_slot"]
                _active_slot, _cs, client, runtime_state = self._resolve_candidate_slot(activation_socket)
                bundle_root = Path(slots_root) / candidate_slot
                descriptor_bytes = descriptor_staging_path(slots_root, candidate_slot).read_bytes()
                trust_policy = self._load_phase_d_trust_policy()
                if trust_policy is None:
                    raise ExecutionError(
                        "UNBOOTSTRAPPED_SUPERVISOR",
                        "this worker has no configured phase_d_trust_policy_path/phase_d_signer_root -- "
                        "cannot independently verify the staged candidate",
                        manual=True,
                    )
                outcome = verify_candidate_independently(
                    trust_policy=trust_policy, descriptor_bytes=descriptor_bytes, bundle_root=bundle_root,
                    attestations_dir=attestations_staging_directory(slots_root, candidate_slot),
                    release_id=transition.release_id,
                    previous_release_id=transition.previous_release_id,
                    previous_generation=runtime_state["active_generation"],
                    current_bootstrap_protocol_version=1, current_wire_protocol_version=HANDOFF_WIRE_PROTOCOL,
                )
                if not outcome.ok:
                    raise ExecutionError(
                        "CANDIDATE_INDEPENDENT_VERIFICATION_FAILED", "; ".join(outcome.reasons), manual=True,
                    )
                needed_units = (
                    set(plan.systemd_units_changed) | set(plan.systemd_units_new_required)
                ) - resolve_known_managed_units(active_policy=self.active_policy)
                # An exact existing template whose bytes change is just as
                # predecessor-diff-checked as a newly added/promoted unit.
                # This matters for the Phase-D Weather transition: the four
                # templates already exist, while the candidate signed policy
                # makes their exact names newly executable by this runtime.
                manifest_declared = (
                    set(plan.systemd_units_changed)
                    | set(plan.systemd_units_new_required)
                    | set(plan.systemd_units_new_optional)
                )
                unit_violations = verify_new_units_authorized_by_candidate_policy(
                    needed_units=frozenset(needed_units), manifest_declared_units=frozenset(manifest_declared),
                    candidate_policy=outcome.candidate_policy,
                )
                if unit_violations:
                    raise ExecutionError("NEW_MANAGED_UNIT_NOT_AUTHORIZED", "; ".join(unit_violations), manual=True)
                self.store.milestone(job_id, MILESTONE_RUNTIME_CANDIDATE_VERIFIED)
                milestones.add(MILESTONE_RUNTIME_CANDIDATE_VERIFIED)

            if MILESTONE_RUNTIME_ACTIVATION_REQUESTED not in milestones:
                record = self.store.load(job_id)["protected_runtime_candidate"]
                _active_slot, _candidate_slot, client, _runtime_state = self._resolve_candidate_slot(activation_socket)
                client.request_activation(
                    transaction_id=job_id, candidate_slot=record["candidate_slot"],
                    candidate_generation=record["generation"], candidate_descriptor_sha256=record["descriptor_sha256"],
                    release_id=transition.release_id,
                    previous_release_id=transition.previous_release_id,
                )
                self.store.milestone(job_id, MILESTONE_RUNTIME_ACTIVATION_REQUESTED)
                milestones.add(MILESTONE_RUNTIME_ACTIVATION_REQUESTED)

            # D3-E/D3-F: SAFE TO YIELD. runtime_activation_requested is
            # now durable (fsync'd via the SAME atomic milestone write
            # every other step uses) -- this is exactly SAFE_YIELD_
            # MILESTONE (runtime_handoff.py). Release this worker's own
            # EXCLUSIVE job-store lock so a candidate worker's own
            # JobStore(...) construction can acquire it -- proof, not
            # convention: see test_phase_d3_lock_ownership.py's own
            # two-real-JobStore-instances test. Reads (store.load,
            # daemon.py's own GET_JOB_STATUS handler) remain legal on
            # this closed store; only exclusive re-acquisition was ever
            # gated by the flock.
            self.store.append_log(job_id, "runtime handoff requested; releasing job-store lock and yielding for candidate resumption")
            self.store.close()
            return self.store.load(job_id)
        except ExecutionError:
            raise
        except (HandoffError, SupervisorClientError) as exc:
            raise ExecutionError("RUNTIME_HANDOFF_FAILED", str(exc), manual=False) from exc

    def _is_authorized_candidate_for(self, job_id: str, protected_runtime_field) -> bool:
        """D4-D: mutation/acceptance authority is bound to the PROCESS
        IDENTITY the supervisor itself assigned at launch time (see
        Executor.__init__'s own docstring), never inferred from job
        state alone. True only when every one of this Executor's own
        expected_handoff_* values (populated ONLY by a real candidate
        launch -- see daemon.py/updaterd.py) matches BOTH the target
        release's own protected_runtime facts and the job_id this
        execute() call was made for. An old worker re-entering
        execute() for an already-yielded job has expected_handoff_*
        all None and can never satisfy this."""
        if self.expected_handoff_generation is None:
            return False
        return (
            self.expected_resumable_job_uuid == job_id
            and self.expected_handoff_generation == protected_runtime_field.generation
            and self.expected_handoff_descriptor_sha256 == protected_runtime_field.descriptor_sha256
        )

    def _accept_runtime_as_candidate(self, job_id: str, protected_runtime_field) -> None:
        """D4-J: the CANDIDATE's own acceptance step -- called only
        once _is_authorized_candidate_for() has already proven this
        exact process is the one the supervisor launched for this exact
        job. By the time this runs, execute()'s own unconditional top-
        of-function fetch+derive_plan()+fingerprint check has ALREADY
        independently re-derived the trusted target plan and verified
        it against the durably-stored expected_plan_fingerprint (D3-I,
        D4-D's own "independently re-derives target plan" requirement
        -- no separate re-derivation needed here). This function's own
        job is narrow: sanity-check this candidate's own identity
        against the job's recorded protected_runtime_candidate facts
        one more time, write MUTATION_GATE_MILESTONE
        (runtime_activation_accepted) durably, and tell the supervisor
        (D4-J: "candidate informs supervisor" -- the ONE fact that may
        legitimize supervisor.commit_transaction()). Deliberately does
        NOT itself continue into the mutation pipeline -- the caller
        (execute()) does that, through the SAME single central barrier
        (_enter_mutation_phase) every other path also passes through;
        this function's only job is to make runtime_activation_accepted
        durable and reported, nothing more."""
        milestones = set(self.store.load(job_id)["milestones"])
        record = self.store.load(job_id).get("protected_runtime_candidate")
        if (not isinstance(record, dict)
                or record.get("generation") != protected_runtime_field.generation
                or record.get("descriptor_sha256") != protected_runtime_field.descriptor_sha256):
            raise ExecutionError(
                "CANDIDATE_IDENTITY_MISMATCH",
                "this candidate's own expected generation/descriptor does not match the job's "
                "recorded protected_runtime_candidate facts",
                manual=True,
            )
        activation_socket = self.config.phase_d_supervisor_activation_socket
        if activation_socket is None:
            raise ExecutionError(
                "UNBOOTSTRAPPED_SUPERVISOR",
                "candidate acceptance requires a configured Phase-D supervisor socket", manual=True,
            )
        if MUTATION_GATE_MILESTONE not in milestones:
            self.store.milestone(job_id, MUTATION_GATE_MILESTONE)
            milestones.add(MUTATION_GATE_MILESTONE)
        try:
            client = SupervisorClient(activation_socket)
            client.confirm_runtime_acceptance(
                transaction_id=job_id, candidate_slot=record["candidate_slot"],
                candidate_generation=record["generation"], candidate_descriptor_sha256=record["descriptor_sha256"],
                resumable_job_uuid=job_id,
            )
        except (SupervisorTransportError, SupervisorRejectedError, SupervisorClientError) as exc:
            # runtime_activation_accepted is ALREADY durable at this
            # point -- per D3-N/D4-J, failure AFTER this milestone
            # never automatically downgrades the runtime and never
            # blocks THIS worker's own progression; it only means the
            # supervisor may not yet know to commit the generation.
            # Logged, not fatal -- mutation proceeds on this worker's
            # own already-accepted authority.
            self.store.append_log(
                job_id, f"could not inform supervisor of runtime acceptance (non-fatal, mutation proceeds): {exc}",
            )
        self.store.append_log(job_id, "runtime activation accepted; entering mutation phase")

    def _enter_mutation_phase(self, plan: TrustedPlan, milestones) -> None:
        """D4-I: the CENTRAL mutation-phase barrier -- ONE explicit,
        unconditional call sitting exactly at the transition point
        between the validation/handoff phase and the mutation phase in
        execute()'s own body, so every path that reaches the mutation
        pipeline (an ordinary release, OR a protected_runtime job that
        just accepted runtime activation above) passes through this
        SAME single checkpoint first -- never merely relying on each
        mutator's own individual check to be the only thing standing
        between "validated" and "mutating." The per-mutator
        require_mutation_allowed() calls throughout the pipeline below
        remain, unchanged, as defense-in-depth: this call and those
        calls share the exact same underlying rule (a no-op for an
        ordinary release, a hard gate on one of the two explicit protected-
        runtime authority milestones for a protected_runtime one), so a bug in one is never silently
        compensated for by the other -- both must independently agree
        mutation is allowed."""
        self._require_mutation_allowed(plan, milestones)

    def execute(self, job_id: str):
        state = self.store.load(job_id)
        if state["state"] in {"succeeded", "failed", "manual_intervention_required"}:
            return state
        milestones = set(state["milestones"])
        if "migration_started" in milestones and "database_verified" not in milestones:
            # The worker died mid-migration (power loss, kill). Automatic
            # retry of THIS job stays forbidden, but the database recorder
            # can still prove exactly which updater-owned prefix committed,
            # so a fresh exact retry is not stranded.
            self._finalize_recovery_from_observation(
                job_id, "AMBIGUOUS_INTERRUPTED_MIGRATION",
                "worker interrupted after migration_started; prefix finalized from database observation",
            )
            return self.store.fail(
                job_id, "AMBIGUOUS_INTERRUPTED_MIGRATION",
                "migration started without a durable verified-completion milestone; automatic retry is forbidden",
                manual=True,
            )
        self.store.update(job_id, state="running", current_step="validating_request")
        staged: StagedSource | None = None

        def cleanup_staging():
            nonlocal staged
            try:
                cleanup(self.config.staging_root, job_id)
                staged = None
            except (StagingError, OSError) as cleanup_exc:
                self.store.append_log(job_id, f"staging cleanup also failed: {cleanup_exc}")

        try:
            live = self._live_identity()
            trusted_tip = self.repository.fetch()
            self.store.milestone(job_id, "trusted_source_fetched")

            basis_head = live["head"]
            previous_plan = state.get("trusted_plan")
            source_already_at_target = bool(
                isinstance(previous_plan, dict)
                and "database_verified" in milestones
                and live["head"] == previous_plan.get("target_commit")
            )
            if ("source_advanced" in milestones or source_already_at_target) and isinstance(previous_plan, dict):
                basis_head = previous_plan.get("installed_commit", basis_head)
            known_units = resolve_known_managed_units(active_policy=self.active_policy)
            plan = derive_plan(
                self.repository, trusted_tip, basis_head,
                state["requested_target_release_id"], known_units=known_units,
            )
            if plan.fingerprint != state["expected_plan_fingerprint"]:
                raise ExecutionError("PLAN_FINGERPRINT_MISMATCH", "root-derived plan does not match the requested plan fingerprint")
            blockers = manual_blockers(plan, known_units=known_units)
            if blockers:
                raise ExecutionError("MANUAL_PREREQUISITE", ", ".join(blockers), manual=True)
            plan_record = dataclass_to_dict(plan)
            self.store.update(job_id, trusted_plan=plan_record)
            self.store.milestone(job_id, "trusted_plan_validated")

            # Update Center Phase D, D4: three-way branch for any plan
            # crossing an effective protected-runtime transition (plan.
            # protected_runtime, set by derive_plan()). Never a
            # simple binary "handoff needed or not" -- D4-D's own
            # "prove old and new workers can never mutate the same job
            # concurrently" requires distinguishing exactly which of
            # three roles THIS process may legitimately play for this
            # job right now:
            #
            #   1. One mutation-gate milestone already present -- either
            #      activation was accepted by this job's candidate, or this
            #      job durably proved the exact trusted runtime was already
            #      authoritative. The latter is re-proved against the
            #      supervisor on every resume before falling through.
            #   2. SAFE_YIELD_MILESTONE present, acceptance not yet --
            #      a handoff is already in flight. ONLY a process the
            #      supervisor actually launched as THIS exact candidate
            #      (self.expected_handoff_* populated and matching) may
            #      perform the candidate's own acceptance step
            #      (_execute_candidate_acceptance) and then fall
            #      through to the pipeline. Any OTHER process --
            #      critically including the OLD worker re-entering
            #      execute() for a job it already yielded -- takes
            #      NEITHER action: it must never re-stage, never
            #      re-request, never mutate. It simply reports the
            #      job's current durable state and returns.
            #   3. Neither milestone present -- this is the OLD
            #      worker's own first pass: stage+verify+request
            #      activation, then YIELD (_execute_runtime_handoff).
            if handoff_required(plan.protected_runtime):
                transition = plan.protected_runtime_transition
                if transition is None:
                    raise ExecutionError(
                        "RUNTIME_PROVENANCE_MISSING",
                        "protected-runtime plan lacks exact introducing-release provenance",
                        manual=True,
                    )
                activation_socket = self.config.phase_d_supervisor_activation_socket
                if activation_socket is None:
                    raise ExecutionError(
                        "UNBOOTSTRAPPED_SUPERVISOR",
                        "this station's protected_runtime transition requires a Phase-D supervisor socket",
                        manual=True,
                    )

                # A persisted exact-active proof is intentionally not
                # self-authenticating.  Revalidate it against the freshly
                # derived trusted plan and the supervisor on every resume.
                if MILESTONE_RUNTIME_ALREADY_AUTHORITATIVE in milestones:
                    self._validate_already_authoritative_satisfaction(
                        job_id, plan, transition, activation_socket,
                    )

            if handoff_required(plan.protected_runtime) and not mutation_gate_satisfied(milestones):
                if SAFE_YIELD_MILESTONE in milestones:
                    if not self._is_authorized_candidate_for(job_id, plan.protected_runtime):
                        self.store.append_log(
                            job_id,
                            "execute() re-entered for a job already past its safe-yield boundary by a process "
                            "not authorized as this job's candidate -- taking no further action",
                        )
                        return self.store.load(job_id)
                    self._accept_runtime_as_candidate(job_id, plan.protected_runtime)
                    milestones = set(self.store.load(job_id)["milestones"])
                    # Falls through below -- an authorized candidate that
                    # just accepted runtime activation proceeds into the
                    # SAME mutation pipeline an ordinary release uses,
                    # through the SAME central barrier immediately below.
                else:
                    handoff_result = self._execute_runtime_handoff(
                        job_id, plan, plan.protected_runtime, milestones,
                    )
                    if handoff_result is not None:
                        return handoff_result
                    milestones = set(self.store.load(job_id)["milestones"])

            # D4-I: central mutation-phase barrier -- see
            # _enter_mutation_phase's own docstring. Every mutating
            # call below this line is reachable only after this check.
            self._enter_mutation_phase(plan, milestones)

            current_payload = self._validate_current_schema() if "source_advanced" not in milestones else {"applied": []}
            self.store.milestone(job_id, "current_schema_validated")

            job_stage = self.config.staging_root / job_id
            if job_stage.exists():
                cleanup(self.config.staging_root, job_id)
            staged = materialize(self.repository, plan.target_commit, self.config.staging_root, job_id)
            self.store.milestone(job_id, "target_staged")
            target_payload = self._probe(
                staged.source_root, release_id=plan.target_release_id, target_commit=plan.target_commit,
            )
            recovery = self._find_partial_recovery(plan, target_payload, current_payload)
            if recovery is not None:
                target_payload = self._probe(
                    staged.source_root,
                    release_id=plan.target_release_id,
                    target_commit=plan.target_commit,
                    recovery_plan_refs=tuple(recovery["ordered_target_plan"]),
                )
            actual_migrations = self._validate_target_schema(
                plan, target_payload, current_payload, job_id,
                migration_already_started="migration_started" in milestones,
                recovery=recovery, trusted_tip=trusted_tip,
            )
            self.store.milestone(job_id, "target_schema_validated")

            if actual_migrations and "database_verified" not in milestones:
                self._run_migration_preflights(staged.source_root, actual_migrations)
                self.store.milestone(job_id, "migration_preflight_passed")
                self._require_mutation_allowed(plan, milestones)
                checkpoint = recovery["checkpoint"] if recovery is not None else state.get("checkpoint")
                if not checkpoint or not verify_checkpoint(self.config.checkpoint_root, checkpoint):
                    checkpoint = create_checkpoint(
                        self.config, self.runner, job_id=job_id,
                        installed_release=plan.installed_release_id,
                        installed_commit=plan.installed_commit,
                        target_release=plan.target_release_id,
                        target_commit=plan.target_commit,
                    )
                self.store.update(job_id, checkpoint=checkpoint)
                self.store.milestone(job_id, "checkpoint_created")
                full_plan = (
                    list(recovery["ordered_target_plan"])
                    if recovery is not None
                    else [item["ref"] for item in target_payload["plan"]]
                )
                successful_prefix = list(recovery["successful_prefix"]) if recovery is not None else []
                digest = (
                    target_payload["recovery_migration_plan_digest"]
                    if recovery is not None else target_payload["migration_plan_digest"]
                )
                prefix_records = dict(recovery["prefix_records"]) if recovery is not None else {}
                # Identity and plan are durable BEFORE migration_started, so
                # any later interruption can be finalized purely from what
                # the database recorder shows (_finalize_recovery_from_observation).
                recovery_record = {
                    "schema_version": 1,
                    "classification": "UPDATER_OWNED_PARTIAL_PREFIX",
                    "evidence_job_id": job_id,
                    "prior_job_id": recovery["evidence_job_id"] if recovery is not None else None,
                    "release_id": plan.target_release_id,
                    "target_commit": plan.target_commit,
                    "manifest_sha256": target_payload["manifest_sha256"],
                    "migration_plan_digest": digest,
                    "trusted_plan_fingerprint": plan.fingerprint,
                    "ordered_target_plan": full_plan,
                    "successful_prefix": successful_prefix,
                    "prefix_records": prefix_records,
                    "in_flight_migration": None,
                    "in_flight_absence_proven": False,
                    "checkpoint": checkpoint,
                    "failure_classification": "",
                    "failure_detail": "",
                    "continued_from_job_id": recovery["evidence_job_id"] if recovery is not None else None,
                    "authorization_source": self.last_authorization_source,
                    "first_remaining_migration": full_plan[len(successful_prefix)],
                    "permitted_action": "retry_same_exact_release",
                    "finalized": False,
                }
                if recovery is not None:
                    # Final ownership decision point #3: nothing may have
                    # changed between target validation and the first command.
                    self._assert_owned_prefix(recovery)
                self.store.update(job_id, migration_recovery=recovery_record)
                self.store.milestone(job_id, "migration_started")
                for ref in actual_migrations:
                    expected_after_command = [*successful_prefix, ref]
                    app_label, migration_name = ref.split(".", 1)
                    # 1. Durable in-flight marker (absence not yet proven).
                    recovery_record["in_flight_migration"] = ref
                    recovery_record["in_flight_absence_proven"] = False
                    self.store.update(job_id, migration_recovery=recovery_record)
                    # 2. Fresh observation AFTER the marker: the transition must
                    # still be exactly the owned prefix with identical rows, so
                    # `ref` itself is absent. A migration applied by anyone in
                    # the gap before this proof is never the updater's: fail
                    # closed without invoking migrate (which would be a no-op
                    # that appears to "apply" it).
                    self._assert_exact_prefix(full_plan, successful_prefix, prefix_records)
                    # 3. Durable proof; only now may a crash claim `ref`.
                    recovery_record["in_flight_absence_proven"] = True
                    self.store.update(job_id, migration_recovery=recovery_record)
                    result, settings = self._run_app(
                        staged.source_root,
                        ["migrate", app_label, migration_name, "--noinput", "--skip-checks"],
                        timeout=1800,
                    )
                    # The recorder is the authority: what is applied, and the
                    # exact row identity of each applied prefix migration.
                    observed = self._observe_migration_records(full_plan)
                    observed_prefix = _contiguous_prefix(full_plan, observed)
                    if set(observed) != set(observed_prefix):
                        raise ExecutionError(
                            "MIGRATION_STATE_DRIFT", "applied target migrations are not a contiguous trusted prefix",
                            manual=True,
                        )
                    if (observed_prefix[:len(successful_prefix)] != successful_prefix
                            or any(observed[item] != prefix_records[item] for item in successful_prefix)):
                        raise ExecutionError(
                            "MIGRATION_STATE_DRIFT", "previously recorded target migration rows changed during the job",
                            manual=True,
                        )
                    if (len(observed_prefix) > len(successful_prefix)
                            and observed_prefix != expected_after_command):
                        raise ExecutionError(
                            "MIGRATION_STATE_DRIFT",
                            "target migrations beyond the in-flight migration appeared during the job",
                            manual=True,
                        )
                    if len(observed_prefix) > len(successful_prefix):
                        successful_prefix = observed_prefix
                        prefix_records = {item: observed[item] for item in successful_prefix}
                        recovery_record["successful_prefix"] = list(successful_prefix)
                        recovery_record["prefix_records"] = dict(prefix_records)
                        recovery_record["first_remaining_migration"] = (
                            full_plan[len(successful_prefix)] if len(successful_prefix) < len(full_plan) else None
                        )
                    # One atomic state write records the observed prefix AND
                    # clears the in-flight marker and its absence proof.
                    recovery_record["in_flight_migration"] = None
                    recovery_record["in_flight_absence_proven"] = False
                    self.store.update(job_id, migration_recovery=recovery_record)
                    if not result.ok:
                        raise ExecutionError("MIGRATION_FAILED", _decode(result, settings), manual=True)
                    if successful_prefix != expected_after_command:
                        raise ExecutionError(
                            "MIGRATION_VERIFY_FAILED", "migration command did not produce its exact trusted prefix",
                            manual=True,
                        )
                verified = self._probe(staged.source_root)
                if verified["conflicts"] or verified["replacements"] or verified["plan"]:
                    raise ExecutionError("MIGRATION_VERIFY_FAILED", "target schema is not clean after migration", manual=True)
                recovery_record["finalized"] = True
                recovery_record["first_remaining_migration"] = None
                recovery_record["permitted_action"] = "none"
                self.store.update(job_id, migration_recovery=recovery_record)
            elif recovery is not None and "database_verified" not in milestones:
                # Complete-prefix continuation: nothing left to migrate, but no
                # exemption -- ordinary verification, then final ownership
                # decision point #4 immediately before database_verified.
                verified = self._probe(staged.source_root)
                if verified["conflicts"] or verified["replacements"] or verified["plan"]:
                    raise ExecutionError("MIGRATION_VERIFY_FAILED", "target schema is not clean for a complete prefix", manual=True)
                self._assert_owned_prefix(recovery)
            self.store.milestone(job_id, "database_verified")

            if "source_advanced" not in milestones:
                if source_already_at_target:
                    self.store.append_log(
                        job_id,
                        "exact target source already present after database verification; recording recovered advancement milestone",
                    )
                else:
                    self._require_mutation_allowed(plan, milestones)
                    self._advance_source(plan)
            else:
                live_after_resume = self._live_identity()
                if live_after_resume["head"] != plan.target_commit:
                    raise ExecutionError("LIVE_SOURCE_VERIFY_FAILED", "recorded source milestone disagrees with live HEAD", manual=True)
            self.store.milestone(job_id, "source_advanced")

            if plan.collectstatic_required and "static_collected" not in milestones:
                self._require_mutation_allowed(plan, milestones)
                result, settings = self._run_app(self.config.application_root, ["collectstatic", "--noinput", "--skip-checks"], timeout=600)
                if not result.ok:
                    raise ExecutionError("COLLECTSTATIC_FAILED", _decode(result, settings), manual=True)
            self.store.milestone(job_id, "static_collected")

            if "systemd_reconciled" not in milestones:
                self._require_mutation_allowed(plan, milestones)
                self.systemd.reconcile(staged.source_root, plan)
            self.store.milestone(job_id, "systemd_reconciled")
            if "services_restarted" not in milestones:
                self._require_mutation_allowed(plan, milestones)
                for service in plan.services_requiring_restart:
                    slug = service.replace("-", "_")
                    started_marker = f"service_restart_started_{slug}"
                    completed_marker = f"service_restarted_{slug}"
                    if completed_marker in milestones:
                        continue
                    if started_marker in milestones:
                        raise ExecutionError(
                            "AMBIGUOUS_INTERRUPTED_SERVICE_RESTART",
                            f"service restart for {service} began without a durable completion milestone",
                            manual=True,
                        )
                    self.store.milestone(job_id, started_marker)
                    milestones.add(started_marker)
                    self.systemd.restart_declared((service,))
                    self.store.milestone(job_id, completed_marker)
                    milestones.add(completed_marker)
            self.store.milestone(job_id, "services_restarted")

            post_live = self._live_identity()
            if post_live["head"] != plan.target_commit:
                raise ExecutionError("POSTFLIGHT_SOURCE_FAILED", "postflight live source identity mismatch", manual=True)
            post_schema = self._probe(self.config.application_root)
            if post_schema["conflicts"] or post_schema["replacements"] or post_schema["plan"]:
                raise ExecutionError("POSTFLIGHT_SCHEMA_FAILED", "postflight schema is not clean", manual=True)
            if "isadoraair-gunicorn" in plan.services_requiring_restart:
                self._postflight_http()
            self.store.milestone(job_id, "postflight_complete")
            cleanup_staging()
            self.store.milestone(job_id, "staging_cleaned")
            return self.store.succeed(job_id)
        except ExecutionError as exc:
            cleanup_staging()
            self._finalize_recovery_from_observation(job_id, exc.classification, exc.detail)
            return self.store.fail(
                job_id, exc.classification, exc.detail, manual=exc.manual,
                migration_plan_review=exc.migration_plan_review,
            )
        except (ReleaseError, StagingError, CheckpointError, SystemdError, ProtectionError, JobError, OSError) as exc:
            cleanup_staging()
            current = self.store.load(job_id)
            crossed_mutation_boundary = bool(
                {"migration_started", "database_verified", "source_advanced"}
                & set(current.get("milestones", []))
            )
            if "migration_started" in current.get("milestones", []):
                self._finalize_recovery_from_observation(job_id, "SAFE_EXECUTION_FAILURE", str(exc))
            return self.store.fail(
                job_id, "SAFE_EXECUTION_FAILURE", str(exc),
                manual=crossed_mutation_boundary or isinstance(exc, (SystemdError, ProtectionError)),
            )


def dataclass_to_dict(plan: TrustedPlan) -> dict:
    result = {
        "installed_release_id": plan.installed_release_id,
        "installed_commit": plan.installed_commit,
        "target_release_id": plan.target_release_id,
        "target_commit": plan.target_commit,
        "releases_in_plan": list(plan.releases_in_plan),
        "migrations_required": list(plan.migrations_required),
        "migration_compatibility": plan.migration_compatibility,
        "python_requirements_changed": plan.python_requirements_changed,
        "apt_packages_new": list(plan.apt_packages_new),
        "systemd_units_changed": list(plan.systemd_units_changed),
        "systemd_units_new_required": list(plan.systemd_units_new_required),
        "systemd_units_new_optional": list(plan.systemd_units_new_optional),
        "systemd_units_removed_or_renamed": list(plan.systemd_units_removed_or_renamed),
        "collectstatic_required": plan.collectstatic_required,
        "services_requiring_restart": list(plan.services_requiring_restart),
        "nginx_changed": plan.nginx_changed,
        "runtime_components_changed": plan.runtime_components_changed,
        "minimum_updater_protocol_version": plan.minimum_updater_protocol_version,
        "manual_bootstrap_required": plan.manual_bootstrap_required,
        "fingerprint": plan.fingerprint,
    }
    transition = plan.protected_runtime_transition
    result["protected_runtime_transition"] = None if transition is None else {
        "release_id": transition.release_id,
        "previous_release_id": transition.previous_release_id,
        "commit": transition.commit,
        "generation": transition.field.generation,
        "descriptor_sha256": transition.field.descriptor_sha256,
    }
    return result
