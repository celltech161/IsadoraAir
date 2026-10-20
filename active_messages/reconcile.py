"""Read-only reconciliation of the active-message publication lifecycle (iPortal D3E-C).

``reconcile()`` reports; it changes NOTHING -- no message, execution,
publication, insertion, Track, media or retention state, no file, no policy
generation (it never calls ``services.current_policy``, which would create the
singleton), no retry counter, no engine command, no SystemEvent. Repeated runs
over unchanged data give equal reports. ``attention()`` is the separate,
explicit step that turns integrity findings into deduplicated Monitoring
SystemEvents.

Every finding is classified (``category``):

* ``expected_pruning`` -- history the model deliberately allows to lose its
  live reference (a pruned LogItem, a Track deleted through the library):
  the id snapshots keep the record; never an alert;
* ``in_flight``        -- publication / recovery the worker or a retry will
  settle (a READY execution's placed file, a current insertion whose LogItem
  the worker is recovering, a retirement whose bytes remain);
* ``integrity_damage`` -- bytes or rows that contradict the evidence;
* ``candidate_orphan`` -- interrupted-publication leftovers a sweep may remove;
* ``candidate_retirement`` -- reclaimable with an operator decision
  (eligibility, or the reason it is blocked, is stated);
* ``retained``         -- deliberately kept (history), informational;
* ``unknown``          -- evidence unavailable (refuse to decide).

A missing Track or LogItem alone is NOT corruption (SET_NULL with id
snapshots is the design), and a missing Track never means "never aired";
ACCEPTED or CLAIMED never means "aired". Bounded: every class is read in
limited batches, the namespace scan is capped, and only ``deep=True`` hashes
bytes (never on the engine's path: this runs from a management command).
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import DatabaseError
from django.utils import timezone

from production.errors import MediaInconsistent, MediaPurged
from production.models import ProductionMedia
from production.services import media_io

from . import playout, rendering, retention
from .models import (
    UNFINISHED_EXECUTION_STATES, ActiveBroadcastExecution, ActiveBroadcastInsertion,
    ActiveBroadcastMessage, ActiveBroadcastPublication, ExecutionState, MessageState,
)

X = ExecutionState
EVIDENCE_LIMIT = 200
SEVERITIES = ("info", "warning", "critical")
CATEGORIES = ("expected_pruning", "in_flight", "integrity_damage", "candidate_orphan", "candidate_retirement",
              "retained", "unknown")
ENGINE_HELD = (X.ACCEPTED, X.CLAIMED, X.STARTED)


@dataclass(frozen=True)
class Finding:
    artifact: str
    identity: str
    code: str
    severity: str
    category: str
    evidence: str
    eligibility: str = "not_applicable"
    action: str = ""

    def as_dict(self) -> dict:
        return {"artifact": self.artifact, "identity": self.identity, "code": self.code, "severity": self.severity,
                "category": self.category, "evidence": self.evidence, "eligibility": self.eligibility,
                "action": self.action}


@dataclass
class Report:
    deep: bool
    findings: list = field(default_factory=list)
    inspected: Counter = field(default_factory=Counter)
    healthy: Counter = field(default_factory=Counter)
    truncated: list = field(default_factory=list)
    bytes: Counter = field(default_factory=Counter)      # quarantine: reclaimable vs held for manual recovery
    generated_at: object = None                          # when this inspection ran (never reused as "current")
    limit: int = 0

    def add(self, artifact, identity, code, severity, category, evidence, eligibility="not_applicable", action=""):
        assert severity in SEVERITIES and category in CATEGORIES
        self.findings.append(Finding(artifact, str(identity)[:64], code, severity, category,
                                     str(evidence)[:EVIDENCE_LIMIT], eligibility[:80], action[:120]))

    def by(self, attribute: str) -> Counter:
        return Counter(getattr(item, attribute) for item in self.findings)

    def signature(self):
        """Order-independent content (idempotency comparisons)."""
        return (sorted(item.as_dict().items() for item in self.findings), sorted(self.inspected.items()),
                sorted(self.healthy.items()), sorted(self.truncated), sorted(self.bytes.items()))


def _bytes_problem(media, *, deep) -> str:
    """"" or why a PRESENT media's stored bytes do not match its row."""
    from production.services import layout
    if media.retention_state != ProductionMedia.RETENTION_PRESENT:
        return "purged"
    try:
        path = layout.resolve_storage_path(media.storage_key)
    except Exception:                                    # noqa: BLE001 -- a refusal, never a crash
        return "unresolvable"
    state, size = retention.file_state(path)
    if state != "regular":
        return f"bytes_{state}"
    if size != media.byte_size:
        return "size_mismatch"
    if deep:
        try:
            media_io.verify_integrity(media)
        except MediaPurged:
            return "purged"
        except MediaInconsistent:
            return "sha_mismatch"
        except OSError:
            return "bytes_unreadable"
    return ""


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- executions and their media ----------------------------------------------------------------------------

