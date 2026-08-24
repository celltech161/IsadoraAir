"""Pre-migration PostgreSQL checkpoint creation and conservative retention."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from .config import StationConfig
from .process import CommandRunner
from .security import assert_root_protected, assert_root_protected_parents


PG_DUMP = "/usr/bin/pg_dump"
MAX_DUMP_BYTES = 50 * 1024 * 1024 * 1024


class CheckpointError(RuntimeError):
    pass


def _atomic_json(path: Path, data: dict):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_checkpoint(config: StationConfig, runner: CommandRunner, *, job_id: str,
                      installed_release: str, installed_commit: str,
                      target_release: str, target_commit: str) -> dict:
    root = config.checkpoint_root
    assert_root_protected_parents(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    assert_root_protected(root)
    os.chmod(root, 0o700)
    stem = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{job_id}"
    partial = root / f"{stem}.dump.partial"
    final = root / f"{stem}.dump"
    metadata_path = root / f"{stem}.json"
    if any(path.exists() or path.is_symlink() for path in (partial, final, metadata_path)):
        raise CheckpointError("checkpoint destination collision")
    db = config.database
    argv = runner.argv_as_user(config.application_user, [
        PG_DUMP, "--format=custom", "--no-owner", "--no-acl",
        "--host", db.host, "--port", str(db.port), "--username", db.user,
        "--dbname", db.name,
    ])
    env = {"PGPASSFILE": str(db.pgpass_file)} if db.pgpass_file else {}
    result = runner.run_to_file(argv, partial, timeout=1800, max_bytes=MAX_DUMP_BYTES, env=env)
    if not result.ok or not partial.is_file() or partial.stat().st_size == 0:
        if partial.exists() and partial.parent == root:
            partial.unlink()
        raise CheckpointError("pg_dump failed, timed out, or produced an invalid checkpoint")
    os.chmod(partial, 0o600)
    size = partial.stat().st_size
    digest = _sha256(partial)
    os.replace(partial, final)
    metadata = {
        "schema_version": 1,
        "valid": True,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "job_id": job_id,
        "installed_release_id": installed_release,
        "installed_commit": installed_commit,
        "target_release_id": target_release,
        "target_commit": target_commit,
        "dump_file": final.name,
        "size_bytes": size,
        "sha256": digest,
    }
    _atomic_json(metadata_path, metadata)
    prune_checkpoints(root, now=dt.datetime.now(dt.timezone.utc))
    return metadata


def _valid_entries(root: Path) -> list[tuple[dt.datetime, Path, Path]]:
    result = []
    for metadata_path in root.glob("*.json"):
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
            created = dt.datetime.fromisoformat(data["created_at"])
            dump = root / data["dump_file"]
            if (data.get("valid") is True and dump.parent == root and dump.is_file()
                    and dump.stat().st_size == data["size_bytes"] and _sha256(dump) == data["sha256"]):
                result.append((created, metadata_path, dump))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(result, reverse=True, key=lambda item: item[0])


def verify_checkpoint(root: Path, metadata: dict) -> bool:
    try:
        dump = Path(root) / metadata["dump_file"]
        return (
            metadata.get("valid") is True
            and dump.parent == Path(root)
            and dump.is_file()
            and dump.stat().st_size == metadata["size_bytes"]
            and _sha256(dump) == metadata["sha256"]
        )
    except (OSError, KeyError, TypeError):
        return False



def prune_checkpoints(root: Path, *, now: dt.datetime, retention_days: int = 30, maximum: int = 5):
    entries = _valid_entries(root)
    keep = set()
    for index, (created, metadata, dump) in enumerate(entries):
        age = now - created.astimezone(dt.timezone.utc)
        if index == 0 or (index < maximum and age <= dt.timedelta(days=retention_days)):
            keep.update({metadata, dump})
    for _created, metadata, dump in entries:
        if metadata not in keep:
            metadata.unlink()
            dump.unlink()
