"""Roadmap 2.25 B6 -- bounded asynchronous Voice Track lookups (F13).

VoiceTrack lookup, ProductionMedia / legacy file resolution and
VoiceTrackConfig retrieval never run on the engine's timing-critical GLib
callbacks. One daemon worker thread serves a bounded request queue and a
bounded result cache:

* ``request(key, payload)`` is non-blocking (``put_nowait``): a full queue is
  counted as saturation and reported as "not requested" -- the caller treats
  the data as unavailable, never waits;
* identical keys are de-duplicated while in flight unless a FRESHER answer is
  required (``fresh_after``);
* every result carries the monotonic time its request was ISSUED, so a caller
  can demand an answer requested after a given instant (an authoritative
  post-claim lookup) and reject anything older (stale-result rejection);
* a result for an older request never replaces a newer cached one;
* a resolver exception is a terminal "unavailable" result for that request,
  never a dead worker; an unexpected worker death is detected, counted and
  restarted a bounded number of times, after which the service reports itself
  unavailable (the engine then plays without VTs, never waits).

Nothing here imports Django, GStreamer or GLib: the engine supplies the
resolver (run on the worker thread, which also owns its own DB connection)
and the notifier (which posts results to the GLib main loop).
"""

from __future__ import annotations

import queue
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Optional

MAX_QUEUE = 32
CACHE_SIZE = 256
MAX_RESTARTS = 3


@dataclass(frozen=True)
class LookupResult:
    key: Hashable
    ok: bool                 # the resolver returned (value may still mean "no recording")
    value: Any
    error: str
    requested_mono: float    # when the request was ISSUED (freshness / authority)
    completed_mono: float
    serial: int


class VtLookupService:
    def __init__(self, resolve: Callable[[Hashable, Any], Any], *,
                 notify: Optional[Callable[[LookupResult], None]] = None,
                 on_thread_exit: Optional[Callable[[], None]] = None,
                 max_queue: int = MAX_QUEUE, cache_size: int = CACHE_SIZE,
                 inline: bool = False, clock: Callable[[], float] = time.monotonic,
                 name: str = "vt-lookup"):
        self._resolve = resolve
        self._notify = notify
        self._on_thread_exit = on_thread_exit
        self._cache_size = cache_size
        self._queue: "queue.Queue" = queue.Queue(maxsize=max_queue)
        self._lock = threading.Lock()
        self._cache: "OrderedDict[Hashable, LookupResult]" = OrderedDict()
        self._inflight: dict = {}
        self._serial = 0
        self._clock = clock
        self._name = name
        self.inline = inline
        self._thread: Optional[threading.Thread] = None
        self._stopping = False
        self.restarts = 0
        self.unavailable = False
        self.stats = {"requests": 0, "deduplicated": 0, "saturated": 0, "completed": 0,
                      "errors": 0, "stale_dropped": 0, "restarts": 0, "max_queue_depth": 0}

    # -- public, any thread (the engine calls from the GLib thread) ----------------
    def request(self, key: Hashable, payload: Any = None, *, fresh_after: Optional[float] = None) -> bool:
        """Ask for ``key`` without blocking. True when a matching request is
        queued or already in flight; False when the service cannot take it
        (saturated, stopping, or unavailable)."""
        with self._lock:
            if self._stopping or self.unavailable:
                return False
        if not self.inline and not self._ensure_worker():
            return False
        now = self._clock()
        with self._lock:
            pending = self._inflight.get(key)
            if pending is not None and (fresh_after is None or pending >= fresh_after):
                self.stats["deduplicated"] += 1
                return True
            self.stats["requests"] += 1
            self._inflight[key] = now
        if self.inline:
            self._serve(key, payload, now)
            return True
        try:
            self._queue.put_nowait((key, payload, now))
        except queue.Full:
            with self._lock:
                self.stats["saturated"] += 1
                if self._inflight.get(key) == now:
                    del self._inflight[key]
            return False
        with self._lock:
            self.stats["max_queue_depth"] = max(self.stats["max_queue_depth"], self._queue.qsize())
        return True

    def get(self, key: Hashable) -> Optional[LookupResult]:
        with self._lock:
            result = self._cache.get(key)
            if result is not None:
                self._cache.move_to_end(key)
            return result

    def pending(self, key: Hashable) -> bool:
        with self._lock:
            return key in self._inflight

    def invalidate(self, key: Hashable) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "alive": self.inline or (self._thread is not None and self._thread.is_alive()),
                "unavailable": self.unavailable,
                "queue_depth": self._queue.qsize(),
                "inflight": len(self._inflight),
                "cached": len(self._cache),
                **self.stats,
            }

    def stop(self, timeout: float = 2.0) -> bool:
        with self._lock:
            self._stopping = True
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        thread.join(timeout)
        return not thread.is_alive()

    # -- worker ---------------------------------------------------------------------
    def _ensure_worker(self) -> bool:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                return True
            if thread is not None:
                # It died unexpectedly (never through stop()): bounded restarts.
                if self.restarts >= MAX_RESTARTS:
                    self.unavailable = True
                    return False
                self.restarts += 1
                self.stats["restarts"] += 1
                self._inflight.clear()          # those requests died with the worker
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                key, payload, requested = item
                self._serve(key, payload, requested)
                with self._lock:
                    if self._stopping:
                        return
        finally:
            if self._on_thread_exit is not None:
                try:
                    self._on_thread_exit()
                except Exception:
                    pass

    def _serve(self, key, payload, requested) -> None:
        try:
            value, ok, error = self._resolve(key, payload), True, ""
        except Exception as exc:          # one failed lookup, never a dead worker
            value, ok, error = None, False, f"{type(exc).__name__}: {exc}"[:200]
        done = self._clock()
        with self._lock:
            self._serial += 1
            result = LookupResult(key, ok, value, error, requested, done, self._serial)
            if self._inflight.get(key) == requested:
                del self._inflight[key]
            cached = self._cache.get(key)
            if cached is not None and cached.requested_mono > requested:
                self.stats["stale_dropped"] += 1
                result = None
            else:
                self._cache[key] = result
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                self.stats["completed"] += 1
                if not ok:
                    self.stats["errors"] += 1
        if result is not None and self._notify is not None:
            try:
                self._notify(result)
            except Exception:
                pass
