"""Retention of active-message artifacts (iPortal D3E-C): the ONE authority on
what may be reclaimed, and the only code that reclaims it.

Two different things are kept apart throughout:

* storage reclamation -- bytes on disk (a published library file, its
  waveform, interrupted-publication scratch, unbound aggregate bytes); and
* historical evidence -- database rows, which are NEVER deleted or rewritten
  here: executions (frozen snapshot, identity, generation, digest, state
  history), the append-only events, publications and insertions (with their
  Track / LogItem id snapshots), LogItems, PlayEvents, ProductionMedia rows
  and their hashes/provenance.

What can be reclaimed (each only with ``apply=True``; dry-run otherwise):

1. Unbound aggregate renditions -- an ``active_message_mix`` rendition no
   durable row references (the worker crashed between render and bind, or
   the composition moved on). Purged through
   ``production.services.retention.purge_media`` (bytes only, row kept), whose
   row lock and reference check are the authority: a worker binding it at the
   same moment either sees the purge (``rendition_unavailable``: it simply
   renders again) or wins and the purge refuses.
2. Interrupted-publication leftovers in the reserved ``UrgentPA`` namespace:
   ``.am-publish-*`` scratch directories, ``.am-<hex>.flac.publish.lock``
   sidecars, and placed ``am-<hex>.flac`` files with no publication row whose
   execution can no longer adopt them (not READY). Never a READY execution's
   placed file (publish reuses it), never inside the grace period, always
   under the destination lock publish itself takes.
3. Retired publications' remaining bytes (crash recovery of 4).
4. Publication retirement -- ONLY for executions an operator names
   explicitly; never by age. See ``retire_publication``.

Everything else is retained deliberately: a bound aggregate (``mix_media``,
``ActiveBroadcastPublication.media``) and message media are PROTECT-referenced
by history and stay; reference/stinger media are the ReferenceAudio
registry's; LogItems and PlayEvents belong to the library's own retention.

Lock order (unchanged): policy -> execution -> library Track. No filesystem
work (stat aside) happens while the policy lock is held; files are removed
after the durable transition commits, under the publication destination
lock. A crash anywhere leaves either the pre-retirement state (rolled back)
or a retired publication whose bytes may remain -- reported by the reconciler
and finished idempotently by ``finish_retirements`` -- never a publication
claiming bytes that are gone.
"""
from __future__ import annotations

import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from production.errors import PurgeRefused
from production.models import ProductionMedia
from production.services import reconcile as production_reconcile
from production.services import retention as production_retention

from . import playout, rendering, services
from .models import (
    TERMINAL_EXECUTION_STATES, UNFINISHED_EXECUTION_STATES, ActiveBroadcastExecution, ActiveBroadcastInsertion,
    ActiveBroadcastMessageEvent, ActiveBroadcastPublication, ActorKind, ExecutionState,
)

RETIRED_EVENT = "publication_retired"
DEFAULT_GRACE = production_reconcile.DEFAULT_GRACE      # 24 h, as Production's sweeps
MIN_GRACE = production_reconcile.MIN_GRACE              # 1 h floor: never races an in-flight publish
DEFAULT_LIMIT = 200
MAX_LIMIT = 5000
MAX_NAMESPACE_ENTRIES = 10000
TERMINAL_EVIDENCE = frozenset({"complete", "interrupted"})
SCRATCH_PREFIX = ".am-publish-"
NATIVE_NAME = playout._NATIVE_NAME                      # am-<32 hex>.flac


