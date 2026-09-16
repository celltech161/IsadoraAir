"""Aircheck recovery/retention -- P2 1.13B.

Builds on Pass A (active-session segmentation, rename-before-reopen
safe cut, persistent staging, async multi-segment finalization) and
Pass A2 (full decode validation before source cleanup, explicit WAV
-rf64) without redesigning either. This module owns exactly what Pass
A's own review deliberately deferred to "roadmap item 1.13B":

  * retrying a finalization whose daemon thread was lost while
    genuinely still pending (never a second concat/remux
    implementation -- always recorder._finalize_segment_set);
  * evacuating recoverable audio that Pass A/A2 can leave under /run
    once a session is no longer the live active session (stranded
    handoffs after an ordinary-path move failure, legacy HE-AAC remux
    failures);
  * bounding how long failed/recoverable material may accumulate
    before an operator loses the practical chance to recover it.

Governing invariant, carried over unchanged from the Pass A review:
recoverable audio is never discarded merely to make cleanup easy, and
failed/recovery artifacts never accumulate without bound either.

Classification used throughout (identifiers are internal, not a public
contract):
  ACTIVE             -- belongs to the currently running session; never
                        touched by anything in this module.
  PENDING            -- capture ended, exit_note is still exactly
                        recorder.FINALIZATION_PENDING_NOTE. Recoverable/
                        retryable automatically, subject to the grace
                        period and finalization_lock below.
  FAILED_RECOVERABLE -- finalization/remux explicitly failed; source
                        audio remains. Never auto-retried by routine
                        maintenance (that would hammer a genuinely
                        broken input/storage failure); bounded by
                        retention instead.
  ORPHAN_OWNED       -- a /run artifact whose filename establishes a
                        real AircheckSession owner, but which the
                        normal active-session reconciliation path will
                        never revisit (the owning session is already
                        stopped). Evacuated to that session's recovery
                        directory.
  ORPHAN_UNKNOWN     -- matches an Aircheck recovery filename pattern,
                        but ownership cannot be established (unparsed
                        name, or no matching AircheckSession row).
                        Quarantined, never guessed, never deleted
                        automatically.
  STALE              -- source material is provably obsolete because
                        the logical recording already finalized
                        successfully and a valid final file exists.
                        Eligible for cleanup once proven, not merely
                        assumed from file existence.
"""
import errno
import fcntl
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.utils import timezone

from aircheck.models import AircheckConfig, AircheckSession
from aircheck.services import recorder
from monitoring.models import emit_event


# --- Finalization lock -------------------------------------------------

FINALIZATION_LOCK_NAME = ".finalize.lock"


@contextmanager
def finalization_lock(session, *, blocking):
    """Per-session cross-process/cross-thread ownership proof for
    segmented finalization -- the SAME fcntl.flock pattern as
    recorder._aircheck_lock, scoped to one session's staging directory
    rather than the whole Aircheck subsystem. Process death releases it
    automatically (it is an OS-held advisory lock, not a PID file or
    any other artifact whose mere existence could be mistaken for
    ownership); a stale *lock file* implies nothing on its own -- only
    the live flock state does.

    The original async worker (recorder._segmented_finalize_worker)
    acquires this BLOCKING before calling _finalize_segment_set, so a
    concurrent recovery pass's non-blocking attempt correctly observes
    "currently owned" and does nothing. A recovery pass acquires it
    NON-BLOCKING; failing to acquire it means some other worker (the
    original thread, or another recovery pass) is already handling
    this session, never grounds to retry."""
    staging = recorder._staging_dir(session)
    staging.mkdir(parents=True, exist_ok=True)
    lock_path = staging / FINALIZATION_LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired_here = False
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            acquired_here = True
        except OSError as exc:
            if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
        if acquired_here:
            # The lock file itself must never be the reason a fully-
            # cleaned-up (successful finalization, or already-empty)
            # staging directory survives -- recorder._finalize_segment_
            # set's own rmdir() runs INSIDE this lock and can only ever
            # see the directory as non-empty while the lock file sits
            # in it. Attempted only after releasing our own hold (never
            # while acquired_here would still be contended by someone
            # else -- see the early `yield False` path above, which
            # skips this block entirely). Harmless no-op via the caught
            # OSError whenever real recovery data still remains: rmdir
            # only ever succeeds on a genuinely empty directory, and a
            # fresh lock file is recreated by O_CREAT next time anyone
            # needs it regardless.
            try:
                lock_path.unlink()
            except OSError:
                pass
            try:
                staging.rmdir()
            except OSError:
                pass


