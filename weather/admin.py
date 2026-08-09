from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import AmberAlertConfig, WeatherConfig


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
        ("Alert Beep", {
            "fields": ["alert_sound_enabled", "alert_sound_cart", "alert_sound_interval_seconds"],
            "description": "Fires the selected FX Cart while a watch/warning is active, "
                            "through IsadoraAir's normal FX/program bus -- present on air "
                            "and in studio/remote-DJ monitoring, same as any other cart "
                            "fire. Separate from the spoken WxAlert statement pipeline. "
                            "The cart's own filepath, gain, and retrigger mode are set on "
                            "the FX Cart itself (Library -> FX Carts), not here.",
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


@admin.register(AmberAlertConfig)
class AmberAlertConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Master Switch", {
            "fields": ["enabled"],
            "description": "OFF by default. Nothing polls, speaks, or inserts until this is on. "
                            "Read the CAP event-code and area-filter settings below before you flip it.",
        }),
        ("IPAWS Feed", {
            "fields": ["ipaws_base_url", "poll_cadence_minutes"],
        }),
        ("What to Include", {
            "fields": ["event_codes", "same_codes"],
            "description": "Event codes are CAP/SAME 3-letter mnemonics (BLU/CAE/MEP by default). "
                            "SAME area codes are 6-digit state+county FIPS -- 020000 for a whole "
                            "state, 020139 for Ottawa County KS specifically.",
        }),
        ("Speech Formatting", {
            "fields": ["include_instruction_in_forecast"],
            "description": "Unlike weather safety instructions, the 'instruction' field on "
                            "AMBER alerts is usually the tip-line phone number -- typically "
                            "worth repeating on every scheduled forecast, not just the urgent insert.",
        }),
    ]

    def has_add_permission(self, request):
        return not AmberAlertConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AmberAlertConfig.load()
        return HttpResponseRedirect(
            reverse("admin:weather_amberalertconfig_change", args=[obj.pk])
        )
