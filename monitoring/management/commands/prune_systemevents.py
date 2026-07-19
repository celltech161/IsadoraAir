from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from monitoring.models import SystemEvent


DEFAULT_RETENTION_DAYS = 30


class Command(BaseCommand):
    help = (
        "Delete SystemEvent rows older than --days (default 30). Intended "
        "to be run daily from isadoraair-prune-systemevents.timer. The "
        "systemd journal remains authoritative for detail past this "
        "window; SystemEvent is just the curated /monitoring/ feed. "
        "Defaults to a dry run that only reports what would happen; "
        "pass --apply to actually delete."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default {DEFAULT_RETENTION_DAYS}). Rows older than this are deleted.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete the rows. Without this, only reports what would happen.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        apply = options["apply"]
        cutoff = timezone.now() - timedelta(days=days)
        qs = SystemEvent.objects.filter(created_at__lt=cutoff)
        count = qs.count()
        mode = "APPLYING" if apply else "DRY-RUN"
        self.stdout.write(f"{mode}: {count} SystemEvent row(s) older than {cutoff.isoformat()} (retention: {days} days)")
        if apply and count:
            deleted, _ = qs.delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))
