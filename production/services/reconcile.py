"""Reconciliation and sweep primitives for the production-media store.

Everything here is callable service code (plus the ``production_reconcile``
management command, dry-run by default). There is deliberately NO scheduler:
no systemd timer, no automatic destruction. What each primitive reclaims:

* ``sweep_stale_parts``  incoming/<32hex>.part with no writes for the grace
  period -- an upload that was interrupted (or whose process was killed).
* ``sweep_orphan_media`` permanent files under media/ that no ProductionMedia
  row owns (the process died between promotion and the row commit, or the
  caller's transaction rolled back) -- and leftover bytes of PURGED rows.
* ``sweep_stale_work``   work/<32hex> scratch workspaces untouched for the
  grace period.
* ``find_inconsistent_media``  REPORT ONLY: present rows whose bytes are
  missing, the wrong size or (deep) the wrong SHA-256. Bytes cannot be
  invented, so this never "fixes" anything.

Safety rules shared by every sweep: only names in the exact system-generated
shape are ever deleted; anything else is reported as ``unexpected`` and left
alone; symlinks are never followed; age is measured from the later of mtime and
ctime (ctime moves when an upload is promoted); and the grace period has a hard
one-hour floor so a short interval can never race an in-flight upload. ``now``
is injectable for deterministic tests.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from ..models import ProductionMedia
from . import layout

DEFAULT_GRACE = timedelta(hours=24)
MIN_GRACE = timedelta(hours=1)
_BATCH = 500


@dataclass
class SweepReport:
    removed: list = field(default_factory=list)
    kept_young: int = 0
    unexpected: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    dry_run: bool = True


def _grace(grace: timedelta) -> timedelta:
    if grace < MIN_GRACE:
        raise ValueError("grace period must be at least one hour")
    return grace


def _now(now) -> datetime:
    return now if now is not None else timezone.now()


def _age_reference(info) -> float:
    return max(info.st_mtime, info.st_ctime)


def _is_old_enough(info, now: datetime, grace: timedelta) -> bool:
    return now.timestamp() - _age_reference(info) >= grace.total_seconds()


def _remove_file(path, report: SweepReport, label: str, apply: bool):
    if not apply:
        report.removed.append(label)
        return
    try:
        os.unlink(path)
        report.removed.append(label)
    except FileNotFoundError:
        pass
    except OSError as exc:
        report.errors.append(f"{label}: {exc.strerror or exc.__class__.__name__}")


def sweep_stale_parts(*, grace=DEFAULT_GRACE, now=None, apply=False) -> SweepReport:
    grace, now = _grace(grace), _now(now)
    report = SweepReport(dry_run=not apply)
    directory = layout.incoming_dir()
    if not directory.is_dir():
        return report
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not layout.PART_NAME_RE.fullmatch(entry.name):
            report.unexpected.append(f"incoming/{entry.name}")
            continue
        if _is_old_enough(info, now, grace):
            _remove_file(entry.path, report, f"incoming/{entry.name}", apply)
        else:
            report.kept_young += 1
    return report


def _media_files():
    """(storage_key, path, lstat) for every plausibly-named file under media/,
    plus a list of unexpected entry names."""
    files, unexpected = [], []
    root = layout.media_dir()
    if not root.is_dir():
        return files, unexpected
    for shard in sorted(os.scandir(root), key=lambda item: item.name):
        info = shard.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or len(shard.name) != 2 or not all(c in "0123456789abcdef" for c in shard.name):
            unexpected.append(f"media/{shard.name}")
            continue
        for entry in sorted(os.scandir(shard.path), key=lambda item: item.name):
            key = f"{shard.name}/{entry.name}"
            entry_info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(entry_info.st_mode) or not layout.STORAGE_KEY_RE.fullmatch(key) \
                    or not entry.name.startswith(shard.name):
                unexpected.append(f"media/{key}")
                continue
            files.append((key, entry.path, entry_info))
    return files, unexpected


def sweep_orphan_media(*, grace=DEFAULT_GRACE, now=None, apply=False) -> SweepReport:
    grace, now = _grace(grace), _now(now)
    report = SweepReport(dry_run=not apply)
    files, report.unexpected = _media_files()
    for start in range(0, len(files), _BATCH):
        batch = files[start:start + _BATCH]
        states = dict(ProductionMedia.objects.filter(storage_key__in=[key for key, _p, _i in batch])
                      .values_list("storage_key", "retention_state"))
        for key, path, info in batch:
            state = states.get(key)
            if state == ProductionMedia.RETENTION_PRESENT:
                continue                              # a live row owns it
            # No row at all (orphan) or a purged row whose unlink never ran.
            if _is_old_enough(info, now, grace):
                _remove_file(path, report, f"media/{key}", apply)
            else:
                report.kept_young += 1
    return report


def _newest_mtime(path) -> float:
    newest = 0.0
    for current, directories, names in os.walk(path, followlinks=False):
        for name in [*directories, *names]:
            try:
                info = os.lstat(os.path.join(current, name))
            except OSError:
                continue
            newest = max(newest, _age_reference(info))
    try:
        newest = max(newest, _age_reference(os.lstat(path)))
    except OSError:
        pass
    return newest


def sweep_stale_work(*, grace=DEFAULT_GRACE, now=None, apply=False) -> SweepReport:
    grace, now = _grace(grace), _now(now)
    report = SweepReport(dry_run=not apply)
    directory = layout.work_root()
    if not directory.is_dir():
        return report
    for entry in sorted(os.scandir(directory), key=lambda item: item.name):
        info = entry.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or not layout.WORK_NAME_RE.fullmatch(entry.name):
            report.unexpected.append(f"work/{entry.name}")
            continue
        if now.timestamp() - _newest_mtime(entry.path) < grace.total_seconds():
            report.kept_young += 1
            continue
        label = f"work/{entry.name}"
        if not apply:
            report.removed.append(label)
            continue
        try:
            shutil.rmtree(entry.path)                  # never follows symlinks
            report.removed.append(label)
        except OSError as exc:
            report.errors.append(f"{label}: {exc.strerror or exc.__class__.__name__}")
    return report


def find_purged_media_still_referenced():
    """REPORT ONLY: purged media that a domain row still references -- the
    symptom of a consumer that skipped retention.lock_for_binding(). Counts
    only; nothing is repaired (the bytes are gone)."""
    from . import retention

    found = []
    for media_id in ProductionMedia.objects.filter(
        retention_state=ProductionMedia.RETENTION_PURGED,
    ).values_list("id", flat=True).iterator():
        for reference in retention.find_references(media_id):
            found.append((str(media_id), reference.model_label, reference.field_name, reference.count))
    return found


@dataclass(frozen=True)
class Inconsistency:
    media_id: str
    problem: str            # missing_bytes | size_mismatch | not_a_regular_file | sha_mismatch | unreadable_bytes


def find_inconsistent_media(*, deep=False, limit=None) -> list[Inconsistency]:
    """Present rows whose bytes do not match their evidence. Read-only."""
    problems = []
    queryset = ProductionMedia.objects.filter(retention_state=ProductionMedia.RETENTION_PRESENT)
    for row in queryset.only("id", "storage_key", "byte_size", "sha256").iterator():
        problem = None
        try:
            path = layout.resolve_storage_path(row.storage_key)
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                problem = "not_a_regular_file"
            elif info.st_size != row.byte_size:
                problem = "size_mismatch"
            elif deep:
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != row.sha256:
                    problem = "sha_mismatch"
        except FileNotFoundError:
            problem = "missing_bytes"
        except OSError:
            problem = "unreadable_bytes"
        if problem:
            problems.append(Inconsistency(str(row.id), problem))
            if limit is not None and len(problems) >= limit:
                break
    return problems
