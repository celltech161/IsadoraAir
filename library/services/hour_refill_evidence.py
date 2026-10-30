"""Roadmap 2.27 Phase A -- the in-process hour-refill evidence writer.

One daemon thread per PlaybackEngine owns every database operation for
evidence captured on the engine thread (completed Ejects, completed Start
Next cuts, engine-path request swaps). The engine side only reads cached
booleans and calls offer(), a bounded put_nowait. It never waits, queries,
locks or logs per event.

Best effort by design (operator decision): an event is dropped -- counted,
never retried -- when capture is Off, the worker isn't running, the queue is
full, the policy can't be read or the insert fails. Events still queued at a
hard crash, SIGKILL or power loss are lost. An orderly stop drains for at
most DRAIN_SECONDS. HourRefillCause.source_key suppresses duplicates.
"""
from __future__ import annotations

import queue
import threading
import time

from django.db import IntegrityError, close_old_connections, connection, transaction

MAX_QUEUED = 64
POLICY_REFRESH_SECONDS = 30.0
DRAIN_SECONDS = 1.0

_STOP = object()


def _read_capture_flags():
    from library.models import HourRefillPolicy

    return HourRefillPolicy.capture_switches(include_automatic=True)


def _insert_cause(evidence):
    from library.models import HourRefillCause

    with transaction.atomic():
        HourRefillCause.objects.create(**evidence.cause_fields())


