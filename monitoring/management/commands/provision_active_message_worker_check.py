"""Add the Monitoring check for the active-message worker, once (D3E-B).

    manage.py provision_active_message_worker_check            # create if absent
    manage.py provision_active_message_worker_check --dry-run  # report only

The worker is watched by an ordinary "Systemd Service" MonitorCheck for
isadoraair-active-message-worker.service; the probe recognizes that exact
unit and adds its heartbeat readiness (monitoring.services.probes). This is
the scripted equivalent of adding that row in Django admin -- the r0109
precedent for the render/spoken units -- and replaces nothing:

* any systemd check already naming the unit (enabled or not, under any name)
  is the operator's and is left exactly as it is;
* otherwise one row is created: DEFAULT_NAME, enabled, the default debounce;
* a different row already holding DEFAULT_NAME is refused, never renamed.

Run it after the unit is installed and verified active (a check of a unit
that does not exist yet reports it stopped). No migration: provisioning is a
deliberate post-install step, so it never makes a release's migrations
"manual" for the protected updater (RunPython is outside its automatic
allowlist).
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Max

from monitoring.models import MonitorCheck
from monitoring.services.probes import ACTIVE_MESSAGE_WORKER_UNIT

DEFAULT_NAME = "Active Message Worker"


class Command(BaseCommand):
    help = f"Create the {ACTIVE_MESSAGE_WORKER_UNIT} Monitoring check if none exists (never changes one)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen; change nothing.")

    def handle(self, *args, **options):
        with transaction.atomic():
            existing = (MonitorCheck.objects.select_for_update()
                        .filter(kind="systemd", systemd_unit=ACTIVE_MESSAGE_WORKER_UNIT).order_by("pk").first())
            if existing is not None:
                state = "enabled" if existing.enabled else "disabled"
                self.stdout.write(f"exists: {existing.name!r} (id {existing.pk}, {state}) -- unchanged")
                return None
            if MonitorCheck.objects.filter(name=DEFAULT_NAME).exists():
                raise CommandError(f"a different check is already named {DEFAULT_NAME!r}; "
                                   f"add the {ACTIVE_MESSAGE_WORKER_UNIT} check in admin under another name")
            if options["dry_run"]:
                self.stdout.write(f"would create: {DEFAULT_NAME!r} for {ACTIVE_MESSAGE_WORKER_UNIT}")
                return None
            highest = MonitorCheck.objects.filter(kind="systemd").aggregate(top=Max("sort_order"))["top"]
            check = MonitorCheck(name=DEFAULT_NAME, kind="systemd", systemd_unit=ACTIVE_MESSAGE_WORKER_UNIT,
                                 enabled=True, sort_order=min((highest or 0) + 1, 32767))
            check.full_clean()
            check.save()
        self.stdout.write(f"created: {check.name!r} (id {check.pk}) for {ACTIVE_MESSAGE_WORKER_UNIT}")
        return None