def _check_executions(report, *, deep, limit):
    rows = list(ActiveBroadcastExecution.objects.filter(mix_media__isnull=False).select_related("mix_media")
                .order_by("-created_at", "-id")[:limit + 1])
    if len(rows) > limit:
        report.truncated.append("executions")
        rows = rows[:limit]
    for row in rows:
        report.inspected["execution_mix"] += 1
        media = row.mix_media
        live = row.state in UNFINISHED_EXECUTION_STATES
        problem = _bytes_problem(media, deep=deep)
        if problem == "purged":
            report.add("execution_mix", row.pk, "mix_media_purged_but_referenced", "critical", "integrity_damage",
                       f"{row.state} execution's aggregate {media.pk} is purged",
                       action="investigate the binding that bypassed lock_for_binding")
            continue
        if problem:
            report.add("execution_mix", row.pk, "mix_media_bytes_inconsistent", "critical" if live else "warning",
                       "integrity_damage", f"{row.state} execution's aggregate {media.pk}: {problem}",
                       action="the worker fails it before publication" if live else "history only; no air impact")
            continue
        if not media.is_valid or not rendering.proves(media, row.composition_snapshot):
            report.add("execution_mix", row.pk, "mix_provenance_inconsistent", "critical" if live else "warning",
                       "integrity_damage", f"{row.state} execution's aggregate {media.pk} does not prove its "
                       "frozen composition", action="operator review; never rebind by hand")
            continue
        report.healthy["execution_mix"] += 1
        if not live:
            report.add("execution_mix", row.pk, "terminal_mix_retained", "info", "retained",
                       f"{row.state} execution keeps aggregate {media.pk} ({media.byte_size} bytes) as history",
                       eligibility="blocked:referenced_by_history")


def _check_inputs(report, *, deep):
    """The unfinished execution's frozen inputs (at most one: open_slot) and
    the ACTIVE messages' media (bounded by the policy ceiling)."""
    unfinished = ActiveBroadcastExecution.objects.filter(state__in=UNFINISHED_EXECUTION_STATES).first()
    if unfinished is not None and unfinished.state == X.PENDING_RENDER:
        stinger, components = rendering.frozen_inputs(unfinished.composition_snapshot)
        ids = [component["media"] for component in components] + ([stinger["media"]] if stinger else [])
        media = {str(row.pk): row for row in ProductionMedia.objects.filter(pk__in=ids)}
        for media_id in ids:
            report.inspected["pending_input"] += 1
            row = media.get(str(media_id))
            problem = "missing_row" if row is None else _bytes_problem(row, deep=deep)
            if problem:
                report.add("pending_input", unfinished.pk, "pending_input_unavailable", "critical",
                           "integrity_damage", f"frozen input {media_id}: {problem}",
                           action="the render fails closed with operator attention")
            else:
                report.healthy["pending_input"] += 1
    for message in (ActiveBroadcastMessage.objects.filter(state=MessageState.ACTIVE)
                    .select_related("message_media").order_by("id")[:retention.MAX_LIMIT]):
        report.inspected["message_media"] += 1
        if message.message_media is None:
            continue
        problem = _bytes_problem(message.message_media, deep=deep)
        if problem:
            report.add("message_media", message.pk, "active_message_media_unavailable", "critical",
                       "integrity_damage", f"active message media {message.message_media_id}: {problem}",
                       action="clear the message or replace its audio")
        else:
            report.healthy["message_media"] += 1


