"""Run the active-message render worker (active_messages.worker, iPortal D2A).

    manage.py active_message_worker --once      # one pass, then exit (tests/admin)
    manage.py active_message_worker             # loop

Renders happen in isadoraair-render; this process materializes the one due
occurrence, asks for its exact aggregate, ingests and binds it
(PENDING_RENDER -> READY). It never sends an engine command and nothing airs.
D2A ships no systemd unit for it: deploying it is a D2B/release decision
(a new managed unit is a signed protected-policy change).
"""
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from active_messages import worker


class Command(BaseCommand):
    help = "Render due active-message aggregates through isadoraair-render and bind them (no airing)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one pass, then exit.")
        parser.add_argument("--idle-seconds", type=float, default=worker.POLL_SECONDS,
                            help="Sleep between passes with nothing to do (loop mode).")

    def handle(self, *args, **options):
        stopping = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.append(True))
        while not stopping:
            outcome = worker.run_once()
            self.stdout.write(" ".join(part for part in (outcome.result, outcome.execution, outcome.code) if part))
            if options["once"]:
                break
            if outcome.result in ("idle", "busy", "blocked", "backoff", "retry"):
                time.sleep(max(1.0, options["idle_seconds"]))
                close_old_connections()
        return None
