"""[P0] 1.3B2 — local input hotplug recovery: pure/testable building
blocks, deliberately GStreamer-agnostic and Django-agnostic so they can be
unit-tested without hardware, a running pipeline, or a database.

This module is the "PURE / TESTABLE" half described in
scratchpad/audio_recovery/PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md; the
"ENGINE-INTEGRATED" half (selector switching, PTT-off semantics, GLib
scheduling, bus routing) lives in library/services/engine.py and imports
this module.

Everything here is the direct, deliberately-faithful production port of
eight rounds of scratchpad investigation under
scratchpad/audio_recovery/ -- see PHASE_P0_1.3_DISCOVERY_AND_DESIGN.md for
the full empirical record. In particular:

- classify_audio_device_error(): signatures are the ACTUAL captured
  GStreamer error/debug text from physical USB device-loss testing
  (scratchpad/audio_recovery/hotplug_harness/logs/*.jsonl), not guessed.
- SlotCoordinator: a faithful port of
  scratchpad/audio_recovery/hotplug_harness/slot_guard.py (Round 3),
  whose synthetic test suite already proved request coalescing, per-slot
  locking, generation-tagged workers, stale-result discarding, and
  bounded-worker abandonment. Only ONE method (mark_degraded) is new here
  -- the original slot_guard.py never needed an explicit "the owner
  observed a fresh failure" transition because its own test harness
  managed slot.state directly; everything else is unchanged.
"""
from __future__ import annotations

import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------
# Confirmed-captured (scratchpad/audio_recovery, Rounds 1-8, physical USB
# unplug against a real alsasrc/alsasink):
#   "gst-resource-error-quark: Error recording from audio device. The
#    device has been disconnected. (9)"
#   "gst-resource-error-quark: Error outputting to audio device. The
#    device has been disconnected. (10)"
#   "gst-resource-error-quark: Could not open audio device for recording.
#    (5)" with debug text "...No such device" (Harness E, opening a
#    nonexistent device name)
# Both the *message* and *debug* text must be checked -- Harness E's
# "Could not open" signature only carries "No such device" in the debug
# string, not the message. A harness bug (matching err_text only) would
# have misclassified it as "unknown"; fixed here from the start.
_DEVICE_LOST_SIGNATURES = ("disconnected", "no such device", "enodev")

# NOT empirically captured in this investigation (no round exercised
# concurrent-open contention) -- included because "Device or resource
# busy" (EBUSY) is the standard, well-documented ALSA errno string for
# exactly the Round-3 open question this feature's teardown/rebuild
# sequencing is built around: a fresh alsasrc trying to open a device
# whose previous handle is still held by an abandoned, wedged teardown.
# Classified "transient" (not device_lost -- don't tear down further;
# not "unknown" -- caller may reasonably back off and retry the SAME
# generation instead of discarding it) rather than treated as a fresh
# device-loss event. Flagged here, and again in the 1.3B2 report, as
# principled-but-unverified.
_TRANSIENT_SIGNATURES = ("busy",)


def classify_audio_device_error(err_text: str, debug_text: str = "") -> str:
    """Pure classifier -- no GStreamer objects, just the rendered message
    and debug strings from message.parse_error(). Returns one of
    "device_lost", "transient", "unknown". Only "device_lost" should ever
    trigger recovery action; "transient" and "unknown" must be logged/
    observable only, never destructive, per the locked policy."""
    combined = f"{err_text} {debug_text}".lower()
    if any(sig in combined for sig in _DEVICE_LOST_SIGNATURES):
        return "device_lost"
    if any(sig in combined for sig in _TRANSIENT_SIGNATURES):
        return "transient"
    return "unknown"


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------
# Capped exponential, approximately 1, 2, 4, 8, 16, 30, 30... seconds,
# with modest jitter -- matches the task's own prescribed schedule.
_BACKOFF_STEPS_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_BACKOFF_CAP_S = 30.0
_BACKOFF_JITTER_FRACTION = 0.15


def compute_backoff_seconds(attempt: int, *, jitter: bool = True,
                             rng: Optional[random.Random] = None) -> float:
    """attempt is 1-indexed (the first retry after a failure is attempt=1).
    Deterministic base schedule with optional +/-15% jitter (a fresh
    random.Random() per call by default, or an injected one for
    reproducible tests)."""
    if attempt <= 0:
        base = _BACKOFF_STEPS_S[0]
    elif attempt <= len(_BACKOFF_STEPS_S):
        base = _BACKOFF_STEPS_S[attempt - 1]
    else:
        base = _BACKOFF_CAP_S
    if not jitter:
        return base
    r = rng or random.Random()
    spread = base * _BACKOFF_JITTER_FRACTION
    return max(0.1, base + r.uniform(-spread, spread))