def _check_purged_references(report, *, limit):
    """Purged ProductionMedia still referenced by an active-message row: a
    binding that skipped lock_for_binding (bytes are gone; nothing repairs)."""
    for relation, label in (("active_messages", "message_media"), ("active_message_executions", "execution_mix"),
                            ("active_message_publications", "publication_media")):
        rows = list(ProductionMedia.objects.filter(retention_state=ProductionMedia.RETENTION_PURGED,
                                                   **{f"{relation}__isnull": False})
                    .values_list("pk", flat=True).distinct()[:limit])
        for media_id in rows:
            report.add("media", media_id, "purged_media_still_referenced", "critical", "integrity_damage",
                       f"purged media is still referenced as {label}",
                       action="operator review: the referencing rows outlive their bytes")


def _check_unbound_aggregates(report, *, now, grace, limit):
    rows = list(retention.unbound_aggregates()[:limit + 1])
    if len(rows) > limit:
        report.truncated.append("unbound_aggregates")
        rows = rows[:limit]
    for media in rows:
        report.inspected["aggregate"] += 1
        assessment = retention.assess_aggregate_purge(media, now=now, grace=grace)
        eligibility = "eligible" if assessment.eligible else "blocked:" + ",".join(assessment.reasons)
        report.add("aggregate", media.pk, "aggregate_unbound", "info", "candidate_retirement",
                   f"aggregate rendition {media.byte_size} bytes, bound to nothing", eligibility=eligibility,
                   action="sweep --apply purges its bytes (row and provenance kept)")


# -- publications and insertions ---------------------------------------------------------------------------

