"""Wait until isadoraair-render is ready, or fail (its ExecStartPost= gate).

deploy/isadoraair-render.service runs this after starting the service, so a
start job -- including Update Center's ``systemctl enable --now`` -- completes
only once the service is really ready by the accepted C2A readiness logic
(production.services.render_health.observe: socket present, status file
readable, empty-request handshake answered). The service binds its socket only
after its start-up cleanup and its confined toolchain probe succeed, so a
renderer that can never become ready makes the start job fail instead of
looking ``active`` while it crash-loops.

Read-only: it never renders, never touches the database and never writes.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from production.services import render_health

POLL_SECONDS = 0.2


class Command(BaseCommand):
    help = "Exit 0 once the render service answers its readiness handshake; non-zero on timeout."

    requires_system_checks = []           # no database involved

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=60.0,
                            help="Seconds to wait for readiness (default 60).")
        parser.add_argument("--socket", default=None,
                            help="Unix socket path (default: settings.PRODUCTION_RENDER_SOCKET).")

    def handle(self, *args, **options):
        timeout = options["timeout"]
        if not 0 < timeout <= 600:
            raise CommandError("--timeout must be between 0 and 600 seconds")
        deadline = time.monotonic() + timeout
        while True:
            readiness = render_health.observe(options["socket"])
            if readiness["ready"]:
                self.stdout.write(f"isadoraair-render ready (at most {readiness['max_active']} rendering, "
                                  f"{readiness['max_pending']} waiting)")
                return None
            if time.monotonic() >= deadline:
                raise CommandError(f"isadoraair-render not ready after {timeout:g}s: {readiness['reason']}")
            time.sleep(POLL_SECONDS)
