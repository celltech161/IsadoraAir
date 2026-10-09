"""Run the isadoraair-render service (production.services.render_service).

Started by systemd as ``isadoraair-render.service`` (deploy/), never by hand on
a station: the service must run in its own delegated cgroup subtree with
``KillMode=control-group`` so that systemd -- not this process -- is the last
line of cleanup for every render step.
"""
from django.core.management.base import BaseCommand, CommandError

from production.services import admission, render_service


class Command(BaseCommand):
    help = "Serve iPortal render requests on the render Unix socket (systemd service)."

    def add_arguments(self, parser):
        parser.add_argument("--socket", default=None,
                            help="Unix socket path (default: settings.PRODUCTION_RENDER_SOCKET).")
        # Plain strings: parsed ONLY by the service's strict admission parser.
        parser.add_argument("--max-active", default=None, metavar="1..2",
                            help="Renders at once (default: settings.PRODUCTION_RENDER_MAX_ACTIVE, else 1).")
        parser.add_argument("--max-pending", default=None, metavar="0..4",
                            help="Admitted requests waiting for a render slot (default: "
                                 "settings.PRODUCTION_RENDER_MAX_PENDING, else 2).")

    def handle(self, *args, **options):
        try:
            service = render_service.RenderService(options["socket"] or render_service.socket_path(),
                                                   max_active=options["max_active"],
                                                   max_pending=options["max_pending"])
        except (ValueError, admission.AdmissionConfigError, render_service.NotReady) as exc:
            raise CommandError(str(exc)) from exc
        try:
            service.serve_forever()
        except render_service.NotReady as exc:
            raise CommandError(str(exc)) from exc       # exit non-zero; systemd restarts and retries
        return None

    requires_system_checks = []           # no database involved; start fast, start always
