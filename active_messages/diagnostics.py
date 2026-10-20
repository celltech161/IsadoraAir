"""Operator Board diagnostics (iPortal D3E-D) -- a read-only presentation layer.

Three read models for the existing Operator Board, each built on an existing
authority and adding no definition of its own:

* ``inspect`` -- D3E-C's reconciliation (``reconcile.reconcile``), run on an
  explicit request with the web bound ``DIAGNOSTIC_LIMIT`` (rows per class),
  metadata only (``deep=False``: never a SHA-256 pass), and classified for an
  operator. It is never cached: a result describes the moment it was
  generated (``generated_at``) and nothing else, and "no findings" is shown
  as health only when an inspection actually ran to completion.
* ``worker_health`` -- the managed worker's state as Monitoring last
  VALIDATED it (D3E-B's probe: systemd's MainPID and ExecMainStartTimestamp
  bound to D3E-A's heartbeat through ``supervision.observe``), read from
  Monitoring's own state file. The board never reads the heartbeat file as
  health, never infers a process from a PID, and never calls systemctl.
* ``publication_evidence`` -- one airing's publication, retirement,
  insertion and play evidence, classified with ``reconcile.inspect_publication``
  (the same publication rules as the reconciler).

Everything here is read-only: no INSERT, UPDATE or DELETE (the policy
singleton is never created -- the gate is read with
``heartbeat.gate_open_read_only``), no file is created, moved or removed, no
Monitoring event is emitted (``reconcile.attention`` is never called), no
maintenance command is run, and nothing reaches the playback engine.
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
import uuid
from collections import Counter

from django.db import DatabaseError
from django.urls import reverse
from django.utils import timezone

from . import heartbeat, reconcile, retention
from .models import (
    ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastMessage, ActiveBroadcastMessageEvent,
    ActiveBroadcastPublication,
)

DIAGNOSTIC_LIMIT = 100              # rows per class for a synchronous web inspection (the CLI allows more)
MAX_FINDINGS = 200                  # findings listed (counts always cover every finding)
EVIDENCE_LIMIT = 50                 # insertions / play events shown on one airing
SEVERITY_ORDER = ("critical", "warning", "info")

# Operator classes, most urgent first: (label, what it means for an operator).
CLASSES = {
    "integrity_damage": ("Integrity damage",
                         "Stored evidence contradicts the records. Nothing is changed automatically; review it "
                         "with the station maintainer before any maintenance."),
    "manual_recovery": ("Manual recovery required",
                        "Held for a person: never deleted or moved automatically. Follow the quarantine "
                        "recovery procedure in docs/ACTIVE_MESSAGES.md."),
    "unknown": ("Unknown / unverifiable",
                "Could not be verified (storage, database or a Track path). Nothing is removed while it is "
                "unknown; fix the cause and inspect again."),
    "quarantine_pending": ("Quarantine pending",
                           "An interrupted cleanup holds these bytes aside. A maintenance sweep re-proves their "
                           "identity and ownership before finishing it."),
    "in_flight": ("Work in progress",
                  "Work the worker or a retry settles by itself; no action is needed."),
    "cleanup_candidate": ("Cleanup candidate",
                          "Unreferenced leftovers a maintenance sweep may reclaim after the grace period."),
    "retirement_candidate": ("Retirement candidate",
                             "History that could be retired by an explicit maintenance command -- never "
                             "automatically, and not from this page."),
    "expected_pruning": ("Expected historical pruning",
                         "Removed through the library's own retention; the history is kept by its snapshots."),
    "retained": ("Healthy / retained", "Deliberately kept history."),
}
ATTENTION_CLASSES = frozenset({"integrity_damage", "manual_recovery", "unknown"})
CANDIDATE_CLASSES = frozenset({"cleanup_candidate", "retirement_candidate", "quarantine_pending"})
MANUAL_CODES = frozenset({"quarantine_restore_blocked", "quarantine_recovery_required", "quarantine_owned_by_track",
                          "quarantine_entry_unexpected", "quarantine_not_private"})
PENDING_CODES = frozenset({"quarantine_pending", "quarantine_name_owned_by_track", "quarantine_operation_leftover"})
CATEGORY_CLASS = {"integrity_damage": "integrity_damage", "unknown": "unknown", "in_flight": "in_flight",
                  "candidate_orphan": "cleanup_candidate", "candidate_retirement": "retirement_candidate",
                  "expected_pruning": "expected_pruning", "retained": "retained"}

# Plain-language titles of D3E-C's condition codes (the code itself is always shown too).
TITLES = {
    "mix_media_bytes_inconsistent": "Aggregate audio does not match its record",
    "mix_provenance_inconsistent": "Aggregate audio does not prove its composition",
    "mix_media_purged_but_referenced": "Aggregate audio purged while still referenced",
    "purged_media_still_referenced": "Purged audio still referenced",
    "terminal_mix_retained": "Finished airing's aggregate kept as history",
    "pending_input_unavailable": "A pending airing's input audio is unavailable",
    "active_message_media_unavailable": "An active message's audio is unavailable",
    "aggregate_unbound": "Aggregate audio bound to nothing",
    "publication_digest_inconsistent": "Publication fingerprint does not match its audio",
    "publication_track_snapshot_mismatch": "Publication points at the wrong library Track",
    "publication_track_missing": "A live airing's library Track was deleted",
    "publication_file_missing": "Published file is missing",
    "publication_track_path_mismatch": "Library Track does not point at the published file",
    "publication_file_digest_mismatch": "Published file's contents changed",
    "publication_file_not_regular": "Published file is not a regular file",
    "publication_file_unreadable": "Published file cannot be read",
    "publication_waveform_missing": "Published Track has no waveform",
    "publication_pruned": "Track and file removed through the library",
    "publication_file_without_track": "Published file remains after its Track was deleted",
    "publication_retained": "Published file and Track kept",
    "publication_path_adopted": "Another library Track owns the published file",
    "retired_bytes_remaining": "Retired publication's cleanup not finished",
    "retired_track_present": "A retired publication's Track exists again",
    "retired_path_owned_by_track": "A library Track owns a retired publication's file",
    "ownership_unavailable": "Track ownership could not be read",
    "insertion_log_item_pruned": "Playlist item pruned; insertion record kept",
    "current_insertion_log_item_missing": "Current insertion's playlist item is gone",
    "insertion_publication_mismatch": "Insertion belongs to another airing's publication",
    "insertion_association_unexpected": "Insertion's playlist item names another Track",
    "namespace_orphan_file": "Placed file left by an interrupted publication",
    "namespace_file_awaiting_adoption": "Placed file waiting for its publication",
    "namespace_entry_unexpected": "Unexpected object in the reserved library folder",
    "publication_scratch_leftover": "Scratch folder left by an interrupted publication",
    "library_unavailable": "Library storage unavailable",
    "namespace_missing": "Reserved library folder missing",
    "namespace_not_a_directory": "Reserved library folder is not a folder",
    "library_root_not_a_directory": "Library root is not a folder",
    "quarantine_pending": "Verified bytes held for deletion",
    "quarantine_operation_leftover": "Interrupted cleanup holding nothing",
    "quarantine_identity_mismatch": "Cleanup moved a file that was not the verified one",
    "quarantine_restore_blocked": "Mismatched file cannot be put back (name occupied)",
    "quarantine_recovery_required": "Held bytes whose identity cannot be proven",
    "quarantine_owned_by_track": "A library Track refers to held bytes",
    "quarantine_name_owned_by_track": "A library Track refers to held bytes' former name",
    "ownership_unverifiable": "A library Track path cannot be resolved",
    "quarantine_entry_unexpected": "Unexpected object in the cleanup quarantine",
    "quarantine_not_private": "Cleanup quarantine is not private",
}


def classify(finding) -> str:
    """The operator class of one reconcile.Finding (codes first, then category)."""
    if finding.code in MANUAL_CODES:
        return "manual_recovery"
    if finding.code in PENDING_CODES:
        return "quarantine_pending"
    return CATEGORY_CLASS.get(finding.category, "unknown")


def title(code: str) -> str:
    return TITLES.get(code) or code.replace("_", " ").capitalize()


# -- identities and links (existing board pages; never a server path) ------------------------------------

def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _execution_of(finding):
    """The execution a finding is about, as far as its identity tells (or None)."""
    identity = finding.identity
    if finding.artifact in ("publication", "execution_mix"):
        return _uuid(identity)
    if finding.artifact == "namespace_file" and retention.NATIVE_NAME.fullmatch(identity):
        return retention.execution_id_for(identity)
    if finding.artifact == "quarantine" and "/" in identity:
        kind, _sep, operation = identity.partition("/")
        name = operation.rsplit(".", 1)[0]
        if kind == "namespace" and retention.NATIVE_NAME.fullmatch(name):
            return retention.execution_id_for(name)
    return None


def _links(findings) -> dict:
    """{finding index: (label, url)} -- bounded lookups, only for rows that exist."""
    executions, messages, tokens, snapshots = {}, {}, {}, {}
    for index, finding in enumerate(findings):
        execution = _execution_of(finding)
        if execution is not None:
            executions[index] = execution
        elif finding.artifact == "message_media" and _uuid(finding.identity):
            messages[index] = _uuid(finding.identity)
        elif finding.artifact == "insertion" and _uuid(finding.identity):
            tokens[index] = _uuid(finding.identity)
        elif finding.artifact == "quarantine" and finding.identity.startswith("waveforms/"):
            name = finding.identity.split("/", 1)[1].rsplit(".", 1)[0]
            if name.endswith(".json") and name[:-5].isdigit():
                snapshots[index] = int(name[:-5])
    by_token = dict(ActiveBroadcastInsertion.objects.filter(attempt_token__in=set(tokens.values()))
                    .values_list("attempt_token", "execution_id")) if tokens else {}
    by_snapshot = dict(ActiveBroadcastPublication.objects.filter(track_id_snapshot__in=set(snapshots.values()))
                       .values_list("track_id_snapshot", "execution_id")) if snapshots else {}
    for index, token in tokens.items():
        if token in by_token:
            executions[index] = by_token[token]
    for index, snapshot in snapshots.items():
        if snapshot in by_snapshot:
            executions[index] = by_snapshot[snapshot]
    present = set(ActiveBroadcastExecution.objects.filter(pk__in=set(executions.values()))
                  .values_list("pk", flat=True)) if executions else set()
    known = set(ActiveBroadcastMessage.objects.filter(pk__in=set(messages.values()))
                .values_list("pk", flat=True)) if messages else set()
    links = {}
    for index, execution in executions.items():
        if execution in present:
            links[index] = ("Airing " + str(execution)[:8], reverse("active_messages:board-airing", args=[execution]))
    for index, message in messages.items():
        if message in known:
            links[index] = ("Message", reverse("active_messages:board-message", args=[message]))
    return links


def _finding(finding, link) -> dict:
    kind = classify(finding)
    return {
        "severity": finding.severity, "code": finding.code, "category": finding.category, "class": kind,
        "class_label": CLASSES[kind][0], "title": title(finding.code), "evidence": finding.evidence,
        "interpretation": CLASSES[kind][1], "action": finding.action, "eligibility": finding.eligibility,
        "artifact": finding.artifact, "identity": finding.identity,
        "link": None if link is None else {"label": link[0], "url": link[1]},
    }


# -- the bounded inspection --------------------------------------------------------------------------------

def inspect(*, limit=None, now=None) -> dict:
    """One explicit, bounded, read-only inspection (metadata only), classified.

    state: "complete" -- every class fit within the bound and nothing was
    unverifiable; "incomplete" -- a class was truncated at the bound, or
    something could not be verified (the counts describe only what was
    inspected); "unavailable" -- the inspection itself failed (database or
    storage): nothing is known, which is never shown as health."""
    started, now = time.monotonic(), now or timezone.now()
    limit = DIAGNOSTIC_LIMIT if limit is None else limit
    try:
        report = reconcile.reconcile(deep=False, limit=limit, now=now)
        findings = sorted(report.findings, key=lambda item: (SEVERITY_ORDER.index(item.severity),
                                                             list(CLASSES).index(classify(item)),
                                                             item.code, item.identity))
        links = _links(findings[:MAX_FINDINGS])
    except (DatabaseError, retention.RetentionUnavailable, OSError) as exc:
        return {"state": "unavailable", "generated_at": now.isoformat(), "reason": type(exc).__name__,
                "duration_ms": round((time.monotonic() - started) * 1000)}
    classes = Counter(classify(item) for item in findings)
    unverifiable = classes["unknown"]
    complete = not report.truncated and not unverifiable
    return {
        "state": "complete" if complete else "incomplete",
        "generated_at": report.generated_at.isoformat(), "limit": report.limit, "deep": False,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "truncated": sorted(report.truncated), "unverifiable": unverifiable,
        "summary": {
            "findings": len(findings),
            "by_severity": {severity: sum(1 for item in findings if item.severity == severity)
                            for severity in SEVERITY_ORDER},
            "by_category": dict(sorted(Counter(item.category for item in findings).items())),
            "by_class": {key: classes[key] for key in CLASSES},
            "integrity_damage": classes["integrity_damage"],
            "candidates": sum(classes[key] for key in CANDIDATE_CLASSES),
            "attention": sum(classes[key] for key in ATTENTION_CLASSES),
            "inspected": dict(sorted(report.inspected.items())), "healthy": dict(sorted(report.healthy.items())),
            "bytes": {"quarantine_reclaimable": report.bytes["quarantine_reclaimable"],
                      "quarantine_manual": report.bytes["quarantine_manual"]},
        },
        "findings": [_finding(item, links.get(index)) for index, item in enumerate(findings[:MAX_FINDINGS])],
        "findings_truncated": len(findings) > MAX_FINDINGS,
    }


# -- the managed worker, as Monitoring last validated it -------------------------------------------------

WORKER_UNIT = "isadoraair-active-message-worker.service"   # monitoring.services.probes.ACTIVE_MESSAGE_WORKER_UNIT
MONITORING_STATE_MAX_BYTES = 4 * 1024 * 1024
# Where Monitoring's poller publishes its verdicts; None: Monitoring's own
# path (monitoring.views.STATE_PATH). The test suite points it at a disposable
# location so no test ever reads a station's live Monitoring state.
MONITORING_STATE_PATH = None
# The worker check's serialized contract, as its one producer writes it
# (monitoring.services.probes.probe_systemd -> _active_message_worker_readiness
# -> active_messages.supervision.observe -> heartbeat.assess, serialized by
# monitoring.services.monitor._build_result). Every reason is listed with what
# it proves; a reason outside these tables is not trusted.
#
# Unit not active: the reason is a function of active_state/sub_state alone
# ("starting", "restarting", "service_failed", "service_inactive").
# Unit active, heartbeat NOT bound to the running process (identity unproven).
UNBOUND_REASONS = frozenset({"observer_error", "identity_unavailable", "starting", "no_heartbeat",
                             "heartbeat_not_current", "malformed"})
# Unit active, heartbeat bound to the running process (supervision.belongs:
# MainPID and ExecMainStartTimestamp) -- heartbeat.assess's verdict on it.
# FRESH passed assess's staleness bound (a render within its deadline is
# assess's own exception); the others say nothing current about it.
FRESH_REASONS = frozenset({"idle", "busy", "retry_backoff", "playout_in_flight", "working", "ready_waiting_for_gate",
                           "rendering", "blocked", "lock_held_elsewhere", "database_errors", "malfunction",
                           "history_unverified", "restarted"})
BOUND_REASONS = FRESH_REASONS | {"stale", "stopped", "crash_looping", "render_overdue"}
# assess's "ok" conditions: an ok verdict may still be lifted to
# "history_unverified" (assess, a malformed predecessor) or "restarted" (the
# probe, NRestarts rose since its last poll).
OK_REASONS = frozenset({"idle", "busy", "retry_backoff", "playout_in_flight", "working", "ready_waiting_for_gate",
                        "rendering"})
LIFTED_REASONS = frozenset({"history_unverified", "restarted"})
# assess's reading of the last cycle's result on a fresh, non-rendering
# heartbeat, in its own order. Where two are listed, counters Monitoring does
# not serialize decide (repeats: busy vs lock_held_elsewhere, database_errors
# vs malfunction) -- either is the producer's. "idle" is ready_waiting_for_gate
# exactly with code "gated"; "" (no cycle completed yet) is idle.
OUTCOME_REASONS = {"error": frozenset({"database_errors", "malfunction"}), "blocked": frozenset({"blocked"}),
                   "busy": frozenset({"busy", "lock_held_elsewhere"}), "backoff": frozenset({"retry_backoff"}),
                   "retry": frozenset({"retry_backoff"}), "playout": frozenset({"playout_in_flight"}),
                   **{result: frozenset({"working"}) for result in heartbeat.PROGRESS_RESULTS}}
# What observe adds only for a bound heartbeat (supervision._SHOWN).
HEARTBEAT_EVIDENCE = ("phase", "outcome", "code", "execution", "heartbeat_age_seconds")
# Raw verdicts the poller never debounces (only warning/critical are held at
# the last stable status), so the written status must be exactly this one.
UNDEBOUNCED_STATUS = {"starting": "unknown", "observer_error": "unknown", "identity_unavailable": "unknown",
                      **{reason: "ok" for reason in OK_REASONS}}
RECOVERY_CONDITIONS = frozenset({"crash_looping", "restarted", "history_unverified", "restarting"})
WORKER_LABELS = {
    "not_configured": "Worker check not provisioned",
    "disabled": "Worker check disabled",
    "monitoring_unavailable": "Monitoring status unavailable",
    "not_observed": "Not yet observed by Monitoring",
    "stale": "Monitoring status out of date",
    "ok": "Worker OK", "warning": "Worker warning", "critical": "Worker critical", "unknown": "Worker unknown",
}


def _monitoring_state(path):
    """Monitoring's state document (never followed through a symlink, size-
    capped), or None when absent, unreadable or not a JSON object."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MONITORING_STATE_MAX_BYTES:
            return None
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(MONITORING_STATE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        document = json.loads(data)
    except ValueError:
        return None
    return document if isinstance(document, dict) else None


MONITORING_STATUSES = ("ok", "warning", "critical", "unknown")
# Monitoring writes ``timestamp`` as ``time.time()``: finite epoch seconds
# (UTC by definition -- no local-time or naive interpretation exists). The
# same plausibility window as the heartbeat contract.
TIMESTAMP_MIN, TIMESTAMP_MAX = heartbeat.MIN_TIMESTAMP, heartbeat.MAX_TIMESTAMP
PROBLEMS = {
    "unreadable": "its state file is missing, unreadable or not a JSON object",
    "timestamp_missing": "its state has no observation time",
    "timestamp_invalid": "its observation time is not a valid epoch timestamp",
    "timestamp_in_future": "its observation time is in the future, so it cannot describe the present",
    "checks_invalid": "its list of check results is malformed",
    "entry_invalid": "the worker's check result is incomplete or malformed",
    "observer_error": "its state could not be interpreted",
}


PROBLEM_LABELS = {"entry_invalid": "worker check data is incomplete"}


class UntrustedState(ValueError):
    """The Monitoring document cannot be trusted; ``code`` says why and
    ``field`` (when one is to blame) which part of the worker's entry."""

    def __init__(self, code, field=""):
        super().__init__(code)
        self.code, self.field = code, field


def _observation_age(document, now) -> float:
    """Seconds since Monitoring's observation, or UntrustedState. A future
    observation is refused, never clamped: it cannot describe the present."""
    if "timestamp" not in document or document["timestamp"] is None:
        raise UntrustedState("timestamp_missing")
    timestamp = document["timestamp"]
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) \
            or not TIMESTAMP_MIN <= timestamp <= TIMESTAMP_MAX:
        raise UntrustedState("timestamp_invalid")
    if type(now) not in (int, float) or not math.isfinite(now):
        raise UntrustedState("observer_error")
    age = now - timestamp
    if age < 0:
        raise UntrustedState("timestamp_in_future")
    return age


