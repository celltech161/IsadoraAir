import json

from django.core.management.base import BaseCommand

from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    """Prints WebRequestConfig as JSON to stdout, same cross-venv pattern
    as dump_weather_config/dump_amber_alert_config -- the external
    legacy standalone ingest scripts have no Django installed. Retained only
    to support a safe deployment cutover."""

    help = "Legacy bridge: dump WebRequestConfig as JSON."

    def handle(self, *args, **options):
        cfg = WebRequestConfig.load()
        self.stdout.write(json.dumps({
            "enabled": cfg.enabled,
            "open_slots": cfg.open_slots,
            "max_fulfilled_per_hour": cfg.max_fulfilled_per_hour,
            "lookahead_warning_minutes": cfg.lookahead_warning_minutes,
            "expire_after_hours": cfg.expire_after_hours,
        }))
