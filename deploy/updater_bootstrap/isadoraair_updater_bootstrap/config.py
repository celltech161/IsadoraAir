"""D2-A: strict supervisor bootstrap configuration -- conceptually
/etc/isadoraair/updater-bootstrap.json. Deliberately narrow: this is
NOT a second copy of /etc/isadoraair/station.json's station/application
feature policy (D2-A's own explicit instruction not to duplicate it) --
only the handful of paths/sockets the supervisor itself needs to find
its own slots, state, trust material, and the two Unix sockets it
owns/talks to.

No command, hook, or executable path is ever a config field -- the
supervisor's own entrypoint invocation (launch.py) uses fixed compiled
constants (`/usr/bin/python3`, `-I`, the descriptor's own fixed
entrypoint name) that this config can never override. That is a
deliberate, permanent restriction, not an oversight: a bootstrap
config field naming an executable would let a compromised config file
alone (no valid candidate signature required) redirect what the
supervisor runs."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import stat

from .security import ProtectionError, assert_root_protected, assert_root_protected_parents

SCHEMA_VERSION = 1
MAX_PATH_LEN = 255

KNOWN_FIELDS = frozenset({
    "schema_version", "bootstrap_protocol_version", "slots_root", "runtime_state_path",
    "activation_socket", "worker_socket", "signer_root", "trust_policy_path",
})
PATH_FIELDS = (
    "slots_root", "runtime_state_path", "activation_socket", "worker_socket",
    "signer_root", "trust_policy_path",
)


class ConfigError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class BootstrapConfig:
    schema_version: int
    bootstrap_protocol_version: int
    slots_root: Path
    runtime_state_path: Path
    activation_socket: Path
    worker_socket: Path
    signer_root: Path
    trust_policy_path: Path


def _require_absolute_path(value, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field}: must be a non-empty string")
    if len(value) > MAX_PATH_LEN:
        raise ConfigError(f"{field}: exceeds {MAX_PATH_LEN} characters")
    if "\x00" in value or any(ord(ch) < 0x20 for ch in value):
        raise ConfigError(f"{field}: contains a control character")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{field}: must be an absolute path")
    if ".." in path.parts:
        raise ConfigError(f"{field}: must not contain '..'")
    return path


def _is_unclassifiable_special_file(path: Path) -> bool:
    """True for a socket, FIFO, or device node -- something that exists
    on disk but satisfies neither S_ISDIR nor S_ISREG, so
    assert_root_protected can never classify it as safe or unsafe.
    Generic over the object type; never keys off a field name or
    filename. A symlink is deliberately NOT special here: lstat still
    reports it distinctly, and leaving it as a stopping point preserves
    assert_root_protected's own existing "contains a symlink" check."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return bool(
        stat.S_ISSOCK(info.st_mode)
        or stat.S_ISFIFO(info.st_mode)
        or stat.S_ISCHR(info.st_mode)
        or stat.S_ISBLK(info.st_mode)
    )


def _closest_existing_ancestor(path: Path) -> Path:
    """Walk upward to the closest ancestor suitable for root-protection
    ancestry validation.

    activation_socket and worker_socket name a live Unix domain socket
    while the supervisor is running -- a legitimate, expected object
    that a plain `.exists()` check treats as "found" but that
    assert_root_protected can never classify (it is neither a directory
    nor a regular file). Rather than stopping there and failing a
    perfectly healthy, correctly-protected socket, skip past it -- and
    past any other socket/FIFO/device found the same way -- and keep
    walking up, exactly as if that path did not exist yet. This
    preserves the check's real intent (the *containing directory
    structure* is root-protected) instead of requiring the configured
    leaf object itself to be a plain file. All other existing paths
    (directories, regular files, symlinks) still stop the walk exactly
    as before."""
    current = path
    while not current.exists() or _is_unclassifiable_special_file(current):
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _assert_no_overlap(paths: dict[str, Path], application_root: Path) -> None:
    application_root = Path(application_root)
    for field, path in paths.items():
        if path == application_root or application_root in path.parents or path in application_root.parents:
            raise ConfigError(f"{field}: overlaps the application checkout root {application_root}")


def validate_config_dict(data: dict, *, application_root: Path, enforce_root_ownership: bool = True) -> BootstrapConfig:
    """`application_root` is supplied by the CALLER (never read from
    this document) so a compromised config file can never lie about
    what counts as "the application checkout" to escape the overlap
    check. `enforce_root_ownership=False` is the same "inactive under
    an unprivileged test process" convention security.py's own
    assert_root_protected already uses -- kept explicit here as a
    parameter (rather than only relying on os.geteuid()) so a test can
    positively exercise the overlap/shape rules without needing to run
    as root at all, while still being able to separately exercise the
    ownership rule under a fixture that simulates it."""
    if not isinstance(data, dict):
        raise ConfigError("bootstrap config must be a JSON object")
    if set(data) != KNOWN_FIELDS:
        unknown = set(data) - KNOWN_FIELDS
        missing = KNOWN_FIELDS - set(data)
        if unknown:
            raise ConfigError(f"unrecognized field(s) {sorted(unknown)!r}")
        raise ConfigError(f"missing required field(s) {sorted(missing)!r}")

    schema_version = data["schema_version"]
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SCHEMA_VERSION:
        raise ConfigError("unsupported schema_version")

    bootstrap_protocol_version = data["bootstrap_protocol_version"]
    if not isinstance(bootstrap_protocol_version, int) or isinstance(bootstrap_protocol_version, bool) or bootstrap_protocol_version < 1:
        raise ConfigError("bootstrap_protocol_version must be a positive integer")

    paths = {field: _require_absolute_path(data[field], field) for field in PATH_FIELDS}

    # No two configured paths may be identical or one nested in another
    # -- e.g. worker_socket must never equal activation_socket, and
    # slots_root must never contain trust_policy_path.
    seen: dict[Path, str] = {}
    for field, path in paths.items():
        if path in seen:
            raise ConfigError(f"{field} and {seen[path]} must not be the identical path")
        seen[path] = field
    for field_a, path_a in paths.items():
        for field_b, path_b in paths.items():
            if field_a == field_b:
                continue
            if path_a in path_b.parents:
                raise ConfigError(f"{field_b} must not live under {field_a}")

    _assert_no_overlap(paths, application_root)

    if enforce_root_ownership:
        for field, path in paths.items():
            ancestor = _closest_existing_ancestor(path)
            try:
                assert_root_protected_parents(ancestor)
                assert_root_protected(ancestor, recursive=False)
            except ProtectionError as exc:
                raise ConfigError(f"{field}: {ancestor} is not safely root-protected: {exc}") from exc

    return BootstrapConfig(
        schema_version=schema_version, bootstrap_protocol_version=bootstrap_protocol_version,
        slots_root=paths["slots_root"], runtime_state_path=paths["runtime_state_path"],
        activation_socket=paths["activation_socket"], worker_socket=paths["worker_socket"],
        signer_root=paths["signer_root"], trust_policy_path=paths["trust_policy_path"],
    )