# ---------------------------------------------------------------------------
# ALSA card presence -- dependency-free, GLib-thread-safe (plain file read,
# no subprocess, no pyudev). Deliberately NOT calling `arecord -l`/`aplay
# -l` (already used elsewhere in hardware/devices.py for admin-form
# rendering) -- those are subprocess calls, and the task's own locked
# finding is explicit: "Do not run blocking aplay -l / arecord -l
# subprocesses on the GLib thread."
# ---------------------------------------------------------------------------
_ALSA_CARDS_PATH = Path("/proc/asound/cards")
# Matches the first line of each two-line /proc/asound/cards record, e.g.:
#  " 5 [CODEC          ]: USB-Audio - USB Audio CODEC"
_ALSA_CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]*)\]:\s*(.*)$")


def parse_alsa_cards(text: str) -> dict:
    """Pure parser: /proc/asound/cards-style text -> {short_id: index}.
    short_id is stripped of the padding /proc/asound/cards uses inside
    the brackets (e.g. "CODEC          " -> "CODEC"). Injectable text
    parameter so this is fully unit-testable without reading any real
    file."""
    out = {}
    for line in text.splitlines():
        m = _ALSA_CARD_LINE_RE.match(line)
        if not m:
            continue
        index, short_id, _rest = m.groups()
        out[short_id.strip()] = int(index)
    return out


def read_alsa_cards_present() -> dict:
    """Thin, best-effort IO wrapper around parse_alsa_cards(). Never
    raises -- an unreadable /proc/asound/cards (e.g. running somewhere
    without ALSA at all, such as a CI sandbox) degrades to "no cards
    present" rather than crashing the caller."""
    try:
        text = _ALSA_CARDS_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    return parse_alsa_cards(text)


def resolve_runtime_device(identity_kind: str, identity: str,
                            legacy_device: str) -> Optional[str]:
    """Pure resolution: given a configured identity (kind + value) and the
    legacy raw `device` field, return the device string to hand to
    alsasrc's `device` property.

    "alsa_card_id" uses ALSA's own native `plughw:CARD=<id>,DEV=0` name
    syntax (confirmed working across all eight rounds of physical testing
    via the identical scratchpad/audio_recovery/hotplug_harness/_common.py
    AlsaCard.plughw_card_form property) -- ALSA resolves the short card
    id to whatever numeric index it currently has internally, at PCM-open
    time, so this function never needs to do that resolution itself and
    is correct even if the card re-enumerates at a different index after
    a replug.

    Anything else (unset/"", or an unrecognized kind) falls back to the
    legacy raw device string unchanged -- zero behavior change for any
    AudioInput row that hasn't opted into the new identity fields."""
    if identity_kind == "alsa_card_id" and identity:
        return f"plughw:CARD={identity},DEV=0"
    return legacy_device or None


def alsa_card_identity_present(identity: str, cards: Optional[dict] = None) -> bool:
    """True iff a card with this short id is currently enumerated.
    `cards` is injectable (from parse_alsa_cards) for testability;
    defaults to a fresh read_alsa_cards_present() call."""
    if not identity:
        return False
    if cards is None:
        cards = read_alsa_cards_present()
    return identity in cards


# ---------------------------------------------------------------------------
# SlotCoordinator -- faithful port of scratchpad/audio_recovery/
# hotplug_harness/slot_guard.py (Round 3). See that file's own extensive
# docstring for the full design rationale (two state machines, the single
# rule that makes late-result handling safe, per-slot-only locking,
# daemon-thread shutdown semantics, and the still-open Round-3 question
# about reopening a device while an abandoned close() may still hold it).
# Reproduced here verbatim except for one additive method, mark_degraded().
# ---------------------------------------------------------------------------


