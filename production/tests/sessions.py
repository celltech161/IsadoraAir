"""A small harness for real two-session PostgreSQL concurrency tests.

Each competitor runs in its own thread, hence its own database connection.
Interleavings are forced by pausing one thread at a named seam (a wrapper
around a function the code under test already calls), and "the other session
is waiting" is proven from pg_stat_activity, never assumed from timing.
"""
import threading
import time

from django.db import connection, connections

WAIT = 30


class Abort(Exception):
    """Raised at a seam to make the paused transaction roll back."""


def run_thread(name, target):
    box = {"error": None, "result": None}

    def runner():
        try:
            box["result"] = target()
        except BaseException as exc:                    # noqa: BLE001 -- reported to the test
            box["error"] = exc
        finally:
            connections.close_all()
    thread = threading.Thread(target=runner, name=name, daemon=True)
    thread.start()
    return thread, box


def blocked_sessions(wait_event=None) -> int:
    sql = ("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
           "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'")
    params = []
    if wait_event is not None:
        sql += " AND wait_event = %s"
        params.append(wait_event)
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()[0]


def wait_until(predicate, timeout=WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def blocked_or_done(thread, wait_event=None, timeout=WAIT) -> str:
    """'blocked' once the other session waits on a lock, 'done' if its thread
    finished first (i.e. nothing serialized it), 'timeout' otherwise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if blocked_sessions(wait_event):
            return "blocked"
        if not thread.is_alive():
            return "done"
        time.sleep(0.02)
    return "timeout"


class Seam:
    """Pause the thread named ``thread_name`` right AFTER (or before) it calls
    ``real``; other threads pass straight through. ``abort`` makes the paused
    call raise Abort once released."""

    def __init__(self, real, thread_name, *, before=False, abort=False):
        self.real, self.thread_name, self.before, self.abort = real, thread_name, before, abort
        self.reached, self.release = threading.Event(), threading.Event()

    def _pause(self):
        if threading.current_thread().name != self.thread_name or self.reached.is_set():
            return False
        self.reached.set()
        assert self.release.wait(WAIT), "seam never released"
        return True

    def __call__(self, *args, **kwargs):
        paused = self._pause() if self.before else False
        result = self.real(*args, **kwargs)
        if not self.before:
            paused = self._pause()
        if paused and self.abort:
            raise Abort(f"{self.thread_name} rolls back at the seam")
        return result