def _worker_entry(document, check_id):
    """The worker's result from a validated ``checks`` list (None: the poller
    has not observed it yet), or UntrustedState. Every entry must be what the
    poller writes -- an object with an integer id -- and ids must be unique;
    anything else makes the whole list untrusted (a lost or garbled entry
    must not read as "not observed")."""
    checks = document.get("checks")
    if type(checks) is not list:
        raise UntrustedState("checks_invalid")
    found, seen = None, set()
    for item in checks:
        if type(item) is not dict or type(item.get("id")) is not int or item["id"] in seen:
            raise UntrustedState("checks_invalid")
        seen.add(item["id"])
        if item["id"] == check_id:
            found = item
    return found


def _invalid(field):
    return UntrustedState("entry_invalid", field)


def _number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _worker_evidence(entry):
    """(status, reason, identity, heartbeat) from one worker entry validated
    against the producer's contract for the state it reports, or
    UntrustedState("entry_invalid", field). Nothing is defaulted: a missing,
    mistyped, unknown or contradictory field can only make the entry
    untrusted -- never idle, running, verified or current."""
    status = entry.get("status")
    if status not in MONITORING_STATUSES:
        raise _invalid("status")
    if "detail" not in entry:
        raise _invalid("detail")
    detail = entry["detail"]
    if type(detail) is not dict:
        raise _invalid("detail")
    if "active_state" not in detail:
        # systemctl itself could not be run, or the probe raised: the poller
        # writes {"error": ...} as "unknown" -- no unit state, no verdict.
        if "reason" in detail or status != "unknown" or type(detail.get("error")) is not str:
            raise _invalid("detail.active_state")
        return status, "", "unknown", "unknown"
    active_state = detail["active_state"]
    if type(active_state) is not str:
        raise _invalid("detail.active_state")
    if type(detail.get("sub_state")) is not str:
        raise _invalid("detail.sub_state")
    sub_state = detail["sub_state"]
    reason = detail.get("reason")
    if type(reason) is not str or not reason:
        raise _invalid("detail.reason")
    if "n_restarts" not in detail or not (detail["n_restarts"] is None or type(detail["n_restarts"]) is int
                                          and detail["n_restarts"] >= 0):
        raise _invalid("detail.n_restarts")
    if UNDEBOUNCED_STATUS.get(reason, status) != status:
        raise _invalid("status")                          # e.g. "ok" over an identity it could not prove
    if active_state != "active":
        expected = ("starting" if active_state == "activating" and sub_state != "auto-restart"
                    else "restarting" if active_state == "activating" or sub_state == "auto-restart"
                    else "service_failed" if active_state == "failed" else "service_inactive")
        if reason != expected:
            raise _invalid("detail.reason")
        _no_heartbeat_evidence(detail)                    # the probe returns before observe runs
        if "native_airing" in detail:
            raise _invalid("detail.native_airing/reason")
        return status, reason, "unknown", "unknown"
    if reason not in UNBOUND_REASONS | BOUND_REASONS:
        raise _invalid("detail.reason")
    if reason != "observer_error" and detail.get("native_airing") not in ("open", "closed"):
        raise _invalid("detail.native_airing")
    if reason not in BOUND_REASONS:
        if reason == "malformed" and type(detail.get("problem")) is not str:
            raise _invalid("detail.problem")
        if reason == "observer_error" and type(detail.get("error")) is not str:
            raise _invalid("detail.error")
        _no_heartbeat_evidence(detail)
        return status, reason, ("mismatch" if reason == "heartbeat_not_current" else "unknown"), {
            "no_heartbeat": "missing", "heartbeat_not_current": "not_current", "malformed": "malformed",
        }.get(reason, "unknown")
    _bound_heartbeat(reason, detail)
    if reason == "restarted" and not (type(detail["n_restarts"]) is int and detail["n_restarts"] >= 1):
        raise _invalid("detail.n_restarts/reason")       # the probe saw NRestarts rise
    if status == "unknown":
        return status, reason, "unknown", "unknown"
    return status, reason, "verified", ("current" if reason in FRESH_REASONS
                                        else "stale" if reason == "stale" else "unknown")