class RetentionUnavailable(Exception):
    """Evidence needed for a safe decision is unavailable: refuse."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Assessment:
    """``eligible`` only when ``reasons`` is empty; reasons are stable codes."""
    reasons: tuple = ()

    @property
    def eligible(self) -> bool:
        return not self.reasons


@dataclass
class Outcome:
    artifact: str
    identity: str
    action: str             # retired | purged | removed | eligible | already_retired | refused | failed | kept
    reasons: tuple = ()
    bytes_reclaimed: int = 0
    bytes_reclaimable: int = 0


@dataclass
class CleanupReport:
    apply: bool
    outcomes: list = field(default_factory=list)

    def add(self, outcome: Outcome) -> Outcome:
        self.outcomes.append(outcome)
        return outcome

    def count(self, action: str) -> int:
        return sum(1 for item in self.outcomes if item.action == action)

    @property
    def bytes_reclaimed(self) -> int:
        return sum(item.bytes_reclaimed for item in self.outcomes)

    @property
    def bytes_reclaimable(self) -> int:
        return sum(item.bytes_reclaimable for item in self.outcomes)

    @property
    def failures(self) -> list:
        return [item for item in self.outcomes if item.action == "failed"]


def _limit(limit) -> int:
    limit = DEFAULT_LIMIT if limit is None else int(limit)
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be from 1 to {MAX_LIMIT}")
    return limit


def _grace(grace) -> timedelta:
    grace = DEFAULT_GRACE if grace is None else grace
    if grace < MIN_GRACE:
        raise ValueError("grace period must be at least one hour")
    return grace


# -- the reserved library namespace ---------------------------------------------------------------

def library_root() -> Path:
    """The configured library root, unresolved (a symlinked root is refused
    by ``namespace_dir``)."""
    return Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))


def waveforms_dir() -> Path:
    """WAVEFORMS_DIR, never created here (analyze_tracks.get_waveforms_dir
    would mkdir it)."""
    return Path(getattr(settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"))


def namespace_dir() -> Path:
    """``LIBRARY_ROOT/UrgentPA`` -- only if it is a real directory directly
    under a real library root (no symlink at either level, so a substituted
    path can never redirect a removal). RetentionUnavailable otherwise."""
    root = library_root()
    try:
        root_info = os.lstat(root)
    except OSError:
        raise RetentionUnavailable("library_unavailable") from None
    if not stat.S_ISDIR(root_info.st_mode):
        raise RetentionUnavailable("library_root_not_a_directory")
    directory = root / playout.CATEGORY_CODE
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        raise RetentionUnavailable("namespace_missing") from None
    except OSError:
        raise RetentionUnavailable("library_unavailable") from None
    if not stat.S_ISDIR(info.st_mode):
        raise RetentionUnavailable("namespace_not_a_directory")
    return directory


def native_name(relpath: str) -> str:
    """The file name of a publication relpath, proven to be the reserved shape."""
    category, _, name = str(relpath).partition("/")
    if category != playout.CATEGORY_CODE or not NATIVE_NAME.fullmatch(name):
        raise RetentionUnavailable("relpath_not_reserved")
    return name


def execution_id_for(name: str) -> uuid.UUID:
    return uuid.UUID(name[3:35])


def file_state(path: Path):
    """(state, size): state is "regular", "missing", "not_regular" or
    "unreadable"; never follows a symlink."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing", 0
    except OSError:
        return "unreadable", 0
    if not stat.S_ISREG(info.st_mode):
        return "not_regular", 0
    return "regular", info.st_size


def _age_seconds(path: Path, now) -> float | None:
    """Age from the later of mtime and ctime of the entry and (for a
    directory) everything below it, without following symlinks."""
    newest = 0.0
    try:
        info = os.lstat(path)
    except OSError:
        return None
    newest = max(info.st_mtime, info.st_ctime)
    if stat.S_ISDIR(info.st_mode):
        for current, directories, names in os.walk(path, followlinks=False):
            for name in (*directories, *names):
                try:
                    child = os.lstat(os.path.join(current, name))
                except OSError:
                    continue
                newest = max(newest, child.st_mtime, child.st_ctime)
    return now.timestamp() - newest


def _tree_size(path) -> int:
    total = 0
    for current, _directories, names in os.walk(path, followlinks=False):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def is_retired(execution_id) -> bool:
    return ActiveBroadcastMessageEvent.objects.filter(execution_id=execution_id, event=RETIRED_EVENT).exists()


def retired_execution_ids(execution_ids) -> set:
    return set(ActiveBroadcastMessageEvent.objects.filter(execution_id__in=list(execution_ids), event=RETIRED_EVENT)
               .values_list("execution_id", flat=True))


# -- eligibility (the single authority) -----------------------------------------------------------------

def assess_execution(row) -> Assessment:
    """Necessary (never sufficient) condition for retiring anything of an
    execution: it is terminal and holds no open slot. PENDING_RENDER, READY,
    ACCEPTED, CLAIMED and STARTED are never settled -- however old (an
    accepted or claimed item may legitimately wait any length of time)."""
    if row.state in UNFINISHED_EXECUTION_STATES:
        return Assessment((f"execution_{row.state}",))
    if row.state not in TERMINAL_EXECUTION_STATES or row.open_slot is not None:
        return Assessment(("execution_not_settled",))
    return Assessment()