# --- Pending-finalization automatic retry -------------------------------

# A session lands in FINALIZATION_PENDING_NOTE the instant Stop hands off
# to the daemon thread. Real finalization time is bounded by
# recorder.FFMPEG_TIMEOUT_SECONDS (concat, 1h) plus recorder's own
# duration-derived decode-validation ceiling (up to 6h), so a genuinely
# still-working finalizer can legitimately run for hours on a very long
# recording. finalization_lock() above -- not this constant -- is what
# actually prevents a collision with a still-live finalizer regardless of
# age. This grace period exists only so routine maintenance does not
# bother attempting the lock (and touching the staging directory) against
# an ordinary, still-in-progress finalization moments after Stop; 15
# minutes is comfortably past concat+validate time for a realistic
# session (concat is fast; decode validation for anything under a few
# hours of audio finishes in minutes) while still short enough that an
# operator is not left waiting long for automatic recovery after a
# genuine crash.
PENDING_FINALIZATION_GRACE_SECONDS = 15 * 60


def _pending_session_candidates():
    cutoff = timezone.now() - timedelta(seconds=PENDING_FINALIZATION_GRACE_SECONDS)
    return AircheckSession.objects.filter(
        still_running=False,
        exit_note=recorder.FINALIZATION_PENDING_NOTE,
        ended_at__lte=cutoff,
    ).order_by("ended_at")


def _retry_one_pending_session(session, *, require_pending=True):
    """Returns "succeeded", "failed", or "skipped" (no longer eligible
    by the time the lock was acquired -- another worker already
    resolved it, or it was never in a retryable state).

    require_pending=True (routine/automatic retry) only ever touches
    the exact FINALIZATION_PENDING_NOTE state -- an explicit failure is
    deliberately never auto-retried (see module docstring). A manual,
    operator-triggered retry passes require_pending=False to also allow
    retrying an explicitly-failed-but-source-still-present session."""
    session.refresh_from_db()
    if session.still_running:
        return "skipped"
    if require_pending and session.exit_note != recorder.FINALIZATION_PENDING_NOTE:
        return "skipped"
    if not require_pending and not recorder._discover_segments(session) and not Path(session.filename).is_file():
        return "skipped"  # nothing left to retry from at all

    dest = Path(session.filename)
    segments = recorder._discover_segments(session)

    if not segments and dest.is_file():
        # No sources left but a destination exists: publication most
        # likely already completed and only the post-publish bookkeeping
        # (session row update, library sync) was lost -- e.g. a process
        # death between _finalize_segment_set's return and the caller
        # completing those two steps. Recognize this rather than calling
        # _finalize_segment_set again, which would otherwise -- wrongly
        # -- treat an empty segment set as a fresh failure and never
        # touch (but also never confirm) the destination that is
        # actually already correct. Bare existence is not proof by
        # itself: re-run the exact same full-decode validation this
        # module already requires before ANY publish, so a present-but-
        # corrupt destination is not mistaken for a completed one.
        try:
            recorder._validate_final_audio(dest)
        except recorder.SegmentError:
            pass  # not actually a valid completed destination; fall through
        else:
            return _complete_pending_session(session, dest)

    try:
        size = recorder._finalize_segment_set(session, dest)
    except Exception as exc:
        recorder._mark_segmented_finalization_failed(session.id, exc)
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck automatic finalization retry failed",
            detail={"session_id": session.id, "error": str(exc)[:300]},
            dedupe_key=f"aircheck|finalize-retry-failed|{session.id}",
        )
        return "failed"

    session.size_bytes = size
    session.exit_note = ""
    session.save(update_fields=["size_bytes", "exit_note"])
    recorder._sync_finalized_recording_to_library(dest)
    return "succeeded"


def _complete_pending_session(session, dest):
    try:
        size = dest.stat().st_size
    except OSError:
        size = None
    session.size_bytes = size
    session.exit_note = ""
    session.save(update_fields=["size_bytes", "exit_note"])
    recorder._sync_finalized_recording_to_library(dest)
    return "succeeded"


