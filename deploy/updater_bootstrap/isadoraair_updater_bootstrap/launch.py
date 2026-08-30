"""D2-I: the stable supervisor's worker process-launch abstraction.
Supervisor launches only a fixed relative entrypoint INSIDE the
selected, already-verified slot -- never a candidate-supplied absolute
path, never anything from config (config.py's own module docstring
already explains why no config field can ever name an executable).

The descriptor identifies the expected fixed entrypoint NAME (already
validated at descriptor-parse time, D1/D2-C: must be a safe relative
path, present in the file inventory, mode 0755) -- this module is what
refuses to let that turn into executing anything outside the slot: the
resolved absolute path must land inside the slot directory, must be a
plain regular file, and must not be a symlink, checked again here
independently of the descriptor verification that already ran, because
this is the one place a resolved filesystem path actually reaches a
subprocess exec call."""
from __future__ import annotations

from pathlib import Path

from .process import TrackedChild, launch_tracked

PYTHON_BINARY = "/usr/bin/python3"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30


class LaunchError(RuntimeError):
    pass


def resolve_entrypoint(slot_path: Path, entrypoint: str) -> Path:
    """The symlink check MUST run against the unresolved candidate path
    -- Path.resolve() itself follows symlinks, so checking is_symlink()
    on an already-.resolve()'d path can never see one (it would already
    be looking at the symlink's TARGET). Checked here before resolve()
    is ever called, not after."""
    slot_path = Path(slot_path).resolve()
    unresolved_candidate = slot_path / entrypoint
    if unresolved_candidate.is_symlink():
        raise LaunchError(f"entrypoint {entrypoint!r} is a symlink")
    candidate = unresolved_candidate.resolve()
    try:
        candidate.relative_to(slot_path)
    except ValueError as exc:
        raise LaunchError(f"entrypoint {entrypoint!r} resolves outside its own slot") from exc
    if not candidate.is_file():
        raise LaunchError(f"entrypoint {entrypoint!r} is not a regular file at {candidate}")
    return candidate


def launch_worker(slot_path: Path, entrypoint: str, *, config_path: Path,
                  extra_env: dict[str, str] | None = None) -> TrackedChild:
    """Runs exactly: /usr/bin/python3 -I <resolved-entrypoint> --config
    <config_path>. `-I` (isolated mode) ignores PYTHONPATH/PYTHONHOME
    and user site-packages -- the candidate worker gets only what its
    own slot directory and the standard library provide, never
    anything this supervisor process's own environment happens to have
    on disk. No shell; argv is a fixed-shape literal list, never string-
    joined/interpolated."""
    entry_path = resolve_entrypoint(slot_path, entrypoint)
    argv = [PYTHON_BINARY, "-I", str(entry_path), "--config", str(config_path)]
    return launch_tracked(argv, cwd=Path(slot_path), env=extra_env)