def _air_evidence_reasons(track_id, log_item_ids) -> list:
    """Reasons a deck, a claim or air evidence may still involve this Track:
    any LogItem naming it (or any insertion's LogItem) that a deck claimed or
    played without TERMINAL PlayEvent evidence, and any non-terminal PlayEvent
    of the Track. An unclaimed, unplayed LogItem is not a consumer: deck-open
    authority refuses every native LogItem that is not the current attempt of
    a live execution."""
    from library.models import LogItem, PlayEvent
    reasons = []
    clauses = Q(pk__in=list(log_item_ids))
    if track_id is not None:
        clauses |= Q(track_id=track_id)
    items = list(LogItem.objects.filter(clauses).values_list("pk", "playback_claimed_at", "played_at")[:1001])
    if len(items) > 1000:
        return ["evidence_too_large"]
    touched = [pk for pk, claimed, played in items if claimed is not None or played is not None]
    states = dict(PlayEvent.objects.filter(log_item_id_snapshot__in=touched)
                  .values_list("log_item_id_snapshot", "duration_evidence_state"))
    for pk in touched:
        if pk not in states:
            reasons.append("deck_claim_unresolved")
        elif states[pk] not in TERMINAL_EVIDENCE:
            reasons.append("air_evidence_open")
    if track_id is not None and PlayEvent.objects.filter(track_id=track_id).exclude(
            duration_evidence_state__in=TERMINAL_EVIDENCE).exists():
        reasons.append("air_evidence_open")
    return sorted(set(reasons))


def _track_collector(track):
    """(reasons, collector): would deleting this Track delete or be refused by
    anything besides history references that keep their own snapshots
    (SET_NULL)? Playlist/rotation PROTECT, a voice track or any other CASCADE
    dependent blocks retirement -- the library's own reference policy decides.
    The returned collector is exactly what was vetted: deleting through it
    deletes nothing else (a dependent created meanwhile fails the deferred
    foreign key at commit, rolling the retirement back)."""
    from django.db import DEFAULT_DB_ALIAS
    from django.db.models.deletion import Collector, ProtectedError, RestrictedError
    from library.models import Track
    collector = Collector(using=DEFAULT_DB_ALIAS)
    try:
        collector.collect([track])
    except (ProtectedError, RestrictedError):
        return ["track_protected"], None
    for model, instances in collector.data.items():
        if model is not Track and instances:
            return ["track_has_dependents"], None
        if model is Track and {item.pk for item in instances} != {track.pk}:
            return ["track_has_dependents"], None
    for queryset in collector.fast_deletes:
        if queryset.exists():
            return ["track_has_dependents"], None
    return [], collector


def _track_dependency_reasons(track) -> list:
    return _track_collector(track)[0]


def assess_publication_retirement(publication, *, execution=None, track=None) -> Assessment:
    """May this publication's library file, waveform and Track be retired?
    Read-only. Never by age; every reason is positive evidence:

    * the execution is terminal (``assess_execution``) and not already retired;
    * the Track (when it still exists) is exactly this publication's: its id
      snapshot, the reserved path and the candidate name -- a Track that is
      not provably ours is never deleted;
    * no deck claim, play or air evidence on it is unresolved;
    * deleting the Track would neither be refused (playlist / rotation) nor
      cascade into anything (voice tracks, other dependents);
    * the reserved namespace is available and unsubstituted."""
    from library.models import Track
    row = execution or ActiveBroadcastExecution.objects.get(pk=publication.execution_id)
    if is_retired(row.pk):
        return Assessment(("already_retired",))
    reasons = list(assess_execution(row).reasons)
    if track is None and publication.track_id is not None:
        track = Track.objects.filter(pk=publication.track_id).first()
    try:
        directory = namespace_dir()
        name = native_name(publication.library_relpath)
    except RetentionUnavailable as exc:
        return Assessment(tuple(sorted({*reasons, exc.code})))
    if track is not None:
        if (track.pk != publication.track_id_snapshot or not playout.is_candidate(track)
                or track.filepath != str(playout._library_root() / publication.library_relpath)):
            reasons.append("track_not_publication")
        else:
            reasons.extend(_track_dependency_reasons(track))
    log_items = ActiveBroadcastInsertion.objects.filter(publication=publication).values_list(
        "log_item_id_snapshot", flat=True)
    reasons.extend(_air_evidence_reasons(publication.track_id_snapshot if track is not None else None,
                                         list(log_items)))
    state, _size = file_state(directory / name)
    if state in ("not_regular", "unreadable"):
        reasons.append(f"file_{state}")
    return Assessment(tuple(sorted(set(reasons))))


