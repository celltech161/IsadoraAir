"""Roadmap 2.27 Phase D -- the single off-thread hour-refill caller.

One daemon thread per PlaybackEngine. Every policy read, cause query, Phase B
projection or proposal, Phase C install and status write runs here, on this
thread's own database connection. The GLib thread's only Phase D work is the
existing Phase B snapshot capture (primitives + put_nowait), at most one
outstanding at a time.

With HourRefillPolicy Off (the default, or no row) a check is one primary-key
SELECT every CHECK_INTERVAL_SECONDS and nothing else. Observation (dry run)
computes advisory Phase B proposals and never writes to a log. Install mode
calls the accepted Phase C HourRefillInstaller, which repeats every fresh
check and decides under its own locks. See
docs/roadmap/2_27_PHASE_D_OPERATIONAL_CONTRACT.md.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connection
from django.utils import timezone

from library.services.hour_refill_causality import (
    CAUSE_AUTOMATIC_SKIP,
    CAUSE_MEDIA_FAILURE,
    CAUSE_OPERATOR_EJECT,
    CAUSE_REQUEST_SUBSTITUTION,
    CAUSE_START_NEXT,
)
from library.services.hour_refill_install import (
    STATUS_INSTALLED,
    STATUS_LIVE_FILL_PENDING,
    HourRefillInstaller,
    coordinator_for,
    request_snapshot,
    safety_budget,
)
from library.services.hour_refill_projection import project_refill
from library.services.hour_refill_proposal import (
    CAUSE_ROW_LIMIT,
    STATUS_PROPOSED,
    PsaResidualPolicy,
    evaluate_hour_refill,
    load_relevant_causes,
    resolve_boundary,
)

STATUS_PATH = Path("/run/isadoraair/hour_refill_state.json")
STATUS_SCHEMA = 1
CHECK_INTERVAL_SECONDS = 15.0
RETRY_SECONDS = 30.0                 # per cause set, between evaluations
MAX_EVALUATIONS_PER_CAUSE_SET = 6
HEARTBEAT_SECONDS = 60.0             # status file rewritten at least this often
STOP_JOIN_SECONDS = 0.5

MODE_OFF = "off"
MODE_OBSERVE = "observation"
MODE_INSTALL = "install"

OUTCOME_NO_ACTIVE_LOG = "no_active_log"
OUTCOME_NO_NEW_CAUSE = "no_new_cause"
OUTCOME_CAUSE_TYPE_DISABLED = "cause_type_disabled"
OUTCOME_EVALUATION_LIMIT = "evaluation_limit"
OUTCOME_ENGINE_STOPPED = "engine_not_running"
OUTCOME_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
OUTCOME_ERROR = "error"

KIND_ADVISORY = "advisory"
KIND_COMMITTED = "committed"
KIND_ABSTAINED = "abstained"

NOTES = (
    "advisory: a dry-run proposal; nothing was written",
    "committed: rows and a ledger entry were written; not proof of air",
    "aired: only an existing PlayEvent for the LogItem shows that",
)


# ---------------------------------------------------------------------------
# Policy (worker side)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyState:
    mode: str
    reason: str
    enabled: bool | None = None
    dry_run_only: bool | None = None
    request_cause: bool = False
    eject_cause: bool = False
    host_armed: bool = False
    start_next_cause: bool = False
    automatic_skip_cause: bool = False
    media_failure_cause: bool = False

    def allows(self, cause_types):
        """Every type has its own switch on; an unknown type never does."""
        switches = {CAUSE_REQUEST_SUBSTITUTION: self.request_cause, CAUSE_OPERATOR_EJECT: self.eject_cause,
                    CAUSE_START_NEXT: self.start_next_cause, CAUSE_AUTOMATIC_SKIP: self.automatic_skip_cause,
                    CAUSE_MEDIA_FAILURE: self.media_failure_cause}
        return all(switches.get(cause_type, False) for cause_type in cause_types)

    def as_dict(self):
        return {"mode": self.mode, "reason": self.reason, "enabled": self.enabled,
                "dry_run_only": self.dry_run_only, "request_cause": self.request_cause,
                "eject_cause": self.eject_cause, "start_next_cause": self.start_next_cause,
                "automatic_skip_cause": self.automatic_skip_cause,
                "media_failure_cause": self.media_failure_cause, "host_armed": self.host_armed}


def host_armed():
    return getattr(settings, "HOUR_REFILL_INSTALL_ARMED", False) is True


def read_policy_state():
    """The runner's mode from one primary-key read. Never creates the row;
    a missing or unreadable row is Off (fail closed)."""
    from library.models import HourRefillPolicy

    armed = host_armed()
    try:
        row = (HourRefillPolicy.objects.filter(pk=1)
               .values_list("enabled", "dry_run_only", "capture_request_substitution", "capture_operator_eject",
                            "capture_start_next", "capture_automatic_skip", "capture_media_failure")
               .first())
    except Exception:  # noqa: BLE001 -- includes a database without the r0121 columns
        return PolicyState(MODE_OFF, "policy_unreadable", host_armed=armed)
    if row is None:
        return PolicyState(MODE_OFF, "no_policy_row", host_armed=armed)
    enabled, dry_run, request, eject, start_next, automatic_skip, media_failure = (bool(v) for v in row)
    common = dict(enabled=enabled, dry_run_only=dry_run, request_cause=request, eject_cause=eject,
                  start_next_cause=start_next, automatic_skip_cause=automatic_skip,
                  media_failure_cause=media_failure, host_armed=armed)
    if not enabled:
        return PolicyState(MODE_OFF, "policy_disabled", **common)
    if dry_run:
        return PolicyState(MODE_OBSERVE, "dry_run_only", **common)
    if not armed:
        return PolicyState(MODE_OBSERVE, "install_not_armed", **common)
    return PolicyState(MODE_INSTALL, "install_permitted", **common)


def psa_policy_from_settings():
    """Off unless HOUR_REFILL_PSA_CATEGORY_ID is set. Phase B validates the
    category (spot kind) and the residual window; an unparseable value
    leaves the PSA unconfigured, never guessed."""
    raw = str(getattr(settings, "HOUR_REFILL_PSA_CATEGORY_ID", "") or "").strip()
    if not raw:
        return PsaResidualPolicy()
    try:
        return PsaResidualPolicy(enabled=True, category_id=int(raw))
    except ValueError:
        return PsaResidualPolicy(enabled=True, category_id=None)


def _iso(value):
    return timezone.localtime(value).isoformat() if value is not None else None


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, sort_keys=True, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _CauseSetBackoff:
    __slots__ = ("evaluations", "next_due", "done", "limit_reported")

    def __init__(self):
        self.evaluations, self.next_due, self.done, self.limit_reported = 0, 0.0, False, False


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class HourRefillRunner:
    """Engine-owned single-flight caller. start() once with the engine;
    stop() once at shutdown. wake() may be called from any thread."""

    def __init__(self, engine, *, idle_add, status_path=STATUS_PATH, evidence=None, clock=time.monotonic,
                 wall_clock=None, check_interval=CHECK_INTERVAL_SECONDS, retry_seconds=RETRY_SECONDS,
                 max_evaluations=MAX_EVALUATIONS_PER_CAUSE_SET, heartbeat_seconds=HEARTBEAT_SECONDS,
                 installer_hooks=None):
        self.engine = engine
        self._idle_add = idle_add
        self.status_path = Path(status_path) if status_path is not None else None
        self.evidence = evidence
        self.clock = clock
        self.wall_clock = wall_clock      # None: station-local now
        self.check_interval = check_interval
        self.retry_seconds = retry_seconds
        self.max_evaluations = max_evaluations
        self.heartbeat_seconds = heartbeat_seconds
        self.installer_hooks = installer_hooks
        self._start_lock = threading.Lock()
        self._thread = None
        self._generation = 0
        self._stopping = threading.Event()
        self._wake = threading.Event()
        self._capture_lock = threading.Lock()
        self._capture_outstanding = False
        self._backoff = {}
        self._pending_adoption = None
        self._evaluating = None           # (log_id, cause_ids) while this check evaluates
        self._last_published_key = None
        self._last_write = None
        self.started_at = None
        self.last_check_at = None
        self.policy = PolicyState(MODE_OFF, "not_checked_yet", host_armed=host_armed())
        self.last_outcome = None
        self.last_install = None
        # Keys that other threads touch exist up front, so a status snapshot
        # never iterates a dict that is growing underneath it.
        self.counters = Counter(wakes=0)
        self.abstentions = Counter()
        self.last_status = None

    # -- lifecycle (engine main thread, outside GLib callbacks) -------------

    def start(self):
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._generation += 1
            self._stopping.clear()
            self._wake.clear()
            self.started_at = self._now()
            self._thread = threading.Thread(target=self._run, args=(self._generation,),
                                            name="hour-refill-runner", daemon=True)
            self._thread.start()
            return True

    def stop(self, join_seconds=STOP_JOIN_SECONDS):
        """Never waits longer than join_seconds. A database call already in
        flight is not cancelled; nothing new starts after it returns."""
        with self._start_lock:
            self._stopping.set()
            self._wake.set()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, join_seconds))
        return thread is None or not thread.is_alive()

    def wake(self):
        """Coalescing wake; safe from any thread (an Event.set, no I/O)."""
        if not self._stopping.is_set():
            self.counters["wakes"] += 1
            self._wake.set()

    @property
    def alive(self):
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stopping.is_set()

    def _live(self, generation):
        return generation == self._generation and not self._stopping.is_set()

    def _run(self, generation):
        try:
            while self._live(generation):
                close_old_connections()
                if not self.run_once(generation):
                    connection.close()  # drop a possibly broken connection after an error
                if not self._live(generation):
                    break
                self._wake.wait(timeout=self.check_interval)
                self._wake.clear()
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    # -- one check (runner thread; tests call it directly) ------------------

    def run_once(self, generation=None):
        """One bounded check. Never raises. False only after an error."""
        generation = self._generation if generation is None else generation
        ok = True
        self._evaluating = None
        try:
            self._check(generation)
        except Exception as exc:  # noqa: BLE001 -- the runner must never take the engine down
            ok = False
            self.counters["errors"] += 1
            total = self.counters["errors"]
            log_id, cause_ids = self._evaluating or (None, ())
            self._set_outcome(OUTCOME_ERROR, (type(exc).__name__,), KIND_ABSTAINED, log_id=log_id,
                              cause_ids=cause_ids)
            if total == 1 or total % 100 == 0:
                try:
                    print(f"  Hour-refill runner error (count={total}); playout unaffected: {type(exc).__name__}")
                except Exception:  # noqa: BLE001 -- a journal write never ends the runner thread
                    self.counters["journal_write_errors"] += 1
        # Journal only after this check's own outcome is final, so an
        # evaluation that raised is logged as `error`, never as the previous
        # evaluation's outcome. A failed journal write leaves that outcome
        # and the publish below untouched.
        if self._evaluating is not None:
            self._journal_evaluation()
        try:
            self._publish(generation)
        except Exception:  # noqa: BLE001 -- status is optional
            self.counters["status_write_errors"] += 1
        return ok

    def _check(self, generation):
        self.counters["checks"] += 1
        self.last_check_at = self._now()
        self.policy = read_policy_state()
        self._observe_adoption()
        if self.policy.mode == MODE_OFF:
            self.counters["skipped_policy_off"] += 1
            return
        if not self._engine_running():
            self._set_outcome(OUTCOME_ENGINE_STOPPED, (), KIND_ABSTAINED)
            return
        log_id = self._active_log_id()
        if log_id is None:
            self._set_outcome(OUTCOME_NO_ACTIVE_LOG, (), KIND_ABSTAINED)
            return
        self._backoff = {k: v for k, v in self._backoff.items() if k[0] == log_id}
        cause_ids, covered = self._cause_ids(log_id)
        if not cause_ids - covered:
            self._set_outcome(OUTCOME_NO_NEW_CAUSE, (), KIND_ABSTAINED, log_id=log_id)
            return
        signature = (log_id, frozenset(cause_ids))
        entry = self._backoff.setdefault(signature, _CauseSetBackoff())
        if entry.done:
            return
        if entry.evaluations >= self.max_evaluations:
            if not entry.limit_reported:
                entry.limit_reported = True
                self._set_outcome(OUTCOME_EVALUATION_LIMIT, (f"max_{self.max_evaluations}_per_cause_set",),
                                  KIND_ABSTAINED, log_id=log_id, cause_ids=cause_ids)
            return
        if self.clock() < entry.next_due:
            self.counters["deferred_backoff"] += 1
            return
        entry.evaluations += 1
        entry.next_due = self.clock() + self.retry_seconds
        if not self._live(generation):
            return
        self.counters["evaluations"] += 1
        self._evaluating = (log_id, cause_ids)
        self._evaluate(entry, log_id, cause_ids)

    def _journal_evaluation(self):
        """One compact line per evaluation (runner thread, never GLib):
        bounded by the per-cause-set cap, ids and numbers only. The shadow
        observation window in the rollout packet is built from these."""
        outcome = self.last_outcome or {}
        keys = ("mode", "status", "log_id", "cause_ids", "net_shortage_seconds", "threshold_seconds",
                "captured_at", "committed_runway_seconds", "boundary_instant", "placement_mode", "track_ids",
                "safety_budget_seconds", "insertion_lead_seconds", "installed_log_item_ids")
        try:
            print("  Hour-refill evaluation " + json.dumps({k: outcome.get(k) for k in keys if k in outcome},
                                                            sort_keys=True, separators=(",", ":")))
        except Exception:  # noqa: BLE001 -- telemetry only: counted, never raised
            self.counters["journal_write_errors"] += 1
            return
        self.counters["journal_lines"] += 1

    def _evaluate(self, entry, log_id, cause_ids):
        snapshot = request_snapshot(self.engine, idle_add=self._bounded_idle_add, wall_clock=self.wall_clock)
        if snapshot is None:
            self.counters["snapshot_unavailable"] += 1
            self._set_outcome(OUTCOME_SNAPSHOT_UNAVAILABLE, (), KIND_ABSTAINED, log_id=log_id, cause_ids=cause_ids)
            return
        if snapshot.engine_instance != getattr(self.engine, "_engine_session_id", None) or not self._engine_running():
            self._set_outcome(OUTCOME_ENGINE_STOPPED, ("engine_instance_changed",), KIND_ABSTAINED, log_id=log_id)
            return
        projection = project_refill(snapshot, causes=load_relevant_causes(snapshot),
                                    boundary=resolve_boundary(snapshot))
        if not projection.eligible:
            self._set_outcome(projection.outcome, projection.reasons, KIND_ABSTAINED, log_id=log_id,
                              cause_ids=cause_ids, projection=projection)
            return
        types = sorted({c.cause_type for c in projection.qualified_causes})
        if not self.policy.allows(types):
            self._set_outcome(OUTCOME_CAUSE_TYPE_DISABLED, tuple(f"switched_off:{t}" for t in types
                                                                 if not self.policy.allows([t])),
                              KIND_ABSTAINED, log_id=log_id, cause_ids=cause_ids, projection=projection)
            return
        if self.policy.mode == MODE_INSTALL:
            installer = HourRefillInstaller(self.engine, idle_add=self._bounded_idle_add,
                                            psa_policy=psa_policy_from_settings(), wall_clock=self.wall_clock,
                                            hooks=self.installer_hooks)
            result = installer.attempt()
            if result.status == STATUS_INSTALLED:
                entry.done = True
                self._record_install(result, projection)
            else:
                self._set_outcome(result.status, result.reasons, KIND_ABSTAINED, log_id=log_id,
                                  cause_ids=cause_ids, projection=projection, result=result)
            return
        proposal = evaluate_hour_refill(snapshot, psa_policy=psa_policy_from_settings(),
                                        safety_budget=safety_budget())
        kind = KIND_ADVISORY if proposal.status == STATUS_PROPOSED else KIND_ABSTAINED
        if kind == KIND_ADVISORY:
            self.counters["advisory_proposals"] += 1
        self._set_outcome(proposal.status, proposal.reasons, kind, log_id=log_id, cause_ids=cause_ids,
                          projection=proposal.projection or projection, proposal=proposal)

    # -- engine reads (runner thread; short I/O-free critical sections) -----

    def _engine_running(self):
        return bool(getattr(self.engine, "running", False)) and not getattr(self.engine, "_stop_started", False)

    def _active_log_id(self):
        with self.engine._lock:
            log = getattr(self.engine, "current_log", None)
            return getattr(log, "id", None) if log is not None else None

    def _observe_adoption(self):
        pending = self._pending_adoption
        if pending is None or self.last_install is None:
            return
        log_id, ids = pending
        with self.engine._lock:
            log = getattr(self.engine, "current_log", None)
            in_memory = ({getattr(item, "id", None) for item in (self.engine.log_items or [])}
                         if log is not None and getattr(log, "id", None) == log_id else None)
        if in_memory is None:
            self.last_install = {**self.last_install, "adoption": "not_observed_engine_left_log"}
            self._pending_adoption = None
        elif ids <= in_memory:
            self.last_install = {**self.last_install, "adoption": "observed",
                                 "adoption_observed_by": _iso(self._now())}
            self._pending_adoption = None

    def _bounded_idle_add(self, fn, *args):
        """At most one capture callback queued on GLib. The callback clears
        the mark with a plain attribute write (no lock on GLib)."""
        with self._capture_lock:
            if self._capture_outstanding:
                self.counters["capture_coalesced"] += 1
                return 0
            self._capture_outstanding = True

        def capture(*callback_args):
            try:
                return fn(*callback_args)
            finally:
                self._capture_outstanding = False

        try:
            return self._idle_add(capture, *args)
        except Exception:
            self._capture_outstanding = False
            raise

    # -- database reads (runner thread) --------------------------------------

    @staticmethod
    def _cause_ids(log_id):
        from library.models import HourRefillCause, HourRefillInstall

        cause_ids = set(HourRefillCause.objects.filter(playlist_log_id_snapshot=log_id)
                        .order_by("id").values_list("id", flat=True)[:CAUSE_ROW_LIMIT])
        covered = set()
        for ids in HourRefillInstall.objects.filter(playlist_log_id_snapshot=log_id).values_list("cause_ids", flat=True):
            covered.update(ids or [])
        return cause_ids, covered

    # -- outcomes and status -------------------------------------------------

    def _now(self):
        return self.wall_clock() if self.wall_clock is not None else timezone.now()

    def _set_outcome(self, status, reasons, kind, *, log_id=None, cause_ids=(), projection=None, proposal=None,
                     result=None):
        outcome = {"at": _iso(self._now()), "mode": self.policy.mode, "status": status,
                   "reasons": list(reasons or ()), "kind": kind, "log_id": log_id,
                   "cause_ids": sorted(cause_ids)}
        if projection is not None:
            outcome.update(
                log_id=projection.active_log_id, log_date=str(projection.active_log_date),
                log_hour=projection.active_log_hour,
                cause_types=dict(Counter(c.cause_type for c in projection.qualified_causes)),
                qualified_cause_ids=[c.cause_id for c in projection.qualified_causes],
                net_shortage_seconds=projection.net_shortage_seconds,
                threshold_seconds=projection.threshold_seconds, threshold_basis=projection.threshold_basis,
                boundary_kind=projection.boundary.kind if projection.boundary else None,
                # Field timing validation: compare captured_at + committed_runway_seconds
                # with the as-run PlayEvent start of the item after the runway.
                captured_at=_iso(projection.captured_at), window_seconds=projection.window_seconds,
                committed_runway_seconds=projection.runway.total_seconds if projection.runway else None,
                boundary_instant=_iso(projection.boundary.instant)
                if projection.boundary and projection.boundary.instant else None,
            )
        if proposal is not None:
            outcome.update(
                placement_mode=proposal.placement.mode if proposal.placement else None,
                track_ids=[m.track_id for m in proposal.music] + ([proposal.psa.track_id] if proposal.psa else []),
                category_ids=[m.category_id for m in proposal.music]
                + ([proposal.psa.category_id] if proposal.psa else []),
                psa_status=proposal.psa_status, residual_after_seconds=proposal.residual_after_seconds,
            )
            if proposal.music:
                base, per_item = safety_budget()
                outcome["safety_budget_seconds"] = base + per_item * (len(proposal.music) + bool(proposal.psa))
        if result is not None:
            outcome.update(
                placement_mode=result.placement_mode, track_ids=list(result.track_ids),
                net_shortage_seconds=result.net_before_seconds if result.net_before_seconds is not None
                else outcome.get("net_shortage_seconds"),
                residual_beyond_budget_seconds=result.residual_beyond_budget_seconds,
                safety_budget_seconds=result.safety_budget_seconds,
                insertion_lead_seconds=result.insertion_lead_seconds,
            )
        self.last_outcome = outcome
        self.counters[f"outcome:{status}"] += 1
        if kind == KIND_ABSTAINED and status not in (OUTCOME_NO_NEW_CAUSE, OUTCOME_NO_ACTIVE_LOG):
            self.abstentions[status] += 1
        if status == STATUS_LIVE_FILL_PENDING or "live_fill_pending" in outcome["reasons"]:
            self.counters["live_fill_conflicts"] += 1
        return outcome

    def _record_install(self, result, projection):
        from library.models import HourRefillInstall, LogItem

        self.counters["installed"] += 1
        categories = dict(LogItem.objects.filter(id__in=result.installed_log_item_ids)
                          .values_list("id", "category_id"))
        # The installer's own cause set (from its fresh S2), not this pre-check's.
        cause_ids = HourRefillInstall.objects.filter(id=result.install_id).values_list("cause_ids", flat=True).first()
        outcome = self._set_outcome(STATUS_INSTALLED, result.reasons, KIND_COMMITTED, log_id=result.log_id,
                                    cause_ids=cause_ids or (), projection=projection, result=result)
        outcome["qualified_cause_ids"] = list(cause_ids or ())
        outcome.update(install_id=result.install_id, install_key=result.install_key,
                       installed_log_item_ids=list(result.installed_log_item_ids),
                       category_ids=[categories.get(i) for i in result.installed_log_item_ids],
                       anchor_log_item_id=result.anchor_log_item_id)
        self.last_install = {**outcome, "adoption": "pending"}
        self._pending_adoption = (result.log_id, frozenset(result.installed_log_item_ids))

    def status(self):
        coordinator = coordinator_for(self.engine)
        try:
            evidence = dict(list((getattr(self.evidence, "counters", None) or {}).items()))
        except RuntimeError:  # the recorder thread added a key mid-copy
            evidence = {}
        return {
            "schema": STATUS_SCHEMA,
            "written_at": _iso(timezone.now()),  # real time: the admin judges staleness by it
            "engine_instance": getattr(self.engine, "_engine_session_id", None),
            "runner": {"running": self.alive, "started_at": _iso(self.started_at),
                       "last_check_at": _iso(self.last_check_at),
                       "check_interval_seconds": self.check_interval,
                       "heartbeat_seconds": self.heartbeat_seconds},
            "policy": self.policy.as_dict(),
            "psa_residual": "configured" if psa_policy_from_settings().enabled else "off",
            "last_outcome": self.last_outcome,
            "last_install": self.last_install,
            "counters": dict(sorted(self.counters.items())),
            "abstentions": dict(sorted(self.abstentions.items())),
            "evidence_counters": dict(sorted(evidence.items())) if evidence else {},
            "live_fill": {"legacy_discards": coordinator.legacy_discards},
            "notes": list(NOTES),
        }

    def _publish(self, generation):
        if generation != self._generation:
            return  # a stale thread never overwrites the current status
        doc = self.status()
        self.last_status = doc
        if self.status_path is None:
            return
        # Rewrite on a meaningful change, else only at the heartbeat: counters
        # and timestamps alone never cause a write.
        stable = {k: v for k, v in doc.items() if k not in ("written_at", "runner", "counters", "evidence_counters")}
        stable["last_outcome"] = {k: v for k, v in (doc["last_outcome"] or {}).items() if k != "at"}
        stable["running"] = doc["runner"]["running"]
        key = json.dumps(stable, sort_keys=True, default=str)
        now = self.clock()
        if key == self._last_published_key and self._last_write is not None \
                and now - self._last_write < self.heartbeat_seconds:
            return
        _atomic_write_json(self.status_path, doc)
        self._last_published_key, self._last_write = key, now
        self.counters["status_writes"] += 1
