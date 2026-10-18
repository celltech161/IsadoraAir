"""Run the active-message render worker (active_messages.worker, iPortal D2A).

    manage.py active_message_worker --once      # one pass, then exit (tests/admin)
    manage.py active_message_worker             # loop

Renders happen in isadoraair-render; this process materializes the one due
occurrence, asks for its exact aggregate, ingests and binds it
(PENDING_RENDER -> READY). With the native-airing gate closed (every station
until Phase G) it never publishes, never sends an engine command and nothing
airs. D2A ships no systemd unit for it: deploying it is a D3E-B release
decision (a new managed unit is a signed protected-policy change).

D3E-A: the loop is ``worker.run_forever`` -- its ONE cadence rule (wait after
every pass that made no progress, playout included), SIGTERM/SIGINT honoured
between passes and within a wait, database failures close the connection --
and the loop writes the worker heartbeat (active_messages.heartbeat).
"""
import signal

from django.core.management.base import BaseCommand
from django.db import connection

from active_messages import heartbeat, worker


class Command(BaseCommand):
    help = "Render due active-message aggregates through isadoraair-render and bind them (no airing)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one pass, then exit.")
        parser.add_argument("--idle-seconds", type=float, default=worker.POLL_SECONDS,
                            help="Wait after a pass that made no progress (loop mode; at least 1 s).")

    def report(self, outcome):
        self.stdout.write(" ".join(part for part in (outcome.result, outcome.execution, outcome.code) if part))

    def handle(self, *args, **options):
        if options["once"]:
            try:
                self.report(worker.run_once())
            finally:
                connection.close()
            return None
        stopping = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.append(True))
        worker.run_forever(poll_seconds=max(1.0, options["idle_seconds"]), stop=lambda: bool(stopping),
                           recorder=heartbeat.Recorder(), report=self.report)
        return None