def _check_publications(report, *, deep, limit, rows=None):
    from library.models import Track
    if rows is None:
        rows = list(ActiveBroadcastPublication.objects.select_related("execution", "media")
                    .order_by("-published_at", "-id")[:limit + 1])
        if len(rows) > limit:
            report.truncated.append("publications")
            rows = rows[:limit]
    retired = retention.retired_execution_ids(row.execution_id for row in rows)
    tracks = {track.pk: track for track in Track.objects.filter(pk__in=[row.track_id for row in rows if row.track_id])}
    try:
        directory = retention.namespace_dir()
    except retention.RetentionUnavailable as exc:
        directory = None
        if rows:
            report.add("publication", "UrgentPA", exc.code, "critical", "unknown",
                       "the reserved library namespace is unavailable; publications cannot be checked",
                       action="check the library mount; nothing is removed while it is unavailable")
    waves = retention.waveforms_dir()
    owners_of = _owners_index([retention.canonical_destination(row.library_relpath) for row in rows
                               if row.track_id is None or row.execution_id in retired
                               or row.execution.state not in UNFINISHED_EXECUTION_STATES]) if directory else {}
    for row in rows:
        report.inspected["publication"] += 1
        execution, live = row.execution, row.execution.state in UNFINISHED_EXECUTION_STATES
        damage = "critical" if live else "warning"
        if row.sha256 != row.media.sha256:
            report.add("publication", row.execution_id, "publication_digest_inconsistent", "critical",
                       "integrity_damage", "the publication's SHA-256 is not its aggregate's",
                       action="operator review; never rewrite the publication")
            continue
        if row.track_id is not None and row.track_id != row.track_id_snapshot:
            report.add("publication", row.execution_id, "publication_track_snapshot_mismatch", "critical",
                       "integrity_damage", f"Track {row.track_id} is not the snapshot {row.track_id_snapshot}",
                       action="operator review")
            continue
        if directory is None:
            continue
        name = row.library_relpath.rsplit("/", 1)[-1]
        file_state, _size = retention.file_state(directory / name)
        track = tracks.get(row.track_id)
        if row.execution_id in retired:
            wave_state = retention.file_state(waves / f"{row.track_id_snapshot}.json")[0]
            owners = owners_of.get(str(retention.canonical_destination(row.library_relpath)))
            if track is not None or Track.objects.filter(pk=row.track_id_snapshot).exists():
                report.add("publication", row.execution_id, "retired_track_present", "warning", "integrity_damage",
                           "a retired publication's Track exists again", action="operator review")
            elif owners:
                report.add("publication", row.execution_id, "retired_path_owned_by_track", "warning",
                           "integrity_damage", f"Track {owners[0]} now owns the retired publication's path; "
                           "its bytes are never removed", eligibility="blocked:path_owned_by_track",
                           action="operator review: keep that Track or move it before any cleanup")
            elif owners is None:
                report.add("publication", row.execution_id, "ownership_unavailable", "warning", "unknown",
                           "Track ownership of the retired path could not be established",
                           action="retry; nothing is removed meanwhile")
            elif file_state != "missing" or wave_state != "missing":
                report.add("publication", row.execution_id, "retired_bytes_remaining", "info", "in_flight",
                           "retired publication still has its file or waveform on disk", eligibility="eligible",
                           action="sweep --apply finishes the retirement")
            elif retention.held_in_quarantine(row):
                report.add("publication", row.execution_id, "retired_bytes_remaining", "info", "in_flight",
                           "retired publication's physical cleanup was interrupted (quarantine operation held)",
                           eligibility="see quarantine findings", action="sweep --apply settles it")
            else:
                report.healthy["publication_retired"] += 1
            continue
        if file_state in ("not_regular", "unreadable"):
            report.add("publication", row.execution_id, f"publication_file_{file_state}", damage, "integrity_damage",
                       f"{name} is {file_state.replace('_', ' ')}", action="operator review; never followed")
            continue
        if track is None:
            if live:
                report.add("publication", row.execution_id, "publication_track_missing", "critical",
                           "integrity_damage", f"{execution.state} execution's Track {row.track_id_snapshot} was "
                           "deleted", action="the worker fails it (publication_unavailable)")
            elif file_state == "missing":
                report.add("publication", row.execution_id, "publication_pruned", "info", "expected_pruning",
                           f"Track {row.track_id_snapshot} and its file were removed through the library; "
                           "the publication record is kept")
            elif owners_of.get(str(retention.canonical_destination(row.library_relpath))):
                report.add("publication", row.execution_id, "publication_path_adopted", "warning",
                           "integrity_damage", f"another Track owns {name}; its bytes are never removed",
                           eligibility="blocked:path_owned_by_track", action="operator review")
            else:
                assessment = retention.assess_publication_retirement(
                    row, execution=execution, track=None,
                    owners=owners_of.get(str(retention.canonical_destination(row.library_relpath))))
                report.add("publication", row.execution_id, "publication_file_without_track", "info",
                           "candidate_retirement", f"Track {row.track_id_snapshot} was deleted; its file remains",
                           eligibility=_eligibility(assessment), action="retire --apply this execution")
            continue
        if track.filepath != str(playout._library_root() / row.library_relpath) or not playout.is_candidate(track):
            report.add("publication", row.execution_id, "publication_track_path_mismatch", damage,
                       "integrity_damage", f"Track {track.pk} does not point at {name}", action="operator review")
            continue
        if file_state == "missing":
            report.add("publication", row.execution_id, "publication_file_missing", damage, "integrity_damage",
                       f"{name} is missing while Track {track.pk} remains",
                       action="the worker fails a live execution (publication_unavailable)" if live
                       else "history only; retire --apply to remove the Track")
            continue
        if deep:
            try:
                actual = _file_sha256(directory / name)
            except OSError:
                actual = ""
            if actual != row.sha256:
                report.add("publication", row.execution_id, "publication_file_digest_mismatch", damage,
                           "integrity_damage", f"{name} does not hash to the publication's SHA-256",
                           action="operator review; the file is never overwritten")
                continue
        wave_state = retention.file_state(waves / f"{track.pk}.json")[0]
        if not track.waveform_path or wave_state != "regular":
            report.add("publication", row.execution_id, "publication_waveform_missing", "warning",
                       "integrity_damage", f"Track {track.pk} has no waveform file",
                       action="re-analyze the Track (display only; no air impact)")
            continue
        report.healthy["publication"] += 1
        if not live:
            assessment = retention.assess_publication_retirement(
                row, execution=execution, track=track,
                owners=owners_of.get(str(retention.canonical_destination(row.library_relpath))))
            report.add("publication", row.execution_id, "publication_retained", "info", "candidate_retirement",
                       f"{execution.state} execution's published file and Track {track.pk} remain",
                       eligibility=_eligibility(assessment), action="retire --apply after review")