def is_aggregate(media) -> bool:
    return (media.kind == ProductionMedia.KIND_RENDITION and media.recipe_key == rendering.ASSEMBLY_KEY)


def assess_aggregate_purge(media, *, now=None, grace=None) -> Assessment:
    """May an aggregate rendition's BYTES be purged? Only an unreferenced
    one (Production's own reference discovery: an execution's mix, a
    publication, a derivative -- anything), past the grace period. The final
    word is purge_media's own check under the row lock."""
    now, grace = now or timezone.now(), _grace(grace)
    if not is_aggregate(media):
        return Assessment(("not_active_message_aggregate",))
    if media.retention_state != ProductionMedia.RETENTION_PRESENT:
        return Assessment(("already_purged",))
    reasons = []
    if production_retention.find_references(media):
        reasons.append("referenced")
    if media.created_at > now - grace:
        reasons.append("too_recent")
    return Assessment(tuple(reasons))


# -- publication retirement -----------------------------------------------------------------------------

def _remove_publication_bytes(publication_relpath: str, track_id_snapshot: int):
    """Remove a RETIRED publication's file and waveform, under the
    destination lock publish takes. Returns (bytes_removed, problems).
    Missing files are already done; a symlink or other non-regular entry is
    never followed or removed (reported)."""
    problems, removed = [], 0
    try:
        dest = namespace_dir() / native_name(publication_relpath)
    except RetentionUnavailable as exc:
        return 0, [exc.code]
    try:
        with playout._destination_lock(dest):
            state, size = file_state(dest)
            if state == "regular":
                try:
                    os.unlink(dest)
                    removed += size
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    problems.append(f"file_{type(exc).__name__}")
            elif state != "missing":
                problems.append(f"file_{state}")
    except OSError as exc:                       # the destination lock itself (namespace not writable)
        problems.append(f"file_{type(exc).__name__}")
    wave = waveforms_dir() / f"{int(track_id_snapshot)}.json"
    state, size = file_state(wave)
    if state == "regular":
        try:
            os.unlink(wave)
            removed += size
        except FileNotFoundError:
            pass
        except OSError as exc:
            problems.append(f"waveform_{type(exc).__name__}")
    elif state != "missing":
        problems.append(f"waveform_{state}")
    return removed, problems


