"""Reusable, security-conscious layer for reading and updating a small,
explicitly ALLOWLISTED set of keys in the project's .env file from
Django admin. Built for Phase 1's SMTP settings, surfaced on Monitoring
-> Notification Config (see monitoring/admin.py), but written so another
admin section can register its own key later (e.g. MUSICBRAINZ_CONTACT)
without duplicating any of this module's parsing/locking/atomic-write/
backup/secret-handling/comparison logic.

NOT a general .env editor. Only keys explicitly registered below via
register_setting()/MANAGED_SETTINGS can ever be read or written through
this module -- there is no code path that accepts an arbitrary caller-
supplied key name for a write, and no raw-textarea/whole-file edit
capability at all. Values stay in .env exclusively; nothing in this
module ever touches a Django model or the database.

Architecture
------------
Reads and writes deliberately use two different parsers:

  * READS reuse `decouple.RepositoryEnv` directly -- the exact class
    Django's own settings.py drives via `from decouple import config`.
    This guarantees the "Saved" value shown in admin is byte-identical
    to what decouple will actually load on the next process start; there
    is no second, hand-rolled value-decoder that could drift from it.
    Critically, we read `RepositoryEnv(path).data` directly rather than
    going through `Config.get()` / the module-level `decouple.config`
    singleton -- `Config.get()` checks `os.environ` BEFORE the file,
    which would silently return the CURRENTLY RUNNING process's value
    (frozen at systemd start) instead of what's actually on disk, and
    defeat the "Saved vs Running" distinction this module exists to
    provide (see settings.py, EMAIL_* block, and monitoring/admin.py).

  * WRITES use a small hand-rolled *structural* line scanner
    (_parse_lines/_render_lines below) that decouple has no equivalent
    for -- it exists only to answer "which line(s), if any, are this
    key's active assignment" so an update can surgically replace or
    append a line while leaving every comment, blank line, unrelated
    key, and unknown key byte-for-byte untouched, and so a key defined
    twice can be detected and rejected before anything is written. Its
    "is this line an active assignment" rule is deliberately identical
    to decouple.RepositoryEnv's own (strip; skip blank/comment/no-'=';
    split on the FIRST '=' only) -- verified against decouple 3.8's
    source, not assumed.

Encoding
--------
encode_env_value() implements the one canonical quoting rule proved (by
direct, live testing against `systemd-run --property=EnvironmentFile=`
and against decouple.RepositoryEnv's own source -- see
isadoraair/tests/test_env_config.py's module docstring for the full
empirical basis) to round-trip identically through BOTH real consumers
of this file: systemd's EnvironmentFile= parser (loads it into every
persistent service's process environment at start) and python-decouple
(every `config(...)` call in settings.py). A value that cannot be safely
represented in a form both agree on is rejected with a clear error
rather than silently corrupted.
"""
import fcntl
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import decouple
from django.conf import settings as django_settings

# ---------------------------------------------------------------------
# Authoritative path. ONE constant, matching production's
# @@ISA_ROOT@@/.env convention exactly (settings.BASE_DIR is the same
# Path django/settings.py itself computes as the repo root). Deliberately
# module-level (not re-derived inside every function) so tests can
# monkeypatch it (`patch.object(env_config, "ENV_FILE_PATH", tmp_path)`)
# the same way encoders/services/lkg.py's CANDIDATE_DIR/LKG_DIR are
# monkeypatched elsewhere in this project -- this NEVER accepts a
# browser-supplied path; every public function's optional `env_path`
# argument exists solely for tests to redirect it to a temp file.
# ---------------------------------------------------------------------
ENV_FILE_PATH = Path(django_settings.BASE_DIR) / ".env"


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------
class EnvConfigError(Exception):
    """Base class for every error this module raises."""


class UnregisteredKeyError(EnvConfigError):
    def __init__(self, key):
        super().__init__(f"{key!r} is not a registered managed setting")
        self.key = key


class DuplicateManagedKeyError(EnvConfigError):
    def __init__(self, key):
        super().__init__(
            f"{key} is defined more than once in .env. Resolve the duplicate "
            f"before editing it from Django admin."
        )
        self.key = key


