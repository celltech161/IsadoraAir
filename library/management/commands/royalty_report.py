"""Generate a royalty report from PlayEvent data.

Usage:
    python manage.py royalty_report --month 2026-07
    python manage.py royalty_report --month 2026-07 --format summary
    python manage.py royalty_report --month 2026-07 --format raw_csv
    python manage.py royalty_report --month 2026-07 --format soundexchange_nce --persist
    python manage.py royalty_report --month 2026-07 --output /tmp/nce-2026-07.csv

Default writes the generated content to stdout for shell redirection.
--persist saves a RoyaltyReport row + copy of the file to
MEDIA_ROOT/royalty_reports/ so the /reports/ web page can list and
re-download it later. --output writes to a specific path in addition
to (or instead of) stdout.

Filter policy: Music-category-kind plays only, 30-second SoundExchange
threshold applied at query time. Raw CSV format ignores both filters
(that's the audit-source format)."""
import calendar
import datetime
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from library.models import RoyaltyReport
from library.services.royalty_reports import (
    GENERATORS,
    compute_stats,
    generate,
)


class Command(BaseCommand):
    help = "Generate a royalty report (SoundExchange NCE / summary / raw CSV) from PlayEvent data."

    def add_arguments(self, parser):
        parser.add_argument("--month", required=True,
                             help="Reporting month in YYYY-MM (covers the entire month, station-local timezone).")
        parser.add_argument("--format", default="soundexchange_nce",
                             choices=list(GENERATORS.keys()),
                             help="Output format. Default: soundexchange_nce.")
        parser.add_argument("--output", default="",
                             help="Write to this file path in addition to stdout. Skip to write to stdout only.")
        parser.add_argument("--persist", action="store_true",
                             help="Save a RoyaltyReport row + a copy of the file for the /reports/ web page.")

    def handle(self, *args, **opts):
        try:
            year, month = opts["month"].split("-")
            year, month = int(year), int(month)
        except (ValueError, IndexError):
            raise CommandError("--month must be YYYY-MM (e.g. 2026-07)")

        period_start = datetime.date(year, month, 1)
        last_day = calendar.monthrange(year, month)[1]
        period_end = datetime.date(year, month, last_day)

        content, ext = generate(period_start, period_end, opts["format"])

        if opts["output"]:
            Path(opts["output"]).write_text(content, encoding="utf-8")
            self.stderr.write(self.style.SUCCESS(f"Wrote {opts['output']}"))
        else:
            self.stdout.write(content, ending="")

        if opts["persist"]:
            stats = compute_stats(period_start, period_end)
            rr = RoyaltyReport(
                period_start=period_start,
                period_end=period_end,
                format=opts["format"],
                total_plays=stats["total_plays"],
                unique_tracks=stats["unique_tracks"],
                unique_artists=stats["unique_artists"],
                plays_with_isrc=stats["plays_with_isrc"],
            )
            fname = f"{period_start:%Y-%m}-{opts['format']}.{ext}"
            rr.file.save(fname, ContentFile(content.encode("utf-8")), save=False)
            rr.save()
            self.stderr.write(self.style.SUCCESS(
                f"Persisted RoyaltyReport id={rr.id} "
                f"({stats['total_plays']} plays, {stats['unique_tracks']} unique tracks)"
            ))