class SlotState(Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    QUIESCING = "QUIESCING"
    RECOVERING = "RECOVERING"
    RESTART_REQUIRED = "RESTART_REQUIRED"


class OperationState(Enum):
    NONE = "NONE"
    IN_FLIGHT = "IN_FLIGHT"
    RETURNED = "RETURNED"
    TIMED_OUT_ABANDONED = "TIMED_OUT_ABANDONED"


@dataclass
class OperationRecord:
    op_id: int
    generation: int
    dispatched_at: float
    done: bool = False
    succeeded: bool = False
    exc: Optional[BaseException] = None
    completed_at: Optional[float] = None
    abandoned: bool = False


class SlotCoordinator:
    """Owns exactly one hardware slot's operation lifecycle. Construct one
    per slot (mic today; a future output slot would get its own,
    independent instance -- never share one instance or its lock across
    slots, per the core invariant this whole design exists to enforce)."""

    def __init__(self, name: str, timeout_s: float = 15.0, log=None):
        self.name = name
        self.timeout_s = timeout_s
        self._log = log  # optional object with .emit(kind, **fields); may be None in production
        self._lock = threading.Lock()

        self.generation = 0
        self.state = SlotState.OK
        self._op: Optional[OperationRecord] = None
        self._next_op_id = 1
        self._recovery_requested_again = False
        self._abandoned_generations: list = []

    def _emit(self, kind: str, **fields):
        if self._log is not None:
            self._log.emit(kind, slot=self.name, **fields)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "slot": self.name,
                "state": self.state.value,
                "generation": self.generation,
                "operation_state": (self._op.done and
                                     (OperationState.TIMED_OUT_ABANDONED if self._op.abandoned
                                      else OperationState.RETURNED) or OperationState.IN_FLIGHT).value
                                    if self._op is not None else OperationState.NONE.value,
                # New in 1.3B4 -- the explicit result of the most recently
                # RESOLVED operation on record (True/False), or None if no
                # operation has ever completed yet. Exists specifically so
                # a caller can distinguish "this DEGRADED state was reached
                # because a worker genuinely failed" from "the owner called
                # mark_degraded() again deliberately, reusing this same
                # already-succeeded record" -- mark_degraded() never
                # touches self._op, so its value keeps reflecting whatever
                # operation last actually ran, exactly what's needed here.
                "operation_succeeded": self._op.succeeded if (self._op is not None and self._op.done) else None,
                "abandoned_generations": list(self._abandoned_generations),
                "recovery_requested_again": self._recovery_requested_again,
            }

    def mark_degraded(self) -> bool:
        """New in 1.3B2 -- the one addition to the original slot_guard.py
        contract. Called by the owner on a freshly classified failure.
        Returns True only on the OK -> DEGRADED transition (i.e. "this is
        the first failure notification for the current generation");
        False if the slot was already non-OK (a coalesced/repeat failure
        notification, or a deliberate re-degrade after a quiesce-only
        success -- see engine.py's _mic_handle_slot_transition)."""
        with self._lock:
            if self.state != SlotState.OK:
                return False
            self.state = SlotState.DEGRADED
            self._emit("slot_marked_degraded", generation=self.generation)
            return True

    def request_recovery(self, worker_fn: Callable[[], bool], now: Optional[float] = None) -> str:
        now = now if now is not None else time.monotonic()
        with self._lock:
            if self.state == SlotState.OK:
                return "ignored_ok"
            if self.state == SlotState.RESTART_REQUIRED:
                self._recovery_requested_again = True
                return "ignored_restart_required"
            if self._op is not None and not self._op.done and not self._op.abandoned:
                self._recovery_requested_again = True
                self._emit("recovery_coalesced", generation=self.generation)
                return "coalesced"
            return self._dispatch_locked(worker_fn, now)

    def retry_now(self, worker_fn: Callable[[], bool], now: Optional[float] = None) -> str:
        now = now if now is not None else time.monotonic()
        with self._lock:
            if self._op is not None and not self._op.done and not self._op.abandoned:
                self._recovery_requested_again = True
                self._emit("recovery_coalesced", generation=self.generation, via="retry_now")
                return "coalesced"
            if self.state == SlotState.OK:
                return "ignored_ok"
            return self._dispatch_locked(worker_fn, now, bump_generation=(self.state == SlotState.RESTART_REQUIRED))

    def _dispatch_locked(self, worker_fn, now, bump_generation=True) -> str:
        if bump_generation:
            self.generation += 1
        gen = self.generation
        op_id = self._next_op_id
        self._next_op_id += 1
        record = OperationRecord(op_id=op_id, generation=gen, dispatched_at=now)
        self._op = record
        self.state = SlotState.RECOVERING
        self._recovery_requested_again = False
        self._emit("operation_dispatched", generation=gen, op_id=op_id)

        def runner():
            try:
                ok = worker_fn()
            except Exception as exc:  # noqa: BLE001 -- worker failure is data, not a coordinator crash
                with self._lock:
                    if record.abandoned:
                        self._emit("late_worker_result_discarded", generation=gen, op_id=op_id,
                                    reason="already abandoned", exc=repr(exc))
                        return
                    record.done = True
                    record.succeeded = False
                    record.exc = exc
                    record.completed_at = time.monotonic()
                self._on_worker_resolved(record)
                return
            with self._lock:
                if record.abandoned:
                    self._emit("late_worker_result_discarded", generation=gen, op_id=op_id,
                                reason="already abandoned", succeeded=ok)
                    return
                record.done = True
                record.succeeded = ok
                record.completed_at = time.monotonic()
            self._on_worker_resolved(record)

        t = threading.Thread(target=runner, name=f"slot-{self.name}-op{op_id}", daemon=True)
        t.start()
        return "dispatched"

    def tick(self, now: Optional[float] = None):
        now = now if now is not None else time.monotonic()
        with self._lock:
            record = self._op
            if record is None or record.done or record.abandoned:
                return
            if now - record.dispatched_at >= self.timeout_s:
                record.abandoned = True
                self._abandoned_generations.append(record.generation)
                self.state = SlotState.RESTART_REQUIRED
                self._emit("operation_abandoned", generation=record.generation, op_id=record.op_id,
                            elapsed_s=round(now - record.dispatched_at, 3),
                            note="background thread left running -- NOT killed, will be reclaimed "
                                 "only on process exit")
                self._recovery_requested_again = False

    def _on_worker_resolved(self, record: OperationRecord):
        with self._lock:
            if record is not self._op:
                self._emit("stale_worker_result_ignored", generation=record.generation,
                            op_id=record.op_id, reason="not the current operation")
                return
            if record.generation != self.generation:
                self._emit("stale_worker_result_ignored", generation=record.generation,
                            op_id=record.op_id, current_generation=self.generation,
                            reason="generation mismatch")
                return
            if record.abandoned:
                self._emit("late_worker_result_discarded", generation=record.generation,
                            op_id=record.op_id, reason="abandoned just before this callback ran")
                return
            if record.succeeded:
                self.state = SlotState.OK
                self._emit("operation_succeeded", generation=record.generation, op_id=record.op_id,
                            elapsed_s=round(record.completed_at - record.dispatched_at, 3))
            else:
                self.state = SlotState.DEGRADED
                self._emit("operation_failed", generation=record.generation, op_id=record.op_id,
                            exc=repr(record.exc) if record.exc else None)
            if self._recovery_requested_again:
                self._recovery_requested_again = False
                self._emit("coalesced_request_cleared", generation=record.generation)