class InvalidValueError(EnvConfigError):
    def __init__(self, key, message):
        super().__init__(f"{key}: {message}")
        self.key = key


class UnsafeValueError(EnvConfigError):
    """The value cannot be represented in a form both systemd's
    EnvironmentFile= parser and python-decouple agree on."""


class EnvWriteError(EnvConfigError):
    """An I/O-level failure (permissions, symlink rejected, lock
    timeout, ...). The previous .env is guaranteed intact whenever this
    is raised -- every write path only ever mutates a temp file and
    swaps it in with a single atomic os.replace() at the very end."""


# ---------------------------------------------------------------------
# Value encoding (write path) -- see module docstring for the empirical
# basis. A value needs quoting if it has leading/trailing whitespace,
# contains a backslash, or is itself already wrapped in a matching pair
# of quote characters (which would otherwise be double-stripped on next
# read). Newlines/CR/NUL can never be represented in a single-line
# assignment under either consumer and are rejected outright.
# ---------------------------------------------------------------------
_FORBIDDEN_CHARS = re.compile(r"[\r\n\x00]")


def encode_env_value(raw):
    if _FORBIDDEN_CHARS.search(raw):
        raise UnsafeValueError(
            "value contains a newline, carriage return, or NUL character, which "
            "cannot be represented in a .env assignment"
        )
    if raw == "":
        return ""
    needs_quoting = (
        raw != raw.strip()
        or "\\" in raw
        or (raw[0] == raw[-1] and raw[0] in ("'", '"'))
    )
    if not needs_quoting:
        return raw
    if "'" not in raw:
        return "'" + raw + "'"
    if '"' not in raw and "\\" not in raw:
        return '"' + raw + '"'
    raise UnsafeValueError(
        "this value can't be safely represented in .env format (it needs quoting "
        "but contains both a single quote and a double-quote-or-backslash) -- "
        "choose a value that avoids this exact combination"
    )


def _decouple_style_bool(value):
    """Mirrors decouple.Config._cast_boolean exactly, verified against
    decouple 3.8's own source: '' -> False, else classic strtobool
    semantics (case-insensitive y/yes/t/true/on/1 -> True;
    n/no/f/false/off/0 -> False; anything else -> ValueError)."""
    value = str(value)
    if value == "":
        return False
    v = value.strip().lower()
    if v in ("y", "yes", "t", "true", "on", "1"):
        return True
    if v in ("n", "no", "f", "false", "off", "0"):
        return False
    raise ValueError(f"invalid truth value {value!r}")


# ---------------------------------------------------------------------
# Field validators. Each receives (key, raw_str) and raises
# InvalidValueError on rejection; returns None on success. Kept generic
# enough to reuse for a future registration by another admin section.
# ---------------------------------------------------------------------
def _reject_control_chars(key, value):
    if _FORBIDDEN_CHARS.search(value):
        raise InvalidValueError(key, "must not contain a newline, carriage return, or NUL character")


def validate_hostname_text(key, value):
    _reject_control_chars(key, value)
    if not value.strip():
        raise InvalidValueError(key, "must not be blank")
    if re.search(r"\s", value):
        raise InvalidValueError(key, "must not contain whitespace")


def validate_port(key, value):
    _reject_control_chars(key, value)
    try:
        port = int(value.strip())
    except ValueError:
        raise InvalidValueError(key, "must be a whole number") from None
    if not (1 <= port <= 65535):
        raise InvalidValueError(key, "must be between 1 and 65535")


def validate_bool_text(key, value):
    _reject_control_chars(key, value)
    try:
        _decouple_style_bool(value)
    except ValueError:
        raise InvalidValueError(
            key, "must be a recognizable true/false value (e.g. True/False, yes/no, 1/0)"
        ) from None


def validate_email_address(key, value):
    _reject_control_chars(key, value)
    from django.core.exceptions import ValidationError as DjangoValidationError
    from django.core.validators import validate_email as django_validate_email

    # Matches this project's existing DEFAULT_FROM_EMAIL convention (see
    # .env.example / settings.py's own default, "isadoraair@localhost")
    # -- a bare address, not "Display Name <addr>" form. Nothing else in
    # this codebase parses the display-name form for this setting, so
    # Phase 1 doesn't special-case it.
    try:
        django_validate_email(value.strip())
    except DjangoValidationError:
        raise InvalidValueError(key, "must be a valid email address") from None


