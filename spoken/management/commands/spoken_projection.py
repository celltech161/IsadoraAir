"""Spoken projection maintenance (spoken.delivery).

    manage.py spoken_projection reconcile [--dry-run]   # converge Tracks/files to the database
    manage.py spoken_projection expire                  # the bounded expiry sweep
    manage.py spoken_projection status                  # read-only health (spoken.projection_health)

The render worker runs both on its own (reconcile at start-up, expiry every
pass); this is the operator's explicit entry point. Monitoring only observes
(spoken.projection_health); repairs happen here.
"""
import json

from django.core.management.base import BaseCommand

from spoken import delivery


class Command(BaseCommand):
    help = "Reconcile spoken library projections with the database, or run the expiry sweep."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("reconcile", "expire", "status"))
        parser.add_argument("--dry-run", action="store_true", help="reconcile: report, change nothing.")

    def handle(self, *args, **options):
        if options["action"] == "status":
            from spoken import projection_health
            self.stdout.write(json.dumps(projection_health.observe(), sort_keys=True, default=str))
            return None
        if options["action"] == "expire":
            for item in delivery.expire_due():
                self.stdout.write(f"{item.pk}: expired")
            return None
        for finding in delivery.reconcile(apply=not options["dry_run"]):
            self.stdout.write(json.dumps(finding, sort_keys=True))
        return None
