"""Strict root-owned station configuration for the standalone updater."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from .security import ProtectionError, assert_root_protected_parents


class ConfigError(ValueError):
    pass


_NAME = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_DB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,63}$")
_SYSTEMD_UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,98}\.service$")
_RENDER_KEYS = frozenset({
    "isa_user", "isa_root", "isa_home", "syndicated_root",
    "weather_root", "ogremote_root",
})
_REQUIRED_FIELDS = frozenset({
    "schema_version", "trusted_repository_url", "trusted_branch",
    "application_root", "application_user", "application_group",
    "application_environment_file", "trusted_repository", "jobs_root",
    "logs_root", "staging_root", "checkpoint_root", "socket_path",
    "systemd_unit_root", "render_values", "database",
    "gunicorn_health_url",
})
_OPTIONAL_FIELDS = frozenset({
    "update_execution_enabled", "operator_restart_units",
    # Update Center Phase D, D3: where the IMMUTABLE supervisor's own
    # private, root-only activation socket and A/B slots_root live --
    # needed only by a station that has actually completed the D0
    # bootstrap (installed the supervisor). Both absent/None (the D0
    # bridge default -- every station through r0026) means this worker
    # cannot request a runtime handoff at all; runtime_handoff.py's own
    # handoff_required() check therefore fails CLOSED for a
    # protected_runtime release on such a station (UNBOOTSTRAPPED_
    # SUPERVISOR, never a silent same-process best-effort attempt).
    "phase_d_supervisor_activation_socket", "phase_d_supervisor_slots_root",
    # D4-P/D4-E: the SAME root-owned trust material the supervisor
    # itself uses (config.py's own signer_root/trust_policy_path,
    # BootstrapConfig) -- read-only for the worker, never a SEPARATE
    # or worker-writable copy. Lets THIS worker independently verify a
    # candidate bundle's signature threshold and read its policy file
    # for D4-G's own two-stage new-unit authorization, without ever
    # trusting the supervisor's later verification alone. Both null
    # (the D0 bridge default) means this worker cannot independently
    # verify anything about a candidate and must treat every
    # protected_runtime release as requiring an already-bootstrapped
    # supervisor for ANY progression -- never a silent best-effort
    # skip of this check.
    "phase_d_trust_policy_path", "phase_d_signer_root",
})
_FIELDS = _REQUIRED_FIELDS | _OPTIONAL_FIELDS
_DB_FIELDS = frozenset({"name", "user", "host", "port", "pgpass_file"})


@dataclasses.dataclass(frozen=True)
class DatabaseConfig:
    name: str
    user: str
    host: str
    port: int
    pgpass_file: Path | None


@dataclasses.dataclass(frozen=True)
class StationConfig:
    trusted_repository_url: str
    trusted_branch: str
    application_root: Path
    application_user: str
    application_group: str
    application_environment_file: Path
    trusted_repository: Path
    jobs_root: Path
    logs_root: Path
    staging_root: Path
    checkpoint_root: Path
    socket_path: Path
    systemd_unit_root: Path
    render_values: dict[str, str]
    database: DatabaseConfig
    gunicorn_health_url: str
    update_execution_enabled: bool
    operator_restart_units: tuple[str, ...]
    phase_d_supervisor_activation_socket: Path | None
    phase_d_supervisor_slots_root: Path | None
    phase_d_trust_policy_path: Path | None
    phase_d_signer_root: Path | None

    @property
    def application_python(self) -> Path:
        return self.application_root / "venv" / "bin" / "python"

    @property
    def live_manage_py(self) -> Path:
        return self.application_root / "manage.py"


def _plain_string(value, field: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ConfigError(f"{field} must be a non-empty string no longer than {maximum} bytes")
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ConfigError(f"{field} contains a forbidden control character")
    return value


def _absolute_path(value, field: str) -> Path:
    raw = _plain_string(value, field)
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{field} must be an absolute normalized path")
    return path.resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_repository_url(value: str, *, allow_local_repository: bool) -> str:
    value = _plain_string(value, "trusted_repository_url", maximum=2048)
    if value.startswith("-"):
        raise ConfigError("trusted_repository_url cannot begin with an option marker")
    if allow_local_repository and Path(value).is_absolute():
        return value
    if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value):
        return value
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
        return value
    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return value
    raise ConfigError("trusted_repository_url must be an explicit HTTPS/SSH repository identity")


def validate_config_dict(data: dict, *, allow_local_repository: bool = False) -> StationConfig:
    if not isinstance(data, dict):
        raise ConfigError("station configuration must be a JSON object")
    unknown = set(data) - _FIELDS
    missing = _REQUIRED_FIELDS - set(data)
    if unknown or missing:
        raise ConfigError(f"station configuration fields mismatch; unknown={sorted(unknown)!r}, missing={sorted(missing)!r}")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise ConfigError("schema_version must be exactly 1")

    execution_enabled = data.get("update_execution_enabled", False)
    if not isinstance(execution_enabled, bool):
        raise ConfigError("update_execution_enabled must be a boolean")
    restart_units = data.get("operator_restart_units", [])
    if (not isinstance(restart_units, list)
            or any(not isinstance(unit, str) or not _SYSTEMD_UNIT.fullmatch(unit) for unit in restart_units)
            or len(restart_units) != len(set(restart_units))
            or len(restart_units) > 32):
        raise ConfigError(
            "operator_restart_units must be a duplicate-free list of at most 32 exact .service names"
        )

    branch = _plain_string(data["trusted_branch"], "trusted_branch", maximum=128)
    if not _BRANCH.fullmatch(branch) or ".." in branch or branch.endswith("/") or branch.startswith("/"):
        raise ConfigError("trusted_branch has an invalid Git ref shape")

    app_user = _plain_string(data["application_user"], "application_user", maximum=32)
    app_group = _plain_string(data["application_group"], "application_group", maximum=32)
    if not _NAME.fullmatch(app_user) or not _NAME.fullmatch(app_group):
        raise ConfigError("application_user/application_group has an invalid account-name shape")
    if app_user == "root" or app_group == "root":
        raise ConfigError("application identity must be unprivileged")
    if app_group != app_user:
        raise ConfigError("Phase B requires the project's existing same-name application user/group convention")

    application_root = _absolute_path(data["application_root"], "application_root")
    environment_file = _absolute_path(data["application_environment_file"], "application_environment_file")
    if not _is_within(environment_file, application_root):
        raise ConfigError("application_environment_file must remain inside application_root")

    trusted_repository = _absolute_path(data["trusted_repository"], "trusted_repository")
    jobs_root = _absolute_path(data["jobs_root"], "jobs_root")
    logs_root = _absolute_path(data["logs_root"], "logs_root")
    staging_root = _absolute_path(data["staging_root"], "staging_root")
    checkpoint_root = _absolute_path(data["checkpoint_root"], "checkpoint_root")
    socket_path = _absolute_path(data["socket_path"], "socket_path")
    systemd_unit_root = _absolute_path(data["systemd_unit_root"], "systemd_unit_root")
    protected = (trusted_repository, jobs_root, logs_root, staging_root, checkpoint_root, socket_path.parent, systemd_unit_root)
    if any(_is_within(path, application_root) or _is_within(application_root, path) for path in protected):
        raise ConfigError("root-controlled updater paths must not overlap application_root")
    for index, left in enumerate(protected):
        for right in protected[index + 1:]:
            if _is_within(left, right) or _is_within(right, left):
                raise ConfigError("root-controlled updater paths must not overlap one another")

    renders = data["render_values"]
    if not isinstance(renders, dict) or set(renders) != _RENDER_KEYS:
        raise ConfigError(f"render_values must contain exactly {sorted(_RENDER_KEYS)!r}")
    normalized_renders: dict[str, str] = {}
    for key, value in renders.items():
        value = _plain_string(value, f"render_values.{key}")
        if key == "isa_user":
            if value != app_user:
                raise ConfigError("render_values.isa_user must equal application_user")
        else:
            value = str(_absolute_path(value, f"render_values.{key}"))
        normalized_renders[key] = value
    if Path(normalized_renders["isa_root"]).resolve(strict=False) != application_root:
        raise ConfigError("render_values.isa_root must equal application_root")

    database = data["database"]
    if not isinstance(database, dict) or set(database) != _DB_FIELDS:
        raise ConfigError(f"database must contain exactly {sorted(_DB_FIELDS)!r}")
    db_name = _plain_string(database["name"], "database.name", maximum=63)
    db_user = _plain_string(database["user"], "database.user", maximum=63)
    db_host = _plain_string(database["host"], "database.host", maximum=255)
    if not _DB_NAME.fullmatch(db_name) or not _DB_NAME.fullmatch(db_user) or any(ch.isspace() for ch in db_host):
        raise ConfigError("database name/user/host has an invalid shape")
    db_port = database["port"]
    if not isinstance(db_port, int) or isinstance(db_port, bool) or not 1 <= db_port <= 65535:
        raise ConfigError("database.port must be an integer from 1 through 65535")
    pgpass = database["pgpass_file"]
    pgpass_path = None if pgpass is None else _absolute_path(pgpass, "database.pgpass_file")

    phase_d_socket = data.get("phase_d_supervisor_activation_socket")
    phase_d_slots = data.get("phase_d_supervisor_slots_root")
    if (phase_d_socket is None) != (phase_d_slots is None):
        raise ConfigError(
            "phase_d_supervisor_activation_socket and phase_d_supervisor_slots_root "
            "must be both null or both present -- a station either has a Phase-D "
            "supervisor installed or it does not"
        )
    phase_d_socket_path = None if phase_d_socket is None else _absolute_path(
        phase_d_socket, "phase_d_supervisor_activation_socket",
    )
    phase_d_slots_path = None if phase_d_slots is None else _absolute_path(
        phase_d_slots, "phase_d_supervisor_slots_root",
    )
    if phase_d_slots_path is not None and _is_within(phase_d_slots_path, application_root):
        raise ConfigError("phase_d_supervisor_slots_root must not overlap application_root")

    phase_d_trust_policy = data.get("phase_d_trust_policy_path")
    phase_d_signers = data.get("phase_d_signer_root")
    if (phase_d_trust_policy is None) != (phase_d_signers is None):
        raise ConfigError(
            "phase_d_trust_policy_path and phase_d_signer_root must be both null or both present"
        )
    phase_d_trust_policy_path = None if phase_d_trust_policy is None else _absolute_path(
        phase_d_trust_policy, "phase_d_trust_policy_path",
    )
    phase_d_signer_root_path = None if phase_d_signers is None else _absolute_path(
        phase_d_signers, "phase_d_signer_root",
    )
    if phase_d_signer_root_path is not None and _is_within(phase_d_signer_root_path, application_root):
        raise ConfigError("phase_d_signer_root must not overlap application_root")

    health_url = _plain_string(data["gunicorn_health_url"], "gunicorn_health_url", maximum=512)
    parsed_health = urlsplit(health_url)
    try:
        health_port = parsed_health.port
    except ValueError as exc:
        raise ConfigError("gunicorn_health_url has an invalid port") from exc
    if (parsed_health.scheme != "http"
            or parsed_health.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_health.username or parsed_health.password
            or health_port is not None and not 1 <= health_port <= 65535):
        raise ConfigError("gunicorn_health_url must be an unauthenticated loopback HTTP URL")

    return StationConfig(
        trusted_repository_url=_validate_repository_url(data["trusted_repository_url"], allow_local_repository=allow_local_repository),
        trusted_branch=branch,
        application_root=application_root,
        application_user=app_user,
        application_group=app_group,
        application_environment_file=environment_file,
        trusted_repository=trusted_repository,
        jobs_root=jobs_root,
        logs_root=logs_root,
        staging_root=staging_root,
        checkpoint_root=checkpoint_root,
        socket_path=socket_path,
        systemd_unit_root=systemd_unit_root,
        render_values=normalized_renders,
        database=DatabaseConfig(db_name, db_user, db_host, db_port, pgpass_path),
        gunicorn_health_url=health_url,
        update_execution_enabled=execution_enabled,
        operator_restart_units=tuple(restart_units),
        phase_d_supervisor_activation_socket=phase_d_socket_path,
        phase_d_supervisor_slots_root=phase_d_slots_path,
        phase_d_trust_policy_path=phase_d_trust_policy_path,
        phase_d_signer_root=phase_d_signer_root_path,
    )


def load_config(path: Path, *, enforce_protection: bool = True, allow_local_repository: bool = False) -> StationConfig:
    path = Path(path)
    if enforce_protection:
        try:
            assert_root_protected_parents(path)
        except ProtectionError as exc:
            raise ConfigError(str(exc)) from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ConfigError(f"cannot safely open station configuration: {exc}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("station configuration is not a regular file")
        if enforce_protection and (info.st_uid != 0 or info.st_mode & 0o022):
            raise ConfigError("station configuration must be root-owned and not group/world writable")
        raw = os.read(fd, 65537)
        if len(raw) > 65536:
            raise ConfigError("station configuration exceeds 64 KiB")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"station configuration is not strict UTF-8 JSON: {exc}") from exc
    return validate_config_dict(data, allow_local_repository=allow_local_repository)
