import json

from django.conf import settings
from django.core.management.base import BaseCommand

from weather.models import WeatherConfig, WeatherVoicePersona


class Command(BaseCommand):
    """Print WeatherConfig as JSON for the in-tree weather_ingest jobs.
    Their dedicated environment does not import Django, so this narrow
    management-command bridge is how they read admin-editable config."""

    help = "Dump WeatherConfig as JSON for the in-tree weather_ingest jobs."

    def handle(self, *args, **options):
        cfg = WeatherConfig.load()
        personas = {
            persona.slot: {
                "logical_voice": persona.tts_voice.name if persona.tts_voice_id else None,
                "display_name": persona.display_name,
                "full_name": persona.full_name,
                "signoff": persona.signoff,
            }
            for persona in WeatherVoicePersona.objects.select_related("tts_voice")
        }
        self.stdout.write(json.dumps({
            # Narrow config handoff for weather_ingest: expose this
            # one non-secret setting, never IsadoraAir's complete .env.
            "weather_data_dir": str(settings.WEATHER_DATA_DIR),
            "station_lat": cfg.station_lat,
            "station_lon": cfg.station_lon,
            "sun_alt_threshold_deg": cfg.sun_alt_threshold_deg,
            "nws_alert_zone": cfg.nws_alert_zone,
            "nws_forecast_office": cfg.nws_forecast_office,
            "nws_forecast_grid_x": cfg.nws_forecast_grid_x,
            "nws_forecast_grid_y": cfg.nws_forecast_grid_y,
            "nws_cloud_stations": [s.strip() for s in cfg.nws_cloud_stations.split(",") if s.strip()],
            "voice_schedule": cfg.voice_schedule,
            "voice_personas": personas,
            "notify_email": cfg.notify_email,
            "alert_sound_enabled": cfg.alert_sound_enabled,
            "alert_sound_cart_id": cfg.alert_sound_cart_id,
            "alert_sound_interval_seconds": cfg.alert_sound_interval_seconds,
            # r0053: Weather Alert Beep qualifying-event list ONLY --
            # has no bearing on the spoken WxAlert/AMBER-family
            # statement pipelines, which have their own independent
            # selection rules (see weather_ingest/update_local_wx_
            # data.py's own event_triggers_alert_beep()).
            "alert_sound_trigger_events": cfg.alert_sound_trigger_events,
        }))