def _owners_index(destinations) -> dict:
    """{str(destination): sorted Track ids referring to it} for every
    destination at once -- ONE bounded scan of the Track table instead of one
    per publication (retention.references_by_location: the same ownership
    rules as cleanup's). Every value is None when ownership cannot be
    established (database error, an unresolvable Track path)."""
    destinations = list(dict.fromkeys(str(destination) for destination in destinations))
    try:
        found = retention.references_by_location(destinations)
    except (DatabaseError, retention.OwnershipUnverifiable):
        return {destination: None for destination in destinations}
    return {destination: sorted({pk for pk, _field, _kind in references})
            for destination, references in found.items()}


def _eligibility(assessment) -> str:
    return "eligible" if assessment.eligible else "blocked:" + ",".join(assessment.reasons)


def _check_insertions(report, *, limit):
    from library.models import LogItem
    rows = list(ActiveBroadcastInsertion.objects.select_related("execution", "publication")
                .order_by("-inserted_at", "-id")[:limit + 1])
    if len(rows) > limit:
        report.truncated.append("insertions")
        rows = rows[:limit]
    items = {item["pk"]: item for item in LogItem.objects.filter(pk__in=[row.log_item_id_snapshot for row in rows])
             .values("pk", "track_id")}
    for row in rows:
        report.inspected["insertion"] += 1
        execution = row.execution
        if row.publication.execution_id != row.execution_id:
            report.add("insertion", row.attempt_token, "insertion_publication_mismatch", "critical",
                       "integrity_damage", "the insertion's publication belongs to another execution",
                       action="operator review")
            continue
        current = execution.attempt_token == row.attempt_token and execution.state in ENGINE_HELD
        item = items.get(row.log_item_id_snapshot)
        if row.log_item_id is None or item is None:
            if current:
                report.add("insertion", row.attempt_token, "current_insertion_log_item_missing", "warning",
                           "in_flight", f"LogItem {row.log_item_id_snapshot} of the current attempt is gone",
                           action="the worker re-sends or settles the attempt")
            else:
                report.add("insertion", row.attempt_token, "insertion_log_item_pruned", "info", "expected_pruning",
                           f"LogItem {row.log_item_id_snapshot} was pruned; the snapshot is kept")
            continue
        if row.log_item_id != row.log_item_id_snapshot or item["track_id"] not in (None, row.publication.track_id_snapshot):
            report.add("insertion", row.attempt_token, "insertion_association_unexpected", "critical",
                       "integrity_damage", f"LogItem {row.log_item_id_snapshot} names another Track",
                       action="operator review")
            continue
        report.healthy["insertion"] += 1


# -- the reserved namespace ------------------------------------------------------------------------------

def _check_namespace(report, *, now, grace, limit):
    try:
        directory = retention.namespace_dir()
    except retention.RetentionUnavailable as exc:
        if exc.code != "namespace_missing":          # nothing published yet: nothing to scan
            report.add("namespace", "UrgentPA", exc.code, "warning", "unknown",
                       "the reserved library namespace cannot be scanned", action="check the library mount")
        return
    import os
    names = []
    with os.scandir(directory) as scan:
        for count, entry in enumerate(scan):
            if count >= retention.MAX_NAMESPACE_ENTRIES:
                report.truncated.append("namespace")
                break
            if (retention.NATIVE_NAME.fullmatch(entry.name) or entry.name.startswith(retention.SCRATCH_PREFIX)
                    or (entry.name.startswith(".am-") and entry.name.endswith(".publish.lock"))):
                names.append(entry.name)
    names.sort()
    if len(names) > limit:
        report.truncated.append("namespace_entries")
        names = names[:limit]
    published = set(ActiveBroadcastPublication.objects.filter(
        library_relpath__in=[f"{playout.CATEGORY_CODE}/{name}" for name in names]).values_list("library_relpath",
                                                                                               flat=True))
    sweep = timedelta(seconds=grace.total_seconds())
    for name in names:
        path = directory / name
        report.inspected["namespace_entry"] += 1
        if retention.NATIVE_NAME.fullmatch(name):
            if f"{playout.CATEGORY_CODE}/{name}" in published:
                report.healthy["namespace_entry"] += 1
                continue
            state, size = retention.file_state(path)
            if state != "regular":
                report.add("namespace_file", name, "namespace_entry_unexpected", "warning", "unknown",
                           f"{name} is not a regular file (never followed or removed)", action="operator review")
                continue
            if not retention._unadoptable(retention.execution_id_for(name)):
                report.add("namespace_file", name, "namespace_file_awaiting_adoption", "info", "in_flight",
                           "a READY execution's placed file: publish adopts it", eligibility="blocked:in_flight")
                continue
            age = retention._age_seconds(path, now)
            old = age is not None and age >= sweep.total_seconds()
            report.add("namespace_file", name, "namespace_orphan_file", "warning", "candidate_orphan",
                       f"{size} bytes with no publication record (interrupted publication)",
                       eligibility="eligible" if old else "blocked:too_recent",
                       action="sweep --apply removes it after the grace period")
        elif name.startswith(retention.SCRATCH_PREFIX):
            age = retention._age_seconds(path, now)
            old = age is not None and age >= sweep.total_seconds()
            report.add("publication_scratch", name, "publication_scratch_leftover", "info", "candidate_orphan",
                       "scratch directory of an interrupted publication",
                       eligibility="eligible" if old else "blocked:too_recent",
                       action="sweep --apply removes it after the grace period")
        else:
            # A persistent destination-lock sidecar: a retained synchronization
            # artifact, never a cleanup candidate (playout._destination_lock).
            if retention.file_state(path)[0] == "regular":
                report.healthy["publication_lock"] += 1
            else:
                report.add("publication_lock", name, "namespace_entry_unexpected", "warning", "unknown",
                           f"{name} is not a regular file; publication of that execution refuses it",
                           action="operator review; never followed or removed")