def _no_heartbeat_evidence(detail):
    """Unit state and unbound verdicts carry no heartbeat reading: one here is
    not the producer's (and must not be read as evidence about the process)."""
    for key in HEARTBEAT_EVIDENCE:
        if key in detail:
            raise _invalid(f"detail.{key}/reason")


def assessed_reasons(phase, outcome=None, code=None, age=None) -> frozenset:
    """Every reason the producer can write for a bound heartbeat showing these
    fields: heartbeat.assess in its own order (stopped; crash loop; rendering
    -- never stale, only overdue; stale; the outcome table), then the lifted
    history_unverified / restarted for an ok verdict. ``age`` is the probe's
    rounded heartbeat age; None (outcome, age) means any."""
    if phase == "stopped":
        return frozenset({"stopped"})
    found = {"crash_looping"}
    if phase == "rendering":
        if age is None or age >= 0:                        # past its deadline, which is after checked_at
            found.add("render_overdue")
        if age is None or age <= heartbeat.RENDER_DEADLINE_MAX_SECONDS:    # within its deadline
            found |= {"rendering", "restarted"}
            if age is None or age <= heartbeat.CRASH_WINDOW_SECONDS:      # history_unverified's window
                found.add("history_unverified")
        return frozenset(found)
    # round(age, 1): above STALE_AFTER_SECONDS rounds to >= it, at most it to <= it.
    if age is None or age >= heartbeat.STALE_AFTER_SECONDS:
        found.add("stale")
    if age is None or age <= heartbeat.STALE_AFTER_SECONDS:
        read = (frozenset().union(*OUTCOME_REASONS.values(), {"ready_waiting_for_gate", "idle"}) if outcome is None
                else OUTCOME_REASONS.get(outcome)
                or frozenset({"ready_waiting_for_gate" if outcome == "idle" and code == "gated" else "idle"}))
        found |= read | (LIFTED_REASONS if read & OK_REASONS else set())
    return frozenset(found)


