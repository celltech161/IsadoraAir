"""Root-owned, read-only materialization of an exact trusted Git tree."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile

from .release import ReleaseError, TrustedRepository
from .security import assert_root_protected, assert_root_protected_parents


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 100000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class StagingError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class StagedSource:
    job_root: Path
    source_root: Path
    archive_path: Path


def _job_root(staging_root: Path, job_id: str) -> Path:
    if not UUID_RE.fullmatch(job_id):
        raise StagingError("job id is not a canonical UUID")
    root = Path(staging_root).resolve(strict=False)
    candidate = root / job_id
    if candidate.parent != root:
        raise StagingError("job staging path escaped the configured root")
    return candidate


def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise StagingError(f"unsafe archive member path: {name!r}")
    return path


def materialize(repository: TrustedRepository, target_commit: str,
                staging_root: Path, job_id: str) -> StagedSource:
    root = Path(staging_root)
    assert_root_protected_parents(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    assert_root_protected(root)
    os.chmod(root, 0o755)
    job_root = _job_root(root, job_id)
    if job_root.exists() or job_root.is_symlink():
        raise StagingError("staging job directory already exists")
    job_root.mkdir(mode=0o755)
    archive = job_root / "target.tar"
    source = job_root / "source"
    source.mkdir(mode=0o755)
    result = repository.archive_to(target_commit, archive, maximum=MAX_ARCHIVE_BYTES)
    if not result.ok or not archive.is_file() or archive.stat().st_size <= 0:
        raise StagingError("trusted target archive creation failed or exceeded its limit")
    os.chmod(archive, 0o600)

    directories = {source}
    seen: set[PurePosixPath] = set()
    extracted = 0
    with tarfile.open(archive, mode="r:") as bundle:
        members = bundle.getmembers()
        if len(members) > MAX_MEMBERS:
            raise StagingError("target archive contains too many members")
        for member in members:
            relative = _safe_member(member.name)
            if relative in seen:
                raise StagingError(f"target archive repeats {member.name!r}")
            seen.add(relative)
            destination = source.joinpath(*relative.parts)
            try:
                destination.relative_to(source)
            except ValueError as exc:
                raise StagingError("target archive escaped the source root") from exc
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=False, mode=0o755)
                directories.add(destination)
                continue
            if not member.isreg():
                raise StagingError(f"links and special files are forbidden in target source: {member.name!r}")
            extracted += member.size
            if extracted > MAX_EXTRACTED_BYTES:
                raise StagingError("target archive exceeds the extracted-size limit")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            directories.add(destination.parent)
            stream = bundle.extractfile(member)
            if stream is None:
                raise StagingError(f"could not read archive member {member.name!r}")
            with open(destination, "xb") as output:
                remaining = member.size
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise StagingError(f"archive member {member.name!r} ended early")
                    output.write(chunk)
                    remaining -= len(chunk)
                if stream.read(1):
                    raise StagingError(f"archive member {member.name!r} exceeded declared size")
            os.chmod(destination, 0o555 if member.mode & 0o111 else 0o444)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        os.chmod(directory, 0o555)
    return StagedSource(job_root, source, archive)


def cleanup(staging_root: Path, job_id: str):
    target = _job_root(staging_root, job_id)
    if not target.exists():
        return
    if target.is_symlink() or target.parent.resolve(strict=True) != Path(staging_root).resolve(strict=True):
        raise StagingError("refusing unsafe staging cleanup target")
    for directory, child_directories, _files in os.walk(target, topdown=False, followlinks=False):
        for child in child_directories:
            candidate = Path(directory) / child
            if candidate.is_symlink():
                raise StagingError("refusing cleanup of staging tree containing a symlink")
            os.chmod(candidate, 0o700)
        os.chmod(directory, 0o700)
    shutil.rmtree(target)
