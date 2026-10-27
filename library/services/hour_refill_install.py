"""Roadmap 2.27 Phase C -- guarded, dormant hour-refill installer.

Worker thread only. Re-derives a Phase B proposal from a FRESH GLib snapshot,
proves it again against a second snapshot (CAS-1) and against the database
under the PlaylistLog lock (CAS-2), then commits new LogItems and one
append-only HourRefillInstall ledger row in a single short transaction. The
COMMIT is the only linearization point; the engine adopts the rows through
its existing guarded 3-second queue reload. The worker never touches GLib
memory, and GLib never waits on the worker.

Nothing calls this automatically, and install_permitted() is False on every
station (HourRefillPolicy.dry_run_only is enforced by a check constraint).
See docs/roadmap/2_27_PHASE_C_GUARDED_INSTALL_CONTRACT.md.
"""
from __future__ import annotations

import hashlib
import queue
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.db import IntegrityError, OperationalError, close_old_connections, connection, transaction
from django.db.models import F
from django.utils import timezone

from library.services.hour_refill_projection import EVIDENCE_NOTE, project_refill
from library.services.hour_refill_proposal import (
    PSA_CATEGORY_KIND,
    STATUS_PROPOSED,
    _window_slots,
    assess_placement,
    build_fit_frame,
    evaluate_hour_refill,
    load_relevant_causes,
    load_request_context,
    resolve_boundary,
)

SNAPSHOT_WAIT_SECONDS = 1.0        # worker waits this long for a GLib capture
MAX_COMMIT_AGE_SECONDS = 2.0       # S2 received -> COMMIT, else roll back
INSERTION_LEAD_SECONDS = 20.0      # first inserted item must be at least this far away
SAFETY_BUDGET_BASE_SECONDS = 5.0   # unused window left after the install ...
SAFETY_BUDGET_PER_ITEM_SECONDS = 1.0  # ... plus this per inserted item
MAX_ATTEMPTS_PER_LOG = 3
LOCK_TIMEOUT = "250ms"
SHIFT_OFFSET = 100000              # same two-pass technique as _splice_log_item_db_at

STATUS_INSTALLED = "installed"
STATUS_DRY_RUN_ONLY = "dry_run_only"
STATUS_LIVE_FILL_PENDING = "live_fill_pending"
STATUS_REFILL_IN_FLIGHT = "refill_in_flight"
STATUS_ADOPTION_PENDING = "adoption_pending"
STATUS_ATTEMPT_LIMIT = "attempt_limit"
STATUS_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
STATUS_STALE = "stale_discarded"
STATUS_UNSAFE = "unsafe_window"
STATUS_NO_SAFE_ANCHOR = "no_safe_anchor"
STATUS_LOCK_CONTENDED = "lock_contended"
STATUS_DUPLICATE = "duplicate_install"


def install_permitted():
    """True only when the persisted policy allows real installation:
    enabled AND not dry_run_only. The Phase A check constraint keeps
    dry_run_only True, so this is False everywhere until Phase D."""
    from library.models import HourRefillPolicy

    row = HourRefillPolicy.objects.filter(pk=1).values_list("enabled", "dry_run_only").first()
    return bool(row and row[0] and not row[1])


# ---------------------------------------------------------------------------
# Coordinator shared with ordinary live fill (GLib-safe: memory only)
# ---------------------------------------------------------------------------

