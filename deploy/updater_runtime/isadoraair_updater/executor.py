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
    GIT, ReleaseError, TrustedPlan, TrustedRepository, derive_plan, manual_blockers,
)
from .security import ProtectionError
from .staging import StagedSource, StagingError, cleanup, materialize
from .systemd import SystemdError, SystemdManager


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
                 *, systemd_manager: SystemdManager | None = None):
        self.config = config
        self.store = store
        self.runner = runner
        self.repository = TrustedRepository(config.trusted_repository, config.trusted_repository_url, config.trusted_branch, runner)
        self.systemd = systemd_manager or SystemdManager(config, runner)

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
            plan = derive_plan(
                self.repository, trusted_tip, basis_head,
                state["requested_target_release_id"],
            )
            if plan.fingerprint != state["expected_plan_fingerprint"]:
                raise ExecutionError("PLAN_FINGERPRINT_MISMATCH", "root-derived plan does not match the requested plan fingerprint")
            blockers = manual_blockers(plan)
            if blockers:
                raise ExecutionError("MANUAL_PREREQUISITE", ", ".join(blockers), manual=True)
            plan_record = dataclass_to_dict(plan)
            self.store.update(job_id, trusted_plan=plan_record)
            self.store.milestone(job_id, "trusted_plan_validated")

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
                    self._advance_source(plan)
            else:
                live_after_resume = self._live_identity()
                if live_after_resume["head"] != plan.target_commit:
                    raise ExecutionError("LIVE_SOURCE_VERIFY_FAILED", "recorded source milestone disagrees with live HEAD", manual=True)
            self.store.milestone(job_id, "source_advanced")

            if plan.collectstatic_required and "static_collected" not in milestones:
                result, settings = self._run_app(self.config.application_root, ["collectstatic", "--noinput", "--skip-checks"], timeout=600)
                if not result.ok:
                    raise ExecutionError("COLLECTSTATIC_FAILED", _decode(result, settings), manual=True)
            self.store.milestone(job_id, "static_collected")

            if "systemd_reconciled" not in milestones:
                self.systemd.reconcile(staged.source_root, plan)
            self.store.milestone(job_id, "systemd_reconciled")
            if "services_restarted" not in milestones:
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
