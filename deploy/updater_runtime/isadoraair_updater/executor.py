"""Milestone-driven safe update pipeline for the protected root runtime."""
from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from . import PROTOCOL_VERSION
from .checkpoint import CheckpointError, create_checkpoint, verify_checkpoint
from .config import StationConfig
from .jobs import JobError, JobStore
from .process import CommandRunner, ProcessResult
from .release import (
    GIT, ReleaseError, TrustedPlan, TrustedRepository, derive_plan, load_chain, manual_blockers,
    resolve_known_managed_units,
)
from .runtime_handoff import (
    MILESTONE_RUNTIME_ACTIVATION_REQUESTED, MILESTONE_RUNTIME_CANDIDATE_STAGED,
    MILESTONE_RUNTIME_CANDIDATE_VERIFIED, MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED,
    MILESTONE_RUNTIME_GENERATION_COMMITTED,
    MUTATION_GATE_MILESTONE, SAFE_YIELD_MILESTONE, HandoffError, MutationGateError,
    attestations_staging_directory, descriptor_staging_path, handoff_required, materialize_candidate,
    new_supervisor_staging_directory, publish_to_candidate_slot, require_mutation_allowed,
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
    def __init__(self, classification: str, detail: str, *, manual: bool = False):
        super().__init__(detail)
        self.classification = classification
        self.detail = detail
        self.manual = manual


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


def _strict_probe(raw: bytes) -> dict:
    if len(raw) > 1024 * 1024:
        raise ExecutionError("PROBE_INVALID", "migration probe output exceeds 1 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ExecutionError("PROBE_INVALID", "migration probe did not emit strict JSON") from exc
    required = {"schema_version", "status", "plan", "nodes", "applied", "conflicts", "replacements"}
    if not isinstance(payload, dict) or set(payload) != required or payload.get("schema_version") != 1 or payload.get("status") != "ok":
        raise ExecutionError("PROBE_INVALID", "migration probe schema/status mismatch")
    if not isinstance(payload["plan"], list) or not isinstance(payload["nodes"], dict) or not isinstance(payload["applied"], list):
        raise ExecutionError("PROBE_INVALID", "migration probe collection types are invalid")
    if not isinstance(payload["conflicts"], dict) or not isinstance(payload["replacements"], list):
        raise ExecutionError("PROBE_INVALID", "migration probe conflict/replacement types are invalid")
    refs = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
    for ref, dependencies in payload["nodes"].items():
        if not isinstance(ref, str) or not refs.fullmatch(ref) or not isinstance(dependencies, list) or any(not isinstance(dep, str) or not refs.fullmatch(dep) for dep in dependencies):
            raise ExecutionError("PROBE_INVALID", "migration graph contains an invalid node/dependency")
    if any(not isinstance(ref, str) or not refs.fullmatch(ref) for ref in payload["applied"]):
        raise ExecutionError("PROBE_INVALID", "migration applied set contains an invalid reference")
    seen = set()
    for item in payload["plan"]:
        if not isinstance(item, dict) or set(item) != {"ref", "dependencies", "operations"}:
            raise ExecutionError("PROBE_INVALID", "migration plan item shape is invalid")
        if item["ref"] in seen or item["ref"] not in payload["nodes"] or item["dependencies"] != payload["nodes"][item["ref"]]:
            raise ExecutionError("PROBE_INVALID", "migration plan identity/dependencies are inconsistent")
        seen.add(item["ref"])
        if not isinstance(item["operations"], list):
            raise ExecutionError("PROBE_INVALID", "migration operations must be a list")
        for operation in item["operations"]:
            if (not isinstance(operation, dict) or set(operation) != {"operation", "classification", "detail"}
                    or operation["classification"] not in {"additive", "manual"}):
                raise ExecutionError("PROBE_INVALID", "migration operation classification is invalid")
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
                 expected_handoff_generation: int | None = None,
                 expected_handoff_descriptor_sha256: str | None = None,
                 expected_resumable_job_uuid: str | None = None,
                 active_policy=None):
        self.config = config
        self.store = store
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

    def _probe(self, source: Path) -> dict:
        result, settings = self._run_app(source, ["updatecenter_probe", "--skip-checks"], timeout=120)
        if not result.ok:
            raise ExecutionError("PROBE_FAILED", _decode(result, settings))
        return _strict_probe(result.stdout.strip())

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

    def _validate_target_schema(self, plan: TrustedPlan, payload: dict, current_payload: dict,
                                *, migration_already_started: bool) -> tuple[str, ...]:
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
        already_applied_explicit = set(plan.migrations_required) & set(current_payload["applied"])
        if already_applied_explicit and not migration_already_started:
            raise ExecutionError(
                "TARGET_MIGRATION_PREAPPLIED",
                f"target transition migration(s) were applied outside this job: {sorted(already_applied_explicit)!r}",
                manual=True,
            )
        if actual and plan.migration_compatibility != "additive":
            raise ExecutionError("MIGRATION_NOT_AUTOMATABLE", "manifest does not classify target migration work as additive", manual=True)
        for item in payload["plan"]:
            for operation in item["operations"]:
                if operation["classification"] != "additive":
                    raise ExecutionError(
                        "MIGRATION_OPERATION_MANUAL",
                        f"{item['ref']} contains {operation['operation']}: {operation['detail']}",
                        manual=True,
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

    def _resolve_candidate_slot(self, activation_socket: Path) -> tuple[str, str, SupervisorClient]:
        client = SupervisorClient(activation_socket)
        runtime_state = client.get_runtime_state()
        active_slot = runtime_state["active_slot"]
        candidate_slot = "B" if active_slot == "A" else "A"
        return active_slot, candidate_slot, client

    def _execute_runtime_handoff(self, job_id: str, plan: TrustedPlan, protected_runtime_field, milestones: set):
        """D3: the OLD worker's own short pipeline for a job whose
        target release declares protected_runtime -- stage+verify the
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
            if MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED not in milestones:
                self.store.milestone(job_id, MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED)
                milestones.add(MILESTONE_RUNTIME_DESCRIPTOR_VALIDATED)

            slots_root = self.config.phase_d_supervisor_slots_root
            activation_socket = self.config.phase_d_supervisor_activation_socket
            if slots_root is None or activation_socket is None:
                raise ExecutionError(
                    "UNBOOTSTRAPPED_SUPERVISOR",
                    "this station's protected_runtime target requires a Phase-D supervisor, "
                    "but none is configured (phase_d_supervisor_slots_root/activation_socket are null)",
                    manual=True,
                )

            if MILESTONE_RUNTIME_CANDIDATE_STAGED not in milestones:
                active_slot, candidate_slot, _client = self._resolve_candidate_slot(activation_socket)
                staging = new_supervisor_staging_directory(slots_root)
                materialized = materialize_candidate(self.repository, protected_runtime_field, plan.target_commit, staging)
                stage_attestations(self.repository, protected_runtime_field, plan.target_commit, slots_root, candidate_slot)
                stage_descriptor(materialized.descriptor_bytes, slots_root, candidate_slot)
                publish_to_candidate_slot(slots_root, candidate_slot, staging, active_slot=active_slot)
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
                # whether the target release's own new (to THIS
                # worker's active policy) managed unit is legitimately
                # authorized -- see verify_new_units_authorized_by_
                # candidate_policy below.
                record = self.store.load(job_id)["protected_runtime_candidate"]
                candidate_slot = record["candidate_slot"]
                _active_slot, _cs, client = self._resolve_candidate_slot(activation_socket)
                runtime_state = client.get_runtime_state()
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
                    release_id=plan.target_release_id, previous_release_id=plan.installed_release_id,
                    previous_generation=runtime_state["active_generation"],
                    current_bootstrap_protocol_version=1, current_wire_protocol_version=PROTOCOL_VERSION,
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
                _active_slot, _candidate_slot, client = self._resolve_candidate_slot(activation_socket)
                client.request_activation(
                    transaction_id=job_id, candidate_slot=record["candidate_slot"],
                    candidate_generation=record["generation"], candidate_descriptor_sha256=record["descriptor_sha256"],
                    release_id=plan.target_release_id, previous_release_id=plan.installed_release_id,
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
        ordinary release, a hard gate on MUTATION_GATE_MILESTONE for a
        protected_runtime one), so a bug in one is never silently
        compensated for by the other -- both must independently agree
        mutation is allowed."""
        self._require_mutation_allowed(plan, milestones)

    def execute(self, job_id: str):
        state = self.store.load(job_id)
        if state["state"] in {"succeeded", "failed", "manual_intervention_required"}:
            return state
        milestones = set(state["milestones"])
        if "migration_started" in milestones and "database_verified" not in milestones:
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

            # Update Center Phase D, D4: three-way branch for a target
            # release that declares protected_runtime (plan.
            # protected_runtime, set by derive_plan() -- D3-J). Never a
            # simple binary "handoff needed or not" -- D4-D's own
            # "prove old and new workers can never mutate the same job
            # concurrently" requires distinguishing exactly which of
            # three roles THIS process may legitimately play for this
            # job right now:
            #
            #   1. MUTATION_GATE_MILESTONE already present -- runtime
            #      acceptance already happened (by some earlier
            #      candidate call). Fall straight through to the
            #      unchanged Phase-B pipeline below, gated at every
            #      mutating step by require_mutation_allowed().
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
            if handoff_required(plan.protected_runtime) and MUTATION_GATE_MILESTONE not in milestones:
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
                    return self._execute_runtime_handoff(job_id, plan, plan.protected_runtime, milestones)

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
            target_payload = self._probe(staged.source_root)
            actual_migrations = self._validate_target_schema(
                plan, target_payload, current_payload,
                migration_already_started="migration_started" in milestones,
            )
            self.store.milestone(job_id, "target_schema_validated")

            if actual_migrations and "database_verified" not in milestones:
                self._require_mutation_allowed(plan, milestones)
                checkpoint = state.get("checkpoint")
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
                self.store.milestone(job_id, "migration_started")
                result, settings = self._run_app(staged.source_root, ["migrate", "--noinput", "--skip-checks"], timeout=1800)
                if not result.ok:
                    raise ExecutionError("MIGRATION_FAILED", _decode(result, settings), manual=True)
                verified = self._probe(staged.source_root)
                if verified["conflicts"] or verified["replacements"] or verified["plan"]:
                    raise ExecutionError("MIGRATION_VERIFY_FAILED", "target schema is not clean after migration", manual=True)
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
            return self.store.fail(job_id, exc.classification, exc.detail, manual=exc.manual)
        except (ReleaseError, StagingError, CheckpointError, SystemdError, ProtectionError, JobError, OSError) as exc:
            cleanup_staging()
            current = self.store.load(job_id)
            crossed_mutation_boundary = bool(
                {"migration_started", "database_verified", "source_advanced"}
                & set(current.get("milestones", []))
            )
            return self.store.fail(
                job_id, "SAFE_EXECUTION_FAILURE", str(exc),
                manual=crossed_mutation_boundary or isinstance(exc, (SystemdError, ProtectionError)),
            )


def dataclass_to_dict(plan: TrustedPlan) -> dict:
    return {
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
