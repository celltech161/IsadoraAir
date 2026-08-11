from pathlib import Path

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from isadoraair import env_admin, env_config

from .models import AmberAlertConfig, WeatherConfig

_WEATHER_ENV_KEYS = ["WEATHER_DATA_DIR"]
_WEATHER_DIAGNOSTIC_FILES = ["latest_weather.json", "wind_history.json", "smoothed_wind.json"]


@admin.register(WeatherConfig)
class WeatherConfigAdmin(admin.ModelAdmin):
    """weather_env_link/weather_env_view (Phase 2, 2026-08-11): Weather
    data directory (WEATHER_DATA_DIR) is a separate, .env-backed setting
    -- surfaced here via a sub-page (isadoraair/env_admin.py's shared
    helper), not injected into this ModelAdmin's own save_model()."""
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
        ("Weather data storage", {
            "fields": ["weather_env_link"],
            "description": "Where GW3000/Ecowitt weather JSON files land -- stored in "
                            ".env, not this database record.",
        }),
    ]
    readonly_fields = ["weather_env_link"]

    def has_add_permission(self, request):
        return not WeatherConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = WeatherConfig.load()
        return HttpResponseRedirect(
            reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        )

    def get_urls(self):
        return [
            path(
                "weather-data-settings/",
                self.admin_site.admin_view(self.weather_env_view),
                name="weather_weatherconfig_weather_env",
            ),
            *super().get_urls(),
        ]

    @admin.display(description="Weather data storage")
    def weather_env_link(self, obj):
        if obj is None or obj.pk is None:
            return "(save the configuration first)"
        url = reverse("admin:weather_weatherconfig_weather_env")
        return format_html(
            '<a class="button" href="{}">Edit weather data storage</a> '
            '<span style="color:#888;font-size:0.85em;">Where GW3000/Ecowitt weather JSON files live on disk.</span>',
            url,
        )

    def weather_env_view(self, request):
        obj = WeatherConfig.load()
        change_url = reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        if request.method == "POST":
            self._handle_weather_env_post(request)
            return HttpResponseRedirect(reverse("admin:weather_weatherconfig_weather_env"))

        notices = [{
            "level": "warning",
            "text": (
                "The separate weather-ingest companion project (its own repo/venv, not "
                "part of IsadoraAir) reads and writes this same directory independently "
                "and has its own configuration -- saving a new path here does not update "
                "that external project. Update its configuration separately (and move any "
                "files it owns) if the shared directory moves."
            ),
        }]
        try:
            saved = env_config.read_managed_values(_WEATHER_ENV_KEYS)
            data_dir_value = saved["WEATHER_DATA_DIR"].display_value
        except env_config.EnvConfigError:
            data_dir_value = None
        if data_dir_value:
            present = [f for f in _WEATHER_DIAGNOSTIC_FILES if (Path(data_dir_value) / f).is_file()]
            missing = [f for f in _WEATHER_DIAGNOSTIC_FILES if f not in present]
            status_text = f"Diagnostic files currently in the saved directory: {', '.join(present) if present else 'none yet'}."
            if missing:
                status_text += f" Missing (optional -- not required to save this page): {', '.join(missing)}."
            notices.append({"level": "info", "text": status_text})

        context = env_admin.env_subform_context(
            request, _WEATHER_ENV_KEYS,
            title="Weather data storage", change_url=change_url,
            admin_site=self.admin_site, model=self.model,
            extra={"notices": notices},
        )
        return TemplateResponse(request, "admin/env_subform.html", context)

    def _handle_weather_env_post(self, request):
        values = {"WEATHER_DATA_DIR": request.POST.get("weather_data_dir", "").strip()}
        env_admin.handle_env_subform_post(
            request, self.message_user, values,
            audit_title="Weather environment configuration updated",
            audit_category="weather",
            dedupe_key="weather|env-updated",
            restart_check_keys=_WEATHER_ENV_KEYS,
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
