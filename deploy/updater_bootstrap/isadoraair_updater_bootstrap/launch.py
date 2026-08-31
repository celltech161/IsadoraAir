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

import dataclasses
from pathlib import Path
import re

from .process import TrackedChild, launch_tracked

PYTHON_BINARY = "/usr/bin/python3"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 30
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclasses.dataclass(frozen=True)
class ActiveIdentity:
    """Identity of the supervisor-selected active A/B generation."""

    slot: str
    generation: int
    descriptor_sha256: str

    def __post_init__(self):
        if self.slot not in ("A", "B"):
            raise ValueError("ActiveIdentity.slot must be exactly 'A' or 'B'")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("ActiveIdentity.generation must be a positive integer")
        if not SHA256_RE.fullmatch(self.descriptor_sha256):
            raise ValueError("ActiveIdentity.descriptor_sha256 must be exactly 64 lowercase hex characters")


@dataclasses.dataclass(frozen=True)
class CandidateIdentity(ActiveIdentity):
    """Update Center Phase D, D4-B: the ONLY extra information
    launch_worker() may ever add to a candidate's argv -- exactly the
    four fixed, closed-shape fields updaterd.py's own argparse expects
    (--expected-slot/--expected-generation/--expected-descriptor-
    sha256/--expected-job-uuid), each independently validated here
    too, never a free-form string, path, or command."""
    job_uuid: str

    def __post_init__(self):
        super().__post_init__()
        if not UUID_RE.fullmatch(self.job_uuid):
            raise ValueError("CandidateIdentity.job_uuid must be a canonical lowercase UUID")


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
                  extra_env: dict[str, str] | None = None,
                  active_identity: ActiveIdentity | None = None,
                  candidate_identity: CandidateIdentity | None = None) -> TrackedChild:
    """Runs exactly: /usr/bin/python3 -I -B <resolved-entrypoint> --config
    <config_path> [--expected-slot ... --expected-generation ...
    --expected-descriptor-sha256 ... [--expected-job-uuid ...]]. The
    three-field form identifies a selected active A/B generation; the
    four-field form identifies a candidate resuming one durable job. `-I`
    (isolated mode) ignores PYTHONPATH/PYTHONHOME and user site-
    packages -- the candidate worker gets only what its own slot
    directory and the standard library provide, never anything this
    supervisor process's own environment happens to have on disk. `-B`
    prevents root bytecode writes from mutating the already-verified signed
    slot; an environment-only PYTHONDONTWRITEBYTECODE setting is ineffective
    because `-I` implies `-E`. No
    shell; argv is a fixed-shape literal list, never string-joined/
    interpolated -- candidate_identity, when given, is itself a
    validated CandidateIdentity (see its own __post_init__), never a
    raw caller-supplied string appended directly."""
    entry_path = resolve_entrypoint(slot_path, entrypoint)
    argv = [PYTHON_BINARY, "-I", "-B", str(entry_path), "--config", str(config_path)]
    if active_identity is not None and candidate_identity is not None:
        raise LaunchError("active_identity and candidate_identity are mutually exclusive")
    identity = candidate_identity if candidate_identity is not None else active_identity
    if identity is not None:
        argv.extend([
            "--expected-slot", identity.slot,
            "--expected-generation", str(identity.generation),
            "--expected-descriptor-sha256", identity.descriptor_sha256,
        ])
    if candidate_identity is not None:
        argv.extend(["--expected-job-uuid", candidate_identity.job_uuid])
    return launch_tracked(argv, cwd=Path(slot_path), env=extra_env)