def _bound_heartbeat(reason, detail):
    """A bound heartbeat's evidence, validated as the one observation the
    producer wrote: each field in its serialized format (heartbeat.validate's
    own rules), the writer's state relations that survive serialization (no
    cycle, counter, history or deadline is serialized), and the reason being
    one heartbeat.assess could derive from exactly these fields."""
    phase, outcome, code, execution, age = (detail.get(key) for key in HEARTBEAT_EVIDENCE)
    if phase not in heartbeat.PHASES:
        raise _invalid("detail.phase")
    if outcome != "" and outcome not in heartbeat.RESULTS:
        raise _invalid("detail.outcome")
    if type(code) is not str or not heartbeat._CODE.fullmatch(code):
        raise _invalid("detail.code")
    if type(execution) is not str:
        raise _invalid("detail.execution")
    if execution:
        try:
            canonical = str(uuid.UUID(execution)) == execution
        except ValueError:
            canonical = False
        if not canonical:                                  # never normalized into evidence
            raise _invalid("detail.execution")
    if not _number(age) or age < -heartbeat.MAX_FUTURE_SKEW_SECONDS:
        raise _invalid("detail.heartbeat_age_seconds")
    # The writer: no outcome only before the first completed cycle -- then no
    # code, never "polling", and an execution only for a render in that pass;
    # "starting" only then; a render always names its execution.
    if outcome == "":
        if code:
            raise _invalid("detail.code/outcome")
        if phase == "polling":
            raise _invalid("detail.outcome/phase")
        if execution and phase != "rendering":
            raise _invalid("detail.execution/phase")
    elif phase == "starting":
        raise _invalid("detail.outcome/phase")
    if phase == "rendering" and not execution:
        raise _invalid("detail.execution/phase")
    if reason not in assessed_reasons(phase, outcome, code, age):    # blame the first relation it breaks
        raise _invalid("detail.reason/phase" if reason not in assessed_reasons(phase)
                       else "detail.reason/outcome" if reason not in assessed_reasons(phase, outcome, code)
                       else "detail.reason/heartbeat_age_seconds")


