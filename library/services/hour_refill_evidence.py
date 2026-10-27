"""Roadmap 2.27 Phase A -- the in-process hour-refill evidence writer.

One daemon thread per PlaybackEngine owns every database operation for
evidence captured on the engine thread (completed Ejects, engine-path
request swaps). The engine side only reads two cached booleans and calls
offer(), a bounded put_nowait. It never waits, queries, locks or logs per
event.

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

    return HourRefillPolicy.capture_flags()


def _insert_cause(evidence):
    from library.models import HourRefillCause

    with transaction.atomic():
        HourRefillCause.objects.create(**evidence.cause_fields())


class HourRefillEvidenceRecorder:
    """Engine-owned evidence writer. Start once; stop once at shutdown."""

    def __init__(self, *, max_queued=MAX_QUEUED, policy_refresh_seconds=POLICY_REFRESH_SECONDS,
                 read_flags=_read_capture_flags, insert=_insert_cause):
        self._queue = queue.Queue(maxsize=max_queued)
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
        self.counters = {
            "recorded": 0, "duplicate": 0, "dropped_full": 0, "dropped_not_running": 0,
            "dropped_policy_off": 0, "dropped_error": 0, "dropped_shutdown": 0,
            "snapshot_error": 0,
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
        try:
            close_old_connections()
            requests, eject = self._read_flags()
        except Exception:  # noqa: BLE001 -- fail closed
            requests, eject = False, False
        self.capture_requests, self.capture_eject = bool(requests), bool(eject)
        self._policy_loaded.set()
        return self.capture_requests, self.capture_eject

    def _write(self, evidence):
        from library.services.hour_refill_causality import CAUSE_OPERATOR_EJECT

        requests, eject = self._refresh_policy()  # authoritative per event
        enabled = eject if evidence.cause_fields()["cause_type"] == CAUSE_OPERATOR_EJECT else requests
        if not enabled:
            self._count("dropped_policy_off")
            return
        try:
            close_old_connections()
            self._insert(evidence)
            self._count("recorded")
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