def retry_pending_finalizations():
    """Scan for PENDING sessions past the grace period and, for each
    one not currently lock-owned by a live finalizer, retry using the
    exact same authoritative recorder._finalize_segment_set a live
    Stop would have used. Never a second concat/remux implementation.

    Safe to call from any process; safe to call concurrently with
    itself or with a still-running original daemon thread -- both are
    fully mediated by finalization_lock()."""
    candidates = list(_pending_session_candidates())
    result = {"candidates": len(candidates), "succeeded": 0, "failed": 0, "skipped_locked": 0}
    for session in candidates:
        with finalization_lock(session, blocking=False) as acquired:
            if not acquired:
                result["skipped_locked"] += 1
                continue
            outcome = _retry_one_pending_session(session)
        if outcome == "succeeded":
            result["succeeded"] += 1
        elif outcome == "failed":
            result["failed"] += 1
    return result


def retry_one_session_by_id(session_id):
    """Manual, operator-triggered retry of exactly one session,
    regardless of grace period -- for `maintain_aircheck_recovery
    --retry-session <id>`. Still fully mediated by finalization_lock();
    still never a second implementation."""
    try:
        session = AircheckSession.objects.get(id=session_id)
    except AircheckSession.DoesNotExist:
        return "not_found"
    with finalization_lock(session, blocking=False) as acquired:
        if not acquired:
            return "locked"
        return _retry_one_pending_session(session, require_pending=False)


# --- /run evacuation -----------------------------------------------------

_HANDOFF_RE = re.compile(
    r"^" + re.escape(recorder.HANDOFF_NAME_PREFIX) + r"(\d+)-(\d{6})\.handoff$"
)
_LEGACY_REMUX_RE = re.compile(r"^aircheck-remux-(\d+)\.aac$")

QUARANTINE_DIR_NAME = "_unknown"


def _quarantine_root():
    cfg = AircheckConfig.load()
    return Path(cfg.output_directory) / recorder.STAGING_ROOT_NAME / QUARANTINE_DIR_NAME


def _quarantine_unknown(path, reason):
    """Preserve, never guess, never delete. A safe (non-traversing,
    collision-free) name derived only from the artifact's own basename
    plus a discovery timestamp -- the original path is never trusted
    for anything beyond its basename."""
    root = _quarantine_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        safe_name = f"{int(time.time())}-{path.name}"
        dest = root / safe_name
        if path.is_dir():
            shutil.move(str(path), str(dest))
        else:
            shutil.move(str(path), str(dest))
    except OSError as exc:
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck unknown recovery artifact could not be quarantined",
            detail={"path": path.name, "reason": reason, "error": str(exc)},
            dedupe_key=f"aircheck|quarantine-failed|{path.name}",
        )
        return None
    emit_event(
        category="aircheck", level="warning",
        title="Aircheck unknown recovery artifact quarantined",
        detail={"artifact": path.name, "reason": reason, "quarantined_as": dest.name},
        dedupe_key=f"aircheck|quarantine|{path.name}",
    )
    return dest


