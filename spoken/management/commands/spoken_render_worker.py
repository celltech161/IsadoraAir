"""Run the leased spoken render worker (spoken.worker).

    manage.py spoken_render_worker --once          # one item, then exit (tests/admin)
    manage.py spoken_render_worker --max-items 5   # at most five items
    manage.py spoken_render_worker                 # loop (isadoraair-spoken-render-worker.service)

Renders happen in isadoraair-render; this process only claims, asks, ingests
and completes. It never runs in the web request path.

D3C: the loop also runs produced_content's bounded TEST / NO-AIR take expiry
(at most once a minute) -- isolated: a failure there is logged and never
stops rendering.
"""
import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from spoken import worker

log = logging.getLogger(__name__)
TEST_TAKE_SWEEP_SECONDS = 60.0


def sweep_test_takes(state, *, clock=time.monotonic) -> int:
    """Expire due produced-content test takes, throttled; never raises."""
    now = clock()
    if state.get("at") is not None and now - state["at"] < TEST_TAKE_SWEEP_SECONDS:
        return 0
    state["at"] = now
    try:
        from produced_content import no_air
        return no_air.expire_due()
    except Exception:                                      # noqa: BLE001 -- never stops rendering
        log.exception("produced-content test take expiry failed; rendering continues")
        return 0


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
        # C2B: converge projections left by a crash before taking new work.
        from spoken import delivery
        findings = [f for f in delivery.reconcile() if f.get("action") not in ("none", "skipped")]
        if findings:
            self.stdout.write(f"projection reconcile: {len(findings)} change(s)")
        sweep_state = {}
        while not stopping:
            expired = sweep_test_takes(sweep_state)
            if expired:
                self.stdout.write(f"test takes expired: {expired}")
            for advanced in worker.advance_ready():
                self.stdout.write(f"{advanced.item_id}: {advanced.result} {advanced.code}".rstrip())
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