def validate_allow_blank_text(key, value):
    """Blank is fine; only rejects control characters. Used for
    EMAIL_HOST_USER and EMAIL_HOST_PASSWORD -- both allow blank, and
    a realistic SMTP username/password can contain almost any other
    printable punctuation."""
    _reject_control_chars(key, value)


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ManagedSetting:
    key: str
    label: str
    category: str
    secret: bool = False
    default: str = ""
    cast: Callable[[str], object] = str
    validate: Callable[[str, str], None] = field(default=lambda key, value: None)
    services_note: str = ""


MANAGED_SETTINGS: dict = {}


def register_setting(setting):
    if setting.key in MANAGED_SETTINGS:
        raise ValueError(f"{setting.key!r} is already a registered managed setting")
    MANAGED_SETTINGS[setting.key] = setting


_GUNICORN_AND_MONITORING_NOTE = (
    "isadoraair-gunicorn and isadoraair-monitoring both hold this in memory for "
    "their whole process lifetime -- restart both to pick up a change. One-shot "
    "management commands (send_weather_notification, send_ogremote_notification, "
    "send_webrequests_notification, generate_dedication_intros, ...) read the "
    "environment fresh at each invocation and need no restart."
)

register_setting(ManagedSetting(
    key="EMAIL_HOST", label="SMTP host", category="smtp", default="localhost",
    validate=validate_hostname_text, services_note=_GUNICORN_AND_MONITORING_NOTE,
))
register_setting(ManagedSetting(
    key="EMAIL_PORT", label="SMTP port", category="smtp", default="587", cast=int,
    validate=validate_port, services_note=_GUNICORN_AND_MONITORING_NOTE,
))
register_setting(ManagedSetting(
    key="EMAIL_HOST_USER", label="SMTP username", category="smtp", default="",
    validate=validate_allow_blank_text, services_note=_GUNICORN_AND_MONITORING_NOTE,
))
register_setting(ManagedSetting(
    key="EMAIL_HOST_PASSWORD", label="SMTP password", category="smtp", default="", secret=True,
    validate=validate_allow_blank_text, services_note=_GUNICORN_AND_MONITORING_NOTE,
))
register_setting(ManagedSetting(
    key="EMAIL_USE_TLS", label="Use TLS", category="smtp", default="True", cast=_decouple_style_bool,
    validate=validate_bool_text, services_note=_GUNICORN_AND_MONITORING_NOTE,
))
register_setting(ManagedSetting(
    key="DEFAULT_FROM_EMAIL", label="Default From address", category="smtp", default="isadoraair@localhost",
    validate=validate_email_address, services_note=_GUNICORN_AND_MONITORING_NOTE,
))


# ---------------------------------------------------------------------
# Structural line parsing (write path only)
# ---------------------------------------------------------------------
def _parse_lines(text):
    """List of (line_text, key_or_None) tuples, one per physical line.
    `key` is set only for a line decouple's own parser would treat as an
    active assignment: non-blank after stripping, not '#'-prefixed, and
    contains '=' (key = everything before the FIRST '=', stripped)."""
    if text == "":
        return []
    body = text[:-1] if text.endswith("\n") else text
    result = []
    for line in body.split("\n"):
        stripped = line.strip()
        key = None
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
        result.append((line, key))
    return result


def _render_lines(pairs):
    if not pairs:
        return ""
    return "\n".join(text for text, _ in pairs) + "\n"


def _active_line_indices(pairs, key):
    return [i for i, (_, k) in enumerate(pairs) if k == key]


def _read_repo_data(path):
    """decouple.RepositoryEnv(path).data -- the file's OWN parsed
    contents, deliberately bypassing decouple.Config.get()'s
    os.environ-first lookup (see module docstring)."""
    if not path.exists():
        return {}
    return decouple.RepositoryEnv(str(path)).data


# ---------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class ManagedValue:
    key: str
    present: bool
    display_value: Optional[str]  # decoded value (default-filled if absent); ALWAYS None for secret keys