def _safe_relative_to(path, root):
    """True only if `path` genuinely resolves under `root` -- refuses a
    symlink or crafted path that would otherwise point mutation
    operations outside the Aircheck staging/recovery namespace."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def evacuate_stranded_handoffs(active_session_id=None):
    """Move every /run Aircheck handoff NOT owned by the currently
    active session into its owning session's persistent recovery/
    staging directory, independent of still_running.

    This is exactly the Pass-A-review carry-forward gap: an ordinary
    (non-segmented) direct-move failure at Stop can leave a handoff for
    an already-*stopped* session, which the active-session
    reconciliation path (recorder.maintain_idle_buffer /
    _recover_pending_handoffs, unchanged by this module) never revisits
    because it only ever looks at the single currently-running session.

    active_session_id is excluded deliberately: that one is already
    fully owned by Pass A's own per-minute reconciliation; touching it
    here would race that established, unmodified path for no benefit."""
    evacuated = []
    working = Path(recorder.AIRCHECK_CURRENT_PATH)
    try:
        candidates = sorted(working.parent.glob(f"{recorder.HANDOFF_NAME_PREFIX}*.handoff"))
    except OSError as exc:
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck recovery could not list /run for handoff evacuation",
            detail={"error": str(exc)},
            dedupe_key="aircheck|run-listing-failed",
        )
        return evacuated
    for path in candidates:
        if not _safe_relative_to(path, working.parent):
            continue
        match = _HANDOFF_RE.match(path.name)
        if not match:
            _quarantine_unknown(path, "unparseable handoff filename")
            continue
        session_id, sequence = int(match.group(1)), int(match.group(2))
        if session_id == active_session_id:
            continue
        session = AircheckSession.objects.filter(id=session_id).first()
        if session is None:
            _quarantine_unknown(path, f"no AircheckSession id={session_id}")
            continue
        try:
            dest = recorder._copy_handoff_to_staging(session, sequence, path)
        except recorder.SegmentError as exc:
            emit_event(
                category="aircheck", level="warning",
                title="Aircheck stranded handoff evacuation failed",
                detail={"session_id": session_id, "sequence": sequence, "error": str(exc)[:300]},
                dedupe_key=f"aircheck|handoff-evacuate-failed|{session_id}-{sequence}",
            )
            continue
        evacuated.append(dest)
        emit_event(
            category="aircheck", level="info",
            title="Aircheck stranded handoff evacuated",
            detail={"session_id": session_id, "sequence": sequence},
            dedupe_key=f"aircheck|handoff-evacuated|{session_id}-{sequence}",
        )
    return evacuated


def evacuate_legacy_remux_artifacts():
    """Move every failed legacy short-path HE-AAC intermediate
    (aircheck-remux-<session-id>.aac, left in place only on remux
    failure by the unmodified recorder._remux_worker) into that
    session's persistent recovery/staging directory as a single-file
    recovery set, so this directly-recoverable ADTS AAC audio no longer
    lives only in tmpfs indefinitely.

    A session whose remux is still genuinely in flight keeps
    exit_note == recorder.REMUX_PENDING_NOTE for the whole duration
    (recorder._remux_worker only ever changes it once, on completion or
    failure) -- the same liveness signal recorder.classify_finalization
    already relies on for this exact path, reused here rather than
    inventing a second one. Never touched while still pending."""
    evacuated = []
    try:
        candidates = sorted(recorder.REMUX_INTERMEDIATE_DIR.glob("aircheck-remux-*.aac"))
    except OSError as exc:
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck recovery could not list /run for legacy HE-AAC evacuation",
            detail={"error": str(exc)},
            dedupe_key="aircheck|run-listing-failed-legacy",
        )
        return evacuated
    for path in candidates:
        if not _safe_relative_to(path, recorder.REMUX_INTERMEDIATE_DIR):
            continue
        match = _LEGACY_REMUX_RE.match(path.name)
        if not match:
            _quarantine_unknown(path, "unparseable legacy remux filename")
            continue
        session_id = int(match.group(1))
        session = AircheckSession.objects.filter(id=session_id).first()
        if session is None:
            _quarantine_unknown(path, f"no AircheckSession id={session_id}")
            continue
        if session.exit_note == recorder.REMUX_PENDING_NOTE:
            continue  # still genuinely in flight -- never touch
        staging = recorder._staging_dir(session)
        try:
            staging.mkdir(parents=True, exist_ok=True)
            committed = staging / f"legacy-remux-source{path.suffix}"
            partial = staging / f".legacy-remux-source{path.suffix}{recorder.SEGMENT_PARTIAL_SUFFIX}"
            if committed.exists():
                if committed.stat().st_size != path.stat().st_size:
                    raise OSError(f"legacy recovery collision at {committed}")
                path.unlink()
            else:
                with open(path, "rb") as src, open(partial, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(partial, committed)
                recorder._fsync_directory(staging)
                path.unlink()
        except OSError as exc:
            emit_event(
                category="aircheck", level="warning",
                title="Aircheck legacy HE-AAC recovery evacuation failed",
                detail={"session_id": session_id, "error": str(exc)[:300]},
                dedupe_key=f"aircheck|legacy-evacuate-failed|{session_id}",
            )
            continue
        evacuated.append(committed)
        emit_event(
            category="aircheck", level="info",
            title="Aircheck legacy HE-AAC recovery evacuated",
            detail={"session_id": session_id},
            dedupe_key=f"aircheck|legacy-evacuated|{session_id}",
        )
    return evacuated


# --- Retention policy ------------------------------------------------

# Minimum time a FAILED_RECOVERABLE (or quarantined) recovery set must
# survive after its most recent write, regardless of byte-budget
# pressure. A conservative default for radio operations generally --
# not tuned to any one station's current free space -- so an operator
# has real wall-clock time (spanning a missed overnight shift or a
# weekend) to notice a failure and manually recover source audio before
# this module will ever delete it. Overridable per-installation via
# Django settings (see the getattr default below) without a migration.
RETENTION_MIN_SAFETY_SECONDS = int(
    getattr(settings, "AIRCHECK_RECOVERY_MIN_SAFETY_SECONDS", 72 * 3600)
)  # 72h

# Ordinary age at which a failed/quarantined recovery set becomes
# eligible for cleanup even without byte-budget pressure.
RETENTION_MAX_AGE_SECONDS = int(
    getattr(settings, "AIRCHECK_RECOVERY_MAX_AGE_SECONDS", 14 * 24 * 3600)
)  # 14 days

# Aggregate byte budget across every FAILED_RECOVERABLE + quarantined
# recovery set. A single WAV failure can be many GiB, so bytes -- not a
# count -- are the primary pressure signal; count is reported for
# operator visibility only and never used as a standalone eligibility
# test on its own.
RETENTION_BYTE_BUDGET_BYTES = int(
    getattr(settings, "AIRCHECK_RECOVERY_BYTE_BUDGET_BYTES", 20 * 1024 * 1024 * 1024)
)  # 20 GiB


@dataclass
class RecoverySet:
    kind: str  # "segmented_failed" | "legacy_remux_failed" | "quarantine_unknown"
    session_id: Optional[int]
    path: Path
    size_bytes: int
    age_seconds: float
    reason: str
    extra_paths: list = field(default_factory=list)


def _dir_size(path):
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _dir_newest_mtime(path):
    newest = None
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
    except OSError:
        pass
    return newest


def _partial_dest_path(session):
    """Same deterministic name recorder._finalize_segment_set itself
    uses for its temporary, pre-publish final-output candidate -- never
    reimplemented, just referenced, so this module can find a stray one
    left over from a crash mid-concat/validate. Not independently
    valuable: it is always rebuilt from staging segments on the very
    next finalization attempt (_finalize_segment_set unlinks any stale
    one before writing a fresh one), so it is safe to bound it together
    with -- never separately from -- its session's own recovery set."""
    dest = Path(session.filename)
    return dest.parent / f".{dest.stem}.aircheck-partial-{session.id}{dest.suffix}"


