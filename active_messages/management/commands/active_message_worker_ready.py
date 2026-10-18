"""Wait until the active-message worker has started, or fail (its ExecStartPost= gate).

deploy/isadoraair-active-message-worker.service runs this with the unit's
$MAINPID after starting the worker, so a start job -- Update Center's
``systemctl enable --now`` included -- completes only once THAT process has
written a valid heartbeat and finished its first pass or begun a render
(active_messages.supervision.startup_failure). A heartbeat left by an earlier
process never counts (pid and start time), and neither a completed render nor
an active message is needed: an idle, blocked or gated-READY first pass is a
successful start. A worker that exits, cannot write its heartbeat or never
completes a pass fails the start instead of looking active while it restarts.

Read-only: it reads the heartbeat file and the process table, never the
database, and never writes.
"""
import time

import psutil
from django.core.management.base import BaseCommand, CommandError

from active_messages import heartbeat, supervision

POLL_SECONDS = 0.25


def _started_at(pid):
    """The process's start (epoch seconds), or None once it has gone."""
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


class Command(BaseCommand):
    help = "Exit 0 once the worker process PID has started (valid current heartbeat, first pass done)."

    requires_system_checks = []           # no database involved

    def add_arguments(self, parser):
        parser.add_argument("--pid", type=int, required=True, help="The worker's process id (the unit's $MAINPID).")
        parser.add_argument("--timeout", type=float, default=supervision.STARTUP_TIMEOUT_SECONDS,
                            help=f"Seconds to wait (default {supervision.STARTUP_TIMEOUT_SECONDS:g}).")
        parser.add_argument("--state-path", default=None,
                            help="Heartbeat file (default: settings.ACTIVE_MESSAGE_WORKER_STATE_PATH; the unit "
                                 "never passes it).")

    def handle(self, *args, **options):
        pid, timeout = options["pid"], options["timeout"]
        if pid <= 0:
            raise CommandError("--pid must be a process id")
        if not 0 < timeout <= supervision.MAX_STARTUP_TIMEOUT_SECONDS:
            raise CommandError(f"--timeout must be between 0 and {supervision.MAX_STARTUP_TIMEOUT_SECONDS:g} seconds")
        path = options["state_path"] or heartbeat.state_path()
        deadline = time.monotonic() + timeout
        while True:
            started = _started_at(pid)
            if started is None:
                raise CommandError(f"active-message worker process {pid} exited during start-up")
            document, unreadable = heartbeat.read_state(path)
            reason = supervision.startup_failure(document, unreadable, pid=pid, process_started_at=started,
                                                 now=time.time())
            if not reason:
                self.stdout.write(f"active-message worker {pid} started")
                return None
            if time.monotonic() >= deadline:
                raise CommandError(f"active-message worker {pid} not started after {timeout:g}s: {reason}")
            time.sleep(POLL_SECONDS)