class RefillCoordinator:
    """Single-flight arbitration between causal refill and ordinary live
    fill for one engine. Every field is read and written only under the
    engine's own `_lock`, in sections that do no I/O."""

    def __init__(self):
        self.refill_owner = None
        # Multiple generations can coexist: the worker guard is released
        # before an older GLib callback necessarily runs. Track generations,
        # rather than one boolean an older callback can clear underneath a
        # newer callback.
        self.legacy_callbacks_pending = set()
        self.pending_adoption = None       # (log_id, frozenset(item ids))
        self.attempts = {}
        self.legacy_discards = 0

    # -- GLib side (caller already holds engine._lock) ----------------------

    def _adoption_outstanding(self, log_id, log_items):
        pending = self.pending_adoption
        if pending is None:
            return False
        pending_log, ids = pending
        if pending_log != log_id:
            return False
        if ids <= {getattr(item, "id", None) for item in log_items}:
            self.pending_adoption = None
            return False
        return True

    def blocks_legacy(self, log_id, log_items):
        """Ordinary live fill must not dispatch or install now."""
        return self.refill_owner is not None or self._adoption_outstanding(log_id, log_items)

    def mark_legacy_pending(self, generation):
        self.legacy_callbacks_pending.add(generation)

    def clear_legacy_pending(self, generation):
        self.legacy_callbacks_pending.discard(generation)

    # -- worker side ---------------------------------------------------------

    def reserve(self, engine, token):
        """None on success, else an abstention status."""
        with engine._lock:
            if getattr(engine, "_live_fill_in_progress", False) or self.legacy_callbacks_pending:
                return STATUS_LIVE_FILL_PENDING
            if self.refill_owner is not None:
                return STATUS_REFILL_IN_FLIGHT
            current = getattr(engine, "current_log", None)
            if current is not None and self._adoption_outstanding(current.id, engine.log_items):
                return STATUS_ADOPTION_PENDING
            self.refill_owner = token
            return None

    def count_attempt(self, engine, log_id):
        with engine._lock:
            self.attempts[log_id] = self.attempts.get(log_id, 0) + 1
            return self.attempts[log_id] <= MAX_ATTEMPTS_PER_LOG

    def release(self, engine, token, *, adopted_ids=None, log_id=None):
        with engine._lock:
            if self.refill_owner == token:
                self.refill_owner = None
            if adopted_ids:
                self.pending_adoption = (log_id, frozenset(adopted_ids))


def coordinator_for(engine):
    coordinator = getattr(engine, "_refill_coordinator", None)
    if coordinator is None:
        coordinator = engine._refill_coordinator = RefillCoordinator()
    return coordinator


# ---------------------------------------------------------------------------
# GLib snapshot request (GLib never waits)
# ---------------------------------------------------------------------------

def request_snapshot(engine, *, idle_add, timeout=SNAPSHOT_WAIT_SECONDS, wall_clock=None):
    """Ask GLib for one capture_playback_window() and wait (worker side)
    at most `timeout`. The GLib callback only copies primitives and does a
    put_nowait; a late callback writes into an abandoned mailbox."""
    from library.services.hour_refill_projection import capture_playback_window

    mailbox = queue.Queue(maxsize=1)

    def capture():
        try:
            mailbox.put_nowait(capture_playback_window(
                engine, now=wall_clock() if wall_clock is not None else None))
        except Exception:  # noqa: BLE001 -- includes queue.Full; never raise on GLib
            pass
        return False

    idle_add(capture)
    try:
        return mailbox.get(timeout=timeout)
    except queue.Empty:
        return None


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InstallResult:
    status: str
    reasons: tuple = ()
    install_key: str = ""
    install_id: int | None = None
    log_id: int | None = None
    placement_mode: str | None = None
    anchor_log_item_id: int | None = None
    installed_log_item_ids: tuple = ()
    track_ids: tuple = ()
    net_before_seconds: float | None = None
    residual_beyond_budget_seconds: float | None = None
    safety_budget_seconds: float | None = None
    insertion_lead_seconds: float | None = None
    proposal_status: str | None = None
    notes: tuple = field(default=(
        "committed rows are adopted by the engine's guarded queue reload; committed is not aired",
        EVIDENCE_NOTE,
    ))

    def as_dict(self):
        """Internal ids and numbers only; no listener data."""
        return {
            "status": self.status, "reasons": list(self.reasons), "install_key": self.install_key,
            "install_id": self.install_id, "log_id": self.log_id, "placement_mode": self.placement_mode,
            "anchor_log_item_id": self.anchor_log_item_id,
            "installed_log_item_ids": list(self.installed_log_item_ids), "track_ids": list(self.track_ids),
            "net_before_seconds": self.net_before_seconds,
            "residual_beyond_budget_seconds": self.residual_beyond_budget_seconds,
            "safety_budget_seconds": self.safety_budget_seconds,
            "insertion_lead_seconds": self.insertion_lead_seconds, "proposal_status": self.proposal_status,
            "aired": False, "notes": list(self.notes),
        }


