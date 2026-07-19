import json

from django.core.management.base import BaseCommand

from ogremote.models import OGRemoteCategory, OGRemoteConfig


class Command(BaseCommand):
    """Prints OGRemoteConfig + OGRemoteCategory as JSON to stdout. The
    ogremote-ingest cron scripts run in their own venv without Django
    installed (same cross-venv-via-subprocess pattern as sync_track_file
    and dump_weather_config), so this is their only way to read the
    admin-editable config and the local category-routing table."""

    help = "Dump OGRemoteConfig + OGRemoteCategory rows as JSON for the external ogremote-ingest scripts."

    def handle(self, *args, **options):
        cfg = OGRemoteConfig.load()
        categories = {
            c.category_key: {
                "name": c.name,
                "target_category": c.target_category.code,
                "output_mode": c.output_mode,
                "recallable": c.recallable,
                "enabled": c.enabled,
                "artist_tag": c.artist_tag,
            }
            for c in OGRemoteCategory.objects.select_related("target_category")
        }
        self.stdout.write(json.dumps({
            "enabled": cfg.enabled,
            "urgent_pa_replay_interval_minutes": cfg.urgent_pa_replay_interval_minutes,
            "poll_interval_minutes": cfg.poll_interval_minutes,
            "notify_email": cfg.notify_email,
            "categories": categories,
        }))