def retire_publication(execution_id, *, operator, apply=False) -> Outcome:
    """Retire ONE named execution's publication: its library file, waveform
    and Track. Never selects anything by itself, never by age.

    Durable first: under the policy -> execution locks, re-assess; then, in
    ONE transaction, append ``publication_retired`` (the publication's
    identity, Track id snapshot, SHA-256, relpath) and delete the Track
    through the vetted Collector -- the library's own order (LogItem /
    PlayEvent / publication Track references become NULL, their snapshots
    stay; then the Track), so a concurrent library deletion serializes with
    it instead of deadlocking. Only after that commits are
    the bytes removed. A crash before the commit changes nothing; after it,
    ``finish_retirements`` removes what remains. The publication, its
    insertions, the execution and every event are untouched; a retired
    publication is never redispatched (its execution is terminal) and is
    never reported as unexpected damage."""
    from library.models import Track
    identity = str(execution_id)
    try:
        execution_uuid = uuid.UUID(identity)
    except ValueError:
        return Outcome("publication", identity[:40], "refused", ("unknown_execution",))
    if apply:
        services._require(operator, services.CONTROL)
    publication = ActiveBroadcastPublication.objects.filter(execution_id=execution_uuid).first()
    if publication is None:
        return Outcome("publication", identity, "refused", ("no_publication",))
    if not apply:
        assessment = assess_publication_retirement(publication)
        if assessment.reasons == ("already_retired",):
            return Outcome("publication", identity, "already_retired")
        if not assessment.eligible:
            return Outcome("publication", identity, "refused", assessment.reasons)
        _state, size = file_state(namespace_dir() / native_name(publication.library_relpath))
        return Outcome("publication", identity, "eligible", bytes_reclaimable=size)
    with transaction.atomic():
        services._lock_policy()
        row = services._lock_execution(execution_uuid)
        if is_retired(row.pk):
            if _track_present(publication):
                return Outcome("publication", identity, "refused", ("retired_track_present",))
            retired_now = False
        else:
            # The Track is NOT row-locked first: the library deletes a Track by
            # nulling its referrers and then deleting it, and the collector below
            # takes exactly that order -- a concurrent library deletion and this
            # retirement serialize instead of deadlocking.
            track = Track.objects.filter(pk=publication.track_id).first() if publication.track_id else None
            assessment = assess_publication_retirement(publication, execution=row, track=track)
            if not assessment.eligible:
                return Outcome("publication", identity, "refused", assessment.reasons)
            collector = None
            if track is not None:
                reasons, collector = _track_collector(track)
                if reasons:
                    return Outcome("publication", identity, "refused", tuple(reasons))
            file_present = file_state(namespace_dir() / native_name(publication.library_relpath))[0] == "regular"
            services._event(RETIRED_EVENT, generation=services.current_policy().generation,
                            actor_kind=ActorKind.USER, user=operator, execution=row,
                            detail={"publication": str(publication.pk),
                                    "track": publication.track_id_snapshot,
                                    "library_relpath": publication.library_relpath,
                                    "sha256": publication.sha256,
                                    "track_present": track is not None,
                                    "file_present": file_present})
            if collector is not None:
                collector.delete()                   # SET_NULL referrers, then the Track (signals included)
            retired_now = True
    removed, problems = _remove_publication_bytes(publication.library_relpath, publication.track_id_snapshot)
    if problems:
        return Outcome("publication", identity, "failed", tuple(problems), bytes_reclaimed=removed)
    return Outcome("publication", identity, "retired" if retired_now else "already_retired",
                   bytes_reclaimed=removed)


def _track_present(publication) -> bool:
    """A Track exists again for a retired publication (its FK or its id)."""
    from library.models import Track
    current = ActiveBroadcastPublication.objects.filter(pk=publication.pk).values_list("track_id", flat=True).first()
    return current is not None or Track.objects.filter(pk=publication.track_id_snapshot).exists()


def finish_retirements(*, apply=False, limit=None) -> CleanupReport:
    """Crash recovery: remove the remaining file / waveform of RETIRED
    publications (idempotent). A retired publication whose Track exists again
    is refused (inconsistent; operator attention)."""
    report, limit = CleanupReport(apply=apply), _limit(limit)
    retired = (ActiveBroadcastMessageEvent.objects.filter(event=RETIRED_EVENT)
               .values_list("execution_id", flat=True).order_by("id"))
    publications = ActiveBroadcastPublication.objects.filter(execution_id__in=retired).order_by("published_at", "id")
    for publication in publications[:limit]:
        identity = str(publication.execution_id)
        try:
            dest = namespace_dir() / native_name(publication.library_relpath)
        except RetentionUnavailable as exc:
            report.add(Outcome("retired_publication", identity, "failed", (exc.code,)))
            continue
        wave = waveforms_dir() / f"{publication.track_id_snapshot}.json"
        remaining = [(p, s) for p, s in (file_state(dest), file_state(wave)) if p != "missing"]
        if not remaining:
            continue
        if _track_present(publication):
            report.add(Outcome("retired_publication", identity, "refused", ("retired_track_present",)))
            continue
        size = sum(s for _p, s in remaining)
        if not apply:
            report.add(Outcome("retired_publication", identity, "eligible", bytes_reclaimable=size))
            continue
        removed, problems = _remove_publication_bytes(publication.library_relpath, publication.track_id_snapshot)
        report.add(Outcome("retired_publication", identity, "failed" if problems else "removed", tuple(problems),
                           bytes_reclaimed=removed))
    return report


# -- interrupted-publication leftovers --------------------------------------------------------------------