def read_managed_values(keys=None, env_path=None):
    """{key: ManagedValue} for each requested key (every registered key
    if `keys` is None). Raises DuplicateManagedKeyError if any requested
    key has more than one active assignment line in the file. A
    secret-registered key's ManagedValue.display_value is always None --
    this is a structural guarantee, not a display-layer choice: the raw
    value never leaves this function for such a key."""
    path = Path(env_path) if env_path is not None else ENV_FILE_PATH
    keys = list(MANAGED_SETTINGS) if keys is None else list(keys)
    for key in keys:
        if key not in MANAGED_SETTINGS:
            raise UnregisteredKeyError(key)

    pairs = _parse_lines(path.read_text(encoding="utf-8")) if path.exists() else []
    for key in keys:
        if len(_active_line_indices(pairs, key)) > 1:
            raise DuplicateManagedKeyError(key)

    repo_data = _read_repo_data(path)
    result = {}
    for key in keys:
        setting = MANAGED_SETTINGS[key]
        present = key in repo_data
        if setting.secret:
            result[key] = ManagedValue(key=key, present=present, display_value=None)
        else:
            result[key] = ManagedValue(key=key, present=present, display_value=repo_data.get(key, setting.default))
    return result


@dataclass(frozen=True)
class ComparisonResult:
    key: str
    matches: bool
    disk_display: Optional[str]     # None for secret keys
    running_display: Optional[str]  # None for secret keys


def compare_to_running(keys=None, env_path=None):
    """Per-key Saved(disk)-vs-Running(current process settings)
    comparison, type-normalized via each setting's `cast` so pure
    representation differences (587 vs "587", True vs "True") never
    read as a false mismatch. For secret keys, `matches` is computed
    from the real values internally but disk_display/running_display
    are always None -- callers must never receive either value for a
    secret key through this function."""
    path = Path(env_path) if env_path is not None else ENV_FILE_PATH
    keys = list(MANAGED_SETTINGS) if keys is None else list(keys)
    repo_data = _read_repo_data(path)
    results = {}
    for key in keys:
        setting = MANAGED_SETTINGS[key]
        disk_raw = repo_data.get(key, setting.default)
        running_raw = str(getattr(django_settings, key, setting.default))
        try:
            matches = setting.cast(disk_raw) == setting.cast(running_raw)
        except (ValueError, TypeError):
            matches = disk_raw == running_raw
        if setting.secret:
            results[key] = ComparisonResult(key=key, matches=matches, disk_display=None, running_display=None)
        else:
            results[key] = ComparisonResult(
                key=key, matches=matches, disk_display=disk_raw, running_display=running_raw,
            )
    return results


def secret_matches_running(key, env_path=None):
    """True/False only -- for a secret key's own restart-required
    comparison, never exposing either the saved or running value. (For
    a non-secret key this is equivalent to compare_to_running(...)
    [key].matches, provided as a convenience for a uniform call site.)"""
    if key not in MANAGED_SETTINGS:
        raise UnregisteredKeyError(key)
    return compare_to_running(keys=[key], env_path=env_path)[key].matches


def secret_configured(key, env_path=None):
    """True/False only -- whether the saved (disk) value for a secret
    key is non-empty. Deliberately TRUTHY-based, not presence-based: an
    explicit `KEY=` (present, but blank) line reads as "not configured",
    matching this project's existing settings.EMAIL_HOST_PASSWORD-style
    "Yes"/"No configured" convention (see monitoring/admin.py's
    pre-existing smtp_status() display, unchanged by this module)."""
    if key not in MANAGED_SETTINGS:
        raise UnregisteredKeyError(key)
    setting = MANAGED_SETTINGS[key]
    path = Path(env_path) if env_path is not None else ENV_FILE_PATH
    return bool(_read_repo_data(path).get(key, setting.default))


# ---------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402  (grouped near use, matches project's local-import style)