# -- interrupted quarantine operations -------------------------------------------------------------------

QUARANTINE_FINDINGS = {
    # code: (severity, category, evidence, eligibility, action)
    "quarantine_pending": ("info", "in_flight", "{size} bytes proven to be the verified file, held for deletion",
                           "eligible", "sweep --apply re-proves identity and authority, then removes them"),
    "quarantine_operation_leftover": ("info", "in_flight", "an interrupted cleanup operation holding nothing",
                                      "eligible", "sweep --apply clears it"),
    "quarantine_identity_mismatch": ("warning", "integrity_damage",
                                     "{size} bytes that are NOT the verified file were moved aside; their name is "
                                     "free", "blocked:not_the_verified_file",
                                     "sweep --apply puts them back under their name (never over an occupant)"),
    "quarantine_restore_blocked": ("warning", "integrity_damage",
                                   "{size} bytes that are NOT the verified file cannot be put back: their name is "
                                   "occupied", "blocked:manual_recovery",
                                   "manual recovery: compare both files; nothing is deleted automatically"),
    "quarantine_recovery_required": ("warning", "unknown", "{size} bytes whose identity cannot be proven",
                                     "blocked:manual_recovery", "manual recovery; never deleted automatically"),
    "quarantine_owned_by_track": ("warning", "integrity_damage",
                                  "{size} verified bytes held in quarantine are referred to by a Track "
                                  "(filepath or waveform_path); never deleted while it does",
                                  "blocked:quarantine_owned_by_track",
                                  "operator review: change or remove that Track reference through the library, "
                                  "then sweep --apply"),
    "quarantine_name_owned_by_track": ("info", "in_flight",
                                       "{size} verified bytes held in quarantine; a Track now refers to their "
                                       "former name", "blocked:name_owned_by_track",
                                       "sweep --apply puts them back under that name (never over an occupant)"),
    "ownership_unavailable": ("warning", "unknown", "{size} bytes held; Track references cannot be read",
                              "blocked:ownership_unavailable", "retry; nothing is removed meanwhile"),
    "ownership_unverifiable": ("warning", "unknown",
                               "{size} bytes held; a Track path cannot be resolved (permission, symlink loop)",
                               "blocked:ownership_unverifiable",
                               "fix or correct that Track path; nothing is removed meanwhile"),
}


