"""Run Reconciliation from the page (roadmap 3.11, minimal launcher; docs/LIBRARY_RECONCILIATION.md).

One request starts the existing ``manage.py reconcile_library`` command, with its defaults, as a detached
process of the web application's own service account, at low CPU and I/O priority, under GNU ``timeout`` (a
fixed two-hour limit: TERM to the scan's process group, then KILL 30 s later). Nothing else is added:
the scan keeps its own lock (``.scan.lock``), progress file (``running.json``), reports and
``scan_status()``. There is no job record and no supervisor; what the page shows comes from those files.

The request waits briefly for ITS OWN process to show what happened, so the answer is never a guess:
* it holds the scan lock and has written its progress file: the scan is running;
* it has already finished: the new attempt is named (complete, suspect or failed);
* it exited without scanning: another scan holds the lock (409), or the launch failed (500);
* none of these within LAUNCH_WAIT_SECONDS (2 s): "starting" -- launched, not yet confirmed (the page keeps
  watching the same files, and says "Start not confirmed" if nothing appears within 30 s).
The launched process is ``timeout`` (``nice`` and ``ionice`` exec it), which leads the new session; the scan is
its child, so "its own" progress file is the one whose pid is in that session. Each answer also carries the
attempt and the progress file that existed before the launch, so the page never mistakes either for new.

Two requests at once: the launch itself runs under ``.launch.lock`` (an empty lock file, held only for the
request), so a second request is refused (409) instead of starting a second process; and the scan lock
still guarantees that at most one scan runs, whoever starts it."""
from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings

from library.services import library_reconciliation as reconciliation

logger = logging.getLogger(__name__)

NICE = "/usr/bin/nice"
IONICE = "/usr/bin/ionice"
# GNU coreutils' timeout. On Ubuntu 25.10+ /usr/bin/timeout is the uutils (Rust) implementation, whose KILL
# escalation reaches only the direct child; GNU's (installed with coreutils-from-uutils as gnu-coreutils, or as
# /usr/bin/timeout's target with coreutils-from-gnu) reaches the whole process group.
TIMEOUT = "/usr/bin/gnutimeout"
SCAN_TIME_LIMIT = "2h"              # then TERM to the scan's process group (the scan and its ffmpeg/ffprobe)
KILL_AFTER = "30s"                  # then KILL, if the scan has not exited
SCAN_NICE = 10                      # lowest-but-one CPU priority (a non-root process may only lower its own)
LAUNCH_LOCK = ".launch.lock"
LAUNCH_WAIT_SECONDS = 2.0           # a short confirmation only; the page follows the scan through the status API
POLL_SECONDS = 0.1


class LaunchRefused(Exception):
    """The scan was not started; ``status`` is the HTTP status the page gets."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def scan_command() -> list[str]:
    """The fixed command: nothing in it comes from a request. ``nice`` and ``ionice`` exec the next program;
    ``timeout`` stays as the scan's parent (it is the process started, the leader of the new session and process
    group), and the scan and its ffmpeg/ffprobe children inherit the priorities and the process group."""
    manage_py = Path(settings.BASE_DIR) / "manage.py"
    return [NICE, "-n", str(SCAN_NICE), IONICE, "-c", "3",
            TIMEOUT, "--signal=TERM", f"--kill-after={KILL_AFTER}", SCAN_TIME_LIMIT,
            sys.executable, str(manage_py), "reconcile_library"]


def _attempt():
    pointer = reconciliation._read_json(reconciliation.report_dir() / "last_attempt.json")
    return pointer if isinstance(pointer, dict) else None


def _running():
    running = reconciliation._read_json(reconciliation.report_dir() / "running.json")
    return running if isinstance(running, dict) else None


def _identity(running):
    """What tells one progress file from another (a new scan writes its own pid and start time)."""
    return {"pid": running.get("pid"), "started": running.get("started")} if running else None


def _in_session(pid, session) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        return os.getsid(pid) == session
    except OSError:
        return False


@contextlib.contextmanager
def _launch_lock():
    directory = reconciliation.report_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / LAUNCH_LOCK, os.O_RDWR | os.O_CREAT, 0o640)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LaunchRefused(409, "scan_starting", "Another request is starting a scan. Its progress appears "
                                                      "here in a moment.") from None
        yield
    finally:
        os.close(fd)


def _busy():
    return LaunchRefused(409, "scan_running", "A reconciliation scan is already running (started here or from "
                                              "the command line). Try again when it has finished.")


def launch(*, username="", wait=None, popen=None, clock=None, sleep=None) -> dict:
    """Start one scan, or raise LaunchRefused. Returns {"state": "running"|"finished"|"starting", "pid", ...}
    with the attempt and the progress file that existed before this launch ("previous_attempt",
    "previous_running"), so the page can tell a new result or a new scan from an older one."""
    popen = popen or subprocess.Popen                  # all looked up at call time (never bound at import)
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    wait = LAUNCH_WAIT_SECONDS if wait is None else wait
    directory = reconciliation.report_dir()
    with _launch_lock():
        if reconciliation._lock_holder_alive(directory):
            raise _busy()
        previous, previous_running = _attempt(), _identity(_running())
        try:
            proc = popen(scan_command(), cwd=str(settings.BASE_DIR), env=os.environ.copy(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None,
                         start_new_session=True, close_fds=True)
        except OSError as exc:
            logger.error("library reconciliation: the scan could not be started (%s)", exc)
            raise LaunchRefused(500, "launch_failed", "The scan could not be started. Nothing was changed; see "
                                                      "the web application's journal.") from None
        logger.info("library reconciliation: scan process %s started by %s", proc.pid, username or "?")
        known = {"pid": proc.pid, "previous_attempt": previous, "previous_running": previous_running}
        deadline = clock() + wait
        while True:
            running = _running()
            if (running and _in_session(running.get("pid"), proc.pid)
                    and reconciliation._lock_holder_alive(directory)):
                return {"state": "running", "scan_pid": running["pid"], **known}
            code = proc.poll()
            if code is not None:
                attempt = _attempt()
                if attempt is not None and attempt != previous:
                    # It ran and finished already (a small Library, or storage that failed its check).
                    return {"state": "finished", "attempt": attempt, **known}
                if reconciliation._lock_holder_alive(directory):
                    raise _busy()               # a command-line scan took the lock first; this one did nothing
                logger.error("library reconciliation: scan process %s exited with status %s before scanning",
                             proc.pid, code)
                raise LaunchRefused(500, "launch_failed", f"The scan process stopped before scanning (exit status "
                                                          f"{code}). Nothing was changed; see the web application's "
                                                          "journal.")
            if clock() >= deadline:
                return {"state": "starting", **known}
            sleep(POLL_SECONDS)
