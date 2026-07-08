from django.db import models


class WeatherConfig(models.Model):
    """Singleton -- station location, NWS lookup parameters, and the
    day/night voice schedule for the weather announcer pipeline. These
    were hardcoded Python constants in the original kogr-sc scripts;
    admin-editable here so a station move or NWS zone change doesn't
    require a code edit + redeploy, matching the LogFillConfig/
    DuckingConfig/RBDSConfig convention used elsewhere in this project.

    Announcer email notifications reuse the project's own EMAIL_*
    settings (see monitoring.services.notify) via a management command
    the external weather-ingest scripts shell out to -- no separate
    SMTP credential file, unlike the original scripts."""

    station_lat = models.FloatField(
        default=39.13, help_text="Station latitude -- used for sunrise/sunset (day/night sky wording).",
    )
    station_lon = models.FloatField(
        default=-97.70, help_text="Station longitude.",
    )
    sun_alt_threshold_deg = models.FloatField(
        default=3.0, help_text="Solar altitude (degrees) above which it's considered daytime.",
    )

    nws_alert_zone = models.CharField(
        max_length=16, default="KSC143",
        help_text="NWS public forecast zone for active alerts, e.g. KSC143.",
    )
    nws_forecast_office = models.CharField(
        max_length=8, default="TOP",
        help_text="NWS forecast office code, e.g. TOP (Topeka).",
    )
    nws_forecast_grid_x = models.PositiveIntegerField(default=10)
    nws_forecast_grid_y = models.PositiveIntegerField(default=53)
    nws_cloud_stations = models.CharField(
        max_length=64, default="KCNK,KSLN",
        help_text="Comma-separated METAR station codes used to blend sky condition.",
    )

    voice_schedule = models.JSONField(
        default=list,
        help_text='List of [voice, start_hour, end_hour] triples, local time, 0-23, '
                   'end inclusive. A range may wrap past midnight (start > end). Must '
                   'cover every hour with no gaps. voice is "day" or "night". Example: '
                   '[["day",3,8],["night",9,14],["day",15,20],["night",21,2]]',
    )

    notify_email = models.EmailField(
        blank=True, default="",
        help_text="Address for weather-pipeline failure notifications. Blank disables.",
    )

    class Meta:
        verbose_name = "Weather Configuration"
        verbose_name_plural = "Weather Configuration"

    def __str__(self):
        return "Weather Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            obj.voice_schedule = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]
            obj.save()
        return obj