def _check_quarantine(report, *, limit):
    """The quarantines of the reserved namespace and WAVEFORMS_DIR, read-only
    (metadata only). Distinguishes bytes a sweep may reclaim from bytes held
    for manual recovery."""
    import os

    from . import quarantine
    for kind, opener in (("namespace", retention._open_namespace), ("waveforms", retention._open_waveforms)):
        try:
            dir_fd = opener()
        except quarantine.Refused as exc:
            if exc.code != "namespace_missing" and kind == "waveforms":
                report.add("quarantine", kind, exc.code, "warning", "unknown", f"{kind} directory unavailable",
                           action="check the mount; nothing is removed while it is unavailable")
            continue
        if dir_fd is None:
            continue
        try:
            held = quarantine.inspect(dir_fd)
        finally:
            os.close(dir_fd)
        if len(held) > limit:
            report.truncated.append(f"quarantine_{kind}")
            held = held[:limit]
        for item in held:
            report.inspected["quarantine_operation"] += 1
            if item.code == "quarantine_pending":
                report.bytes["quarantine_reclaimable"] += item.size
            elif item.code != "quarantine_operation_leftover":
                report.bytes["quarantine_manual"] += item.size
            if item.code not in QUARANTINE_FINDINGS:
                report.add("quarantine", f"{kind}/{item.operation}", item.code, "warning", "unknown",
                           "unexpected object in the cleanup quarantine (never followed or removed)",
                           action="operator review")
                continue
            code = item.code
            if code == "quarantine_pending":
                owned = _quarantine_ownership(kind, item)
                if owned:
                    report.bytes["quarantine_reclaimable"] -= item.size
                    report.bytes["quarantine_manual"] += item.size
                    code = owned
            severity, category, evidence, eligibility, action = QUARANTINE_FINDINGS[code]
            report.add("quarantine", f"{kind}/{item.operation}", code, severity, category,
                       evidence.format(size=item.size), eligibility=eligibility, action=action)


def _quarantine_ownership(kind, item) -> str:
    """Read-only: "" when no Track refers to a pending payload or its former
    name; otherwise the code that keeps it (the sweep would keep it too)."""
    directory = retention.namespace_dir() if kind == "namespace" else retention.waveforms_dir()
    problem = retention._ownership_problem(directory / item.name, item.identity, True, "quarantine_name_owned_by_track")
    return problem


# -- entry points ----------------------------------------------------------------------------------------

def reconcile(*, deep=False, limit=None, now=None, grace=None) -> Report:
    """The whole read-only report (see the module docstring)."""
    limit, grace, now = retention._limit(limit), retention._grace(grace), now or timezone.now()
    report = Report(deep=deep, generated_at=now, limit=limit)
    _check_executions(report, deep=deep, limit=limit)
    _check_inputs(report, deep=deep)
    _check_purged_references(report, limit=limit)
    _check_unbound_aggregates(report, now=now, grace=grace, limit=limit)
    _check_publications(report, deep=deep, limit=limit)
    _check_insertions(report, limit=limit)
    _check_namespace(report, now=now, grace=grace, limit=limit)
    _check_quarantine(report, limit=limit)
    return report


def inspect_publication(publication, *, now=None) -> Report:
    """The same publication rules as ``reconcile`` for ONE publication (the
    operator board's airing detail), plus its held quarantine operations.
    Read-only, metadata only; nothing else is inspected."""
    report = Report(deep=False, generated_at=now or timezone.now(), limit=1)
    row = (ActiveBroadcastPublication.objects.select_related("execution", "media")
           .filter(pk=publication.pk).first())
    if row is not None:
        _check_publications(report, deep=False, limit=1, rows=[row])
    return report


ATTENTION_CATEGORIES = frozenset({"integrity_damage", "unknown"})
ATTENTION_SEVERITIES = frozenset({"warning", "critical"})
ATTENTION_SAMPLE = 5


def attention(report) -> int:
    """One deduplicated Monitoring SystemEvent per condition code that needs
    an operator (integrity damage or unavailable evidence, warning or
    critical). Expected pruning, in-flight work, candidates and retained
    history never alert; a closed native-airing gate is not a condition.
    Returns the number of codes emitted."""
    from monitoring.models import emit_event
    grouped = {}
    for finding in report.findings:
        if finding.category in ATTENTION_CATEGORIES and finding.severity in ATTENTION_SEVERITIES:
            grouped.setdefault(finding.code, []).append(finding)
    for code, findings in sorted(grouped.items()):
        level = "error" if any(item.severity == "critical" for item in findings) else "warning"
        emit_event("active_messages", f"Active messages retention: {code}", level=level,
                   detail={"code": code, "count": len(findings),
                           "sample": [item.identity for item in findings[:ATTENTION_SAMPLE]],
                           "action": findings[0].action},
                   dedupe_key=f"active_messages|retention|{code}")
    return len(grouped)
