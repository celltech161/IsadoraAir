"""Run the leased spoken render worker (spoken.worker).

    manage.py spoken_render_worker --once          # one item, then exit (tests/admin)
    manage.py spoken_render_worker --max-items 5   # at most five items
    manage.py spoken_render_worker                 # loop (isadoraair-spoken-render-worker.service)

Renders happen in isadoraair-render; this process only claims, asks, ingests
and completes. It never runs in the web request path.
"""
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from spoken import worker


class Command(BaseCommand):
    help = "Render validated spoken items through the isadoraair-render service (leased worker)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one item, then exit.")
        parser.add_argument("--max-items", type=int, default=0, help="Stop after this many items (0: no limit).")
        parser.add_argument("--idle-seconds", type=float, default=15.0,
                            help="Sleep between polls when nothing is claimable (loop mode).")

    def handle(self, *args, **options):
        stopping = []
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stopping.append(True))
        limit = 1 if options["once"] else max(0, options["max_items"])
        done = 0
        while not stopping:
            outcome = worker.process_one()
            if outcome is None:
                if options["once"]:
                    break
                time.sleep(max(1.0, options["idle_seconds"]))
                close_old_connections()       # a long-lived worker: drop broken/aged connections between polls
                continue
            done += 1
            self.stdout.write(f"{outcome.item_id}: {outcome.result} {outcome.code}".rstrip())
            if limit and done >= limit:
                break
        return None
