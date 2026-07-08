from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import WeatherConfig


@admin.register(WeatherConfig)
class WeatherConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Station Location", {
            "fields": ["station_lat", "station_lon", "sun_alt_threshold_deg"],
        }),
        ("NWS Lookup", {
            "fields": ["nws_alert_zone", "nws_forecast_office", "nws_forecast_grid_x",
                       "nws_forecast_grid_y", "nws_cloud_stations"],
        }),
        ("Announcer Voices", {
            "fields": ["voice_schedule"],
            "description": "Day/night voice shift schedule -- see field help text for format.",
        }),
        ("Notifications", {
            "fields": ["notify_email"],
        }),
    ]

    def has_add_permission(self, request):
        return not WeatherConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = WeatherConfig.load()
        return HttpResponseRedirect(
            reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        )
