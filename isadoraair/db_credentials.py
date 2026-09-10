"""Transactional PostgreSQL/.env/.pgpass credential rotation authority."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import grp
import json
import os
from pathlib import Path
import pwd
import secrets
import signal
import stat
import string
import subprocess
import tempfile
import time
from typing import Callable

import decouple
from django.conf import settings
import psycopg2
from psycopg2 import extensions, sql

from deploy.updater_runtime.isadoraair_updater.config import StationConfig, load_config
from isadoraair import env_config
from isadoraair.maintenance_lock import (
    database_maintenance_lock,
    database_rotation_is_pending,
    database_rotation_pending_path,
)


PSQL = "/usr/bin/psql"
PG_DUMP = "/usr/bin/pg_dump"
RUNUSER = "/usr/sbin/runuser"
DEFAULT_STATION_CONFIG = Path("/etc/isadoraair/station.json")
MAX_CREDENTIAL_FILE_BYTES = 1024 * 1024
MAX_RECOVERY_RECORD_BYTES = 4 * 1024 * 1024
MAX_VALIDATION_DUMP_BYTES = 50 * 1024 * 1024 * 1024
RECOVERY_RECORD_NAME = ".database-credential-rotation.recovery"
ACTIVE_UPDATE_STATES = frozenset({"accepted", "running"})
TERMINAL_UPDATE_STATES = frozenset({"succeeded", "failed", "manual_intervention_required"})
SAFE_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "_-~"
PERSISTENT_DATABASE_SERVICES = (
    "isadoraair-gunicorn.service",
    "isadoraair-engine.service",
    "isadoraair-encoders.service",
    "isadoraair-monitoring.service",
    "isadoraair-rbds.service",
)


class CredentialRotationError(RuntimeError):
    pass


class CredentialPreflightError(CredentialRotationError):
    pass


class CredentialRollbackError(CredentialRotationError):
    def __init__(self, original_phase: str, failed_authorities: list[str]):
        self.original_phase = original_phase
        self.failed_authorities = tuple(failed_authorities)
        super().__init__(
            f"credential rotation failed during {original_phase} and rollback was incomplete; "
            "unrestored authorities: " + ", ".join(failed_authorities)
        )


class CredentialRotationInterrupted(BaseException):
    """Catchable process termination while a credential transaction is live."""


class CredentialLoggingSuppressionError(CredentialRotationError):
    """The PostgreSQL logging/audit environment for the dedicated credential-
    mutation session could not be proven safe before the sensitive ALTER
    ROLE statement -- see _guarded_mutation_session()'s own docstring. A
    subclass of CredentialRotationError so every existing _alter_role()
    caller (rotate()'s forward path, _rollback(), and
    _recover_pending_under_lock()) already handles it exactly like any
    other mutation failure without needing to know about it specifically."""


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    data: bytes
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class DatabaseIdentity:
    host: str
    port: int
    name: str
    user: str


@dataclass
class _PreflightState:
    station: StationConfig
    identity: DatabaseIdentity
    environment: FileSnapshot
    pgpass: FileSnapshot
    old_password: str
    rollback_connection: object


@dataclass
class _RecoveryState:
    station: StationConfig
    identity: DatabaseIdentity
    environment: FileSnapshot
    pgpass: FileSnapshot
    old_password: str
    new_password: str


def generate_password(length: int = 48) -> str:
    if length < 32:
        raise ValueError("generated database passwords must be at least 32 characters")
    return "".join(secrets.choice(SAFE_PASSWORD_ALPHABET) for _ in range(length))


def validate_new_password(password: str):
    if not isinstance(password, str) or not 32 <= len(password) <= 1024:
        raise CredentialPreflightError("the new password must contain 32 through 1024 characters")
    if any(char not in SAFE_PASSWORD_ALPHABET for char in password):
        raise CredentialPreflightError(
            "the new password may contain only ASCII letters, digits, underscore, hyphen, and tilde"
        )
    # Every supported password is deliberately raw-value-safe for Django's
    # dotenv parser and the existing formal-backup/restore shell readers.
    if env_config.encode_env_value(password) != password or _escape_pgpass_password(password) != password:
        raise CredentialPreflightError("the new password is not safe for every database consumer")


@contextmanager
def _defer_termination_during_transaction():
    """Convert the first catchable termination signal into rollback control.

    Further termination signals are ignored until the caller has completed
    rollback and restored the original handlers. SIGKILL/power loss is handled
    by the durable recovery record instead.
    """
    handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
    previous = {}
    interrupted = False
    rollback_started = False

    def interrupt(signum, _frame):
        nonlocal interrupted
        if interrupted or rollback_started:
            return
        interrupted = True
        raise CredentialRotationInterrupted(f"termination signal {signum}")

    def defer_further_termination():
        nonlocal rollback_started
        rollback_started = True

    try:
        for signum in handled:
            previous[signum] = signal.signal(signum, interrupt)
        yield defer_further_termination
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _split_pgpass_fields(line: str) -> list[str]:
    fields = []
    current = []
    escaped = False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        raise CredentialPreflightError(".pgpass contains a trailing escape")
    fields.append("".join(current))
    if len(fields) != 5:
        raise CredentialPreflightError(".pgpass contains a malformed active entry")
    return fields


def _escape_pgpass_password(password: str) -> str:
    if any(char in password for char in ("\x00", "\r", "\n")):
        raise CredentialPreflightError("the requested password contains a forbidden control character")
    return password.replace("\\", "\\\\").replace(":", "\\:")


def _read_bounded(fd: int, maximum: int) -> bytes:
    chunks = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _fsync_directory_strict(directory: Path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(Path(directory), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# psycopg2.extensions.encrypt_password() (see _alter_role(), the sole
# caller of the guard below) computes a salted SCRAM-SHA-256 verifier
# client-side, through psycopg2's own libpq binding (PQencryptPasswordConn
# on the exact libpq instance that created the connection -- never a
# separately resolved native library; an earlier version of this module
# bridged to a ctypes.CDLL-loaded libpq directly, which could be a
# DIFFERENT libpq build than the one psycopg2 itself linked against, e.g.
# psycopg2-binary's bundled copy vs. the system package -- passing a
# PGconn* across that boundary is undefined behavior, observed directly
# during this correction's own real-PostgreSQL integration testing as a
# hard process segfault with no Python exception raised at all, bypassing
# every rollback/signal-handling path in this module) specifically so the
# *cleartext password* is never sent over the wire -- but the ALTER ROLE
# statement it issues still carries that verifier as a literal, and
# PostgreSQL's own statement/error logging and pgAudit are both driven by
# the statement TEXT, not by whether the value inside it happens to be a
# hash rather than a password. Every value below is a fixed, hardcoded
# constant -- never derived from caller input -- so building `SET`/`RESET`
# statement text by plain interpolation in _guarded_mutation_session() is
# safe.
_SENSITIVE_LOGGING_GUCS: tuple[tuple[str, str], ...] = (
    # log_statement = none alone is not sufficient: a *failing* ALTER ROLE
    # can still be captured through log_min_error_statement, and a random
    # transaction sample can still be captured through
    # log_transaction_sample_rate regardless of log_statement.
    ("log_statement", "none"),
    ("log_min_error_statement", "panic"),
    ("log_min_duration_statement", "-1"),
    ("log_min_duration_sample", "-1"),
    ("log_transaction_sample_rate", "0"),
)
_PGAUDIT_LOG_GUC = "pgaudit.log"
_PGAUDIT_SAFE_LITERAL = "none"
# Bounds the sensitive mutation itself -- _connect()'s connect_timeout only
# bounds establishing the TCP/socket connection, not a query that blocks
# waiting on, e.g., a competing lock on the role's catalog row. Not
# security-sensitive (a timeout that fails to apply can only make an
# eventual failure surface sooner or later, never leak anything), so these
# are applied best-effort in _guarded_mutation_session(), never fail-closed.
_MUTATION_STATEMENT_TIMEOUT_MS = 30_000
_MUTATION_LOCK_TIMEOUT_MS = 10_000


def _logging_value_is_safe(guc: str, effective: str) -> bool:
    effective = (effective or "").strip()
    if guc == "log_statement":
        return effective == "none"
    if guc == "log_min_error_statement":
        return effective == "panic"
    if guc in ("log_min_duration_statement", "log_min_duration_sample"):
        try:
            return int(effective) < 0
        except ValueError:
            return False
    if guc == "log_transaction_sample_rate":
        try:
            return float(effective) == 0.0
        except ValueError:
            return False
    return False


def _pgaudit_is_installed(cursor) -> bool:
    cursor.execute("SELECT 1 FROM pg_settings WHERE name = %s", (_PGAUDIT_LOG_GUC,))
    return cursor.fetchone() is not None


@contextmanager
def _guarded_mutation_session(connection):
    """Verified, session-scoped suppression of every PostgreSQL mechanism
    that could otherwise render the sensitive ALTER ROLE statement issued
    inside this block -- including its client-generated SCRAM verifier
    literal -- into server-side logs or pgAudit output, plus a bounded
    operation timeout for that same statement.

    Each listed parameter is set with a plain session-level SET (not SET
    LOCAL: this connection runs autocommit=True like every other connection
    in this module, and SET LOCAL's effect evaporates the instant the
    single-statement implicit transaction that issued it completes --
    before the very next statement, the actual mutation, would ever run)
    and immediately verified via current_setting(). Any parameter that
    cannot be set (most commonly: the connecting role has not been granted
    ``GRANT SET ON PARAMETER ...`` for it -- see
    docs/DATABASE_CREDENTIAL_ROTATION.md) or does not read back as expected
    raises CredentialLoggingSuppressionError *before* the caller is allowed
    to proceed -- fail closed, no PostgreSQL or credential-file authority
    touched. pgAudit is treated the same way, but only if this server
    actually has it loaded (a plain, always-permitted pg_settings existence
    check) -- there is nothing to suppress, and nothing to fail closed
    over, on a server without it.

    Every parameter this context manager touches (including the best-effort
    timeout bound) is unconditionally RESET before it returns, success or
    failure -- a long-lived connection (state.rollback_connection is reused
    for the rest of the transaction, and for every later rollback/recovery
    attempt) must never carry suppressed logging beyond the one sensitive
    statement this boundary exists to protect.
    """
    cursor = connection.cursor()
    applied: list[str] = []
    try:
        for guc, literal in (
            ("statement_timeout", str(_MUTATION_STATEMENT_TIMEOUT_MS)),
            ("lock_timeout", str(_MUTATION_LOCK_TIMEOUT_MS)),
        ):
            try:
                cursor.execute(f"SET {guc} TO {literal}")
                applied.append(guc)
            except Exception:
                pass

        for guc, literal in _SENSITIVE_LOGGING_GUCS:
            try:
                cursor.execute(f"SET {guc} TO {literal}")
                applied.append(guc)
                cursor.execute("SELECT current_setting(%s)", (guc,))
                safe = _logging_value_is_safe(guc, cursor.fetchone()[0])
            except Exception:
                raise CredentialLoggingSuppressionError(
                    f"PostgreSQL parameter {guc!r} could not be verified suppressed for the "
                    "dedicated credential-mutation session (permission denied, or the "
                    "parameter could not be read back) -- rotation aborted before any "
                    "credential was changed"
                ) from None
            if not safe:
                raise CredentialLoggingSuppressionError(
                    f"PostgreSQL parameter {guc!r} did not verify as suppressed for the "
                    "dedicated credential-mutation session -- rotation aborted before any "
                    "credential was changed"
                )

        pgaudit_active = _pgaudit_is_installed(cursor)
        if pgaudit_active:
            try:
                cursor.execute(f"SET {_PGAUDIT_LOG_GUC} TO '{_PGAUDIT_SAFE_LITERAL}'")
                applied.append(_PGAUDIT_LOG_GUC)
                cursor.execute("SELECT current_setting(%s)", (_PGAUDIT_LOG_GUC,))
                effective = (cursor.fetchone()[0] or "").strip().lower()
            except Exception:
                raise CredentialLoggingSuppressionError(
                    "pgAudit role/DDL auditing is active on this server and could not be "
                    "verified suppressed for the dedicated credential-mutation session -- "
                    "rotation aborted before any credential was changed"
                ) from None
            if effective not in ("", "none"):
                raise CredentialLoggingSuppressionError(
                    "pgAudit role/DDL auditing did not verify as suppressed for the dedicated "
                    "credential-mutation session -- rotation aborted before any credential was "
                    "changed"
                )

        yield pgaudit_active
    finally:
        for guc in reversed(applied):
            try:
                cursor.execute(f"RESET {guc}")
            except Exception:
                pass
        cursor.close()


def _matching_pgpass_entries(text: str, identity: DatabaseIdentity):
    wanted = (identity.host, str(identity.port), identity.name, identity.user)
    matches = []
    for index, physical in enumerate(text.splitlines(keepends=True)):
        body = physical.rstrip("\r\n")
        if not body or body.startswith("#"):
            continue
        fields = _split_pgpass_fields(body)
        if all(actual == "*" or actual == expected for actual, expected in zip(fields[:4], wanted)):
            matches.append((index, fields))
    return matches


def read_pgpass_password(raw: bytes, identity: DatabaseIdentity) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialPreflightError(".pgpass is not strict UTF-8") from exc
    matches = _matching_pgpass_entries(text, identity)
    if len(matches) != 1:
        raise CredentialPreflightError(
            ".pgpass must contain exactly one entry matching the configured database identity"
        )
    return matches[0][1][4]


def render_pgpass_password_update(raw: bytes, identity: DatabaseIdentity, password: str) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialPreflightError(".pgpass is not strict UTF-8") from exc
    matches = _matching_pgpass_entries(text, identity)
    if len(matches) != 1:
        raise CredentialPreflightError(
            ".pgpass must contain exactly one entry matching the configured database identity"
        )
    target_index, _fields = matches[0]
    lines = text.splitlines(keepends=True)
    physical = lines[target_index]
    body = physical.rstrip("\r\n")
    ending = physical[len(body):]
    # Preserve the first four fields byte-for-byte, including their existing
    # escaping. Only the password field is replaced.
    separators = []
    escaped = False
    for index, char in enumerate(body):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            separators.append(index)
    if len(separators) != 4:
        raise CredentialPreflightError(".pgpass contains a malformed matching entry")
    lines[target_index] = body[:separators[3] + 1] + _escape_pgpass_password(password) + ending
    return "".join(lines).encode("utf-8")


def _read_snapshot(path: Path, *, uid: int, gid: int, label: str) -> FileSnapshot:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise CredentialPreflightError(f"cannot safely open {label}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialPreflightError(f"{label} is not a regular file")
        if (info.st_uid, info.st_gid) != (uid, gid):
            raise CredentialPreflightError(f"{label} ownership does not match the station application identity")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise CredentialPreflightError(f"{label} mode must be 0600")
        raw = _read_bounded(fd, MAX_CREDENTIAL_FILE_BYTES)
        if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
            raise CredentialPreflightError(f"{label} exceeds the one-MiB safety limit")
    finally:
        os.close(fd)
    return FileSnapshot(Path(path), raw, 0o600, uid, gid)


def _env_values(snapshot: FileSnapshot) -> dict[str, str]:
    try:
        text = snapshot.data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialPreflightError("application environment file is not strict UTF-8") from exc
    pairs = env_config._parse_lines(text)
    keys = ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD")
    for key in keys:
        if len(env_config._active_line_indices(pairs, key)) != 1:
            raise CredentialPreflightError(f"application environment must contain exactly one {key} assignment")
    try:
        values = decouple.RepositoryEnv(str(snapshot.path)).data
    except Exception as exc:
        raise CredentialPreflightError("application environment cannot be parsed safely") from exc
    return {key: values[key] for key in keys}


def _assert_updater_idle(jobs_root: Path, *, enforce_root_protection: bool = True):
    try:
        root_info = Path(jobs_root).stat(follow_symlinks=False)
    except OSError as exc:
        raise CredentialPreflightError("protected updater job root cannot be inspected") from exc
    if (not stat.S_ISDIR(root_info.st_mode)
            or enforce_root_protection and (root_info.st_uid != 0 or root_info.st_mode & 0o022)):
        raise CredentialPreflightError("protected updater job-root protection is invalid")
    for path in sorted(Path(jobs_root).glob("*.json")):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode)
                        or enforce_root_protection and (info.st_uid != 0 or info.st_mode & 0o077)):
                    raise CredentialPreflightError("protected updater job-state protection is invalid")
                raw = _read_bounded(fd, MAX_CREDENTIAL_FILE_BYTES)
            finally:
                os.close(fd)
            if len(raw) > MAX_CREDENTIAL_FILE_BYTES:
                raise CredentialPreflightError("protected updater job state exceeds the safety limit")
            state = json.loads(raw.decode("utf-8"))
        except CredentialPreflightError:
            raise
        except Exception as exc:
            raise CredentialPreflightError("protected updater job state cannot be validated") from exc
        if (not isinstance(state, dict)
                or state.get("job_id") != path.stem or state.get("schema_version") != 1):
            raise CredentialPreflightError("protected updater job-state identity is invalid")
        status = state.get("state")
        if status in ACTIVE_UPDATE_STATES:
            raise CredentialPreflightError("a protected Update Center installation is active")
        if status not in TERMINAL_UPDATE_STATES:
            raise CredentialPreflightError("protected updater job state is unknown")


class CredentialRotator:
    """Coordinates the three credential authorities under one exclusive lock."""

    def __init__(
        self,
        *,
        station_config_path: Path = DEFAULT_STATION_CONFIG,
        lock_timeout: float = 30.0,
        command_timeout: float = 1800.0,
        require_root: bool = True,
        enforce_config_protection: bool = True,
        enforce_live_root: bool = True,
        failure_injector: Callable[[str], None] | None = None,
        progress: Callable[[str], None] | None = None,
    ):
        self.station_config_path = Path(station_config_path)
        self.lock_timeout = lock_timeout
        self.command_timeout = command_timeout
        self.require_root = require_root
        self.enforce_config_protection = enforce_config_protection
        self.enforce_live_root = enforce_live_root
        self.failure_injector = failure_injector or (lambda _phase: None)
        self.progress = progress or (lambda _message: None)

    def _station(self) -> StationConfig:
        if self.require_root and os.geteuid() != 0:
            raise CredentialPreflightError("credential rotation must be invoked as root")
        try:
            station = load_config(self.station_config_path, enforce_protection=self.enforce_config_protection)
        except Exception as exc:
            raise CredentialPreflightError("station configuration validation failed") from exc
        if station.database.pgpass_file is None:
            raise CredentialPreflightError("station configuration does not define a pgpass file")
        if self.enforce_live_root and station.application_root.resolve() != Path(settings.BASE_DIR).resolve():
            raise CredentialPreflightError("command is not running from the configured live application root")
        return station

    def _assert_persistent_consumers_stopped(self):
        if not self.enforce_live_root:
            return
        not_stopped = []
        for unit in PERSISTENT_DATABASE_SERVICES:
            try:
                loaded = subprocess.run(
                    ["/usr/bin/systemctl", "show", "--property=LoadState", "--value", unit],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                )
                active = subprocess.run(
                    ["/usr/bin/systemctl", "show", "--property=ActiveState", "--value", unit],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise CredentialPreflightError("persistent database-service state could not be inspected") from None
            if loaded.returncode != 0 or loaded.stdout.strip() != "loaded":
                raise CredentialPreflightError(f"required database service is not loaded: {unit}")
            if active.returncode != 0 or active.stdout.strip() != "inactive":
                not_stopped.append(unit)
        if not_stopped:
            raise CredentialPreflightError(
                "stop the persistent database consumers before rotation: " + ", ".join(not_stopped)
            )

    @staticmethod
    def _as_application_user(station: StationConfig, argv: list[str]) -> list[str]:
        expected_uid = pwd.getpwnam(station.application_user).pw_uid
        if os.geteuid() == expected_uid:
            return argv
        return [RUNUSER, "--user", station.application_user, "--", *argv]

    def _run(self, station: StationConfig, argv: list[str], *, env: dict[str, str] | None = None,
             stdout=None, output_path: Path | None = None,
             max_output_bytes: int | None = None) -> bool:
        controlled_env = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "HOME": pwd.getpwnam(station.application_user).pw_dir,
        }
        if env:
            controlled_env.update(env)
        try:
            process = subprocess.Popen(
                self._as_application_user(station, argv),
                cwd=station.application_root,
                env=controlled_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL if stdout is None else stdout,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            return False
        try:
            deadline = time.monotonic() + self.command_timeout
            exceeded = False
            while process.poll() is None:
                if output_path is not None and max_output_bytes is not None:
                    try:
                        exceeded = output_path.stat().st_size > max_output_bytes
                    except OSError:
                        # A validator output which disappears or becomes
                        # uninspectable is itself a failed validation.
                        exceeded = True
                    if exceeded:
                        break
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return False
            return process.returncode == 0 and not exceeded
        except BaseException:
            # A signal converted by the transaction guard (or Ctrl-C) must
            # never orphan psql/pg_dump while rollback releases the lock.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise

    @staticmethod
    def _connect(identity: DatabaseIdentity, password: str):
        try:
            connection = psycopg2.connect(
                host=identity.host,
                port=identity.port,
                dbname=identity.name,
                user=identity.user,
                password=password,
                connect_timeout=10,
                application_name="isadoraair-credential-rotation",
            )
            connection.autocommit = True
            return connection
        except Exception:
            raise CredentialPreflightError("PostgreSQL authentication failed") from None

    def _alter_role(self, connection, user: str, password: str):
        """Change `user`'s PostgreSQL password to `password`.

        Every caller (rotate()'s forward mutation, _rollback()'s primary
        and fallback attempts, and _recover_pending_under_lock()'s crash
        recovery) reaches PostgreSQL only through this guarded session
        boundary -- see _guarded_mutation_session()'s own docstring. A
        guard failure raises CredentialLoggingSuppressionError (a
        CredentialRotationError) before a verifier is ever generated or any
        SQL is issued, so no authority is touched and every existing
        caller's exception handling already treats it exactly like any
        other _alter_role failure.

        The SCRAM-SHA-256 verifier is generated by psycopg2's own
        extensions.encrypt_password() -- backed by PQencryptPasswordConn
        through psycopg2's own libpq binding, the exact same libpq instance
        that created `connection` -- rather than a raw ctypes bridge to an
        independently resolved libpq (see this module's own history: that
        approach segfaulted the whole process outright when the two libpq
        builds differed). The cleartext password is never rendered into
        SQL; the role name is passed as a proper SQL identifier, never
        string-interpolated.
        """
        with _guarded_mutation_session(connection) as pgaudit_active:
            self.progress(
                "PostgreSQL statement/error logging and pgAudit (if present) verified "
                f"suppressed, and a bounded operation timeout applied, for the "
                f"credential-mutation session (pgaudit active: {pgaudit_active})"
            )
            try:
                verifier = extensions.encrypt_password(
                    password, user, scope=connection, algorithm="scram-sha-256",
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(user)),
                        (verifier,),
                    )
            except Exception:
                raise CredentialRotationError("PostgreSQL role credential update failed") from None

    def _django_probe(self, station: StationConfig) -> bool:
        return self._run(
            station,
            [str(station.application_python), str(station.live_manage_py), "probe_database_credentials"],
        )

    def _psql_probe(self, station: StationConfig, identity: DatabaseIdentity) -> bool:
        return self._run(
            station,
            [PSQL, "-X", "-w", "--host", identity.host, "--port", str(identity.port),
             "--username", identity.user, "--dbname", identity.name, "--tuples-only", "--no-align",
             "--command", "SELECT 1"],
            env={"PGPASSFILE": str(station.database.pgpass_file)},
        )

    def _pg_dump_probe(self, station: StationConfig, identity: DatabaseIdentity) -> bool:
        fd, temporary = tempfile.mkstemp(
            prefix=".db-credential-pgdump-", suffix=".dump", dir=station.application_root,
        )
        path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as output:
                ok = self._run(
                    station,
                    [PG_DUMP, "--format=custom", "--no-owner", "--no-acl",
                     "--host", identity.host, "--port", str(identity.port),
                     "--username", identity.user, "--dbname", identity.name],
                    env={"PGPASSFILE": str(station.database.pgpass_file)},
                    stdout=output,
                    output_path=path,
                    max_output_bytes=MAX_VALIDATION_DUMP_BYTES,
                )
            return ok and path.is_file() and path.stat().st_size > 0 and stat.S_IMODE(path.stat().st_mode) == 0o600
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            path.unlink(missing_ok=True)

    def _validate_paths(self, station: StationConfig, identity: DatabaseIdentity):
        if not self._django_probe(station):
            raise CredentialRotationError("Django database authentication validation failed")
        if not self._psql_probe(station, identity):
            raise CredentialRotationError("pgpass psql authentication validation failed")
        if not self._pg_dump_probe(station, identity):
            raise CredentialRotationError("updater-equivalent pg_dump validation failed")

    def _preflight_under_lock(self, station: StationConfig) -> _PreflightState:
        app_account = pwd.getpwnam(station.application_user)
        app_group = grp.getgrnam(station.application_group)
        environment = _read_snapshot(
            station.application_environment_file, uid=app_account.pw_uid, gid=app_group.gr_gid,
            label="application environment file",
        )
        pgpass = _read_snapshot(
            station.database.pgpass_file, uid=app_account.pw_uid, gid=app_group.gr_gid,
            label="updater pgpass file",
        )
        values = _env_values(environment)
        try:
            identity = DatabaseIdentity(values["DB_HOST"], int(values["DB_PORT"]), values["DB_NAME"], values["DB_USER"])
        except (TypeError, ValueError) as exc:
            raise CredentialPreflightError("application database identity is invalid") from exc
        configured = station.database
        if (identity.host, identity.port, identity.name, identity.user) != (
            configured.host, configured.port, configured.name, configured.user,
        ):
            raise CredentialPreflightError("station and application database identities disagree")
        running = settings.DATABASES["default"]
        if (
            str(running["HOST"]), int(running["PORT"]), str(running["NAME"]),
            str(running["USER"]), str(running["PASSWORD"]),
        ) != (identity.host, identity.port, identity.name, identity.user, values["DB_PASSWORD"]):
            raise CredentialPreflightError("running Django database settings disagree with the application environment file")
        pgpass_password = read_pgpass_password(pgpass.data, identity)
        if not secrets.compare_digest(values["DB_PASSWORD"], pgpass_password):
            raise CredentialPreflightError("application and updater database credentials disagree")
        _assert_updater_idle(
            station.jobs_root, enforce_root_protection=self.enforce_config_protection,
        )
        connection = self._connect(identity, values["DB_PASSWORD"])
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                if cursor.fetchone()[0] != identity.user:
                    raise CredentialPreflightError("PostgreSQL authenticated as an unexpected role")
            self._validate_paths(station, identity)
        except Exception:
            connection.close()
            raise
        return _PreflightState(station, identity, environment, pgpass, values["DB_PASSWORD"], connection)

    @staticmethod
    def _write_snapshot(snapshot: FileSnapshot, data: bytes):
        env_config._atomic_write_bytes(
            snapshot.path, data, snapshot.mode, uid=snapshot.uid, gid=snapshot.gid,
        )
        _fsync_directory_strict(snapshot.path.parent)

    @staticmethod
    def _verify_file(snapshot: FileSnapshot, expected: bytes):
        current = _read_snapshot(snapshot.path, uid=snapshot.uid, gid=snapshot.gid, label=snapshot.path.name)
        if current.data != expected:
            raise CredentialRotationError(f"{snapshot.path.name} content verification failed")

    @staticmethod
    def _recovery_path(station: StationConfig) -> Path:
        return Path(station.jobs_root) / RECOVERY_RECORD_NAME

    def _recovery_owner(self) -> tuple[int, int]:
        if self.enforce_config_protection:
            return 0, 0
        return os.geteuid(), os.getegid()

    @staticmethod
    def _snapshot_record(snapshot: FileSnapshot) -> dict:
        return {
            "path": str(snapshot.path),
            "data": base64.b64encode(snapshot.data).decode("ascii"),
            "mode": snapshot.mode,
            "uid": snapshot.uid,
            "gid": snapshot.gid,
        }

    def _write_recovery_record(self, state: _PreflightState, new_password: str):
        record = {
            "schema_version": 1,
            "identity": {
                "host": state.identity.host,
                "port": state.identity.port,
                "name": state.identity.name,
                "user": state.identity.user,
            },
            "old_password": state.old_password,
            "new_password": new_password,
            "environment": self._snapshot_record(state.environment),
            "pgpass": self._snapshot_record(state.pgpass),
        }
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_RECOVERY_RECORD_BYTES:
            raise CredentialRotationError("secure recovery state exceeds the safety limit")
        uid, gid = self._recovery_owner()
        env_config._atomic_write_bytes(
            self._recovery_path(state.station), raw, 0o600, uid=uid, gid=gid,
        )
        _fsync_directory_strict(self._recovery_path(state.station).parent)

    @staticmethod
    def _write_pending_marker(station: StationConfig):
        path = database_rotation_pending_path(station.application_environment_file)
        app_info = Path(station.application_environment_file).stat(follow_symlinks=False)
        env_config._atomic_write_bytes(
            path, b"database credential recovery pending\n", 0o600,
            uid=app_info.st_uid, gid=app_info.st_gid,
        )
        _fsync_directory_strict(path.parent)

    @staticmethod
    def _decode_snapshot_record(value, *, expected_path: Path, uid: int, gid: int) -> FileSnapshot:
        if not isinstance(value, dict):
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"])
        try:
            path = Path(value["path"])
            raw = base64.b64decode(value["data"], validate=True)
            mode = int(value["mode"])
            record_uid = int(value["uid"])
            record_gid = int(value["gid"])
        except (KeyError, TypeError, ValueError):
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"]) from None
        if (path != Path(expected_path) or mode != 0o600
                or (record_uid, record_gid) != (uid, gid)
                or len(raw) > MAX_CREDENTIAL_FILE_BYTES):
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"])
        return FileSnapshot(path, raw, mode, record_uid, record_gid)

    def _load_recovery_record(self, station: StationConfig) -> _RecoveryState | None:
        path = self._recovery_path(station)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"]) from None
        try:
            info = os.fstat(fd)
            expected_owner = self._recovery_owner()
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or (info.st_uid, info.st_gid) != expected_owner):
                raise CredentialRollbackError("pending_recovery", ["secure recovery record"])
            raw = _read_bounded(fd, MAX_RECOVERY_RECORD_BYTES)
        finally:
            os.close(fd)
        if len(raw) > MAX_RECOVERY_RECORD_BYTES:
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"])
        try:
            record = json.loads(raw.decode("utf-8"))
            identity_record = record["identity"]
            identity = DatabaseIdentity(
                identity_record["host"], int(identity_record["port"]),
                identity_record["name"], identity_record["user"],
            )
            old_password = record["old_password"]
            new_password = record["new_password"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"]) from None
        configured = station.database
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or not isinstance(old_password, str) or not isinstance(new_password, str)
                or (identity.host, identity.port, identity.name, identity.user) != (
                    configured.host, configured.port, configured.name, configured.user,
                )):
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"])
        try:
            validate_new_password(new_password)
        except Exception:
            raise CredentialRollbackError("pending_recovery", ["secure recovery record"]) from None
        account = pwd.getpwnam(station.application_user)
        group = grp.getgrnam(station.application_group)
        environment = self._decode_snapshot_record(
            record.get("environment"), expected_path=station.application_environment_file,
            uid=account.pw_uid, gid=group.gr_gid,
        )
        pgpass = self._decode_snapshot_record(
            record.get("pgpass"), expected_path=station.database.pgpass_file,
            uid=account.pw_uid, gid=group.gr_gid,
        )
        return _RecoveryState(
            station, identity, environment, pgpass, old_password, new_password,
        )

    def _remove_recovery_record(self, station: StationConfig):
        path = self._recovery_path(station)
        path.unlink(missing_ok=True)
        _fsync_directory_strict(path.parent)

    @staticmethod
    def _remove_pending_marker(station: StationConfig):
        path = database_rotation_pending_path(station.application_environment_file)
        path.unlink(missing_ok=True)
        _fsync_directory_strict(path.parent)

    def _remove_transaction_records(self, station: StationConfig):
        # Keep the public nonsecret gate until the secret recovery authority
        # has been durably removed. A crash between these operations can only
        # leave an over-conservative marker, never an un-gated mixed state.
        self._remove_recovery_record(station)
        self._remove_pending_marker(station)

    def _restore_snapshot_if_needed(self, snapshot: FileSnapshot):
        try:
            self._verify_file(snapshot, snapshot.data)
            return
        except Exception:
            pass
        self._write_snapshot(snapshot, snapshot.data)
        self._verify_file(snapshot, snapshot.data)

    def _recover_pending_under_lock(self, station: StationConfig) -> bool:
        pending = self._load_recovery_record(station)
        if pending is None:
            return False
        # Repair a missing/corrupt public gate before touching authorities.
        # This marker contains no credential and is readable by Gunicorn and
        # the formal backup path after a process/power-loss boundary.
        self._write_pending_marker(station)
        self.progress("secure pending recovery detected")
        old_connection = new_connection = None
        try:
            try:
                old_connection = self._connect(pending.identity, pending.old_password)
            except Exception:
                pass
            try:
                new_connection = self._connect(pending.identity, pending.new_password)
            except Exception:
                pass
            if (old_connection is None) == (new_connection is None):
                raise CredentialRollbackError("pending_recovery", ["PostgreSQL role"])
            if new_connection is not None:
                try:
                    self._alter_role(
                        new_connection, pending.identity.user, pending.old_password,
                    )
                except Exception:
                    # As in the in-process rollback path, a lost response is
                    # indeterminate. Treat a successful old-password login as
                    # proof that the intended restoration already committed.
                    try:
                        already_old = self._connect(
                            pending.identity, pending.old_password,
                        )
                        already_old.close()
                    except Exception:
                        raise CredentialRollbackError(
                            "pending_recovery", ["PostgreSQL role"],
                        ) from None
            failures = []
            for label, snapshot in (
                ("application environment", pending.environment),
                ("updater pgpass", pending.pgpass),
            ):
                try:
                    self._restore_snapshot_if_needed(snapshot)
                except Exception:
                    failures.append(label)
            if not failures:
                try:
                    restored = self._connect(pending.identity, pending.old_password)
                    restored.close()
                    self._validate_paths(station, pending.identity)
                except Exception:
                    failures.append("restored credential validation")
            if failures:
                raise CredentialRollbackError("pending_recovery", failures)
            try:
                self._remove_transaction_records(station)
            except Exception:
                raise CredentialRollbackError(
                    "pending_recovery", ["secure recovery record"],
                ) from None
            self.progress("pending credential transaction restored to its prior validated state")
            return True
        finally:
            if old_connection is not None:
                old_connection.close()
            if new_connection is not None:
                new_connection.close()

    def preflight(self):
        station = self._station()
        with database_maintenance_lock(
            station.application_environment_file, shared=False, timeout=self.lock_timeout,
        ):
            recovery_pending = self._load_recovery_record(station) is not None
            marker_pending = database_rotation_is_pending(
                station.application_environment_file,
            )
            if recovery_pending:
                self._assert_persistent_consumers_stopped()
                _assert_updater_idle(
                    station.jobs_root,
                    enforce_root_protection=self.enforce_config_protection,
                )
                self._recover_pending_under_lock(station)
                raise CredentialPreflightError(
                    "a pending credential transaction was restored; restart consumers and rerun preflight"
                )
            state = self._preflight_under_lock(station)
            state.rollback_connection.close()
            # Only a complete, authenticated three-authority preflight proves
            # that a marker without a secret recovery record is stale (e.g.
            # crash before mutation, or after final commit cleanup began).
            if marker_pending:
                try:
                    self._remove_pending_marker(station)
                except Exception:
                    raise CredentialRotationError(
                        "credential authorities are healthy but the pending recovery gate could not be cleared"
                    ) from None
        return state.identity

    def _rollback(self, state: _PreflightState, new_password: str, original_phase: str):
        failures = []
        try:
            self._alter_role(state.rollback_connection, state.identity.user, state.old_password)
        except Exception:
            try:
                fallback = self._connect(state.identity, new_password)
                try:
                    self._alter_role(fallback, state.identity.user, state.old_password)
                finally:
                    fallback.close()
            except Exception:
                # An ALTER ROLE transport failure is an indeterminate boundary:
                # the server may still have rejected it before mutation. If the
                # old credential authenticates, PostgreSQL is already in the
                # desired rollback state and must not be reported as broken.
                try:
                    already_old = self._connect(state.identity, state.old_password)
                    already_old.close()
                except Exception:
                    failures.append("PostgreSQL role")
        for label, snapshot in (("application environment", state.environment), ("updater pgpass", state.pgpass)):
            try:
                self._restore_snapshot_if_needed(snapshot)
            except Exception:
                failures.append(label)
        if not failures:
            try:
                old_connection = self._connect(state.identity, state.old_password)
                old_connection.close()
                self._validate_paths(state.station, state.identity)
            except Exception:
                failures.append("restored credential validation")
        if not failures:
            try:
                self._remove_transaction_records(state.station)
            except Exception:
                failures.append("secure recovery record")
        if failures:
            raise CredentialRollbackError(original_phase, failures)

    def rotate(self, new_password: str):
        # Renderability/escaping failures occur before the lock and before any
        # authority can be mutated.
        validate_new_password(new_password)
        station = self._station()
        self.progress("rotation started")
        with database_maintenance_lock(
            station.application_environment_file, shared=False, timeout=self.lock_timeout,
        ):
            self.progress("maintenance lock acquired")
            self._assert_persistent_consumers_stopped()
            self.progress("persistent database consumers are stopped")
            _assert_updater_idle(
                station.jobs_root,
                enforce_root_protection=self.enforce_config_protection,
            )
            if self._recover_pending_under_lock(station):
                raise CredentialPreflightError(
                    "a pending credential transaction was restored; rerun rotation deliberately"
                )
            if database_rotation_is_pending(station.application_environment_file):
                raise CredentialPreflightError(
                    "a pending credential gate requires a successful --preflight before rotation"
                )
            state = self._preflight_under_lock(station)
            self.progress("preflight passed")
            try:
                if secrets.compare_digest(state.old_password, new_password):
                    raise CredentialPreflightError("the new password must differ from the current password")
                env_bytes = env_config.render_database_password_update(state.environment.data, new_password)
                pgpass_bytes = render_pgpass_password_update(state.pgpass.data, state.identity, new_password)
            except Exception:
                state.rollback_connection.close()
                raise
            phase = "recovery_record_staging"
            try:
                self._write_pending_marker(station)
                self._write_recovery_record(state, new_password)
            except BaseException:
                cleanup_failed = False
                try:
                    self._remove_recovery_record(station)
                except Exception:
                    cleanup_failed = True
                try:
                    self._remove_pending_marker(station)
                except Exception:
                    cleanup_failed = True
                state.rollback_connection.close()
                if cleanup_failed:
                    raise CredentialRollbackError(
                        phase, ["secure recovery record"],
                    ) from None
                raise CredentialRotationError(
                    "credential rotation failed during recovery_record_staging; no authority was changed"
                ) from None
            self.progress("secure rollback state persisted")
            database_mutation_attempted = False
            with _defer_termination_during_transaction() as defer_further_termination:
                try:
                    phase = "before_database_change"
                    self.failure_injector(phase)
                    database_mutation_attempted = True
                    self._alter_role(state.rollback_connection, state.identity.user, new_password)
                    self.progress("PostgreSQL authority updated")
                    phase = "after_database_change"
                    self.failure_injector(phase)

                    phase = "environment_staging"
                    self.failure_injector(phase)
                    phase = "environment_atomic_replace"
                    self._write_snapshot(state.environment, env_bytes)
                    self.failure_injector(phase)
                    self.progress("application environment updated")

                    phase = "pgpass_staging"
                    self.failure_injector(phase)
                    phase = "pgpass_atomic_replace"
                    self._write_snapshot(state.pgpass, pgpass_bytes)
                    self.failure_injector(phase)
                    self.progress("updater credential updated")

                    self._verify_file(state.environment, env_bytes)
                    self._verify_file(state.pgpass, pgpass_bytes)
                    phase = "django_validation"
                    self.failure_injector(phase)
                    if not self._django_probe(station):
                        raise CredentialRotationError("Django database authentication validation failed")
                    self.progress("Django authentication passed")
                    phase = "psql_validation"
                    self.failure_injector(phase)
                    if not self._psql_probe(station, state.identity):
                        raise CredentialRotationError("pgpass psql authentication validation failed")
                    self.progress("pgpass psql authentication passed")
                    phase = "pg_dump_validation"
                    self.failure_injector(phase)
                    if not self._pg_dump_probe(station, state.identity):
                        raise CredentialRotationError("updater-equivalent pg_dump validation failed")
                    self.progress("updater-equivalent pg_dump passed")
                    phase = "recovery_record_commit"
                    self._remove_transaction_records(station)
                except BaseException:
                    # Whether the failure was a signal, disk error, or failed
                    # validator, no later termination signal may interrupt the
                    # rollback and its real pg_dump validation.
                    defer_further_termination()
                    if phase == "recovery_record_commit":
                        state.rollback_connection.close()
                        raise CredentialRotationError(
                            "all new credential authorities validated, but durable cleanup is incomplete; "
                            "leave consumers stopped and rerun --preflight"
                        ) from None
                    if not database_mutation_attempted:
                        try:
                            self._remove_transaction_records(station)
                        except Exception:
                            state.rollback_connection.close()
                            raise CredentialRollbackError(
                                phase, ["secure recovery record"],
                            ) from None
                        state.rollback_connection.close()
                        raise CredentialRotationError(
                            f"credential rotation failed during {phase}; no authority was changed"
                        ) from None
                    try:
                        self._rollback(state, new_password, phase)
                    except CredentialRollbackError:
                        raise
                    finally:
                        state.rollback_connection.close()
                    raise CredentialRotationError(
                        f"credential rotation failed during {phase}; the previous working state was restored and validated"
                    ) from None
            state.rollback_connection.close()
            self.progress("rotation committed")
        return state.identity