class _Abort(Exception):
    def __init__(self, status, *reasons):
        super().__init__(status)
        self.status, self.reasons = status, reasons


def install_key_for(log_id, cause_ids):
    canonical = ",".join(str(i) for i in sorted(cause_ids))
    return f"refill:log{log_id}:causes:{hashlib.sha256(canonical.encode()).hexdigest()[:40]}"


def safety_budget():
    return (SAFETY_BUDGET_BASE_SECONDS, SAFETY_BUDGET_PER_ITEM_SECONDS)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Planned:
    proposal: object
    items: tuple            # Candidate objects in play order (music..., psa?)
    categories: tuple       # slot category id per item


class HourRefillInstaller:
    """One attempt per call to attempt(), on a worker thread."""

    def __init__(self, engine, *, idle_add, psa_policy=None, clock=time.monotonic, wall_clock=None, hooks=None):
        self.engine = engine
        self.idle_add = idle_add
        self.psa_policy = psa_policy
        self.clock = clock
        self.wall_clock = wall_clock  # None: station-local now, taken on GLib
        self.hooks = hooks or {}   # test-only fault injection points

    def _hook(self, name, *args):
        hook = self.hooks.get(name)
        if hook is not None:
            hook(*args)

    def run_in_worker(self):
        """attempt() with the worker thread's own connection lifecycle:
        recycle before, close after. Never call this on GLib."""
        close_old_connections()
        try:
            return self.attempt()
        finally:
            connection.close()

    def attempt(self):
        """One guarded attempt on the calling (worker) thread's connection."""
        if not install_permitted():
            return InstallResult(STATUS_DRY_RUN_ONLY, ("hour_refill_policy_dry_run_only_or_off",))
        coordinator = coordinator_for(self.engine)
        token = object()
        refused = coordinator.reserve(self.engine, token)
        if refused:
            return InstallResult(refused)
        adopted, log_id = None, None
        try:
            result = self._attempt(coordinator)
            if result.status == STATUS_INSTALLED:
                adopted, log_id = result.installed_log_item_ids, result.log_id
            return result
        finally:
            coordinator.release(self.engine, token, adopted_ids=adopted, log_id=log_id)

    # -- the attempt ---------------------------------------------------------

    def _attempt(self, coordinator):
        s1 = request_snapshot(self.engine, idle_add=self.idle_add, wall_clock=self.wall_clock)
        if s1 is None:
            return InstallResult(STATUS_SNAPSHOT_UNAVAILABLE)
        if s1.active_log_id is not None and not coordinator.count_attempt(self.engine, s1.active_log_id):
            return InstallResult(STATUS_ATTEMPT_LIMIT, log_id=s1.active_log_id)
        proposal = evaluate_hour_refill(s1, offline_dry_run=True, psa_policy=self.psa_policy,
                                        safety_budget=safety_budget())
        if proposal.status != STATUS_PROPOSED:
            return InstallResult(proposal.status, proposal.reasons, log_id=s1.active_log_id,
                                 net_before_seconds=proposal.net_before_seconds,
                                 proposal_status=proposal.status)
        planned = self._planned(proposal)
        self._hook("before_second_snapshot")
        s2 = request_snapshot(self.engine, idle_add=self.idle_add, wall_clock=self.wall_clock)
        received = self.clock()
        if s2 is None:
            return InstallResult(STATUS_SNAPSHOT_UNAVAILABLE)
        try:
            # Last file check, outside any database lock.
            if any(not Path(c.filepath).is_file() for c in planned.items):
                raise _Abort(STATUS_STALE, "media_missing")
            frame, lead = self._cas_one(s1, s2, proposal, planned)
            self._hook("before_transaction", s2)
            return self._commit(s2, proposal, planned, frame, lead, received)
        except _Abort as abort:
            return InstallResult(abort.status, abort.reasons, log_id=s2.active_log_id,
                                 net_before_seconds=proposal.net_before_seconds,
                                 proposal_status=proposal.status)

    def _planned(self, proposal):
        """The proposal's Candidates, recovered from its selection pass."""
        categories = [m.category_id for m in proposal.music]
        if proposal.psa is not None:
            categories.append(proposal.psa.category_id)
        return _Planned(proposal, tuple(proposal.chosen), tuple(categories))

    @staticmethod
    def _selection_signature(planned):
        return tuple(
            (
                candidate.track_id, category_id, candidate.cue_in,
                candidate.end, candidate.effective, candidate.filepath,
                tuple(sorted(candidate.identity_keys)),
            )
            for candidate, category_id in zip(planned.items, planned.categories)
        )

    def _cas_one(self, s1, s2, proposal, planned):
        """S2 must equal S1 and re-prove the window, threshold, placement,
        budget and lead on S2's own positions (pure + boundary reads)."""
        if s2.engine_instance != s1.engine_instance:
            raise _Abort(STATUS_STALE, "engine_instance_changed")
        if s2.active_log_id != s1.active_log_id:
            raise _Abort(STATUS_STALE, "active_log_changed")
        if s2.digest() != s1.digest():
            raise _Abort(STATUS_STALE, "queue_or_deck_changed")
        boundary = resolve_boundary(s2)
        before = proposal.projection.boundary
        if (boundary.kind, boundary.instant, boundary.source) != (before.kind, before.instant, before.source):
            raise _Abort(STATUS_STALE, "boundary_changed")
        projection = project_refill(s2, causes=load_relevant_causes(s2), boundary=boundary)
        if not projection.eligible:
            raise _Abort(projection.outcome, *projection.reasons)
        if ([c.cause_id for c in projection.qualified_causes]
                != [c.cause_id for c in proposal.projection.qualified_causes]):
            raise _Abort(STATUS_STALE, "cause_set_changed")
        placement = assess_placement(s2, load_request_context())
        if (placement.mode, placement.anchor_log_item_id) != (proposal.placement.mode,
                                                              proposal.placement.anchor_log_item_id):
            raise _Abort(STATUS_NO_SAFE_ANCHOR if placement.mode is None else STATUS_STALE,
                         f"placement_changed:{placement.reason}")
        frame = build_fit_frame(s2, projection, placement, safety_budget=safety_budget())
        if frame is None:
            raise _Abort(STATUS_NO_SAFE_ANCHOR, "anchor_not_in_committed_sequence")
        if frame.window - frame.total(list(planned.items)) < 0:
            raise _Abort(STATUS_UNSAFE, "safety_budget_not_met")
        # Measured to the trigger that will read the first new row (its
        # audible start minus its own cue-in), not to its audible start.
        lead = frame.insertion_lead(planned.items[0]) - (planned.items[0].cue_in or 0.0)
        if lead < INSERTION_LEAD_SECONDS:
            raise _Abort(STATUS_UNSAFE, "insertion_too_soon")

        # S1 selected the candidates. Re-run the complete bounded Phase B
        # selection from S2 so category membership/kind, recency, holidays,
        # related-artist separation, next-hour commitments, requests and media
        # are all fresh too. Matching queue digests alone do not cover any of
        # those database-side eligibility inputs.
        fresh = evaluate_hour_refill(
            s2, offline_dry_run=True, psa_policy=self.psa_policy,
            safety_budget=safety_budget(),
        )
        if fresh.status != STATUS_PROPOSED:
            raise _Abort(STATUS_STALE, f"candidate_revalidation:{fresh.status}")
        fresh_planned = self._planned(fresh)
        if self._selection_signature(fresh_planned) != self._selection_signature(planned):
            raise _Abort(STATUS_STALE, "candidate_selection_changed")
        return frame, lead

    def _commit(self, s2, proposal, planned, frame, lead, received):
        from library.models import Category, HourRefillInstall, LogItem, PlaylistLog, Track
        from library.services.log_builder import _active_holidays_at
        from library.services.related_artists import track_identity_keys
        from webrequests.models import SongRequest

        placement = proposal.placement
        cause_ids = [c.cause_id for c in proposal.projection.qualified_causes]
        key = install_key_for(s2.active_log_id, cause_ids)
        track_ids = [c.track_id for c in planned.items]
        k = len(planned.items)
        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                log = PlaylistLog.objects.select_for_update().get(pk=s2.active_log_id)
                self._hook("locked")
                if HourRefillInstall.objects.filter(install_key=key).exists():
                    raise _Abort(STATUS_DUPLICATE, "install_key_exists")
                if (log.status, log.date, log.hour) != ("approved", s2.active_log_date, s2.active_log_hour):
                    raise _Abort(STATUS_STALE, "log_changed")
                rows = list(LogItem.objects.filter(playlist_log_id=log.id).order_by("position").values_list(
                    "id", "position", "track_id", "category_id", "playback_claimed_at", "played_at"))
                visible = [r for r in rows if (log.id, r[0]) not in s2.poison_skip]
                expected = [(it.log_item_id, it.position, it.track_id, it.category_id) for it in s2.queue]
                if [r[:4] for r in visible] != expected:
                    raise _Abort(STATUS_STALE, "rows_changed")
                pending = {it.log_item_id for it in s2.queue[s2.cursor:]}
                if any((r[4] is not None or r[5] is not None) for r in visible if r[0] in pending):
                    raise _Abort(STATUS_STALE, "engine_advanced")
                tracks = {t["id"]: t for t in Track.objects.filter(id__in=track_ids).values(
                    "id", "ready2air", "blocked_slots", "cue_in_seconds", "next_start_seconds",
                    "duration_seconds", "filepath", "title", "artist__name", "category_id",
                    "related_artists")}
                category_kinds = dict(Category.objects.filter(id__in=planned.categories)
                                      .values_list("id", "kind__code"))
                additional = set(Track.additional_categories.through.objects.filter(
                    track_id__in=track_ids, category_id__in=planned.categories,
                ).values_list("track_id", "category_id"))
                slots = set(_window_slots(s2.captured_at, proposal.projection.boundary.instant,
                                          timezone.get_current_timezone()))
                for index, (candidate, category_id) in enumerate(zip(planned.items, planned.categories)):
                    t = tracks.get(candidate.track_id)
                    expected_kind = "music" if index < len(proposal.music) else PSA_CATEGORY_KIND
                    if category_kinds.get(category_id) != expected_kind:
                        raise _Abort(STATUS_STALE, "category_changed")
                    if t is not None and t["category_id"] != category_id \
                            and (candidate.track_id, category_id) not in additional:
                        raise _Abort(STATUS_STALE, "category_membership_changed")
                    if t is not None and slots & set(t["blocked_slots"] or []):
                        raise _Abort(STATUS_STALE, "track_restricted")
                    end = t["next_start_seconds"] if t and t["next_start_seconds"] is not None else (
                        t["duration_seconds"] if t else None)
                    if (t is None or not t["ready2air"] or t["filepath"] != candidate.filepath
                            or (t["cue_in_seconds"] or 0.0) != candidate.cue_in or end != candidate.end):
                        raise _Abort(STATUS_STALE, "track_changed")
                    if track_identity_keys(t["artist__name"], t["related_artists"]) != candidate.identity_keys:
                        raise _Abort(STATUS_STALE, "track_identity_changed")
                active_holidays = [h.code for h in _active_holidays_at(
                    s2.captured_at.astimezone(timezone.get_current_timezone()))]
                if active_holidays and Track.objects.filter(
                        id__in=track_ids, holidays__code__in=active_holidays).exists():
                    raise _Abort(STATUS_STALE, "track_holiday_changed")
                if SongRequest.objects.filter(status__in=SongRequest.ACTIVE_STATUSES,
                                              track_id__in=track_ids).exists():
                    raise _Abort(STATUS_STALE, "track_requested")
                if placement.mode == "insert_before":
                    new_positions = self._open_gap(log, rows, placement, s2, k)
                else:
                    if not rows or rows[-1][0] != placement.after_log_item_id:
                        raise _Abort(STATUS_STALE, "tail_changed")
                    new_positions = [rows[-1][1] + 1 + i for i in range(k)]
                start = s2.captured_at + timedelta(seconds=frame.insertion_lead(planned.items[0]))
                created = []
                for index, (candidate, category_id, position) in enumerate(
                        zip(planned.items, planned.categories, new_positions)):
                    t = tracks[candidate.track_id]
                    created.append(LogItem.objects.create(
                        playlist_log=log, position=position, scheduled_time=start, track_id=t["id"],
                        track_title=t["title"], track_artist=t["artist__name"] or "", category_id=category_id,
                        origin=LogItem.ORIGIN_REFILL_INSTALLED,
                    ))
                    start = start + timedelta(seconds=candidate.effective)
                residual = frame.window - frame.total(list(planned.items))
                ledger = HourRefillInstall.objects.create(
                    install_key=key, playlist_log_id_snapshot=log.id, log_date=log.date, log_hour=log.hour,
                    cause_ids=cause_ids, placement_mode=placement.mode,
                    anchor_log_item_id=placement.anchor_log_item_id,
                    installed_log_item_ids=[item.id for item in created], track_ids=track_ids,
                    net_before_seconds=proposal.projection.net_shortage_seconds,
                    residual_after_seconds=round(residual, 3),
                    safety_budget_seconds=frame.reserve(k), engine_instance=s2.engine_instance,
                    snapshot_digest=s2.digest(),
                )
                self._hook("before_commit")
                if self.clock() - received > MAX_COMMIT_AGE_SECONDS:
                    raise _Abort(STATUS_STALE, "snapshot_too_old_at_commit")
        except _Abort:
            raise
        except IntegrityError:
            raise _Abort(STATUS_DUPLICATE, "integrity_conflict")
        except OperationalError as exc:
            cause = getattr(exc, "__cause__", None)
            code = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            if code in ("55P03", "40P01"):
                raise _Abort(STATUS_LOCK_CONTENDED, "lock_timeout_or_deadlock")
            raise _Abort(STATUS_LOCK_CONTENDED, "database_operational_error")
        return InstallResult(
            STATUS_INSTALLED, ("committed",), install_key=key, install_id=ledger.id, log_id=log.id,
            placement_mode=placement.mode, anchor_log_item_id=placement.anchor_log_item_id,
            installed_log_item_ids=tuple(item.id for item in created), track_ids=tuple(track_ids),
            net_before_seconds=proposal.projection.net_shortage_seconds,
            residual_beyond_budget_seconds=round(residual, 3), safety_budget_seconds=frame.reserve(k),
            insertion_lead_seconds=round(lead, 3), proposal_status=proposal.status,
        )

    def _open_gap(self, log, rows, placement, s2, k):
        """Shift ONLY the protected closing block up by k (two passes, no
        unique-position collision) and return the freed positions."""
        from library.models import LogItem
        from webrequests.models import SongRequest

        ids = [r[0] for r in rows]
        if placement.anchor_log_item_id not in ids:
            raise _Abort(STATUS_NO_SAFE_ANCHOR, "anchor_missing")
        index = ids.index(placement.anchor_log_item_id)
        anchor_position = rows[index][1]
        block = rows[index:]
        snapshot_ids = [it.log_item_id for it in s2.queue]
        if placement.anchor_log_item_id not in snapshot_ids or \
                [r[0] for r in block] != snapshot_ids[snapshot_ids.index(placement.anchor_log_item_id):]:
            raise _Abort(STATUS_NO_SAFE_ANCHOR, "closing_block_changed")
        if any(r[4] is not None or r[5] is not None for r in block):
            raise _Abort(STATUS_NO_SAFE_ANCHOR, "closing_block_claimed")
        block_ids = [r[0] for r in block]
        if SongRequest.objects.filter(log_item_id__in=block_ids).exists() or \
                SongRequest.objects.filter(intro_log_item_id__in=block_ids).exists():
            raise _Abort(STATUS_NO_SAFE_ANCHOR, "closing_block_request_link")
        LogItem.objects.filter(playlist_log_id=log.id, position__gte=anchor_position).update(
            position=F("position") + SHIFT_OFFSET)
        LogItem.objects.filter(playlist_log_id=log.id, position__gte=anchor_position + SHIFT_OFFSET).update(
            position=F("position") - (SHIFT_OFFSET - k))
        return [anchor_position + i for i in range(k)]