def worker_health(*, now=None, state_path=None) -> dict:
    """The worker as Monitoring last validated it -- never re-derived here.

    state: "not_configured" (no Monitoring check of the unit), "disabled",
    "monitoring_unavailable" (no trustworthy state: unreadable, malformed,
    or an observation time that is missing, invalid or in the future, or a
    worker entry that is incomplete or contradicts the producer's contract --
    ``problem`` says which, ``field`` which part of the entry), "stale" (Monitoring's last poll is older than its
    own staleness bound: the verdict shown is only the last known one),
    "not_observed" (the poller has no result for the check yet), or the
    verdict's status ("ok", "warning", "critical", "unknown"). Never raises:
    an unforeseen fault is "monitoring_unavailable" too, never health."""
    base = {"monitoring_url": "/monitoring/", "unit": WORKER_UNIT, "identity": "unknown", "heartbeat": "unknown",
            "recovery": False, "reason": "", "problem": "", "field": "", "observed_age_seconds": None, "detail": {}}
    try:
        return _worker_health(base, now=time.time() if now is None else now, state_path=state_path)
    except Exception:                                 # noqa: BLE001 -- fail closed, never a crash or "ok"
        return _unavailable(base, "observer_error")


def _unavailable(base, problem, field="", **extra):
    label = WORKER_LABELS["monitoring_unavailable"]
    return {**base, **extra, "state": "monitoring_unavailable", "problem": problem, "field": field,
            "label": label + (f" — {PROBLEM_LABELS[problem]}" if problem in PROBLEM_LABELS else ""),
            "explanation": PROBLEMS[problem]}


