import json

from django.core.management.base import BaseCommand

from weather.models import AmberAlertConfig


class Command(BaseCommand):
    """Prints AmberAlertConfig as JSON to stdout, matching the
    dump_weather_config cross-venv pattern so the external ingest
    scripts can read the admin-editable config without importing
    Django."""

    help = "Dump AmberAlertConfig as JSON for the external ingest scripts."

    def handle(self, *args, **options):
        cfg = AmberAlertConfig.load()
        self.stdout.write(json.dumps({
            "enabled": cfg.enabled,
            "ipaws_base_url": cfg.ipaws_base_url.rstrip("/"),
            "event_codes": sorted(cfg.event_codes_set),
            "same_codes": sorted(cfg.same_codes_set),
            "poll_cadence_minutes": cfg.poll_cadence_minutes,
            "include_instruction_in_forecast": cfg.include_instruction_in_forecast,
        }))