@contextmanager
def _locked(lock_path, timeout=5.0):
    """Exclusive inter-process lock on a dedicated lock file whose
    inode never changes (this is deliberate -- an atomic os.replace()
    on .env itself changes ITS inode, so locking .env's own inode
    would not exclude a concurrent writer; see EnvWriteError's
    docstring and requirement discussion in the module docstring)."""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise EnvWriteError(f"could not open the .env update lock ({lock_path}): {exc}") from exc
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise EnvWriteError(f"timed out waiting for the .env update lock ({lock_path})")
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------
def _atomic_write_bytes(path, data, mode):
    """temp file in the SAME directory -> write -> flush -> fsync ->
    chmod -> atomic os.replace() -> best-effort directory fsync. Any
    failure before os.replace() leaves `path` completely untouched
    (the temp file is cleaned up and the exception propagates);
    os.replace() itself is a single atomic rename syscall that either
    fully succeeds or fully fails, so a failure there also cannot leave
    `path` partially written."""
    directory = path.parent
    tmp_path = directory / f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(str(tmp_path), str(path))
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _fsync_dir(directory):
    try:
        fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # best-effort; not every filesystem supports fsync on a directory fd


# ---------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------
@dataclass(frozen=True)
class EnvUpdateResult:
    changed_keys: list


def update_managed_values(values, env_path=None, lock_path=None, backup_path=None):
    """values: {key: new_raw_string}. Only keys explicitly present in
    this dict are touched at all -- omitting a key leaves it completely
    unchanged on disk. This is how "blank password field means keep the
    current password" is implemented by the caller: it simply never
    includes EMAIL_HOST_PASSWORD in `values` unless the admin typed a
    new one or explicitly asked to clear it (values={"EMAIL_HOST_PASSWORD": ""}).

    Returns EnvUpdateResult(changed_keys=[...]); changed_keys is only
    the keys whose on-disk value actually differs from what's being
    written -- resubmitting identical values performs no write and
    reports no changes.

    Raises BEFORE any filesystem mutation: UnregisteredKeyError,
    InvalidValueError, UnsafeValueError, DuplicateManagedKeyError.
    Raises DURING the write (previous .env guaranteed intact):
    EnvWriteError (includes lock timeout, symlink rejection, and any
    OS-level I/O failure)."""
    path = Path(env_path) if env_path is not None else ENV_FILE_PATH
    lock = Path(lock_path) if lock_path is not None else path.with_name(path.name + ".lock")
    backup = Path(backup_path) if backup_path is not None else path.with_name(path.name + ".bak")

    for key in values:
        if key not in MANAGED_SETTINGS:
            raise UnregisteredKeyError(key)
    for key, value in values.items():
        MANAGED_SETTINGS[key].validate(key, value)
    encoded = {key: encode_env_value(value) for key, value in values.items()}

    with _locked(lock):
        try:
            if path.exists():
                if path.is_symlink():
                    raise EnvWriteError(f"{path} is a symlink; refusing to write through it for safety")
                if not path.is_file():
                    raise EnvWriteError(f"{path} exists but is not a regular file")
                original_text = path.read_text(encoding="utf-8")
                original_mode = stat.S_IMODE(path.stat().st_mode)
            else:
                original_text = ""
                original_mode = 0o600
        except OSError as exc:
            raise EnvWriteError(f"could not read {path}: {exc}") from exc

        pairs = _parse_lines(original_text)
        for key in values:
            if len(_active_line_indices(pairs, key)) > 1:
                raise DuplicateManagedKeyError(key)

        changed_keys = []
        for key, new_encoded in encoded.items():
            new_assignment = f"{key}={new_encoded}"
            indices = _active_line_indices(pairs, key)
            if indices:
                idx = indices[0]
                if pairs[idx][0].strip() != new_assignment:
                    pairs[idx] = (new_assignment, key)
                    changed_keys.append(key)
            else:
                pairs.append((new_assignment, key))
                changed_keys.append(key)

        if not changed_keys:
            return EnvUpdateResult(changed_keys=[])

        new_text = _render_lines(pairs)

        try:
            if path.exists():
                backup_mode = stat.S_IMODE(backup.stat().st_mode) if backup.exists() else 0o600
                _atomic_write_bytes(backup, original_text.encode("utf-8"), backup_mode)
            _atomic_write_bytes(path, new_text.encode("utf-8"), original_mode)
        except OSError as exc:
            raise EnvWriteError(f"failed to write {path}: {exc}") from exc

    return EnvUpdateResult(changed_keys=changed_keys)