def _worker_health(base, *, now, state_path) -> dict:
    from monitoring.models import MonitorCheck
    from monitoring.views import STATE_PATH, STATE_STALE_SECONDS
    try:
        check = (MonitorCheck.objects.filter(kind="systemd", systemd_unit=WORKER_UNIT)
                 .order_by("id").values("id", "name", "enabled").first())
    except DatabaseError:
        return _unavailable(base, "observer_error")
    if check is None:
        return {**base, "state": "not_configured", "label": WORKER_LABELS["not_configured"]}
    if not check["enabled"]:
        return {**base, "state": "disabled", "label": WORKER_LABELS["disabled"], "check": check["name"]}
    document = _monitoring_state(state_path or MONITORING_STATE_PATH or STATE_PATH)
    if document is None:
        return _unavailable(base, "unreadable", check=check["name"])
    try:
        age = _observation_age(document, now)
        entry = _worker_entry(document, check["id"])
        if entry is not None:
            status, reason, identity, heartbeat_state = _worker_evidence(entry)
    except UntrustedState as exc:
        return _unavailable(base, exc.code, exc.field, check=check["name"])
    age = round(age, 1)
    if entry is None:
        return {**base, "state": "not_observed", "label": WORKER_LABELS["not_observed"], "check": check["name"],
                "observed_age_seconds": age}
    detail = entry["detail"]
    shown = {key: detail[key] for key in ("phase", "outcome", "code", "heartbeat_age_seconds", "native_airing",
                                          "n_restarts", "active_state", "sub_state")
             if key in detail and isinstance(detail[key], (str, int, float, bool, type(None)))
             and not (isinstance(detail[key], float) and not math.isfinite(detail[key]))}
    stale = age > STATE_STALE_SECONDS
    if stale:                                             # the last known verdict proves nothing current
        identity, heartbeat_state = "unknown", "unknown"
    return {**base, "state": "stale" if stale else status, "status": status,
            "label": (WORKER_LABELS["stale"] if stale else WORKER_LABELS[status]) + (f" — {reason}" if reason else ""),
            "reason": reason, "identity": identity, "heartbeat": heartbeat_state,
            "recovery": reason in RECOVERY_CONDITIONS, "check": check["name"], "observed_age_seconds": age,
            "detail": shown}


