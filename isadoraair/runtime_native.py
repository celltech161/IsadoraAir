"""Foundation E4 orchestration for deterministic fdkaac publication.

Foundation D remains the build and capability authority.  This module only
verifies its local source inputs, invokes it into an unprivileged prefix, and
publishes the two runtime-critical files through a protected transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from isadoraair.runtime_bundle import product_contract_digest
from isadoraair.runtime_components import MANIFEST_PATH, load_runtime_components
from isadoraair.runtime_provisioning import (
    ProvisioningLayout,
    RuntimeProvisioningError,
    _assert_confined_non_symlink,
    _fsync_directory,
    _minimal_environment,
    _mkdir_controlled,
    manifest_for_layout,
    runtime_provision_lock,
)
from isadoraair.runtime_requirements import RuntimeRequirements
from isadoraair.runtime_validation import RuntimeEvidence, RuntimeValidator, STATUS_PASS


PREPARED_SCHEMA_VERSION = 1
NATIVE_PLAN_SCHEMA_VERSION = 1
NATIVE_BUILD_TIMEOUT_SECONDS = 1800.0
NATIVE_VALIDATE_TIMEOUT_SECONDS = 120.0
LDCONFIG_TIMEOUT_SECONDS = 30.0
MAX_DIAGNOSTIC_BYTES = 32768
RECEIPT_NAME = "prepared-native.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_message(value: object, *, max_length: int = 512) -> str:
    """Collapse whitespace and bound the length, keeping the end of long text.

    Failure diagnostics (compiler/build transcripts routed through
    ``_run_bounded``) carry their most useful content at the end, not the
    start, so truncation keeps the tail rather than the head.
    """

    collapsed = " ".join(str(value).split())
    if not collapsed:
        return "native provisioning failed"
    if len(collapsed) <= max_length:
        return collapsed
    return collapsed[-max_length:]


def _regular_file(
    path: Path,
    *,
    executable: bool = False,
    expected_uid: int | None = None,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeProvisioningError(f"required regular file is unavailable: {path.name}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeProvisioningError(f"required path is not a regular file: {path.name}")
    if metadata.st_nlink != 1:
        raise RuntimeProvisioningError(f"hard-linked input is not permitted: {path.name}")
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise RuntimeProvisioningError(f"prepared path has unexpected owner: {path.name}")
    if executable and not metadata.st_mode & stat.S_IXUSR:
        raise RuntimeProvisioningError(f"required executable bit is missing: {path.name}")
    if metadata.st_mode & 0o022:
        raise RuntimeProvisioningError(f"group/world-writable input is not permitted: {path.name}")
    return metadata


def _assert_existing_directory(
    path: Path,
    *,
    owner: int | None = None,
    forbid_shared_write: bool = False,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeProvisioningError(f"directory is unavailable: {path}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise RuntimeProvisioningError(f"path must be a non-symlink directory: {path}")
    if owner is not None and metadata.st_uid != owner:
        raise RuntimeProvisioningError(f"directory has unexpected owner: {path}")
    if forbid_shared_write and metadata.st_mode & 0o022:
        raise RuntimeProvisioningError(f"prepared directory is group/world writable: {path}")
    return metadata


def _ensure_noncanonical_publication_directories(root: Path, *targets: Path) -> None:
    """Establish the minimal confined directory skeleton ONE noncanonical
    (never "/") native-publication target root needs, one path component
    at a time.

    Runtime Foundation E7C: a noncanonical --target-root (e.g. an
    isolated `--staging-root` restore tree) is intentionally an
    incomplete filesystem skeleton -- it must never borrow /usr/local
    from the installer host, and nothing upstream of native publication
    creates it. Canonical "/" gets none of this: _preflight_publish only
    calls this helper when root != Path("/"), so the trusted system
    hierarchy under real "/" is never auto-created -- it must already
    exist, exactly as before.

    Each existing path component is validated as a real, non-symlink
    directory (never chmodded/chowned just because it already existed);
    each missing component is created fresh, one mkdir at a time, at a
    fixed 0755 mode, and immediately re-validated as a real, non-symlink
    directory before moving on -- deliberately NOT `path.mkdir(parents=
    True, exist_ok=True)` (see _mkdir_controlled in runtime_provisioning.py),
    which creates every missing ancestor in one call without proving each
    one it silently traverses is a real directory rather than a symlink.
    Never follows a symlink, never replaces or deletes anything, and
    never creates anything outside `root` -- a target outside `root`, or
    any existing non-directory/symlink collision along the way, fails
    closed instead.

    Idempotent: calling this again on an already-established skeleton
    validates and does nothing further, so a retried publish (including
    one that partially created this skeleton before failing later) sees
    the same skeleton as fully valid and moves on -- see this module's
    NativeRuntimeProvisioner.publish() docstring / this repo's E4
    publication-transaction contract for why these directories are
    deliberately NOT rolled back on a later failure: they are inert,
    contain nothing sensitive, and are not part of the two-file
    (fdkaac binary + libfdk-aac library) transaction that IS rolled back.
    """

    if root == Path("/"):
        raise RuntimeProvisioningError(
            "internal error: noncanonical publication directories requested for the canonical root"
        )
    for target in targets:
        if not target.is_relative_to(root):
            raise RuntimeProvisioningError("publication target escapes the requested root")
        cursor = root
        for part in target.relative_to(root).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RuntimeProvisioningError("publication path contains an unexpected symlink")
            if cursor.exists():
                if not cursor.is_dir():
                    raise RuntimeProvisioningError(
                        f"publication path component is not a directory: {cursor}"
                    )
                continue
            try:
                os.mkdir(cursor, 0o755)
            except FileExistsError as exc:
                raise RuntimeProvisioningError(
                    f"publication path component appeared unexpectedly during creation: {cursor}"
                ) from exc
            if cursor.is_symlink() or not stat.S_ISDIR(cursor.lstat().st_mode):
                raise RuntimeProvisioningError(
                    f"newly created publication directory is not a plain directory: {cursor}"
                )
            os.chmod(cursor, 0o755)
            metadata = cursor.lstat()
            if metadata.st_uid != os.geteuid():
                raise RuntimeProvisioningError(
                    f"newly created publication directory has an unexpected owner: {cursor}"
                )


def _validated_preparer_uid(value: int | None, *, canonical: bool) -> int:
    if value is None:
        if canonical:
            raise RuntimeProvisioningError(
                "canonical native publication requires an explicit trusted preparer UID"
            )
        return os.geteuid()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeProvisioningError("trusted preparer UID must be a non-negative integer")
    return value


def _assert_no_symlink_ancestors(path: Path) -> None:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if cursor.is_symlink():
            raise RuntimeProvisioningError("native path contains an unexpected symlink")
        if not cursor.exists():
            break


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_bounded(command: list[str], *, cwd: Path, timeout: float, label: str) -> None:
    """Drain unbounded child output while retaining only a bounded diagnostic tail."""

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=_minimal_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            umask=0o022,
        )
    except OSError as exc:
        raise RuntimeProvisioningError(f"{label} could not start") from exc
    tail = bytearray()

    def drain() -> None:
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read(8192), b""):
            tail.extend(chunk)
            if len(tail) > MAX_DIAGNOSTIC_BYTES:
                del tail[:-MAX_DIAGNOSTIC_BYTES]

    reader = threading.Thread(target=drain, name="fdkaac-output-drain", daemon=True)
    reader.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_group(process)
        reader.join(timeout=2.0)
        raise RuntimeProvisioningError(f"{label} timed out") from exc
    reader.join(timeout=2.0)
    if return_code != 0:
        prefix = f"{label} exited with status {return_code}: "
        diagnostic = _safe_message(
            tail.decode("utf-8", errors="replace"), max_length=max(0, 512 - len(prefix))
        )
        raise RuntimeProvisioningError(prefix + diagnostic)


def _run_build(script: Path, source_dir: Path, prefix: Path) -> None:
    _run_bounded(
        [str(script), "--source-dir", str(source_dir), "--prefix", str(prefix)],
        cwd=script.parent.parent,
        timeout=NATIVE_BUILD_TIMEOUT_SECONDS,
        label="authoritative fdkaac build",
    )


def _run_validator(script: Path, prefix: Path) -> None:
    _run_bounded(
        [str(script), "--prefix", str(prefix)],
        cwd=script.parent.parent,
        timeout=NATIVE_VALIDATE_TIMEOUT_SECONDS,
        label="authoritative HE-AAC validator",
    )


def _run_ldconfig(target_root: Path) -> None:
    executable = next(
        (path for path in (Path("/sbin/ldconfig"), Path("/usr/sbin/ldconfig")) if path.is_file()),
        None,
    )
    if executable is None:
        raise RuntimeProvisioningError("host ldconfig is unavailable")
    command = [str(executable)]
    if target_root != Path("/"):
        command.extend(("-n", str(ProvisioningLayout._map("/usr/local/lib", target_root))))
    _run_bounded(
        command,
        cwd=target_root,
        timeout=LDCONFIG_TIMEOUT_SECONDS,
        label="ldconfig",
    )


def _validate_runtime(
    manifest: dict[str, Any], requirements: RuntimeRequirements
) -> RuntimeEvidence:
    return RuntimeValidator(manifest=manifest, manifest_path=MANIFEST_PATH).validate(requirements)


BuildNative = Callable[[Path, Path, Path], None]
ValidatePrefix = Callable[[Path, Path], None]
RunLdconfig = Callable[[Path], None]
ValidateRuntime = Callable[[dict[str, Any], RuntimeRequirements], RuntimeEvidence]
Checkpoint = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class NativeProvisioningSeams:
    build: BuildNative = _run_build
    validate_prefix: ValidatePrefix = _run_validator
    ldconfig: RunLdconfig = _run_ldconfig
    validate_runtime: ValidateRuntime = _validate_runtime
    checkpoint: Checkpoint = lambda _name: None


@dataclass(frozen=True, slots=True)
class NativeSourceArchiveEvidence:
    name: str
    filename: str
    bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "filename": self.filename,
            "name": self.name,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class NativeSourceEvidence:
    source_dir: str
    archives: tuple[NativeSourceArchiveEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "archives": [archive.to_dict() for archive in self.archives],
            "source_dir": self.source_dir,
        }


@dataclass(frozen=True, slots=True)
class NativeArtifactEvidence:
    name: str
    relative_path: str
    bytes: int
    sha256: str
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes": self.bytes,
            "mode": self.mode,
            "name": self.name,
            "path": self.relative_path,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PreparedNativeRuntime:
    root: Path
    prefix: Path
    product_contract_sha256: str
    fdkaac_version: str
    libfdk_aac_version: str
    artifacts: tuple[NativeArtifactEvidence, ...]
    soname_path: str
    soname_target: str
    preparer_uid: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts": [item.to_dict() for item in self.artifacts],
            "component": "fdkaac",
            "fdkaac_version": self.fdkaac_version,
            "libfdk_aac_version": self.libfdk_aac_version,
            "product_contract_sha256": self.product_contract_sha256,
            "preparer_uid": self.preparer_uid,
            "schema_version": PREPARED_SCHEMA_VERSION,
            "soname": {"path": self.soname_path, "target": self.soname_target},
        }


@dataclass(frozen=True, slots=True)
class NativeProvisioningPlan:
    action: str
    required: bool
    reasons: tuple[str, ...]
    current_status: str
    source_status: str
    source_dir: str | None
    prepared_root: str | None
    privilege_required: bool
    current_evidence: RuntimeEvidence
    errors: tuple[str, ...] = ()
    schema_version: int = NATIVE_PLAN_SCHEMA_VERSION

    @property
    def ready(self) -> bool:
        return not self.errors

    @property
    def needs_work(self) -> bool:
        return self.action in {"prepare", "publish"}

    @property
    def plan_id(self) -> str:
        encoded = json.dumps(
            self.to_dict(include_plan_id=False), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self, *, include_plan_id: bool = True) -> dict[str, Any]:
        result = {
            "action": self.action,
            "component": "fdkaac",
            "current_evidence": self.current_evidence.to_dict(),
            "current_status": self.current_status,
            "errors": list(self.errors),
            "needs_work": self.needs_work,
            "prepared_root": self.prepared_root,
            "privilege_required": self.privilege_required,
            "ready": self.ready,
            "reasons": list(self.reasons),
            "required": self.required,
            "schema_version": self.schema_version,
            "source_dir": self.source_dir,
            "source_status": self.source_status,
        }
        if include_plan_id:
            result["plan_id"] = self.plan_id
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class NativePreparationResult:
    plan_id: str
    prepared_root: str | None
    no_op: bool
    source_evidence: NativeSourceEvidence | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": "fdkaac",
            "no_op": self.no_op,
            "plan_id": self.plan_id,
            "prepared_root": self.prepared_root,
            "source_evidence": (
                self.source_evidence.to_dict() if self.source_evidence is not None else None
            ),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class NativePublicationResult:
    plan_id: str
    changed_components: tuple[str, ...]
    evidence: RuntimeEvidence
    no_op: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_components": list(self.changed_components),
            "evidence": self.evidence.to_dict(),
            "no_op": self.no_op,
            "plan_id": self.plan_id,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def verify_native_sources(
    source_dir: str | Path, manifest: dict[str, Any]
) -> NativeSourceEvidence:
    root = Path(source_dir).absolute()
    _assert_no_symlink_ancestors(root)
    _assert_existing_directory(root)
    evidence: list[NativeSourceArchiveEvidence] = []
    archives = manifest["components"]["fdkaac"]["source_archives"]
    for name in sorted(archives):
        expected = archives[name]
        filename = expected["filename"]
        if Path(filename).name != filename:
            raise RuntimeProvisioningError("native archive contract filename is unsafe")
        path = root / filename
        metadata = _regular_file(path)
        actual_hash = _sha256(path)
        if metadata.st_size != expected["bytes"]:
            raise RuntimeProvisioningError(f"native source byte count mismatch: {filename}")
        if actual_hash != expected["sha256"]:
            raise RuntimeProvisioningError(f"native source SHA-256 mismatch: {filename}")
        evidence.append(
            NativeSourceArchiveEvidence(
                name=name,
                filename=filename,
                bytes=metadata.st_size,
                sha256=actual_hash,
            )
        )
    return NativeSourceEvidence(source_dir=str(root), archives=tuple(evidence))


def _artifact(
    path: Path,
    root: Path,
    name: str,
    *,
    executable: bool = False,
    expected_uid: int | None = None,
) -> NativeArtifactEvidence:
    _assert_no_symlink_ancestors(path.parent)
    metadata = _regular_file(
        path, executable=executable, expected_uid=expected_uid
    )
    return NativeArtifactEvidence(
        name=name,
        relative_path=path.relative_to(root).as_posix(),
        bytes=metadata.st_size,
        sha256=_sha256(path),
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _prepared_paths(root: Path, manifest: dict[str, Any]) -> dict[str, Path | str]:
    runtime = manifest["components"]["fdkaac"]["runtime"]
    version = runtime["libfdk_aac_version"]
    prefix = root / "prefix"
    versioned_name = f"libfdk-aac.so.{version}"
    soname_name = f"libfdk-aac.so.{version.split('.', 1)[0]}"
    return {
        "prefix": prefix,
        "binary": prefix / "bin" / "fdkaac",
        "library": prefix / "lib" / versioned_name,
        "pkgconfig": prefix / "lib" / "pkgconfig" / "fdk-aac.pc",
        "soname": prefix / "lib" / soname_name,
        "soname_name": soname_name,
        "versioned_name": versioned_name,
    }


def _inspect_prepared_prefix(
    root: Path,
    manifest: dict[str, Any],
    *,
    expected_preparer_uid: int,
) -> PreparedNativeRuntime:
    paths = _prepared_paths(root, manifest)
    prefix = paths["prefix"]
    assert isinstance(prefix, Path)
    _assert_existing_directory(
        prefix, owner=expected_preparer_uid, forbid_shared_write=True
    )
    for directory in (
        prefix / "bin",
        prefix / "lib",
        prefix / "lib" / "pkgconfig",
    ):
        _assert_existing_directory(
            directory, owner=expected_preparer_uid, forbid_shared_write=True
        )
    artifacts = (
        _artifact(
            paths["binary"], root, "fdkaac", executable=True,
            expected_uid=expected_preparer_uid,
        ),
        _artifact(
            paths["library"], root, "libfdk-aac",
            expected_uid=expected_preparer_uid,
        ),
        _artifact(
            paths["pkgconfig"], root, "fdk-aac-pkgconfig",
            expected_uid=expected_preparer_uid,
        ),
    )
    soname = paths["soname"]
    assert isinstance(soname, Path)
    _assert_no_symlink_ancestors(soname.parent)
    try:
        metadata = soname.lstat()
    except OSError as exc:
        raise RuntimeProvisioningError("prepared libfdk-aac SONAME link is unavailable") from exc
    if not stat.S_ISLNK(metadata.st_mode):
        raise RuntimeProvisioningError("prepared libfdk-aac SONAME must be a symlink")
    if metadata.st_uid != expected_preparer_uid:
        raise RuntimeProvisioningError("prepared SONAME has unexpected owner")
    target = os.readlink(soname)
    if target != paths["versioned_name"]:
        raise RuntimeProvisioningError("prepared libfdk-aac SONAME target is invalid")
    runtime = manifest["components"]["fdkaac"]["runtime"]
    return PreparedNativeRuntime(
        root=root,
        prefix=prefix,
        product_contract_sha256=product_contract_digest(manifest),
        fdkaac_version=runtime["fdkaac_version"],
        libfdk_aac_version=runtime["libfdk_aac_version"],
        artifacts=artifacts,
        soname_path=soname.relative_to(root).as_posix(),
        soname_target=target,
        preparer_uid=expected_preparer_uid,
    )


def _write_prepared_receipt(prepared: PreparedNativeRuntime) -> None:
    path = prepared.root / RECEIPT_NAME
    temporary = prepared.root / f".{RECEIPT_NAME}.{uuid.uuid4().hex}"
    data = json.dumps(prepared.to_dict(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _fsync_directory(prepared.root)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _read_prepared_receipt(path: Path, *, expected_uid: int) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeProvisioningError("prepared-native receipt is not a regular file")
        if metadata.st_nlink != 1:
            raise RuntimeProvisioningError("prepared-native receipt must not be hard-linked")
        if metadata.st_uid != expected_uid:
            raise RuntimeProvisioningError("prepared-native receipt has unexpected owner")
        if metadata.st_mode & 0o022:
            raise RuntimeProvisioningError(
                "prepared-native receipt is group/world writable"
            )
        if metadata.st_size > 1024 * 1024:
            raise RuntimeProvisioningError("prepared-native receipt is unreasonably large")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = source.read(1024 * 1024 + 1)
    except OSError as exc:
        raise RuntimeProvisioningError("prepared-native receipt is invalid") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeProvisioningError("prepared-native receipt is invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeProvisioningError("prepared-native receipt is invalid")
    return payload


def load_prepared_native(
    prepared_root: str | Path,
    manifest: dict[str, Any],
    *,
    expected_preparer_uid: int,
) -> PreparedNativeRuntime:
    root = Path(prepared_root).absolute()
    _assert_no_symlink_ancestors(root)
    _assert_existing_directory(
        root, owner=expected_preparer_uid, forbid_shared_write=True
    )
    receipt = root / RECEIPT_NAME
    payload = _read_prepared_receipt(receipt, expected_uid=expected_preparer_uid)
    observed = _inspect_prepared_prefix(
        root, manifest, expected_preparer_uid=expected_preparer_uid
    )
    if payload != observed.to_dict():
        raise RuntimeProvisioningError("prepared-native material disagrees with its receipt")
    return observed


def _copy_regular(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
    expected_bytes: int,
    mode: int,
    expected_uid: int | None = None,
) -> None:
    metadata = _regular_file(source, expected_uid=expected_uid)
    if metadata.st_size != expected_bytes:
        raise RuntimeProvisioningError("native artifact changed during protected handoff")
    source_descriptor = -1
    destination_descriptor = -1
    digest = hashlib.sha256()
    size = 0
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != expected_bytes
            or opened.st_mode & 0o022
            or (expected_uid is not None and opened.st_uid != expected_uid)
        ):
            raise RuntimeProvisioningError(
                "native artifact changed during protected handoff"
            )
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(source_descriptor, "rb") as input_file, os.fdopen(
            destination_descriptor, "wb"
        ) as output_file:
            source_descriptor = -1
            destination_descriptor = -1
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(destination, mode)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    if size != expected_bytes or digest.hexdigest() != expected_sha256:
        destination.unlink(missing_ok=True)
        raise RuntimeProvisioningError("native artifact changed during protected copy")


@dataclass(slots=True)
class _CanonicalState:
    path: Path
    existed: bool
    backup: Path | None
    mode: int | None


class NativeRuntimeProvisioner:
    """Reusable, explicit unprivileged-prepare / protected-publish adapter."""

    def __init__(
        self,
        *,
        requirements: RuntimeRequirements,
        product_manifest: dict[str, Any] | None = None,
        target_root: str | Path = "/",
        project_root: str | Path | None = None,
        bootstrap: bool = False,
        seams: NativeProvisioningSeams = NativeProvisioningSeams(),
    ) -> None:
        self.manifest = product_manifest or load_runtime_components()
        self.requirements = requirements
        self.layout = ProvisioningLayout.from_manifest(self.manifest, target_root=target_root)
        self.project_root = Path(project_root or MANIFEST_PATH.parent.parent).absolute()
        self.bootstrap = bootstrap
        self.seams = seams

    @property
    def build_script(self) -> Path:
        return self.project_root / self.manifest["components"]["fdkaac"]["build"]["script"]

    @property
    def validator_script(self) -> Path:
        return self.project_root / self.manifest["components"]["fdkaac"]["build"]["validator"]

    def _current_evidence(self) -> RuntimeEvidence:
        return self.seams.validate_runtime(
            manifest_for_layout(self.manifest, self.layout), self.requirements
        )

    def plan(
        self,
        *,
        source_dir: str | Path | None = None,
        prepared_root: str | Path | None = None,
        expected_preparer_uid: int | None = None,
    ) -> NativeProvisioningPlan:
        if source_dir is not None and prepared_root is not None:
            raise RuntimeProvisioningError("choose source material or prepared material, not both")
        current = self._current_evidence()
        requirement = self.requirements.components["fdkaac"]
        required = requirement.required or self.bootstrap
        reasons = requirement.reasons + (("explicit fdkaac bootstrap",) if self.bootstrap else ())
        component = current.components.get("fdkaac")
        status = component.status if component is not None else "fail"
        source_status = "not_checked"
        errors: list[str] = list(self.requirements.errors)
        action = "not_selected"
        if required and status == STATUS_PASS:
            action = "no_op"
        elif required and errors:
            action = "blocked"
        elif required and source_dir is not None:
            try:
                verify_native_sources(source_dir, self.manifest)
                source_status = "verified"
                action = "prepare"
            except RuntimeProvisioningError as exc:
                source_status = "invalid"
                errors.append(_safe_message(exc))
                action = "blocked"
        elif required and prepared_root is not None:
            try:
                trusted_uid = _validated_preparer_uid(
                    expected_preparer_uid,
                    canonical=self.layout.target_root == Path("/"),
                )
                load_prepared_native(
                    prepared_root,
                    self.manifest,
                    expected_preparer_uid=trusted_uid,
                )
                source_status = "prepared_verified"
                action = "publish"
            except RuntimeProvisioningError as exc:
                source_status = "invalid"
                errors.append(_safe_message(exc))
                action = "blocked"
        elif required:
            action = "blocked"
            errors.append("required fdkaac needs an explicit source or prepared directory")
        return NativeProvisioningPlan(
            action=action,
            required=required,
            reasons=tuple(sorted(set(reasons))),
            current_status=status,
            source_status=source_status,
            source_dir=str(Path(source_dir).absolute()) if source_dir is not None else None,
            prepared_root=(
                str(Path(prepared_root).absolute()) if prepared_root is not None else None
            ),
            privilege_required=self.layout.target_root == Path("/"),
            current_evidence=current,
            errors=tuple(sorted(set(errors))),
        )

    def prepare(
        self, *, source_dir: str | Path, prepared_root: str | Path
    ) -> NativePreparationResult:
        plan = self.plan(source_dir=source_dir)
        if plan.action == "no_op":
            return NativePreparationResult(plan.plan_id, None, True, None)
        if not plan.ready or plan.action != "prepare":
            raise RuntimeProvisioningError("native preparation plan contains blocking errors")
        evidence = verify_native_sources(source_dir, self.manifest)
        root = Path(prepared_root).absolute()
        _assert_no_symlink_ancestors(root.parent)
        _assert_existing_directory(root.parent, owner=os.geteuid())
        if root.exists() or root.is_symlink():
            raise RuntimeProvisioningError("prepared root must not already exist")
        os.mkdir(root, 0o700)
        try:
            private_sources = root / "sources"
            private_sources.mkdir(mode=0o700)
            source_contract = {
                item.name: item for item in evidence.archives
            }
            for name, item in sorted(source_contract.items()):
                expected = self.manifest["components"]["fdkaac"]["source_archives"][name]
                _copy_regular(
                    Path(evidence.source_dir) / expected["filename"],
                    private_sources / expected["filename"],
                    expected_sha256=item.sha256,
                    expected_bytes=item.bytes,
                    mode=0o600,
                )
            prefix = root / "prefix"
            self.seams.checkpoint("before_native_build")
            self.seams.build(self.build_script, private_sources, prefix)
            self.seams.checkpoint("after_native_build")
            prepared = _inspect_prepared_prefix(
                root, self.manifest, expected_preparer_uid=os.geteuid()
            )
            self.seams.validate_prefix(self.validator_script, prefix)
            self.seams.checkpoint("after_staged_native_validation")
            _write_prepared_receipt(prepared)
            return NativePreparationResult(
                plan.plan_id, str(root), False, evidence
            )
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    def _preflight_identity(self) -> None:
        root = self.layout.target_root
        _assert_existing_directory(root)
        if root == Path("/") and os.geteuid() != 0:
            raise RuntimeProvisioningError("canonical native publication requires root privileges")
        if root != Path("/") and root.stat().st_uid != os.geteuid():
            raise RuntimeProvisioningError("noncanonical target root must be owned by the caller")
        if not os.access(root, os.W_OK | os.X_OK):
            raise RuntimeProvisioningError("target root is not writable by the caller")

    def _preflight_publish(self) -> Path:
        self._preflight_identity()
        root = self.layout.target_root
        local_root = ProvisioningLayout._map("/usr/local", root)
        _assert_confined_non_symlink(root, local_root)
        # Canonical "/" never gets here: the trusted system hierarchy
        # (/usr/local, its bin/lib) must already exist there -- native
        # publication must never manufacture trusted system ancestors on
        # the real host. A noncanonical target root (e.g. an isolated
        # --staging-root restore tree) is intentionally an incomplete
        # filesystem skeleton that nothing upstream creates, so it is the
        # one case allowed to establish its own confined /usr/local
        # skeleton first -- see _ensure_noncanonical_publication_directories.
        if root != Path("/"):
            _ensure_noncanonical_publication_directories(
                root, local_root, self.layout.fdkaac_binary.parent, self.layout.fdkaac_library_root
            )
        for directory in (local_root, self.layout.fdkaac_binary.parent, self.layout.fdkaac_library_root):
            _assert_confined_non_symlink(root, directory)
            _assert_existing_directory(directory)
        for target in (self.layout.fdkaac_binary, self._canonical_library()):
            _assert_confined_non_symlink(root, target)
            if target.is_symlink():
                raise RuntimeProvisioningError("canonical native target is an unexpected symlink")
            if target.exists():
                _regular_file(target)
        return local_root

    def _canonical_library(self) -> Path:
        version = self.manifest["components"]["fdkaac"]["runtime"]["libfdk_aac_version"]
        return self.layout.fdkaac_library_root / f"libfdk-aac.so.{version}"

    def _protected_copy(
        self,
        prepared: PreparedNativeRuntime,
        protected_root: Path,
        *,
        expected_preparer_uid: int,
    ) -> tuple[Path, dict[str, NativeArtifactEvidence]]:
        protected_prefix = protected_root / "prefix"
        (protected_prefix / "bin").mkdir(parents=True, mode=0o700)
        (protected_prefix / "lib" / "pkgconfig").mkdir(parents=True, mode=0o700)
        by_name = {item.name: item for item in prepared.artifacts}
        destinations = {
            "fdkaac": protected_prefix / "bin" / "fdkaac",
            "libfdk-aac": protected_prefix / "lib" / Path(by_name["libfdk-aac"].relative_path).name,
            "fdk-aac-pkgconfig": protected_prefix / "lib" / "pkgconfig" / "fdk-aac.pc",
        }
        modes = {"fdkaac": 0o755, "libfdk-aac": 0o644, "fdk-aac-pkgconfig": 0o644}
        for name, destination in destinations.items():
            item = by_name[name]
            _copy_regular(
                prepared.root / item.relative_path,
                destination,
                expected_sha256=item.sha256,
                expected_bytes=item.bytes,
                mode=modes[name],
                expected_uid=expected_preparer_uid,
            )
            copied = _artifact(destination, protected_root, name, executable=name == "fdkaac")
            if copied.sha256 != item.sha256 or copied.bytes != item.bytes:
                raise RuntimeProvisioningError("protected native copy identity mismatch")
        soname = protected_root / prepared.soname_path
        os.symlink(prepared.soname_target, soname)
        self.seams.validate_prefix(self.validator_script, protected_prefix)
        self.seams.checkpoint("after_protected_native_validation")
        return protected_prefix, by_name

    def _snapshot(self, path: Path, snapshot_root: Path) -> _CanonicalState:
        if not path.exists():
            return _CanonicalState(path, False, None, None)
        metadata = _regular_file(path)
        backup = snapshot_root / path.name
        _copy_regular(
            path,
            backup,
            expected_sha256=_sha256(path),
            expected_bytes=metadata.st_size,
            mode=stat.S_IMODE(metadata.st_mode),
        )
        return _CanonicalState(path, True, backup, stat.S_IMODE(metadata.st_mode))

    def _replace(self, source: Path, target: Path, *, mode: int) -> None:
        metadata = _regular_file(source, executable=mode == 0o755)
        temporary = target.parent / f".{target.name}.e4-{uuid.uuid4().hex}"
        try:
            _copy_regular(
                source,
                temporary,
                expected_sha256=_sha256(source),
                expected_bytes=metadata.st_size,
                mode=mode,
            )
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _restore(self, state: _CanonicalState) -> None:
        if state.existed:
            assert state.backup is not None and state.mode is not None
            self._replace(state.backup, state.path, mode=state.mode)
        elif state.path.exists() or state.path.is_symlink():
            if state.path.is_symlink() or state.path.is_file():
                state.path.unlink()
                _fsync_directory(state.path.parent)
            else:
                raise RuntimeProvisioningError("rollback target became an unexpected directory")

    def publish(
        self,
        *,
        prepared_root: str | Path,
        expected_preparer_uid: int | None = None,
    ) -> NativePublicationResult:
        trusted_uid = _validated_preparer_uid(
            expected_preparer_uid,
            canonical=self.layout.target_root == Path("/"),
        )
        self._preflight_identity()
        with runtime_provision_lock(self.layout):
            locked_plan = self.plan(
                prepared_root=prepared_root,
                expected_preparer_uid=trusted_uid,
            )
            if not locked_plan.ready:
                raise RuntimeProvisioningError(
                    "native publication plan contains blocking errors: "
                    + "; ".join(locked_plan.errors)
                )
            if locked_plan.action == "no_op":
                return NativePublicationResult(
                    locked_plan.plan_id, (), locked_plan.current_evidence, True
                )
            if locked_plan.action != "publish":
                raise RuntimeProvisioningError("native publication is not selected")
            local_root = self._preflight_publish()
            prepared = load_prepared_native(
                prepared_root,
                self.manifest,
                expected_preparer_uid=trusted_uid,
            )
            protected_root = local_root / f".isadoraair-fdkaac-e4-{uuid.uuid4().hex}"
            os.mkdir(protected_root, 0o700)
            protected_metadata = protected_root.stat()
            if (
                protected_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(protected_metadata.st_mode) != 0o700
            ):
                protected_root.rmdir()
                raise RuntimeProvisioningError(
                    "protected native staging ownership or permissions are unsafe"
                )
            before = locked_plan.current_evidence
            states: list[_CanonicalState] = []
            mutation_started = False
            try:
                protected_prefix, artifacts = self._protected_copy(
                    prepared,
                    protected_root,
                    expected_preparer_uid=trusted_uid,
                )
                snapshot_root = protected_root / "snapshot"
                snapshot_root.mkdir(mode=0o700)
                library = self._canonical_library()
                states = [
                    self._snapshot(library, snapshot_root),
                    self._snapshot(self.layout.fdkaac_binary, snapshot_root),
                ]
                self.seams.checkpoint("before_native_publication")
                mutation_started = True
                self._replace(
                    protected_prefix / "lib" / Path(artifacts["libfdk-aac"].relative_path).name,
                    library,
                    mode=0o644,
                )
                self.seams.checkpoint("after_library_publication")
                self._replace(
                    protected_prefix / "bin" / "fdkaac",
                    self.layout.fdkaac_binary,
                    mode=0o755,
                )
                self.seams.checkpoint("after_binary_publication")
                self.seams.ldconfig(self.layout.target_root)
                self.seams.checkpoint("after_ldconfig")
                final = self._current_evidence()
                if final.contract_errors:
                    raise RuntimeProvisioningError("runtime contract validation failed")
                if final.requirement_errors:
                    raise RuntimeProvisioningError("station requirement validation failed")
                component = final.components.get("fdkaac")
                if component is None or component.status != STATUS_PASS:
                    raise RuntimeProvisioningError(
                        "published fdkaac component failed Foundation E2 validation"
                    )
                self.seams.checkpoint("after_native_final_acceptance")
                return NativePublicationResult(
                    locked_plan.plan_id, ("fdkaac",), final, False
                )
            except Exception as exc:
                if mutation_started:
                    rollback_errors: list[str] = []
                    for state in reversed(states):
                        try:
                            self._restore(state)
                        except Exception as rollback_exc:
                            rollback_errors.append(_safe_message(rollback_exc))
                    try:
                        self.seams.ldconfig(self.layout.target_root)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"rollback ldconfig: {_safe_message(rollback_exc)}")
                    try:
                        after = self._current_evidence()
                        before_fd = before.components.get("fdkaac")
                        after_fd = after.components.get("fdkaac")
                        if (
                            before.contract_errors != after.contract_errors
                            or before.requirement_errors != after.requirement_errors
                            or before_fd is None
                            or after_fd is None
                            or before_fd.to_dict() != after_fd.to_dict()
                        ):
                            rollback_errors.append("fdkaac did not return to pre-publication E2 state")
                    except Exception as rollback_exc:
                        rollback_errors.append(f"rollback validation: {_safe_message(rollback_exc)}")
                    if rollback_errors:
                        raise RuntimeProvisioningError(
                            "native publication failed and rollback failed: "
                            + "; ".join(rollback_errors)
                        ) from exc
                if isinstance(exc, RuntimeProvisioningError):
                    raise
                raise RuntimeProvisioningError(_safe_message(exc)) from exc
            finally:
                shutil.rmtree(protected_root, ignore_errors=True)
