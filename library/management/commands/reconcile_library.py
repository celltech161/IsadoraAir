from django.core.management.base import BaseCommand, CommandError

from library.services import library_reconciliation as reconciliation


class Command(BaseCommand):
    help = (
        "Read-only Library reconciliation scan (roadmap 3.11 Phase B): compares the media files under "
        "LIBRARY_ROOT with the Track rows and reports missing, moved, unregistered, duplicate, drifted, "
        "shared, protected, in-delivery and uncertain items for review at /library/reconciliation/. "
        "Changes nothing: no Track, file, cue point, readiness, waveform cache or duplicate decision is "
        "written. Run it as the service account so the report is readable by the web application. "
        "See docs/LIBRARY_RECONCILIATION.md."
    )

    def add_arguments(self, parser):
        parser.add_argument("--max-decodes", type=int, default=reconciliation.DEFAULT_MAX_DECODES,
                            help="Most relink candidates whose audio is decoded for comparison "
                                 f"(default {reconciliation.DEFAULT_MAX_DECODES}).")
        parser.add_argument("--max-hash-gb", type=float,
                            default=reconciliation.DEFAULT_MAX_HASH_BYTES / (1 << 30),
                            help="Most gigabytes read to compare equal-size files byte for byte (default 20).")

    def handle(self, *args, **options):
        def echo(phase, done):
            if options["verbosity"] >= 2 or done == 0:
                self.stdout.write(f"  {phase}: {done}")

        try:
            report = reconciliation.run_scan(
                max_decodes=max(0, options["max_decodes"]),
                max_hash_bytes=int(max(0.0, options["max_hash_gb"]) * (1 << 30)),
                echo=echo,
            )
        except reconciliation.ScanBusy as exc:
            raise CommandError(str(exc))
        if report["status"] == "failed":
            raise CommandError(f"Scan failed: {report['status_reason']} (nothing was reported as healthy or missing)")
        counts = report["counts"]
        self.stdout.write(f"Scan {report['scan_id']}: {report['status']}"
                          + (f" -- {report['status_reason']}" if report["status_reason"] else ""))
        self.stdout.write(f"  Tracks {counts['tracks_total']} (healthy {counts['healthy']}), "
                          f"audio files {counts['files_total']}")
        for kind in reconciliation.TYPES:
            self.stdout.write(f"  {kind}: {counts[kind]}")
        self.stdout.write(f"  relink reviewable: {counts['relink_reviewable']}, "
                          f"open duplicate groups: {counts['duplicate_groups_open']}")
        for error in report["errors"]:
            self.stdout.write(self.style.WARNING(f"  note: {error}"))
        self.stdout.write("Review the findings at /library/reconciliation/.")
