"""Runtime Foundation E6 -- read-only evidence for the pre-existing,
service-account-owned TTS scratch surface (``/run/isadoraair/tts``).

This directory is deliberately NOT owned by Runtime Foundation E5
(``deploy/isadoraair-runtime-tmpfiles.conf`` excludes it on purpose --
see docs/RUNTIME_SYSTEM_SURFACES.md's "Tmpfiles authority" section). It
stays owned by the pre-existing ``deploy/isadoraair-tmpfiles.conf`` /
``@@ISA_USER@@`` convention restore/install tooling already establishes
(``deploy/restore/90-system-config.sh``, ``deploy/restore/95-validate.sh``).
This module gives Runtime Foundation E6's aggregate baseline read-only
evidence about that SAME surface without creating a second, competing
establishing authority for it.

Service identity resolution is deliberately never guessed here: the
caller must supply the expected identity explicitly (the same
``ISA_USER`` value ``deploy/restore/90-system-config.sh`` itself
resolves -- ``id -un`` by default, or an explicit ``--isa-user``). A
noncanonical target resolves that name only from its own ``/etc/passwd``;
trusted callers may instead supply an explicit numeric UID/GID pair.
Absent that, evidence is reported as UNRESOLVED_IDENTITY -- never
silently treated as healthy merely because the directory happens to
exist. Every confined path ancestor is inspected with ``lstat`` before
the final directory, so a symlinked parent cannot be followed and
mistaken for a healthy scratch surface.
"""

from __future__ import annotations

import pwd
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TTS_SCRATCH_PATH = Path("/run/isadoraair/tts")
SCRATCH_DIRECTORY_MODE = 0o700

STATE_ABSENT = "absent"
STATE_WRONG_TYPE = "wrong_type"
STATE_SYMLINK = "symlink"
STATE_WRONG_OWNER = "wrong_owner"
STATE_UNSAFE_PERMISSIONS = "unsafe_permissions"
STATE_UNSAFE_ANCESTRY = "unsafe_ancestry"
STATE_UNRESOLVED_IDENTITY = "unresolved_identity"
STATE_HEALTHY = "healthy"


def _identity_from_target_passwd(
    isa_user: str, target_root: Path
) -> tuple[int, int] | None:
    etc_path = target_root / "etc"
    passwd_path = target_root / "etc" / "passwd"
    try:
        root_metadata = target_root.lstat()
        etc_metadata = etc_path.lstat()
        passwd_metadata = passwd_path.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or stat.S_ISLNK(etc_metadata.st_mode)
            or not stat.S_ISDIR(etc_metadata.st_mode)
            or stat.S_ISLNK(passwd_metadata.st_mode)
            or not stat.S_ISREG(passwd_metadata.st_mode)
        ):
            return None
        text = passwd_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) < 7 or fields[0] != isa_user:
            continue
        try:
            return int(fields[2], 10), int(fields[3], 10)
        except ValueError:
            return None
    return None


def resolve_expected_identity(
    isa_user: str | None,
    *,
    target_root: Path = Path("/"),
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> tuple[int, int] | None:
    """Resolve an explicit ``@@ISA_USER@@``-style username to
    ``(uid, gid)`` -- the same resolution
    ``deploy/restore/90-system-config.sh``'s own
    ``ISA_USER="$(id -un)"`` / ``--isa-user`` convention performs, just
    from Python. Returns None (never raises) for an absent or
    unresolvable identity -- a caller that cannot supply one gets an
    explicit "unresolved" evidence state, never a guess."""

    if expected_uid is not None or expected_gid is not None:
        if expected_uid is None or expected_gid is None:
            return None
        if expected_uid < 0 or expected_gid < 0:
            return None
        return expected_uid, expected_gid
    if not isa_user:
        return None
    if target_root != Path("/"):
        return _identity_from_target_passwd(isa_user, target_root)
    try:
        entry = pwd.getpwnam(isa_user)
    except (KeyError, OverflowError):
        return None
    return entry.pw_uid, entry.pw_gid


@dataclass(frozen=True, slots=True)
class ScratchSurfaceEvidence:
    path: str
    state: str
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.state == STATE_HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostics": list(self.diagnostics),
            "expected": self.expected,
            "observed": self.observed,
            "path": self.path,
            "state": self.state,
        }


def _mapped_path(path: Path, target_root: Path) -> Path:
    if target_root == Path("/"):
        return path
    if not path.is_absolute():
        raise ValueError("scratch path must be absolute")
    return target_root / path.relative_to("/")


def _unsafe_ancestor(path: Path, confinement_root: Path) -> Path | None:
    current = path.parent
    ancestors: list[Path] = []
    while True:
        ancestors.append(current)
        if current == confinement_root or current == current.parent:
            break
        current = current.parent
    if confinement_root not in ancestors:
        return confinement_root
    for ancestor in reversed(ancestors):
        try:
            metadata = ancestor.lstat()
        except OSError:
            # A missing ancestor necessarily makes the final path absent;
            # let the normal final-path evidence report that state.
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return ancestor
    return None


def evaluate_scratch_surface(
    *,
    isa_user: str | None,
    path: Path = TTS_SCRATCH_PATH,
    target_root: Path = Path("/"),
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> ScratchSurfaceEvidence:
    """Read-only evidence for one scratch surface. Never installs,
    repairs, or otherwise mutates anything."""

    target_root = Path(target_root)
    observed_path = _mapped_path(Path(path), target_root)
    identity = resolve_expected_identity(
        isa_user,
        target_root=target_root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    if identity is None:
        return ScratchSurfaceEvidence(
            path=str(observed_path),
            state=STATE_UNRESOLVED_IDENTITY,
            expected={
                "mode": oct(SCRATCH_DIRECTORY_MODE),
                "isa_user": isa_user,
                "target_root": str(target_root),
            },
            diagnostics=(
                "expected service identity could not be resolved -- supply the configured "
                "IsadoraAir service identity with --isa-user; for an offline target it "
                "must exist in the target's /etc/passwd (or explicit expected UID/GID "
                "must be supplied by trusted restore tooling)",
            ),
        )
    expected_uid, expected_gid = identity
    expected = {"mode": oct(SCRATCH_DIRECTORY_MODE), "owner": expected_uid, "group": expected_gid}
    try:
        unsafe_ancestor = _unsafe_ancestor(observed_path, target_root)
        if unsafe_ancestor is not None:
            return ScratchSurfaceEvidence(
                path=str(observed_path),
                state=STATE_UNSAFE_ANCESTRY,
                expected=expected,
                diagnostics=(
                    f"scratch path has a symlink or non-directory ancestor: {unsafe_ancestor}",
                ),
            )
        metadata = observed_path.lstat()
    except OSError:
        return ScratchSurfaceEvidence(path=str(observed_path), state=STATE_ABSENT, expected=expected)
    observed = {
        "mode": oct(stat.S_IMODE(metadata.st_mode)),
        "owner": metadata.st_uid,
        "group": metadata.st_gid,
    }
    if stat.S_ISLNK(metadata.st_mode):
        state = STATE_SYMLINK
    elif not stat.S_ISDIR(metadata.st_mode):
        state = STATE_WRONG_TYPE
    elif metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        state = STATE_WRONG_OWNER
    elif stat.S_IMODE(metadata.st_mode) != SCRATCH_DIRECTORY_MODE:
        state = STATE_UNSAFE_PERMISSIONS
    else:
        state = STATE_HEALTHY
    return ScratchSurfaceEvidence(
        path=str(observed_path), state=state, expected=expected, observed=observed
    )