# ---------------------------------------------------------------------------
# Bounded teardown worker
# ---------------------------------------------------------------------------


@dataclass
class TeardownRecord:
    """One isolated generation's potentially-blocking teardown call."""

    generation: int
    worker_fn: Callable[[], Any]
    metadata: dict
    submitted_at: float
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Any = None
    exc: Optional[BaseException] = None
    abandoned: bool = False


class BoundedTeardownCoordinator:
    """Serialize hazardous teardown calls on one bounded daemon worker.

    This deliberately differs from :class:`SlotCoordinator`: deck retirement
    has no rebuild/retry state machine. The live topology is detached by the
    owner first; this helper owns only the final operation which may hang.

    Exactly one daemon worker is created lazily. If an operation exceeds the
    timeout the coordinator becomes permanently poisoned, abandons its fixed-
    capacity queue, and rejects future operations. It never creates a
    replacement thread for a worker Python cannot safely kill. The owner can
    drain structured events from its GLib/main thread for operator reporting.
    """

    def __init__(self, name: str, *, timeout_s: float = 15.0, queue_capacity: int = 4):
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        self.name = name
        self.timeout_s = timeout_s
        self.queue_capacity = queue_capacity
        self._condition = threading.Condition()
        self._pending = deque()
        self._active: Optional[TeardownRecord] = None
        self._events = deque()
        self._worker = None
        self._stopping = False
        self._poisoned = False
        self._worker_starts = 0
        self._completed = 0
        self._rejected = 0
        self._poison_rejection_reported = False
        self._suppressed_poison_rejections = 0
        self._abandoned_generations = []

    def _event_locked(self, kind: str, record: TeardownRecord, **fields) -> None:
        self._events.append(
            {
                "kind": kind,
                "generation": record.generation,
                "metadata": dict(record.metadata),
                **fields,
            }
        )

    def _start_worker_locked(self) -> None:
        if self._worker is not None:
            return
        self._worker_starts += 1
        self._worker = threading.Thread(
            target=self._run,
            name=f"{self.name}-worker",
            daemon=True,
        )
        self._worker.start()

    def submit(
        self,
        generation: int,
        worker_fn: Callable[[], Any],
        *,
        metadata: Optional[dict] = None,
        now: Optional[float] = None,
    ) -> str:
        """Queue one generation, or reject it without spawning a worker."""

        record = TeardownRecord(
            generation=generation,
            worker_fn=worker_fn,
            metadata=dict(metadata or {}),
            submitted_at=now if now is not None else time.monotonic(),
        )
        with self._condition:
            if self._stopping:
                self._rejected += 1
                self._event_locked("rejected_stopping", record)
                return "rejected_stopping"
            if self._poisoned:
                self._rejected += 1
                # The timeout event already made the restart-required state
                # operator-visible. Report the first later rejected teardown
                # once, then expose only counters in snapshot() rather than
                # emitting one critical event per otherwise-healthy deck.
                if not self._poison_rejection_reported:
                    self._poison_rejection_reported = True
                    self._event_locked("rejected_poisoned", record)
                else:
                    self._suppressed_poison_rejections += 1
                return "rejected_poisoned"
            generations = {item.generation for item in self._pending}
            if self._active is not None:
                generations.add(self._active.generation)
            if generation in generations:
                self._event_locked("duplicate_ignored", record)
                return "duplicate"
            if len(self._pending) >= self.queue_capacity:
                self._rejected += 1
                self._event_locked("rejected_full", record)
                return "rejected_full"
            self._pending.append(record)
            self._event_locked("queued", record, queue_depth=len(self._pending))
            self._start_worker_locked()
            self._condition.notify()
            return "queued"

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping and not self._pending:
                    return
                record = self._pending.popleft()
                if self._poisoned:
                    record.abandoned = True
                    self._abandoned_generations.append(record.generation)
                    self._event_locked("abandoned_without_start", record, reason="coordinator_poisoned")
                    continue
                self._active = record
                record.started_at = time.monotonic()
                self._event_locked("started", record)
            try:
                result = record.worker_fn()
            except Exception as exc:  # noqa: BLE001 -- failure is reported as coordinator data
                record.exc = exc
            else:
                record.result = result
            completed_at = time.monotonic()
            with self._condition:
                record.completed_at = completed_at
                if record.abandoned:
                    self._event_locked(
                        "late_result_discarded",
                        record,
                        elapsed_s=round(completed_at - (record.started_at or completed_at), 3),
                    )
                    return
                self._completed += 1
                self._event_locked(
                    "failed" if record.exc is not None else "completed",
                    record,
                    elapsed_s=round(completed_at - (record.started_at or completed_at), 3),
                    error=repr(record.exc) if record.exc is not None else None,
                )
                self._active = None

    def tick(self, *, now: Optional[float] = None) -> None:
        """Detect one timeout without waiting for or replacing the worker."""

        current = now if now is not None else time.monotonic()
        with self._condition:
            record = self._active
            if (
                record is None
                or record.started_at is None
                or record.abandoned
                or current - record.started_at < self.timeout_s
            ):
                return
            record.abandoned = True
            self._poisoned = True
            self._abandoned_generations.append(record.generation)
            self._event_locked(
                "timed_out",
                record,
                elapsed_s=round(current - record.started_at, 3),
            )
            while self._pending:
                queued = self._pending.popleft()
                queued.abandoned = True
                self._abandoned_generations.append(queued.generation)
                self._event_locked("abandoned_without_start", queued, reason="earlier_operation_timed_out")

    def drain_events(self) -> list[dict]:
        with self._condition:
            events = list(self._events)
            self._events.clear()
            return events

    def snapshot(self) -> dict:
        with self._condition:
            return {
                "name": self.name,
                "poisoned": self._poisoned,
                "stopping": self._stopping,
                "worker_starts": self._worker_starts,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
                "active_generation": self._active.generation if self._active else None,
                "queue_depth": len(self._pending),
                "completed": self._completed,
                "rejected": self._rejected,
                "suppressed_poison_rejections": self._suppressed_poison_rejections,
                "abandoned_generations": list(self._abandoned_generations),
            }

    def stop(self) -> None:
        """Ask an idle worker to exit; deliberately never join a hung one."""

        with self._condition:
            self._stopping = True
            self._condition.notify_all()
