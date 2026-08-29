"""Runtime Foundation E5: stable OS filesystem/CLI surfaces.

E5 owns exactly four system surfaces that the completed E1-E4 runtime
architecture depends on existing, but that nothing before E5 actually
installs or repairs:

  - the stable installed TTS launcher, /usr/local/bin/isadoraair-tts;
  - the persistent provider-runtime root, /opt/isadoraair-runtime
    (Foundation E3 publishes Kokoro/Piper generations beneath it);
  - the persistent TTS asset/data root, /var/lib/isadoraair/tts
    (Foundation E3 publishes Kokoro/Piper asset generations beneath it);
  - the systemd-tmpfiles configuration that establishes/repairs the two
    directories above (a third, pre-existing tmpfiles file already owns
    /run/isadoraair and its /tts scratch subdirectory -- see
    deploy/isadoraair-tmpfiles.conf -- and is not touched here).

E5 does not provision Kokoro, Piper, or fdkaac (Foundation E3/E4 own
that), does not migrate any historical caller onto the new launcher, and
does not activate anything in production. It only makes the durable
surfaces those future activations will need exist and be correctly
owned, idempotently and safely.

Every canonical path consumed here comes from
isadoraair.runtime_components.json's canonical_paths -- the same single
path authority Foundation E1-E4 already use -- via the shared,
target-root-aware isadoraair.runtime_provisioning.ProvisioningLayout.
Mutation shares the same Foundation E provisioning lock E3/E4 already
use, so an E5 apply cannot race a concurrent E3 TTS publish or E4 native
publish.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from isadoraair.runtime_components import MANIFEST_PATH, load_runtime_components
from isadoraair.runtime_native import _run_bounded
from isadoraair.runtime_provisioning import (
    ProvisioningLayout,
    RuntimeProvisioningError,
    _assert_confined_non_symlink,
    _fsync_directory,
    _mkdir_controlled,
    _write_atomic,
    runtime_provision_lock,
)


SURFACE_SCHEMA_VERSION = 1
TMPFILES_TIMEOUT_SECONDS = 30.0

STATE_ABSENT = "absent"
STATE_WRONG_TYPE = "wrong_type"
STATE_SYMLINK = "symlink"
STATE_WRONG_OWNER = "wrong_owner"
STATE_UNSAFE_PERMISSIONS = "unsafe_permissions"
STATE_WRONG_CONTENT = "wrong_content"
STATE_HEALTHY = "healthy"

LAUNCHER_MODE = 0o755
TMPFILES_CONFIG_MODE = 0o644
DIRECTORY_MODE = 0o755

APPLICATION_ROOT_MARKER = "@@ISADORAAIR_APPLICATION_ROOT@@"
SURFACE_UID_MARKER = "@@ISADORAAIR_SURFACE_UID@@"
SURFACE_GID_MARKER = "@@ISADORAAIR_SURFACE_GID@@"

TMPFILES_DESTINATION_RELATIVE = Path("etc/tmpfiles.d/isadoraair-runtime.conf")


def _safe_message(value: object) -> str:
    return " ".join(str(value).split())[:512] or "system surface operation failed"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _assert_no_symlink_ancestors(path: Path) -> None:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeProvisioningError("system surface path contains an unexpected symlink")
        if not cursor.exists():
            break


def _run_tmpfiles(config_file: Path, *, target_root: Path) -> None:
    executable = next(
        (
            path
            for path in (Path("/usr/bin/systemd-tmpfiles"), Path("/bin/systemd-tmpfiles"))
            if path.is_file()
        ),
        None,
    )
    if executable is None:
        raise RuntimeProvisioningError("host systemd-tmpfiles is unavailable")
    command = [str(executable), "--create"]
    if target_root != Path("/"):
        command.append(f"--root={target_root}")
    command.append(str(config_file))
    _run_bounded(
        command,
        cwd=config_file.parent,
        timeout=TMPFILES_TIMEOUT_SECONDS,
        label="systemd-tmpfiles",
    )


@dataclass(frozen=True, slots=True)
class SurfaceEvidence:
    name: str
    kind: str
    path: str
    state: str
    expected: dict[str, Any] = field(default_factory=dict)
    observed: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "diagnostics": list(self.diagnostics),
            "expected": self.expected,
            "kind": self.kind,
            "name": self.name,
            "observed": self.observed,
            "path": self.path,
            "state": self.state,
        }


@dataclass(frozen=True, slots=True)
class SystemSurfaceEvidence:
    surfaces: dict[str, SurfaceEvidence]
    schema_version: int = SURFACE_SCHEMA_VERSION

    @property
    def healthy(self) -> bool:
        return all(item.state == STATE_HEALTHY for item in self.surfaces.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "schema_version": self.schema_version,
            "surfaces": {name: self.surfaces[name].to_dict() for name in sorted(self.surfaces)},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SystemSurfacePlan:
    action: str
    target_root: str
    privilege_required: bool
    current_evidence: SystemSurfaceEvidence
    surfaces_needing_repair: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    schema_version: int = SURFACE_SCHEMA_VERSION

    @property
    def ready(self) -> bool:
        return not self.errors

    @property
    def needs_work(self) -> bool:
        return self.action == "install"

    @property
    def plan_id(self) -> str:
        encoded = json.dumps(
            self.to_dict(include_plan_id=False), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return _sha256_bytes(encoded)

    def to_dict(self, *, include_plan_id: bool = True) -> dict[str, Any]:
        result = {
            "action": self.action,
            "current_evidence": self.current_evidence.to_dict(),
            "errors": list(self.errors),
            "needs_work": self.needs_work,
            "privilege_required": self.privilege_required,
            "ready": self.ready,
            "schema_version": self.schema_version,
            "surfaces_needing_repair": list(self.surfaces_needing_repair),
            "target_root": self.target_root,
        }
        if include_plan_id:
            result["plan_id"] = self.plan_id
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class SystemSurfaceResult:
    plan_id: str
    changed_surfaces: tuple[str, ...]
    evidence: SystemSurfaceEvidence
    no_op: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_surfaces": list(self.changed_surfaces),
            "evidence": self.evidence.to_dict(),
            "no_op": self.no_op,
            "plan_id": self.plan_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


RunTmpfiles = Callable[[Path, Path], None]
Checkpoint = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class SystemSurfaceSeams:
    run_tmpfiles: RunTmpfiles = lambda config_file, target_root: _run_tmpfiles(
        config_file, target_root=target_root
    )
    checkpoint: Checkpoint = lambda _name: None


@dataclass(slots=True)
class _FileSnapshot:
    path: Path
    existed: bool
    backup: Path | None
    mode: int | None


class RuntimeSystemSurfaceManager:
    """Reusable API for the E5 system-surface contract: plan/apply/validate."""

    def __init__(
        self,
        *,
        target_root: str | Path = "/",
        product_manifest: dict[str, Any] | None = None,
        project_root: str | Path | None = None,
        seams: SystemSurfaceSeams = SystemSurfaceSeams(),
        embed_mapped_application_root: bool = False,
    ) -> None:
        self.manifest = product_manifest or load_runtime_components()
        self.layout = ProvisioningLayout.from_manifest(self.manifest, target_root=target_root)
        self.project_root = Path(project_root or MANIFEST_PATH.parent.parent).absolute()
        self.seams = seams
        # target_root governs WHERE surfaces are written (file placement),
        # never what a persistent file's own content should reference once
        # the target filesystem is actually running as /. The installed
        # launcher is the one surface whose CONTENT is itself a path an
        # offline/restore target must use correctly after boot -- so by
        # default its embedded application root is always the canonical,
        # unmapped manifest value, regardless of target_root. This is an
        # explicit, narrowly-named API/test-only opt-in, never inferred
        # merely from target_root != "/", and never exposed as an ordinary
        # operator CLI flag -- see docs/RUNTIME_SYSTEM_SURFACES.md.
        self.embed_mapped_application_root = embed_mapped_application_root

    # ---- rendered content -------------------------------------------------

    def _launcher_template_path(self) -> Path:
        return self.project_root / "deploy" / "isadoraair-tts-canonical"

    def _tmpfiles_source_path(self) -> Path:
        return self.project_root / "deploy" / "isadoraair-runtime-tmpfiles.conf"

    def _tmpfiles_destination(self) -> Path:
        return ProvisioningLayout._map(
            "/" + TMPFILES_DESTINATION_RELATIVE.as_posix(), self.layout.target_root
        )

    def _expected_owner(self) -> int:
        return 0 if self.layout.target_root == Path("/") else os.geteuid()

    def _expected_uid_gid(self) -> tuple[int, int]:
        if self.layout.target_root == Path("/"):
            return 0, 0
        return os.geteuid(), os.getegid()

    def _read_source_template(self, path: Path, *, label: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeProvisioningError(f"{label} source template is unavailable: {path}") from exc

    def _embedded_application_root(self) -> Path:
        """The application root the INSTALLED LAUNCHER'S OWN CONTENT should
        reference -- deliberately independent of target_root's file-
        placement mapping by default.

        Product/default behavior (embed_mapped_application_root=False):
        always the canonical, unmapped manifest value
        (canonical_paths.application_root), so an offline/restore target
        written beneath --target-root still references /opt/isadoraair
        once that target filesystem actually becomes / after boot -- not
        the installer host's own mount point.

        Explicit opt-in (embed_mapped_application_root=True): the
        target-root-mapped value, for a disposable test seam that wants
        to genuinely execute the installed launcher against a fake
        application root while it's still mounted beneath the same
        scratch directory. Never inferred merely from target_root != "/".
        """
        if self.embed_mapped_application_root:
            return self.layout.application_root
        return Path(self.manifest["canonical_paths"]["application_root"])

    def _rendered_launcher(self) -> bytes:
        template = self._read_source_template(self._launcher_template_path(), label="launcher")
        if APPLICATION_ROOT_MARKER not in template:
            raise RuntimeProvisioningError("launcher template is missing its application-root marker")
        rendered = template.replace(
            APPLICATION_ROOT_MARKER, str(self._embedded_application_root())
        )
        return rendered.encode("utf-8")

    def _rendered_tmpfiles_config(self) -> bytes:
        template = self._read_source_template(self._tmpfiles_source_path(), label="tmpfiles config")
        if SURFACE_UID_MARKER not in template or SURFACE_GID_MARKER not in template:
            raise RuntimeProvisioningError("tmpfiles template is missing its ownership markers")
        uid, gid = self._expected_uid_gid()
        rendered = template.replace(SURFACE_UID_MARKER, str(uid)).replace(
            SURFACE_GID_MARKER, str(gid)
        )
        return rendered.encode("utf-8")

    # ---- validation ---------------------------------------------------

    def _validate_directory(self, name: str, path: Path) -> SurfaceEvidence:
        expected_owner = self._expected_owner()
        expected = {"mode": oct(DIRECTORY_MODE), "owner": expected_owner}
        try:
            metadata = path.lstat()
        except OSError:
            return SurfaceEvidence(
                name=name, kind="directory", path=str(path), state=STATE_ABSENT, expected=expected
            )
        observed = {"mode": oct(stat.S_IMODE(metadata.st_mode)), "owner": metadata.st_uid}
        if stat.S_ISLNK(metadata.st_mode):
            state = STATE_SYMLINK
        elif not stat.S_ISDIR(metadata.st_mode):
            state = STATE_WRONG_TYPE
        elif metadata.st_uid != expected_owner:
            state = STATE_WRONG_OWNER
        elif stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE:
            state = STATE_UNSAFE_PERMISSIONS
        else:
            state = STATE_HEALTHY
        return SurfaceEvidence(
            name=name,
            kind="directory",
            path=str(path),
            state=state,
            expected=expected,
            observed=observed,
        )

    def _validate_file(
        self, name: str, path: Path, *, mode: int, expected_content: bytes
    ) -> SurfaceEvidence:
        expected_owner = self._expected_owner()
        expected = {
            "mode": oct(mode),
            "owner": expected_owner,
            "sha256": _sha256_bytes(expected_content),
        }
        try:
            metadata = path.lstat()
        except OSError:
            return SurfaceEvidence(
                name=name, kind="file", path=str(path), state=STATE_ABSENT, expected=expected
            )
        observed = {"mode": oct(stat.S_IMODE(metadata.st_mode)), "owner": metadata.st_uid}
        if stat.S_ISLNK(metadata.st_mode):
            state = STATE_SYMLINK
        elif not stat.S_ISREG(metadata.st_mode):
            state = STATE_WRONG_TYPE
        elif metadata.st_uid != expected_owner:
            state = STATE_WRONG_OWNER
        elif stat.S_IMODE(metadata.st_mode) != mode or metadata.st_mode & 0o022:
            state = STATE_UNSAFE_PERMISSIONS
        else:
            try:
                observed_bytes = path.read_bytes()
            except OSError as exc:
                return SurfaceEvidence(
                    name=name,
                    kind="file",
                    path=str(path),
                    state=STATE_WRONG_TYPE,
                    expected=expected,
                    observed=observed,
                    diagnostics=(_safe_message(exc),),
                )
            observed["sha256"] = _sha256_bytes(observed_bytes)
            state = (
                STATE_HEALTHY if observed_bytes == expected_content else STATE_WRONG_CONTENT
            )
        return SurfaceEvidence(
            name=name, kind="file", path=str(path), state=state, expected=expected, observed=observed
        )

    def current_evidence(self) -> SystemSurfaceEvidence:
        surfaces = {
            "launcher": self._validate_file(
                "launcher",
                self.layout.tts_cli,
                mode=LAUNCHER_MODE,
                expected_content=self._rendered_launcher(),
            ),
            "runtime_root": self._validate_directory("runtime_root", self.layout.runtime_root),
            "tts_asset_root": self._validate_directory("tts_asset_root", self.layout.tts_root),
            "tmpfiles_config": self._validate_file(
                "tmpfiles_config",
                self._tmpfiles_destination(),
                mode=TMPFILES_CONFIG_MODE,
                expected_content=self._rendered_tmpfiles_config(),
            ),
        }
        return SystemSurfaceEvidence(surfaces=surfaces)

    # ---- plan -----------------------------------------------------------

    def plan(self) -> SystemSurfacePlan:
        current = self.current_evidence()
        needing_repair = tuple(
            sorted(
                name
                for name, item in current.surfaces.items()
                if item.state != STATE_HEALTHY
            )
        )
        action = "install" if needing_repair else "no_op"
        return SystemSurfacePlan(
            action=action,
            target_root=str(self.layout.target_root),
            privilege_required=self.layout.target_root == Path("/"),
            current_evidence=current,
            surfaces_needing_repair=needing_repair,
        )

    # ---- apply ------------------------------------------------------------

    def _preflight_apply(self) -> None:
        root = self.layout.target_root
        if root.is_symlink() or not root.is_dir():
            raise RuntimeProvisioningError("target root must be an existing non-symlink directory")
        if root == Path("/") and os.geteuid() != 0:
            raise RuntimeProvisioningError("canonical system-surface publication requires root privileges")
        if root != Path("/") and root.stat().st_uid != os.geteuid():
            raise RuntimeProvisioningError("noncanonical target root must be owned by the caller")
        if not os.access(root, os.W_OK | os.X_OK):
            raise RuntimeProvisioningError("target root is not writable by the caller")
        for target in (
            self.layout.tts_cli.parent,
            self._tmpfiles_destination().parent,
            self.layout.runtime_root.parent,
            self.layout.tts_root.parent,
        ):
            _assert_no_symlink_ancestors(target)
            _assert_confined_non_symlink(root, target)

    def _snapshot_file(self, path: Path, snapshot_root: Path) -> _FileSnapshot:
        if not path.exists():
            return _FileSnapshot(path, False, None, None)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeProvisioningError(f"cannot snapshot unexpected object at {path}")
        backup = snapshot_root / f"{path.name}.{uuid.uuid4().hex}"
        _write_atomic(backup, path.read_bytes(), mode=stat.S_IMODE(metadata.st_mode))
        return _FileSnapshot(path, True, backup, stat.S_IMODE(metadata.st_mode))

    def _restore_file(self, snapshot: _FileSnapshot) -> None:
        if snapshot.existed:
            assert snapshot.backup is not None and snapshot.mode is not None
            _write_atomic(snapshot.path, snapshot.backup.read_bytes(), mode=snapshot.mode)
        elif snapshot.path.exists() or snapshot.path.is_symlink():
            if snapshot.path.is_symlink() or snapshot.path.is_file():
                snapshot.path.unlink()
                _fsync_directory(snapshot.path.parent)
            else:
                raise RuntimeProvisioningError("rollback target became an unexpected directory")

    def _publish_file(self, path: Path, data: bytes, *, mode: int) -> None:
        _assert_no_symlink_ancestors(path.parent)
        if path.is_symlink():
            raise RuntimeProvisioningError(f"refusing to replace an unexpected symlink at {path}")
        if path.exists() and not path.is_file():
            raise RuntimeProvisioningError(f"refusing to replace an unexpected object at {path}")
        _mkdir_controlled(path.parent)
        _write_atomic(path, data, mode=mode)

    def apply(self) -> SystemSurfaceResult:
        plan = self.plan()
        if not plan.ready:
            raise RuntimeProvisioningError("system-surface plan contains blocking errors")
        self._preflight_apply()
        with runtime_provision_lock(self.layout):
            locked_plan = self.plan()
            if not locked_plan.ready:
                raise RuntimeProvisioningError("system-surface plan changed and is no longer ready")
            if not locked_plan.needs_work:
                return SystemSurfaceResult(
                    plan_id=locked_plan.plan_id,
                    changed_surfaces=(),
                    evidence=locked_plan.current_evidence,
                    no_op=True,
                )
            repair = set(locked_plan.surfaces_needing_repair)
            # Note: acquiring the shared lock above already creates
            # layout.runtime_root (to hold the lock file itself) as a
            # side effect if it didn't already exist -- that alone can
            # already satisfy this surface's own health check before any
            # of the repair steps below run. changed_surfaces is
            # therefore computed from a real before/after evidence
            # comparison, not from the repair set, so it stays accurate
            # regardless of side effects from shared infrastructure.
            before_states = {
                name: item.state for name, item in locked_plan.current_evidence.surfaces.items()
            }
            snapshot_root = self.layout.target_root / (
                ".isadoraair-e5-snapshot-" + uuid.uuid4().hex
            )
            os.mkdir(snapshot_root, 0o700)
            snapshots: list[_FileSnapshot] = []
            try:
                if "launcher" in repair:
                    snapshots.append(
                        self._snapshot_file(self.layout.tts_cli, snapshot_root)
                    )
                    self.seams.checkpoint("before_launcher_publish")
                    self._publish_file(
                        self.layout.tts_cli, self._rendered_launcher(), mode=LAUNCHER_MODE
                    )
                    self.seams.checkpoint("after_launcher_publish")
                if "tmpfiles_config" in repair:
                    snapshots.append(
                        self._snapshot_file(self._tmpfiles_destination(), snapshot_root)
                    )
                    self.seams.checkpoint("before_tmpfiles_config_publish")
                    self._publish_file(
                        self._tmpfiles_destination(),
                        self._rendered_tmpfiles_config(),
                        mode=TMPFILES_CONFIG_MODE,
                    )
                    self.seams.checkpoint("after_tmpfiles_config_publish")
                if repair & {"runtime_root", "tts_asset_root", "tmpfiles_config"}:
                    self.seams.checkpoint("before_tmpfiles_execution")
                    self.seams.run_tmpfiles(
                        self._tmpfiles_destination(), self.layout.target_root
                    )
                    self.seams.checkpoint("after_tmpfiles_execution")
                final_evidence = self.current_evidence()
                unhealthy = tuple(
                    name
                    for name, item in final_evidence.surfaces.items()
                    if item.state != STATE_HEALTHY
                )
                if unhealthy:
                    raise RuntimeProvisioningError(
                        "system surfaces failed final validation: " + ", ".join(sorted(unhealthy))
                    )
                self.seams.checkpoint("after_final_validation")
                changed = tuple(
                    sorted(
                        name
                        for name, item in final_evidence.surfaces.items()
                        if before_states.get(name) != STATE_HEALTHY
                        and item.state == STATE_HEALTHY
                    )
                )
                return SystemSurfaceResult(
                    plan_id=locked_plan.plan_id,
                    changed_surfaces=changed,
                    evidence=final_evidence,
                    no_op=False,
                )
            except Exception as exc:
                rollback_errors: list[str] = []
                for snapshot in reversed(snapshots):
                    try:
                        self._restore_file(snapshot)
                    except Exception as rollback_exc:
                        rollback_errors.append(_safe_message(rollback_exc))
                if rollback_errors:
                    raise RuntimeProvisioningError(
                        "system-surface publication failed and rollback failed: "
                        + "; ".join(rollback_errors)
                    ) from exc
                if isinstance(exc, RuntimeProvisioningError):
                    raise
                raise RuntimeProvisioningError(_safe_message(exc)) from exc
            finally:
                shutil.rmtree(snapshot_root, ignore_errors=True)


def validate_system_surfaces(
    *,
    target_root: str | Path = "/",
    product_manifest: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> SystemSurfaceEvidence:
    """Read-only convenience entry point -- never mutates anything."""

    manager = RuntimeSystemSurfaceManager(
        target_root=target_root, product_manifest=product_manifest, project_root=project_root
    )
    return manager.current_evidence()
