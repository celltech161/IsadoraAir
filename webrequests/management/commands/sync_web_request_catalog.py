"""Push the eligible music catalog and request-hours grid."""

from django.core.management.base import BaseCommand, CommandError

from webrequests.ingest import PublicSiteClient, build_catalog_payload, report_failure, safe_error_message
from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    help = "Push the eligible music catalog and availability grid to the configured public site."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.objects.filter(pk=1).first()
        if cfg is None or not cfg.enabled:
            return
        try:
            client = PublicSiteClient.from_settings()
            payload = build_catalog_payload(cfg)
            accepted = client.push_catalog(payload)
            if accepted != len(payload["tracks"]):
                raise CommandError(
                    f"public site accepted {accepted} of {len(payload['tracks'])} catalog tracks"
                )
        except Exception as exc:
            report_failure("catalog sync", exc, cfg=cfg)
            if isinstance(exc, CommandError):
                raise
            raise CommandError(safe_error_message(exc)) from exc