def _is_finalization_locked(session):
    """Non-mutating probe: True iff some other worker currently holds
    this session's finalization lock. Never itself holds the lock."""
    with finalization_lock(session, blocking=False) as acquired:
        return not acquired


def _iter_failed_recovery_sets():
    """Every FAILED_RECOVERABLE staging directory currently on disk,
    discovered from AircheckSession rows -- never from blind filesystem
    globbing under one assumed root, since staging is anchored to each
    session's OWN immutable destination parent (recorder._staging_dir),
    which can differ across an AircheckConfig.output_directory change
    made after older sessions were created."""
    now = time.time()
    sets = []
    qs = (
        AircheckSession.objects.filter(still_running=False)
        .exclude(exit_note="")
        .exclude(exit_note=recorder.FINALIZATION_PENDING_NOTE)
        .exclude(exit_note=recorder.REMUX_PENDING_NOTE)
    )
    for session in qs.iterator():
        staging = recorder._staging_dir(session)
        partial = _partial_dest_path(session)
        has_staging = staging.is_dir()
        has_partial = partial.is_file()
        if not has_staging and not has_partial:
            continue
        # A live finalization_lock means some worker -- automatic or
        # manual -- is actively deciding this session's fate right now;
        # retention must never touch material underneath that work.
        if _is_finalization_locked(session):
            continue
        size = (_dir_size(staging) if has_staging else 0)
        newest = _dir_newest_mtime(staging) if has_staging else None
        extra_paths = []
        if has_partial:
            try:
                pstat = partial.stat()
                size += pstat.st_size
                if newest is None or pstat.st_mtime > newest:
                    newest = pstat.st_mtime
                extra_paths.append(partial)
            except OSError:
                pass
        age = (now - newest) if newest is not None else 0.0
        kind = "segmented_failed" if recorder._discover_segments(session) else "legacy_remux_failed"
        sets.append(RecoverySet(
            kind=kind, session_id=session.id, path=staging, size_bytes=size,
            age_seconds=age, reason=session.exit_note, extra_paths=extra_paths,
        ))
    return sets


