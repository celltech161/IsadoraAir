"""Poll the configured public site and advance the local request mirror."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from webrequests.ingest import (
    PublicSiteClient,
    build_status_payload,
    ingest_pending_items,
    report_failure,
    safe_error_message,
)
from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    help = "Poll the public website for listener requests and push authoritative local statuses."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.objects.filter(pk=1).first()
        if cfg is None or not cfg.enabled:
            return

        try:
            client = PublicSiteClient.from_settings()
            pending = client.fetch_pending_requests()
            result = ingest_pending_items(pending)

            # Keep the existing lifecycle/scheduling implementation authoritative.
            call_command(
                "refresh_song_request_statuses",
                stdout=StringIO(),
                stderr=StringIO(),
                verbosity=0,
            )
            updates = build_status_payload()
            acknowledged = client.push_status_updates(updates)
        except Exception as exc:
            report_failure("ingest", exc, cfg=cfg)
            raise CommandError(safe_error_message(exc)) from exc

        # Empty polls are intentionally silent; systemd still records exit status.
        if result["created"]:
            self.stdout.write(
                f"Imported {result['created']} request(s); "
                f"{result['skipped']} duplicate(s); public site accepted "
                f"{acknowledged} status update(s)."
            )
