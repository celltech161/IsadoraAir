"""Run the isadoraair-validation service (production.services.validation_service).

Started by systemd as ``isadoraair-validation.service`` (deploy/), never by
hand on a station: the service must run in its own delegated cgroup subtree
with ``KillMode=control-group`` so that systemd -- not this process -- is the
last line of cleanup for every validation run.
"""
import dataclasses

from django.core.management.base import BaseCommand, CommandError

from production.services import confinement
from production.services.validation_service import ValidationService


class Command(BaseCommand):
    help = "Serve iPortal media-validation requests on the validation Unix socket (systemd service)."

    def add_arguments(self, parser):
        parser.add_argument("--socket", default=None,
                            help="Unix socket path (default: settings.PRODUCTION_VALIDATION_SOCKET).")
        parser.add_argument("--limit", action="append", default=[], metavar="FIELD=INTEGER",
                            help="Override one confinement.Limits field (service configuration; "
                                 "default: settings.PRODUCTION_VALIDATION_LIMITS).")
        parser.add_argument("--max-active", type=int, default=None,
                            help="Runs at once (default: settings.PRODUCTION_VALIDATION_MAX_ACTIVE or 2).")
        parser.add_argument("--max-pending", type=int, default=None,
                            help="Admitted requests waiting for a run slot (default: "
                                 "settings.PRODUCTION_VALIDATION_MAX_PENDING or 4).")

    def handle(self, *args, **options):
        limits = confinement.configured_limits()
        overrides = {}
        for item in options["limit"]:
            name, _, value = item.partition("=")
            if name not in confinement.Limits.__dataclass_fields__ or not value.isdigit():
                raise CommandError(f"bad --limit {item!r}")
            overrides[name] = int(value)
        if overrides:
            limits = dataclasses.replace(limits, **overrides)
        try:
            service = ValidationService(options["socket"] or confinement.socket_path(), limits=limits,
                                        max_active=options["max_active"], max_pending=options["max_pending"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        service.serve_forever()
        return None

    requires_system_checks = []           # no database involved; start fast, start always