class HourRefillEvidenceRecorder:
    """Engine-owned evidence writer. Start once; stop once at shutdown."""

    def __init__(self, *, max_queued=MAX_QUEUED, policy_refresh_seconds=POLICY_REFRESH_SECONDS,
                 read_flags=_read_capture_flags, insert=_insert_cause, on_recorded=None):
        self._queue = queue.Queue(maxsize=max_queued)
        # Phase D: called on this worker thread after each committed cause
        # (the runner's coalescing wake). Must not block; errors are ignored.
        self._on_recorded = on_recorded
        self._policy_refresh_seconds = policy_refresh_seconds
        self._read_flags = read_flags
        self._insert = insert
        self._start_lock = threading.Lock()
        self._thread = None
        self._stopping = threading.Event()
        self._drain_expired = threading.Event()
        self._policy_loaded = threading.Event()
        # Engine-side gates. Written only by the worker; False until the first
        # successful policy read and after any failed one (fail closed).
        self.capture_requests = False
        self.capture_eject = False
        self.capture_start_next = False
        self.capture_automatic_skip = False
        self.capture_media_failure = False
        self.counters = {
            "recorded": 0, "duplicate": 0, "dropped_full": 0, "dropped_not_running": 0,
            "dropped_policy_off": 0, "dropped_error": 0, "dropped_shutdown": 0,
            "dropped_unknown_type": 0, "snapshot_error": 0,
        }

    # -- engine thread -------------------------------------------------------

    def offer(self, evidence):
        """Hand one evidence object to the worker without waiting. Never
        raises. Returns True only if queued."""
        try:
            if self._thread is None or not self._thread.is_alive() or self._stopping.is_set():
                self._count("dropped_not_running", emit=False)
                return False
            self._queue.put_nowait(evidence)
            return True
        except queue.Full:
            self._count("dropped_full", emit=False)
            return False
        except Exception:  # noqa: BLE001
            self._count("dropped_error", emit=False)
            return False

    def note_snapshot_error(self):
        self._count("snapshot_error", emit=False)

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._drain_expired.clear()
            self._thread = threading.Thread(target=self._run, name="hour-refill-evidence", daemon=True)
            self._thread.start()

    def stop(self, drain_seconds=DRAIN_SECONDS):
        """Stop accepting events, let the worker write what is already queued
        for at most `drain_seconds`, then return. Anything left is dropped."""
        thread = self._thread
        self.capture_requests = False
        self.capture_eject = False
        self.capture_start_next = False
        self.capture_automatic_skip = False
        self.capture_media_failure = False
        self._stopping.set()
        if thread is None:
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass  # the worker sees _stopping after its current item
        thread.join(timeout=max(0.0, drain_seconds))
        if thread.is_alive():
            # The in-flight DB call cannot safely be cancelled. Once it
            # returns, abandon the remaining queue instead of continuing to
            # write after the bounded orderly-drain window has expired.
            self._drain_expired.set()

    def wait_until_ready(self, timeout):
        """Tests and diagnostics only."""
        return self._policy_loaded.wait(timeout)

    # -- worker thread -------------------------------------------------------

    def _count(self, name, *, emit=True):
        self.counters[name] = self.counters.get(name, 0) + 1
        if emit and (name.startswith("dropped") or name == "snapshot_error"):
            total = self.counters[name]
            if total == 1 or total % 100 == 0:
                print(f"  Hour-refill evidence {name} (count={total}); playout unaffected")

    def _refresh_policy(self):
        """(requests, eject) as r0120; the r0121 switches (Start Next,
        automatic skip, media failure) are cached on their attributes. A
        shorter reader leaves the missing ones Off."""
        try:
            close_old_connections()
            flags = tuple(self._read_flags()) + (False,) * 5
            requests, eject, start_next, automatic_skip, media_failure = flags[:5]
        except Exception:  # noqa: BLE001 -- fail closed
            requests = eject = start_next = automatic_skip = media_failure = False
        self.capture_requests, self.capture_eject = bool(requests), bool(eject)
        self.capture_start_next = bool(start_next)
        self.capture_automatic_skip = bool(automatic_skip)
        self.capture_media_failure = bool(media_failure)
        self._policy_loaded.set()
        return self.capture_requests, self.capture_eject

    def _enabled_for(self, cause_type):
        """The switch for exactly this cause type; None for an unknown type
        (never another type's switch)."""
        from library.services.hour_refill_causality import (
            CAUSE_AUTOMATIC_SKIP, CAUSE_MEDIA_FAILURE, CAUSE_OPERATOR_EJECT, CAUSE_REQUEST_SUBSTITUTION,
            CAUSE_START_NEXT,
        )

        return {
            CAUSE_REQUEST_SUBSTITUTION: self.capture_requests,
            CAUSE_OPERATOR_EJECT: self.capture_eject,
            CAUSE_START_NEXT: self.capture_start_next,
            CAUSE_AUTOMATIC_SKIP: self.capture_automatic_skip,
            CAUSE_MEDIA_FAILURE: self.capture_media_failure,
        }.get(cause_type)

    def _write(self, evidence):
        self._refresh_policy()  # authoritative per event
        try:
            cause_type = evidence.cause_fields()["cause_type"]
        except Exception:  # noqa: BLE001
            cause_type = None
        enabled = self._enabled_for(cause_type)
        if enabled is None:
            self._count("dropped_unknown_type")
            return
        if not enabled:
            self._count("dropped_policy_off")
            return
        try:
            close_old_connections()
            self._insert(evidence)
            self._count("recorded")
            if self._on_recorded is not None:
                try:
                    self._on_recorded()
                except Exception:  # noqa: BLE001 -- a wake is optional
                    pass
        except IntegrityError:
            self._count("duplicate")
        except Exception:  # noqa: BLE001
            self._count("dropped_error")

    def _run(self):
        try:
            self._refresh_policy()
            next_refresh = time.monotonic() + self._policy_refresh_seconds
            while True:
                if self._drain_expired.is_set():
                    break
                if self._stopping.is_set() and self._queue.empty():
                    break
                timeout = max(0.0, next_refresh - time.monotonic())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None
                if item is _STOP:
                    continue  # drain whatever is still queued, then exit
                if item is not None:
                    self._write(item)
                if time.monotonic() >= next_refresh and not self._stopping.is_set():
                    self._refresh_policy()
                    next_refresh = time.monotonic() + self._policy_refresh_seconds
        finally:
            self.capture_requests = False
            self.capture_eject = False
            self.capture_start_next = False
            self.capture_automatic_skip = False
            self.capture_media_failure = False
            while True:
                try:
                    if self._queue.get_nowait() is not _STOP:
                        self._count("dropped_shutdown")
                except queue.Empty:
                    break
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