# -- one airing's publication evidence ----------------------------------------------------------------------

PUBLICATION_STATES = {
    "none": "Never published to the library",
    "present": "Published file and Track present",
    "retired": "Retired by maintenance (history kept)",
    "retired_cleanup_pending": "Retired; physical cleanup not finished",
    "retired_attention": "Retired; needs review",
    "pruned": "Track and file removed through the library (history kept)",
    "quarantine_pending": "Cleanup quarantine pending",
    "damaged": "Missing or damaged (integrity finding)",
    "unknown": "Could not be verified",
}


def publication_evidence(execution, *, now=None) -> dict:
    """What happened to one airing's library publication, and its insertion
    and play evidence -- durable snapshots shown as recorded, whether or not
    the Track or the file still exists."""
    from library.models import LogItem, PlayEvent, Track
    insertions = list(ActiveBroadcastInsertion.objects.filter(execution=execution)
                      .order_by("inserted_at", "id")[:EVIDENCE_LIMIT])
    snapshots = [row.log_item_id_snapshot for row in insertions]
    live_items = set(LogItem.objects.filter(pk__in=snapshots).values_list("pk", flat=True)) if snapshots else set()
    plays = list(PlayEvent.objects.filter(log_item_id_snapshot__in=snapshots)
                 .order_by("started_at", "id")[:EVIDENCE_LIMIT]) if snapshots else []
    evidence = {
        "insertions": [{"attempt_token": str(row.attempt_token), "log_item_id_snapshot": row.log_item_id_snapshot,
                        "log_item_present": row.log_item_id_snapshot in live_items,
                        "engine_instance": row.engine_instance, "inserted_at": row.inserted_at}
                       for row in insertions],
        "plays": [{"started_at": row.started_at, "ended_at": row.ended_at,
                   "duration_played_seconds": row.duration_played_seconds,
                   "evidence_state": row.duration_evidence_state, "log_item_id_snapshot": row.log_item_id_snapshot,
                   "track_present": row.track_id is not None} for row in plays],
        "aired": execution.started_at is not None,
    }
    publication = ActiveBroadcastPublication.objects.filter(execution=execution).first()
    if publication is None:
        return {**evidence, "state": "none", "label": PUBLICATION_STATES["none"], "publication": None,
                "retirement": None, "findings": []}
    retired = (ActiveBroadcastMessageEvent.objects.filter(execution=execution, event=retention.RETIRED_EVENT)
               .order_by("id").first())
    try:
        report = reconcile.inspect_publication(publication, now=now)
        findings = report.findings
        healthy = report.healthy
    except (DatabaseError, retention.RetentionUnavailable, OSError):
        findings, healthy = None, {}
    codes = {item.code for item in findings or ()}
    if findings is None or any(item.category == "unknown" for item in findings):
        state = "unknown"
    elif retired is not None:
        state = ("retired_attention" if codes & {"retired_track_present", "retired_path_owned_by_track"}
                 else "retired_cleanup_pending" if "retired_bytes_remaining" in codes else "retired")
    elif "publication_pruned" in codes:
        state = "pruned"
    elif any(item.category == "integrity_damage" for item in findings):
        state = "damaged"
    elif retention.held_in_quarantine(publication):
        state = "quarantine_pending"
    else:
        state = "present"
    detail = retired.detail if retired is not None and isinstance(retired.detail, dict) else {}
    return {
        **evidence, "state": state, "label": PUBLICATION_STATES[state],
        "publication": {"id": str(publication.pk), "name": publication.library_relpath.rsplit("/", 1)[-1],
                        "sha256": publication.sha256, "track_id_snapshot": publication.track_id_snapshot,
                        "track_present": Track._base_manager.filter(pk=publication.track_id_snapshot).exists(),
                        "published_at": publication.published_at, "healthy": dict(healthy)},
        "retirement": None if retired is None else {
            "at": retired.created_at, "actor": retired.get_actor_kind_display(),
            "via": str(detail.get("via") or ""), "file_present": detail.get("file_present"),
            "track_present": detail.get("track_present")},
        "findings": [_finding(item, None) for item in findings or ()],
    }
