import json

from django.core.management.base import BaseCommand

from weather.models import WeatherConfig


class Command(BaseCommand):
    """Prints WeatherConfig as JSON to stdout. The weather-ingest cron
    scripts run in their own venv without Django installed (same
    cross-venv-via-subprocess pattern as sync_track_file), so this is
    their only way to read the admin-editable config."""

    help = "Dump WeatherConfig as JSON for the external weather-ingest scripts."

    def handle(self, *args, **options):
        cfg = WeatherConfig.load()
        self.stdout.write(json.dumps({
            "station_lat": cfg.station_lat,
            "station_lon": cfg.station_lon,
            "sun_alt_threshold_deg": cfg.sun_alt_threshold_deg,
            "nws_alert_zone": cfg.nws_alert_zone,
            "nws_forecast_office": cfg.nws_forecast_office,
            "nws_forecast_grid_x": cfg.nws_forecast_grid_x,
            "nws_forecast_grid_y": cfg.nws_forecast_grid_y,
            "nws_cloud_stations": [s.strip() for s in cfg.nws_cloud_stations.split(",") if s.strip()],
            "voice_schedule": cfg.voice_schedule,
            "notify_email": cfg.notify_email,
            "alert_sound_enabled": cfg.alert_sound_enabled,
            "alert_sound_path": cfg.alert_sound_path,
            "alert_sound_device": cfg.alert_sound_device,
            "alert_sound_interval_seconds": cfg.alert_sound_interval_seconds,
            "alert_sound_gain_db": cfg.alert_sound_gain_db,
        }))