def _unadoptable(execution_id) -> bool:
    """A placed file of this execution can no longer be adopted by publish
    (which needs a READY execution and adopts only its own exact bytes)."""
    state = ActiveBroadcastExecution.objects.filter(pk=execution_id).values_list("state", flat=True).first()
    return state != ExecutionState.READY


def sweep_publication_namespace(*, apply=False, grace=None, now=None, limit=None) -> CleanupReport:
    """Interrupted-publication leftovers in ``LIBRARY_ROOT/UrgentPA``
    (bounded scan; only the reserved shapes are considered -- legacy
    OGRemote files and every other name are never touched):

    * ``am-<hex>.flac`` with NO publication row, whose execution is absent or
      not READY, older than the grace: removed under the destination lock
      after re-checking both (a READY execution's file is kept: publish adopts
      it);
    * ``.am-publish-*`` scratch directories older than the grace;
    * ``.am-<hex>.flac.publish.lock`` sidecars older than the grace, for an
      execution that cannot publish, removed only while their flock is held.
    Symlinks and other non-regular entries are reported, never followed."""
    import fcntl
    report, grace, now, limit = CleanupReport(apply=apply), _grace(grace), now or timezone.now(), _limit(limit)
    try:
        directory = namespace_dir()
    except RetentionUnavailable as exc:
        if exc.code == "namespace_missing":          # nothing was ever published here: nothing to sweep
            return report
        raise
    entries = []
    with os.scandir(directory) as scan:
        for count, entry in enumerate(scan):
            if count >= MAX_NAMESPACE_ENTRIES:
                report.add(Outcome("namespace", "UrgentPA", "refused", ("scan_truncated",)))
                break
            entries.append(entry.name)
    handled, publishing = 0, None
    published = set(ActiveBroadcastPublication.objects.filter(
        library_relpath__in=[f"{playout.CATEGORY_CODE}/{name}" for name in entries if NATIVE_NAME.fullmatch(name)])
        .values_list("library_relpath", flat=True))
    for name in sorted(entries):
        if handled >= limit:
            break
        path = directory / name
        if NATIVE_NAME.fullmatch(name):
            if f"{playout.CATEGORY_CODE}/{name}" in published:
                continue                                             # a publication owns it
            handled += 1
            state, size = file_state(path)
            if state != "regular":
                report.add(Outcome("namespace_file", name, "refused", (f"entry_{state}",)))
                continue
            execution_id = execution_id_for(name)
            if not _unadoptable(execution_id):
                report.add(Outcome("namespace_file", name, "kept", ("in_flight_publication",)))
                continue
            age = _age_seconds(path, now)
            if age is None or age < grace.total_seconds():
                report.add(Outcome("namespace_file", name, "kept", ("too_recent",)))
                continue
            if not apply:
                report.add(Outcome("namespace_file", name, "eligible", bytes_reclaimable=size))
                continue
            report.add(_remove_orphan(path, name, execution_id))
        elif name.startswith(SCRATCH_PREFIX):
            handled += 1
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISDIR(info.st_mode):
                report.add(Outcome("publication_scratch", name[:64], "refused", ("entry_not_regular",)))
                continue
            age = _age_seconds(path, now)
            if age is None or age < grace.total_seconds():
                report.add(Outcome("publication_scratch", name[:64], "kept", ("too_recent",)))
                continue
            if publishing is None:
                publishing = _publish_in_progress(directory, entries)
            if publishing:
                report.add(Outcome("publication_scratch", name[:64], "kept", ("publication_in_progress",)))
                continue
            size = _tree_size(path)
            if not apply:
                report.add(Outcome("publication_scratch", name[:64], "eligible", bytes_reclaimable=size))
                continue
            try:
                shutil.rmtree(path)                                  # never follows symlinks
                report.add(Outcome("publication_scratch", name[:64], "removed", bytes_reclaimed=size))
            except OSError as exc:
                report.add(Outcome("publication_scratch", name[:64], "failed", (type(exc).__name__,)))
        elif name.startswith(".am-") and name.endswith(".flac.publish.lock") and NATIVE_NAME.fullmatch(name[1:-13]):
            handled += 1
            state, _size = file_state(path)
            if state != "regular":
                report.add(Outcome("publication_lock", name, "refused", (f"entry_{state}",)))
                continue
            age = _age_seconds(path, now)
            if age is None or age < grace.total_seconds() or not _unadoptable(execution_id_for(name[1:-13])):
                report.add(Outcome("publication_lock", name, "kept", ("in_use_or_recent",)))
                continue
            if not apply:
                report.add(Outcome("publication_lock", name, "eligible"))
                continue
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            except OSError as exc:
                report.add(Outcome("publication_lock", name, "failed", (type(exc).__name__,)))
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    report.add(Outcome("publication_lock", name, "kept", ("in_use_or_recent",)))
                    continue
                os.unlink(path)
                report.add(Outcome("publication_lock", name, "removed"))
            except OSError as exc:
                report.add(Outcome("publication_lock", name, "failed", (type(exc).__name__,)))
            finally:
                os.close(fd)
    return report


