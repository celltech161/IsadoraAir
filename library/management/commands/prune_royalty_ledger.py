"""Delete PlayEvent and IcecastSample rows older than a retention
window. Intended to be run daily by isadoraair-prune-royalty-ledger
.timer, but safe to invoke manually. Defaults to a DRY RUN that only
reports what would happen; pass --apply to actually delete.

Retention defaults: 3 years for both. SoundExchange's typical audit
lookback is 3 years, and RoyaltyReport rows snapshot the derived
ATH + play counts at generation time -- the raw ledger becomes
disposable evidence past that window (the report file itself carries
the numbers).

Two knobs (--playevent-days and --sample-days) rather than one so an
operator who wants shorter IcecastSample retention (they're written
every minute -> ~525k rows/year) can prune them more aggressively
without also touching PlayEvent's audit window."""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from library.models import IcecastSample, PlayEvent


DEFAULT_PLAYEVENT_DAYS = 1095   # ~3 years
DEFAULT_SAMPLE_DAYS = 1095      # ~3 years


class Command(BaseCommand):
    help = (
        "Delete PlayEvent and IcecastSample rows older than the retention "
        "windows (defaults 3 years each). Runs as a dry-run by default -- "
        "pass --apply to actually delete."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--playevent-days", type=int, default=DEFAULT_PLAYEVENT_DAYS,
            help=f"PlayEvent retention window in days (default {DEFAULT_PLAYEVENT_DAYS} = ~3y).",
        )
        parser.add_argument(
            "--sample-days", type=int, default=DEFAULT_SAMPLE_DAYS,
            help=f"IcecastSample retention window in days (default {DEFAULT_SAMPLE_DAYS} = ~3y).",
        )
        parser.add_argument(
            "--apply", action="store_true",
            help="Actually delete rows. Without this, only reports what would happen.",
        )

    def handle(self, *args, **opts):
        apply = opts["apply"]
        mode = "APPLYING" if apply else "DRY-RUN"

        pe_cutoff = timezone.now() - timedelta(days=opts["playevent_days"])
        pe_qs = PlayEvent.objects.filter(started_at__lt=pe_cutoff)
        pe_count = pe_qs.count()
        self.stdout.write(
            f"{mode}: {pe_count:,} PlayEvent row(s) older than "
            f"{pe_cutoff.isoformat()} ({opts['playevent_days']}d retention)"
        )
        if apply and pe_count:
            deleted, _ = pe_qs.delete()
            self.stdout.write(self.style.SUCCESS(f"  Deleted {deleted:,} PlayEvent row(s)."))

        s_cutoff = timezone.now() - timedelta(days=opts["sample_days"])
        s_qs = IcecastSample.objects.filter(sampled_at__lt=s_cutoff)
        s_count = s_qs.count()
        self.stdout.write(
            f"{mode}: {s_count:,} IcecastSample row(s) older than "
            f"{s_cutoff.isoformat()} ({opts['sample_days']}d retention)"
        )
        if apply and s_count:
            deleted, _ = s_qs.delete()
            self.stdout.write(self.style.SUCCESS(f"  Deleted {deleted:,} IcecastSample row(s)."))