def _iter_quarantine_sets():
    root = _quarantine_root()
    if not root.is_dir():
        return []
    now = time.time()
    sets = []
    for path in root.iterdir():
        try:
            stat = path.stat()
        except OSError:
            continue
        size = _dir_size(path) if path.is_dir() else stat.st_size
        sets.append(RecoverySet(
            kind="quarantine_unknown", session_id=None, path=path,
            size_bytes=size, age_seconds=now - stat.st_mtime, reason="unknown ownership",
        ))
    return sets


def _delete_recovery_set(recovery_set, reason):
    try:
        if recovery_set.path.is_dir():
            shutil.rmtree(recovery_set.path)
        elif recovery_set.path.is_file():
            recovery_set.path.unlink()
        for extra in recovery_set.extra_paths:
            extra.unlink(missing_ok=True)
    except OSError as exc:
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck recovery retention cleanup failed",
            detail={
                "session_id": recovery_set.session_id,
                "path": recovery_set.path.name, "error": str(exc),
            },
            dedupe_key=f"aircheck|retention-cleanup-failed|{recovery_set.session_id}-{recovery_set.path.name}",
        )
        return False
    emit_event(
        category="aircheck", level="warning",
        title="Aircheck recovery material deleted by retention",
        detail={
            "session_id": recovery_set.session_id,
            "kind": recovery_set.kind,
            "age_seconds": round(recovery_set.age_seconds),
            "bytes": recovery_set.size_bytes,
            "reason": reason,
        },
        dedupe_key=f"aircheck|retention-deleted|{recovery_set.session_id}-{recovery_set.kind}-{recovery_set.path.name}",
    )
    return True


def collect_retention(*, dry_run=False):
    """Bound FAILED_RECOVERABLE and quarantined recovery sets by age
    and aggregate bytes. ACTIVE/PENDING/locked material is never even
    considered (see _iter_failed_recovery_sets). A whole recovery set
    (a session's entire staging directory, or one quarantined artifact)
    is retired atomically -- never a partial segment prune. Oldest
    eligible sets are retired first under budget pressure; a set still
    inside its minimum safety window is never touched regardless of
    budget. dry_run=True performs the full scan/decision and makes zero
    mutations."""
    sets = _iter_failed_recovery_sets() + _iter_quarantine_sets()
    sets.sort(key=lambda s: -s.age_seconds)  # oldest (largest age) first

    total_bytes = sum(s.size_bytes for s in sets)
    running_total = total_bytes
    deleted = []
    kept = []
    for recovery_set in sets:
        if recovery_set.age_seconds < RETENTION_MIN_SAFETY_SECONDS:
            kept.append(recovery_set)
            continue
        age_expired = recovery_set.age_seconds >= RETENTION_MAX_AGE_SECONDS
        over_budget = running_total > RETENTION_BYTE_BUDGET_BYTES
        if not (age_expired or over_budget):
            kept.append(recovery_set)
            continue
        reason = "age_expired" if age_expired else "recovery_budget_pressure"
        if dry_run or _delete_recovery_set(recovery_set, reason):
            deleted.append((recovery_set, reason))
            running_total -= recovery_set.size_bytes
        else:
            kept.append(recovery_set)

    return {
        "total_sets": len(sets), "total_bytes": total_bytes,
        "deleted_sets": len(deleted), "deleted_bytes": sum(s.size_bytes for s, _ in deleted),
        "kept_sets": len(kept), "kept_bytes": sum(s.size_bytes for s in kept),
        "deleted": deleted, "kept": kept, "dry_run": dry_run,
    }


def collect_stale_success_cleanup(*, dry_run=False):
    """A staging directory can outlive a SUCCESSFUL finalization only
    in narrow edge cases (e.g. _finalize_segment_set's own best-effort
    final rmdir call failing because something else briefly occupied
    the directory). Only ever removes a session's staging directory
    when its exit_note is genuinely clean AND its destination
    independently re-validates via the same full-decode check this
    module already requires before any publish -- never from bare file
    existence, and never while finalization_lock is held."""
    removed = []
    qs = AircheckSession.objects.filter(still_running=False, exit_note="")
    for session in qs.iterator():
        staging = recorder._staging_dir(session)
        if not staging.is_dir():
            continue
        if _is_finalization_locked(session):
            continue
        try:
            recorder._validate_final_audio(Path(session.filename))
        except recorder.SegmentError:
            continue  # not actually proven successful -- never touch
        if not dry_run:
            try:
                shutil.rmtree(staging)
            except OSError:
                continue
        removed.append(staging)
    return removed


