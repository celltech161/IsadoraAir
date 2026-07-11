from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import EmailLog


DEFAULT_RETENTION_DAYS = 90


class Command(BaseCommand):
    help = (
        "Delete EmailLog rows older than --days (default 90). Intended "
        "to be run daily from isadoraair-prune-emaillog.timer, but safe "
        "to invoke manually. Uses a single .delete() so the row count is "
        "reliable in the output. Defaults to a dry run that only reports "
        "what would happen; pass --apply to actually delete."
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
        qs = EmailLog.objects.filter(sent_at__lt=cutoff)
        count = qs.count()
        mode = "APPLYING" if apply else "DRY-RUN"
        self.stdout.write(f"{mode}: {count} EmailLog row(s) older than {cutoff.isoformat()} (retention: {days} days)")
        if apply and count:
            deleted, _ = qs.delete()
            self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s)."))