def _publish_in_progress(directory, names) -> bool:
    """Does any publish hold its destination lock right now? Every in-flight
    publish holds the flock on its ``.am-<hex>.flac.publish.lock`` sidecar for
    its whole duration (scratch directory included). Probed without creating
    anything; an unreadable sidecar counts as in progress (refuse)."""
    import errno
    import fcntl
    for name in names:
        if not (name.startswith(".am-") and name.endswith(".flac.publish.lock")):
            continue
        try:
            fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return True
            return True
        finally:
            os.close(fd)
    return False


def _remove_orphan(path, name, execution_id) -> Outcome:
    """Under the destination lock publish takes, re-check that no publication
    owns the file and its execution still cannot adopt it, then remove it."""
    try:
        with playout._destination_lock(path):
            if (ActiveBroadcastPublication.objects.filter(library_relpath=f"{playout.CATEGORY_CODE}/{name}")
                    .exists() or not _unadoptable(execution_id)):
                return Outcome("namespace_file", name, "kept", ("in_flight_publication",))
            state, size = file_state(path)
            if state != "regular":
                return Outcome("namespace_file", name, "refused", (f"entry_{state}",))
            os.unlink(path)
            return Outcome("namespace_file", name, "removed", bytes_reclaimed=size)
    except OSError as exc:
        return Outcome("namespace_file", name, "failed", (type(exc).__name__,))


# -- unbound aggregate bytes -------------------------------------------------------------------------------

def unbound_aggregates():
    """Present aggregate renditions no active-message row references (the
    candidates; purge_media re-checks every reference under its lock)."""
    recipe = rendering.assembly()
    return (ProductionMedia.objects
            .filter(kind=ProductionMedia.KIND_RENDITION, recipe_key=recipe.key,
                    retention_state=ProductionMedia.RETENTION_PRESENT,
                    active_message_executions__isnull=True, active_message_publications__isnull=True)
            .order_by("created_at", "id"))


def purge_unbound_aggregates(*, apply=False, grace=None, now=None, limit=None) -> CleanupReport:
    report, grace, now, limit = CleanupReport(apply=apply), _grace(grace), now or timezone.now(), _limit(limit)
    for media in unbound_aggregates()[:limit]:
        identity = str(media.pk)
        assessment = assess_aggregate_purge(media, now=now, grace=grace)
        if not assessment.eligible:
            report.add(Outcome("aggregate", identity, "kept" if assessment.reasons == ("too_recent",) else "refused",
                               assessment.reasons))
            continue
        if not apply:
            report.add(Outcome("aggregate", identity, "eligible", bytes_reclaimable=media.byte_size))
            continue
        try:
            with transaction.atomic():
                production_retention.purge_media(media)
        except PurgeRefused:
            report.add(Outcome("aggregate", identity, "refused", ("referenced",)))
            continue
        if transaction.get_connection().in_atomic_block:
            # purge_media unlinks the bytes when the caller's transaction commits:
            # purged now, but nothing is reclaimed yet -- never claimed early.
            report.add(Outcome("aggregate", identity, "purged", ("bytes_removed_on_commit",)))
            continue
        bytes_left = file_state(Path(_storage_path(media)))[0] == "regular"
        report.add(Outcome("aggregate", identity, "failed" if bytes_left else "purged",
                           ("bytes_remain",) if bytes_left else (), bytes_reclaimed=0 if bytes_left else media.byte_size))
    return report


def _storage_path(media) -> str:
    from production.services import layout
    return str(layout.resolve_storage_path(media.storage_key))