# --- Inventory / observability -----------------------------------------

def _run_handoff_bytes(active_session_id=None):
    working = Path(recorder.AIRCHECK_CURRENT_PATH)
    count = 0
    total = 0
    try:
        paths = working.parent.glob(f"{recorder.HANDOFF_NAME_PREFIX}*.handoff")
    except OSError:
        return 0, 0
    for path in paths:
        match = _HANDOFF_RE.match(path.name)
        if match and int(match.group(1)) == active_session_id:
            continue  # still legitimately part of the live active session
        try:
            total += path.stat().st_size
        except OSError:
            continue
        count += 1
    return count, total


def _pending_finalization_summary():
    qs = AircheckSession.objects.filter(
        still_running=False, exit_note=recorder.FINALIZATION_PENDING_NOTE,
    )
    return {"count": qs.count(), "oldest_ended_at": (
        qs.order_by("ended_at").values_list("ended_at", flat=True).first()
    )}


def inventory_summary(*, active_session_id=None):
    """One-shot, read-only snapshot of every recovery-relevant
    quantity this module tracks -- the shared source of truth for
    `maintain_aircheck_recovery --dry-run`, the Aircheck status API,
    and the maintenance heartbeat. Never mutates anything."""
    handoff_count, handoff_bytes = _run_handoff_bytes(active_session_id)
    failed_sets = _iter_failed_recovery_sets()
    quarantine_sets = _iter_quarantine_sets()
    pending = _pending_finalization_summary()

    legacy_remux_count = 0
    try:
        legacy_remux_count = sum(
            1 for p in recorder.REMUX_INTERMEDIATE_DIR.glob("aircheck-remux-*.aac")
        )
    except OSError:
        pass

    oldest_failed_age = max((s.age_seconds for s in failed_sets), default=None)

    return {
        "pending_finalizations": pending,
        "run_handoffs": {"count": handoff_count, "bytes": handoff_bytes},
        "run_legacy_remux_artifacts": legacy_remux_count,
        "failed_recovery": {
            "count": len(failed_sets),
            "bytes": sum(s.size_bytes for s in failed_sets),
            "oldest_age_seconds": oldest_failed_age,
        },
        "quarantine": {
            "count": len(quarantine_sets),
            "bytes": sum(s.size_bytes for s in quarantine_sets),
        },
        "retention_policy": {
            "min_safety_seconds": RETENTION_MIN_SAFETY_SECONDS,
            "max_age_seconds": RETENTION_MAX_AGE_SECONDS,
            "byte_budget_bytes": RETENTION_BYTE_BUDGET_BYTES,
        },
    }


# --- Default /run capacity MonitorCheck provisioning (P2 1.13B2) --------
#
# Originally a RunPython data migration (monitoring/migrations/0014_
# seed_run_tmpfs_disk_check.py, P2 1.13B). Removed: RunPython falls
# outside Update Center's Phase B v1 automatic-migration allowlist (see
# updatecenter/management/commands/updatecenter_probe.py's
# _classify_operation -- anything that isn't CreateModel/AddField/
# AlterField classifies "manual"), so a pure default-row-seeding step
# would have forced this entire release to require manual review to
# deploy for no schema reason. monitoring/migrations/0013's own history
# already established the fix for this exact situation: seed default
# configuration through a normal runtime/activation path instead of a
# migration. Here, that path is the existing recovery-maintenance
# timer this same roadmap item already introduced.

RUN_TMPFS_MONITOR_CHECK_NAME = "Disk: /run (runtime tmpfs)"
RUN_TMPFS_MONITOR_CHECK_DEFAULTS = {
    "kind": "disk",
    "sort_order": 70,
    "disk_path": "/run",
    "warning_threshold": 60.0,
    "critical_threshold": 75.0,
}


