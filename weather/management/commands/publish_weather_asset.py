from django.core.management.base import BaseCommand, CommandError

from weather.publication import PublicationError, publish_weather_asset


class Command(BaseCommand):
    """Thin CLI wrapper around weather.publication.publish_weather_asset
    -- see that module's docstring for the full last-known-good publish
    sequence. This is the bridge weather_ingest/lib/delivery.py's
    deliver() shells out to (via $ISADORAAIR_DIR/venv/bin/python
    manage.py publish_weather_asset ...) from its own isolated venv,
    which has no Django/library-app access of its own.

    Exit code: 0 on success, non-zero (via CommandError) on any
    publish failure -- weather_ingest's existing per-script
    notify()/failure-email behavior triggers on that non-zero exit
    exactly as it already does for any other delivery failure.

    A non-fatal provenance-write failure is a SUCCESSFUL exit (0) --
    the audio/Track publish already durably succeeded -- but is still
    written to stderr (self.stderr, not self.stdout) so
    weather_ingest/lib/delivery.py's deliver(), which runs this command
    as a subprocess from its own isolated venv with no other way to
    observe what happened inside this process, can forward it into the
    calling Weather script's own log as a visible warning without
    treating a clean run's silence on stderr as anything to report."""

    help = "Publish an already-rendered Weather MP3 with last-known-good rollback."

    def add_arguments(self, parser):
        parser.add_argument("candidate_path", help="Path to the already-rendered candidate MP3.")
        parser.add_argument("category_code", help="Library Category code, e.g. WxTemp.")
        parser.add_argument("filename", help="Final filename under LIBRARY_ROOT/<category_code>/.")
        parser.add_argument("--producer", default="", help="Producing script, e.g. wx_forecast.py.")
        parser.add_argument("--voice", default="", help="Logical StationTTSVoice name used.")
        parser.add_argument(
            "--source-kind", default="", dest="source_kind",
            help="e.g. live_nws, cached_fallback, derived_local, event.",
        )
        parser.add_argument(
            "--source-age-seconds", type=float, default=None, dest="source_age_seconds",
            help="Age of the underlying source data at generation time, if known.",
        )
        parser.add_argument(
            "--used-fallback", action="store_true", dest="used_fallback",
            help="Set if a cache/fallback source was used instead of a live fetch.",
        )
        parser.add_argument(
            "--alert-family", default=None, dest="alert_family",
            help="For WxAlert only: nws_watch_warning or ipaws_amber.",
        )

    def handle(self, *args, **options):
        try:
            result = publish_weather_asset(
                options["candidate_path"], options["category_code"], options["filename"],
                producer=options["producer"], voice=options["voice"],
                source_kind=options["source_kind"], source_age_seconds=options["source_age_seconds"],
                used_fallback=options["used_fallback"], alert_family=options["alert_family"],
            )
        except PublicationError as exc:
            raise CommandError(str(exc))

        self.stdout.write(
            f"{'Created' if result.created else 'Updated'} track {result.track.id} "
            f"({options['category_code']}/{result.final_path.name})."
        )
        if result.provenance_error:
            # Deliberately self.stderr, not self.stdout -- see the
            # class docstring. Plain text (no self.style.WARNING
            # coloring codes), since delivery.py forwards this literal
            # string into a plain-text log line.
            self.stderr.write(
                f"WARNING: provenance write failed (publish still succeeded): {result.provenance_error}"
            )
