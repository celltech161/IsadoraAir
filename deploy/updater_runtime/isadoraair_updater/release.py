"""Independent trusted-repository, release-chain, and execution-plan logic."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from . import PROTOCOL_VERSION
from .process import CommandRunner, ProcessResult
from .security import assert_root_protected, assert_root_protected_parents


GIT = "/usr/bin/git"
RELEASE_DIR = "deploy/releases"
RELEASE_ID_RE = re.compile(r"^r[0-9]{4,}$")
MIGRATION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[0-9]{4}_[a-z0-9_]+$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:service|timer)$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
CORE_SERVICES = frozenset({
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-encoders",
    "isadoraair-monitoring", "isadoraair-rbds",
})
RESTART_ORDER = (
    "isadoraair-gunicorn", "isadoraair-engine", "isadoraair-monitoring",
    "isadoraair-encoders", "isadoraair-rbds",
)
KNOWN_MANAGED_UNITS = frozenset(f"{name}.service" for name in CORE_SERVICES)
KNOWN_FIELDS = frozenset({
    "schema_version", "release_id", "previous_release_id", "bootstrap_commit",
    "minimum_updater_protocol_version", "summary", "migrations_required",
    "migration_compatibility", "python_requirements_changed",
    "requirements_sha256", "apt_packages_new", "systemd_units_changed",
    "systemd_units_new_required", "systemd_units_new_optional",
    "systemd_units_removed_or_renamed", "collectstatic_required",
    "services_requiring_restart", "nginx_changed", "runtime_components_changed",
    "minimum_supported_release_id",
    "manual_bootstrap_required",
})
FORBIDDEN_FIELDS = frozenset({
    "pre_update_hooks", "post_update_hooks", "hooks", "commands", "shell",
    "script", "exec", "release_commit", "commit", "sha", "git_sha",
})
APP_MIGRATION_PATHS = {"tts": "isadoraair/tts"}


class ReleaseError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Manifest:
    release_id: str
    previous_release_id: str | None
    bootstrap_commit: str | None
    minimum_updater_protocol_version: int
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


@dataclasses.dataclass(frozen=True)
class ChainEntry:
    manifest: Manifest
    index: int
    commit: str


@dataclasses.dataclass(frozen=True)
class TrustedPlan:
    installed_release_id: str
    installed_commit: str
    target_release_id: str
    target_commit: str
    releases_in_plan: tuple[str, ...]
    migrations_required: tuple[str, ...]
    migration_compatibility: str | None
    python_requirements_changed: bool
    apt_packages_new: tuple[str, ...]
    systemd_units_changed: tuple[str, ...]
    systemd_units_new_required: tuple[str, ...]
    systemd_units_new_optional: tuple[str, ...]
    systemd_units_removed_or_renamed: tuple[str, ...]
    collectstatic_required: bool
    services_requiring_restart: tuple[str, ...]
    nginx_changed: bool
    runtime_components_changed: bool
    minimum_updater_protocol_version: int
    manual_bootstrap_required: bool
    fingerprint: str

    def fingerprint_payload(self) -> dict:
        return execution_fingerprint_payload(
            installed_release_id=self.installed_release_id,
            installed_commit=self.installed_commit,
            target_release_id=self.target_release_id,
            target_commit=self.target_commit,
            releases_in_plan=self.releases_in_plan,
            migrations_required=self.migrations_required,
            migration_compatibility=self.migration_compatibility,
            python_requirements_changed=self.python_requirements_changed,
            apt_packages_new=self.apt_packages_new,
            systemd_units_changed=self.systemd_units_changed,
            systemd_units_new_required=self.systemd_units_new_required,
            systemd_units_new_optional=self.systemd_units_new_optional,
            systemd_units_removed_or_renamed=self.systemd_units_removed_or_renamed,
            collectstatic_required=self.collectstatic_required,
            services_requiring_restart=self.services_requiring_restart,
            nginx_changed=self.nginx_changed,
            runtime_components_changed=self.runtime_components_changed,
            minimum_updater_protocol_version=self.minimum_updater_protocol_version,
            manual_bootstrap_required=self.manual_bootstrap_required,
        )


def execution_fingerprint_payload(**values) -> dict:
    """Fingerprint contract v2 authorization facts used by protocol v3."""
    return {
        "contract_version": 2,
        "installed_release_id": values["installed_release_id"],
        "installed_commit": values["installed_commit"],
        "target_release_id": values["target_release_id"],
        "target_commit": values["target_commit"],
        "releases_in_plan": list(values["releases_in_plan"]),
        "migrations_required": list(values["migrations_required"]),
        "migration_compatibility": values["migration_compatibility"],
        "python_requirements_changed": values["python_requirements_changed"],
        "apt_packages_new": list(values["apt_packages_new"]),
        "systemd_units_changed": list(values["systemd_units_changed"]),
        "systemd_units_new_required": list(values["systemd_units_new_required"]),
        "systemd_units_new_optional": list(values["systemd_units_new_optional"]),
        "systemd_units_removed_or_renamed": list(values["systemd_units_removed_or_renamed"]),
        "collectstatic_required": values["collectstatic_required"],
        "services_requiring_restart": list(values["services_requiring_restart"]),
        "nginx_changed": values["nginx_changed"],
        "runtime_components_changed": values["runtime_components_changed"],
        "minimum_updater_protocol_version": values["minimum_updater_protocol_version"],
        "manual_bootstrap_required": values["manual_bootstrap_required"],
    }


def fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _list(value, field: str, pattern=None) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReleaseError(f"{field} must be a list of strings")
    if len(value) != len(set(value)):
        raise ReleaseError(f"{field} contains duplicate entries")
    if pattern and any(not pattern.fullmatch(item) for item in value):
        raise ReleaseError(f"{field} contains an invalid value")
    return tuple(value)


def parse_manifest(data: dict, *, label: str) -> Manifest:
    if not isinstance(data, dict):
        raise ReleaseError(f"{label}: manifest must be an object")
    forbidden = set(data) & FORBIDDEN_FIELDS
    unknown = set(data) - KNOWN_FIELDS
    if forbidden or unknown:
        raise ReleaseError(f"{label}: forbidden/unknown fields: {sorted(forbidden | unknown)!r}")
    required = KNOWN_FIELDS - {
        "bootstrap_commit", "summary", "migration_compatibility", "requirements_sha256",
        "minimum_supported_release_id", "manual_bootstrap_required",
    }
    if required - set(data):
        raise ReleaseError(f"{label}: missing fields: {sorted(required - set(data))!r}")
    if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
        raise ReleaseError(f"{label}: unsupported schema_version")
    release_id = data.get("release_id")
    previous = data.get("previous_release_id")
    if not isinstance(release_id, str) or not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseError(f"{label}: invalid release_id")
    if previous is not None and (not isinstance(previous, str) or not RELEASE_ID_RE.fullmatch(previous) or previous == release_id):
        raise ReleaseError(f"{label}: invalid previous_release_id")
    bootstrap = data.get("bootstrap_commit")
    if previous is None:
        if not isinstance(bootstrap, str) or not re.fullmatch(r"[0-9a-f]{40}", bootstrap):
            raise ReleaseError(f"{label}: bootstrap release needs a full bootstrap_commit")
    elif bootstrap is not None:
        raise ReleaseError(f"{label}: bootstrap_commit is forbidden on a normal release")
    minimum_protocol = data.get("minimum_updater_protocol_version")
    if not isinstance(minimum_protocol, int) or isinstance(minimum_protocol, bool) or minimum_protocol < 1:
        raise ReleaseError(f"{label}: invalid minimum_updater_protocol_version")
    migrations = _list(data.get("migrations_required"), "migrations_required", MIGRATION_RE)
    compatibility = data.get("migration_compatibility")
    if migrations and compatibility not in {"additive", "destructive"}:
        raise ReleaseError(f"{label}: migrations require a compatibility classification")
    if not migrations and compatibility is not None:
        raise ReleaseError(f"{label}: migration_compatibility must be null without migrations")
    requirements_changed = data.get("python_requirements_changed")
    if not isinstance(requirements_changed, bool):
        raise ReleaseError(f"{label}: python_requirements_changed must be boolean")
    requirements_hash = data.get("requirements_sha256")
    if requirements_changed:
        if not isinstance(requirements_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", requirements_hash):
            raise ReleaseError(f"{label}: changed requirements need an exact SHA-256")
    elif requirements_hash is not None:
        raise ReleaseError(f"{label}: unchanged requirements cannot carry a hash")
    units_changed = _list(data.get("systemd_units_changed"), "systemd_units_changed", UNIT_RE)
    units_required = _list(data.get("systemd_units_new_required"), "systemd_units_new_required", UNIT_RE)
    units_optional = _list(data.get("systemd_units_new_optional"), "systemd_units_new_optional", UNIT_RE)
    units_removed = _list(data.get("systemd_units_removed_or_renamed", []), "systemd_units_removed_or_renamed", UNIT_RE)
    all_units = units_changed + units_required + units_optional + units_removed
    if len(all_units) != len(set(all_units)):
        raise ReleaseError(f"{label}: a unit appears in multiple intent lists")
    restarts = _list(data.get("services_requiring_restart"), "services_requiring_restart")
    if set(restarts) - CORE_SERVICES:
        raise ReleaseError(f"{label}: services_requiring_restart contains an unknown service")
    bool_fields = ("collectstatic_required", "nginx_changed", "runtime_components_changed")
    if any(not isinstance(data.get(field), bool) for field in bool_fields):
        raise ReleaseError(f"{label}: boolean manifest field has the wrong type")
    minimum_release = data.get("minimum_supported_release_id")
    if minimum_release is not None and (not isinstance(minimum_release, str) or not RELEASE_ID_RE.fullmatch(minimum_release)):
        raise ReleaseError(f"{label}: invalid minimum_supported_release_id")
    manual_bootstrap_required = data.get("manual_bootstrap_required", False)
    if not isinstance(manual_bootstrap_required, bool):
        raise ReleaseError(f"{label}: manual_bootstrap_required must be boolean")
    summary = data.get("summary", "")
    if not isinstance(summary, str) or len(summary) > 500:
        raise ReleaseError(f"{label}: invalid summary")
    return Manifest(
        release_id, previous, bootstrap, minimum_protocol, migrations,
        compatibility, requirements_changed, requirements_hash,
        _list(data.get("apt_packages_new"), "apt_packages_new", PACKAGE_RE),
        units_changed, units_required, units_optional, units_removed,
        data["collectstatic_required"], restarts, data["nginx_changed"],
        data["runtime_components_changed"], minimum_release, manual_bootstrap_required,
    )


def _safe_repo_path(path: str):
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\x00" in path:
        raise ReleaseError("repository path is not safe")


class TrustedRepository:
    def __init__(self, path: Path, upstream: str, branch: str, runner: CommandRunner):
        self.path = Path(path)
        self.upstream = upstream
        self.branch = branch
        self.runner = runner

    def _run(self, args: list[str], timeout: float = 30) -> ProcessResult:
        return self.runner.run(
            [GIT, f"--git-dir={self.path}", "-c", "core.hooksPath=/dev/null", *args],
            timeout=timeout,
        )

    def initialize_or_verify(self):
        assert_root_protected_parents(self.path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            result = self.runner.run([GIT, "init", "--bare", str(self.path)], timeout=30)
            if not result.ok:
                raise ReleaseError("could not initialize trusted bare repository")
            if not self._run(["remote", "add", "origin", self.upstream]).ok:
                raise ReleaseError("could not set the trusted repository origin")
        if not self.path.is_dir() or not (self.path / "HEAD").is_file():
            raise ReleaseError("trusted repository path is not a bare Git repository")
        assert_root_protected(self.path, recursive=True)
        result = self._run(["remote", "get-url", "origin"])
        if not result.ok or result.stdout.decode("utf-8", "strict").strip() != self.upstream:
            raise ReleaseError("trusted repository origin does not match root-owned station identity")

    def fetch(self) -> str:
        self.initialize_or_verify()
        fetched_ref = f"refs/isadoraair-updater/fetched/{self.branch}"
        branch_ref = f"refs/heads/{self.branch}"
        result = self._run([
            "fetch", "--quiet", "--no-tags", "origin",
            f"refs/heads/{self.branch}:{fetched_ref}",
        ], timeout=120)
        if not result.ok:
            raise ReleaseError("trusted fetch failed or upstream history is non-fast-forward")
        new_sha = self.rev_parse(fetched_ref)
        old_sha = self.rev_parse(branch_ref)
        if not new_sha:
            raise ReleaseError("trusted branch did not resolve after fetch")
        if old_sha and self.is_ancestor(old_sha, new_sha) is not True:
            raise ReleaseError("trusted upstream branch rewrote or diverged from previously accepted history")
        args = ["update-ref", branch_ref, new_sha]
        if old_sha:
            args.append(old_sha)
        if not self._run(args).ok:
            raise ReleaseError("could not atomically advance trusted branch reference")
        return new_sha

    def rev_parse(self, ref: str) -> str | None:
        result = self._run(["rev-parse", "--verify", "-q", ref])
        if not result.ok:
            return None
        value = result.stdout.decode("ascii", "strict").strip()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None

    def commit_exists(self, sha: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-f]{40}", sha)) and self._run(["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0

    def is_ancestor(self, ancestor: str, descendant: str) -> bool | None:
        result = self._run(["merge-base", "--is-ancestor", ancestor, descendant])
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        return None

    def read_file(self, sha: str, path: str, *, maximum: int = 1024 * 1024) -> bytes | None:
        _safe_repo_path(path)
        result = self._run(["show", f"{sha}:{path}"])
        if not result.ok or len(result.stdout) > maximum:
            return None
        return result.stdout

    def path_exists(self, sha: str, path: str) -> bool:
        _safe_repo_path(path)
        return self._run(["cat-file", "-e", f"{sha}:{path}"]).returncode == 0

    def changed_paths(self, before: str, after: str, prefix: str) -> tuple[tuple[str, str], ...]:
        _safe_repo_path(prefix)
        result = self._run([
            "diff", "--name-status", "-z", "--no-renames", before, after,
            "--", prefix,
        ])
        if not result.ok:
            raise ReleaseError(f"could not compare trusted target path {prefix!r}")
        try:
            fields = result.stdout.decode("utf-8").split("\x00")
        except UnicodeDecodeError as exc:
            raise ReleaseError("trusted target diff contains a non-UTF-8 path") from exc
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) % 2:
            raise ReleaseError("trusted target diff has an invalid machine-readable shape")
        changes = []
        for index in range(0, len(fields), 2):
            status, path = fields[index:index + 2]
            if status not in {"A", "D", "M", "T"}:
                raise ReleaseError(f"trusted target diff has unsupported status {status!r}")
            _safe_repo_path(path)
            changes.append((status, path))
        return tuple(changes)

    def list_release_files(self, sha: str) -> list[str]:
        result = self._run(["ls-tree", "--name-only", "-z", sha, "--", f"{RELEASE_DIR}/"])
        if not result.ok:
            raise ReleaseError("could not enumerate release manifests from trusted target")
        try:
            paths = result.stdout.decode("utf-8").split("\x00")
        except UnicodeDecodeError as exc:
            raise ReleaseError("release manifest paths are not UTF-8") from exc
        return sorted(PurePosixPath(path).name for path in paths if path and path.endswith(".json"))

    def introducing_commit(self, path: str) -> str | None:
        _safe_repo_path(path)
        additions = self._run(["log", "--all", "--diff-filter=A", "--format=%H", "--", path])
        history = self._run(["log", "--all", "--format=%H", "--", path])
        if not additions.ok or not history.ok:
            return None
        added = additions.stdout.decode("ascii", "strict").splitlines()
        all_history = history.stdout.decode("ascii", "strict").splitlines()
        return added[0] if len(added) == 1 and all_history == added else None

    def archive_to(self, sha: str, destination: Path, *, maximum: int = 512 * 1024 * 1024) -> ProcessResult:
        return self.runner.run_to_file(
            [GIT, f"--git-dir={self.path}", "-c", "core.hooksPath=/dev/null", "archive", "--format=tar", sha],
            destination, timeout=180, max_bytes=maximum,
        )


def load_chain(repository: TrustedRepository, trusted_tip: str) -> list[ChainEntry]:
    manifests: dict[str, Manifest] = {}
    for name in repository.list_release_files(trusted_tip):
        raw = repository.read_file(trusted_tip, f"{RELEASE_DIR}/{name}", maximum=65536)
        if raw is None:
            raise ReleaseError(f"cannot read release manifest {name}")
        try:
            parsed = parse_manifest(json.loads(raw.decode("utf-8")), label=name)
        except (ValueError, UnicodeDecodeError) as exc:
            if isinstance(exc, ReleaseError):
                raise
            raise ReleaseError(f"{name}: invalid JSON") from exc
        if name != f"{parsed.release_id}.json" or parsed.release_id in manifests:
            raise ReleaseError(f"{name}: release filename/id mismatch or duplicate")
        manifests[parsed.release_id] = parsed
    if not manifests:
        raise ReleaseError("trusted branch contains no release manifests")
    bootstraps = [item for item in manifests.values() if item.previous_release_id is None]
    if len(bootstraps) != 1:
        raise ReleaseError("release chain must have exactly one bootstrap")
    successors: dict[str, Manifest] = {}
    for item in manifests.values():
        if item.previous_release_id is None:
            continue
        if item.previous_release_id not in manifests or item.previous_release_id in successors:
            raise ReleaseError("release chain has a missing predecessor or fork")
        successors[item.previous_release_id] = item
    ordered = [bootstraps[0]]
    seen = {bootstraps[0].release_id}
    while ordered[-1].release_id in successors:
        item = successors[ordered[-1].release_id]
        if item.release_id in seen:
            raise ReleaseError("release chain contains a cycle")
        seen.add(item.release_id)
        ordered.append(item)
    if len(ordered) != len(manifests):
        raise ReleaseError("release chain contains unreachable releases")

    commit_owner: dict[str, str] = {}
    entries: list[ChainEntry] = []
    for index, item in enumerate(ordered):
        commit = item.bootstrap_commit if index == 0 else repository.introducing_commit(f"{RELEASE_DIR}/{item.release_id}.json")
        if not commit or not repository.commit_exists(commit) or repository.is_ancestor(commit, trusted_tip) is not True:
            raise ReleaseError(f"release {item.release_id} has no unique immutable commit on the trusted branch")
        if commit in commit_owner:
            raise ReleaseError(f"releases {commit_owner[commit]} and {item.release_id} share an introducing commit")
        commit_owner[commit] = item.release_id
        entries.append(ChainEntry(item, index, commit))
    indexes = {entry.manifest.release_id: entry.index for entry in entries}
    for entry in entries:
        minimum = entry.manifest.minimum_supported_release_id
        if minimum is not None and (minimum not in indexes or indexes[minimum] >= entry.index):
            raise ReleaseError(f"release {entry.manifest.release_id} has an invalid minimum supported release")
    return entries


def _content(repository: TrustedRepository, commit: str, path: str) -> bytes | None:
    exists = repository.path_exists(commit, path)
    content = repository.read_file(commit, path)
    if exists and content is None:
        raise ReleaseError(f"trusted file {path} is unreadable or exceeds its size bound")
    return content


def _cross_check(repository: TrustedRepository, previous_commit: str, entry: ChainEntry):
    manifest = entry.manifest
    for ref in manifest.migrations_required:
        app, name = ref.split(".", 1)
        root = APP_MIGRATION_PATHS.get(app, app)
        if not repository.path_exists(entry.commit, f"{root}/migrations/{name}.py"):
            raise ReleaseError(f"{manifest.release_id}: declared migration {ref} is absent from its commit")
    if manifest.python_requirements_changed:
        requirements = repository.read_file(entry.commit, "requirements.txt")
        if requirements is None or hashlib.sha256(requirements).hexdigest() != manifest.requirements_sha256:
            raise ReleaseError(f"{manifest.release_id}: requirements hash does not match trusted content")
    requirements_changed = (
        _content(repository, previous_commit, "requirements.txt")
        != _content(repository, entry.commit, "requirements.txt")
    )
    if requirements_changed != manifest.python_requirements_changed:
        raise ReleaseError(
            f"{manifest.release_id}: python_requirements_changed does not match the predecessor diff"
        )
    for unit in (*manifest.systemd_units_changed, *manifest.systemd_units_new_required, *manifest.systemd_units_new_optional):
        if not repository.path_exists(entry.commit, f"deploy/{unit}"):
            raise ReleaseError(f"{manifest.release_id}: declared unit {unit} is absent from its commit")
    for unit in manifest.systemd_units_removed_or_renamed:
        if repository.path_exists(entry.commit, f"deploy/{unit}"):
            raise ReleaseError(f"{manifest.release_id}: unit {unit} is declared removed but remains present")

    actual_changed: set[str] = set()
    actual_added: set[str] = set()
    actual_removed: set[str] = set()
    for status, path in repository.changed_paths(previous_commit, entry.commit, "deploy"):
        pure = PurePosixPath(path)
        if pure.parent != PurePosixPath("deploy") or not UNIT_RE.fullmatch(pure.name):
            continue
        destination = {"A": actual_added, "D": actual_removed}.get(status, actual_changed)
        destination.add(pure.name)
    declared_added = set(manifest.systemd_units_new_required) | set(manifest.systemd_units_new_optional)
    if (actual_changed != set(manifest.systemd_units_changed)
            or actual_added != declared_added
            or actual_removed != set(manifest.systemd_units_removed_or_renamed)):
        raise ReleaseError(f"{manifest.release_id}: systemd unit intent does not match the predecessor diff")

    for field, path, declared in (
        ("nginx_changed", "deploy/isadoraair.nginx", manifest.nginx_changed),
        ("runtime_components_changed", "isadoraair/runtime_components.json", manifest.runtime_components_changed),
    ):
        actual = _content(repository, previous_commit, path) != _content(repository, entry.commit, path)
        if actual != declared:
            raise ReleaseError(f"{manifest.release_id}: {field} does not match the predecessor diff")


def derive_plan(repository: TrustedRepository, trusted_tip: str, live_head: str,
                requested_target_release_id: str) -> TrustedPlan:
    chain = load_chain(repository, trusted_tip)
    exact = [entry for entry in chain if entry.commit == live_head]
    if len(exact) != 1:
        raise ReleaseError("live HEAD must exactly equal one independently resolved release commit")
    installed = exact[0]
    target = chain[-1]
    if requested_target_release_id != target.manifest.release_id:
        raise ReleaseError("requested target is not the independently derived latest trusted release")
    if installed.index >= target.index:
        raise ReleaseError("station is already current or target does not advance the installed release")
    transitions = chain[installed.index + 1:target.index + 1]
    for entry in transitions:
        _cross_check(repository, chain[entry.index - 1].commit, entry)
        minimum = entry.manifest.minimum_supported_release_id
        if minimum and installed.index < next(item.index for item in chain if item.manifest.release_id == minimum):
            raise ReleaseError(f"installed release is older than {entry.manifest.release_id} permits")
    migrations: list[str] = []
    packages: list[str] = []
    changed: list[str] = []
    required: list[str] = []
    optional: list[str] = []
    removed: list[str] = []
    restart_set: set[str] = set()
    compatibility: set[str] = set()
    for entry in transitions:
        item = entry.manifest
        for source, destination in (
            (item.migrations_required, migrations), (item.apt_packages_new, packages),
            (item.systemd_units_changed, changed), (item.systemd_units_new_required, required),
            (item.systemd_units_new_optional, optional), (item.systemd_units_removed_or_renamed, removed),
        ):
            for value in source:
                if value not in destination:
                    destination.append(value)
        restart_set.update(item.services_requiring_restart)
        if item.migrations_required:
            compatibility.add(item.migration_compatibility)
    overall = "destructive" if "destructive" in compatibility else ("additive" if compatibility else None)
    values = dict(
        installed_release_id=installed.manifest.release_id,
        installed_commit=installed.commit,
        target_release_id=target.manifest.release_id,
        target_commit=target.commit,
        releases_in_plan=tuple(entry.manifest.release_id for entry in transitions),
        migrations_required=tuple(migrations),
        migration_compatibility=overall,
        python_requirements_changed=any(entry.manifest.python_requirements_changed for entry in transitions),
        apt_packages_new=tuple(packages),
        systemd_units_changed=tuple(changed),
        systemd_units_new_required=tuple(required),
        systemd_units_new_optional=tuple(optional),
        systemd_units_removed_or_renamed=tuple(removed),
        collectstatic_required=any(entry.manifest.collectstatic_required for entry in transitions),
        services_requiring_restart=tuple(name for name in RESTART_ORDER if name in restart_set),
        nginx_changed=any(entry.manifest.nginx_changed for entry in transitions),
        runtime_components_changed=any(entry.manifest.runtime_components_changed for entry in transitions),
        minimum_updater_protocol_version=max(entry.manifest.minimum_updater_protocol_version for entry in transitions),
        manual_bootstrap_required=any(entry.manifest.manual_bootstrap_required for entry in transitions),
    )
    plan_fingerprint = fingerprint(execution_fingerprint_payload(**values))
    return TrustedPlan(**values, fingerprint=plan_fingerprint)


def manual_blockers(plan: TrustedPlan) -> tuple[str, ...]:
    blockers = []
    if plan.minimum_updater_protocol_version > PROTOCOL_VERSION:
        blockers.append("UPDATER_UPGRADE_REQUIRED")
    if plan.manual_bootstrap_required:
        blockers.append("MANUAL_BOOTSTRAP_REQUIRED")
    if plan.python_requirements_changed:
        blockers.append("PYTHON_REQUIREMENTS_MANUAL")
    if plan.apt_packages_new:
        blockers.append("APT_PACKAGES_MANUAL")
    if plan.migration_compatibility == "destructive":
        blockers.append("DESTRUCTIVE_MIGRATION_MANUAL")
    if plan.systemd_units_removed_or_renamed:
        blockers.append("SYSTEMD_REMOVAL_MANUAL")
    unknown_units = (set(plan.systemd_units_changed) | set(plan.systemd_units_new_required)) - KNOWN_MANAGED_UNITS
    if unknown_units:
        blockers.append("UNKNOWN_MANAGED_UNIT")
    if plan.nginx_changed:
        blockers.append("NGINX_CHANGE_MANUAL")
    if plan.runtime_components_changed:
        blockers.append("RUNTIME_COMPONENT_CHANGE_MANUAL")
    return tuple(blockers)