def ensure_run_tmpfs_monitor_check():
    """Idempotently provisions the default "/run" capacity MonitorCheck.
    Called only from the non-dry-run recovery-maintenance path (never
    at import time, never from a model save(), never from a request
    handler, never from dry-run) -- see run_recovery_maintenance.

    Semantic-duplicate policy: a single kind="disk", disk_path="/run"
    query covers both "the canonical named row already exists" (it
    necessarily has this same kind/path) and "an operator already has
    their own equivalent /run disk check under a different name" --
    either way, nothing is created. Falling through to get_or_create
    keyed on the canonical name is still name-idempotent on its own
    even in the edge case where an operator kept the canonical row but
    retargeted ITS disk_path elsewhere. No field on an existing row is
    ever touched -- only genuine absence of any /run disk check at all
    is filled.

    Never raises: a failure here must never abort the rest of recovery
    maintenance (see run_recovery_maintenance) -- preservation of
    recoverable audio does not depend on a dashboard check existing.
    Returns True if a row was created, False otherwise."""
    from monitoring.models import MonitorCheck  # lazy: keeps this aircheck module's monitoring dependency narrow and deliberate (matches its existing emit_event import), never a module-level coupling beyond what already exists

    try:
        if MonitorCheck.objects.filter(kind="disk", disk_path="/run").exists():
            return False
        _, created = MonitorCheck.objects.get_or_create(
            name=RUN_TMPFS_MONITOR_CHECK_NAME,
            defaults=RUN_TMPFS_MONITOR_CHECK_DEFAULTS,
        )
        return created
    except Exception as exc:
        emit_event(
            category="aircheck", level="warning",
            title="Aircheck could not provision default /run capacity monitor",
            detail={"error": str(exc)[:300]},
            dedupe_key="aircheck|run-monitor-provision-failed",
        )
        return False


# --- Maintenance-cadence entry points ------------------------------------

def run_bounded_reconciliation(active_session_id=None):
    """Called every minute from maintain_aircheck_buffer, right after
    Pass A's own (unmodified) idle/active-segment cut logic. Only ever
    does bounded, single-file-copy-class work -- evacuating whatever
    /run handoffs and legacy HE-AAC intermediates are currently
    stranded -- the same class of cost as the active-segment cut this
    timer already performs every cycle. Never retries a finalization
    (that can legitimately take hours) and never runs retention
    scanning/deletion (see run_recovery_maintenance)."""
    handoffs = evacuate_stranded_handoffs(active_session_id=active_session_id)
    legacy = evacuate_legacy_remux_artifacts()
    return {"handoffs_evacuated": len(handoffs), "legacy_evacuated": len(legacy)}


def run_recovery_maintenance(*, dry_run=False):
    """Called from the separate, less-frequent maintain_aircheck_recovery
    timer/command -- see that command's own docstring for why this is a
    second timer rather than folded into the one-minute buffer timer:
    pending-finalization retry can legitimately run for hours
    (recorder.FFMPEG_TIMEOUT_SECONDS + the decode-validation ceiling),
    and a systemd Type=oneshot unit's own process lifetime IS the
    timer's busy window -- letting that run inside the one-minute unit
    would silently suspend routine /run reconciliation for the same
    duration. This function performs (in order): default /run capacity
    MonitorCheck provisioning (P2 1.13B2 -- never on a dry run),
    automatic pending-finalization retry, stale-success staging
    cleanup, and bounded age/byte retention -- each mediated by
    finalization_lock or real Pass-A/A2 validation, never by
    heuristic. ensure_run_tmpfs_monitor_check() already never raises on
    its own; the extra try/except here is deliberate defense in depth
    -- preservation of recoverable audio must never depend on a
    dashboard check successfully existing, so even an unanticipated
    failure at this call site (not just the ordinary DB/validation
    errors the helper already catches) can never skip retry/retention.
    Ordered first only so the dashboard gains visibility as early in a
    maintenance cycle as possible."""
    monitor_check_created = False
    retry_result = {"candidates": 0, "succeeded": 0, "failed": 0, "skipped_locked": 0}
    stale_removed = []
    if not dry_run:
        try:
            monitor_check_created = ensure_run_tmpfs_monitor_check()
        except Exception as exc:
            emit_event(
                category="aircheck", level="warning",
                title="Aircheck /run capacity monitor provisioning failed unexpectedly",
                detail={"error": str(exc)[:300]},
                dedupe_key="aircheck|run-monitor-provision-unexpected-failure",
            )
        retry_result = retry_pending_finalizations()
        stale_removed = collect_stale_success_cleanup(dry_run=False)
    retention_result = collect_retention(dry_run=dry_run)
    return {
        "run_tmpfs_monitor_check_created": monitor_check_created,
        "retry": retry_result,
        "stale_success_cleaned": len(stale_removed),
        "retention": retention_result,
    }
