from pathlib import Path

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from isadoraair import env_admin, env_config

from .diagnostics import get_weather_diagnostics
from .forms import WeatherConfigForm
from .models import AmberAlertConfig, WeatherConfig, WeatherVoicePersona
from .setup_status import render_setup_status

_WEATHER_ENV_KEYS = ["WEATHER_DATA_DIR"]
# r0050: proves weather.diagnostics is genuinely reusable -- this page no
# longer maintains its own separate hardcoded file-presence list, it
# reads the same facts the weather_diagnostics management command does.
_WEATHER_DIAGNOSTIC_FILE_KEYS = [
    "weather_data_file:latest_weather.json",
    "weather_data_file:wind_history.json",
    "weather_data_file:smoothed_wind.json",
]


@admin.register(WeatherVoicePersona)
class WeatherVoicePersonaAdmin(admin.ModelAdmin):
    list_display = ["slot", "display_name", "full_name", "tts_voice"]
    search_fields = ["slot", "display_name", "full_name", "tts_voice__name"]


@admin.register(WeatherConfig)
class WeatherConfigAdmin(admin.ModelAdmin):
    """weather_env_link/weather_env_view (Phase 2, 2026-08-11): Weather
    data directory (WEATHER_DATA_DIR) is a separate, .env-backed setting
    -- surfaced here via a sub-page (isadoraair/env_admin.py's shared
    helper), not injected into this ModelAdmin's own save_model()."""
    form = WeatherConfigForm
    fieldsets = [
        ("Weather Setup Status", {
            "fields": ["setup_status"],
            "description": "Read-only snapshot from weather.diagnostics, generated fresh on "
                            "every page load -- reload the page to recheck. This panel never "
                            "changes anything; the ordinary form below is unaffected by it.",
        }),
        ("Station Location", {
            "fields": ["station_lat", "station_lon", "sun_alt_threshold_deg"],
        }),
        ("NWS Lookup", {
            "fields": ["nws_alert_zone", "nws_forecast_office", "nws_forecast_grid_x",
                       "nws_forecast_grid_y", "nws_cloud_stations"],
        }),
        ("Announcer Voices", {
            "fields": ["voice_schedule"],
            "description": "Click an hour to assign the announcer on duty for that hour, "
                            "station-local time.",
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
    readonly_fields = ["setup_status", "weather_env_link"]

    def has_add_permission(self, request):
        return not WeatherConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = WeatherConfig.load()
        return HttpResponseRedirect(
            reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        )

    @admin.display(description="")
    def setup_status(self, obj):
        """Pass C: the whole panel is rendered by weather.setup_status
        from a single, fresh get_weather_diagnostics() call -- this
        method itself does no ORM/filesystem inspection of its own (see
        that module's own docstring). `obj` is intentionally unused:
        the diagnostics snapshot reads WeatherConfig/AmberAlertConfig
        directly, so this renders identically on the add form (before
        a row exists) and the change form."""
        return render_setup_status()

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
            "level": "info",
            "text": (
                "The in-tree weather_ingest jobs read this authoritative setting through "
                "the dump_weather_config management-command bridge on every new job. A "
                "saved path therefore takes effect for the next invocation; move existing "
                "shared files before jobs resume, and restart the web service for Django's "
                "long-running process."
            ),
        }]
        try:
            saved = env_config.read_managed_values(_WEATHER_ENV_KEYS)
            data_dir_value = saved["WEATHER_DATA_DIR"].display_value
        except env_config.EnvConfigError:
            data_dir_value = None
        if data_dir_value:
            snapshot = get_weather_diagnostics(data_dir=Path(data_dir_value))
            # r0051: a malformed-but-present file is physically here --
            # `exists` (structured filesystem evidence on the fact,
            # independent of `state`) is what settles that, so a parse
            # failure is never described as "Missing". State alone
            # decides only healthy-vs-not among files that DO exist.
            present = []
            unhealthy = []
            missing = []
            for key in _WEATHER_DIAGNOSTIC_FILE_KEYS:
                fact = snapshot.get(key)
                filename = key.split(":", 1)[1]
                exists = bool(fact and fact.evidence and fact.evidence.get("exists"))
                if fact and fact.state == "ready":
                    present.append(filename)
                elif exists:
                    unhealthy.append(filename)
                else:
                    missing.append(filename)
            status_text = f"Diagnostic files currently in the saved directory: {', '.join(present) if present else 'none yet'}."
            if unhealthy:
                status_text += f" Present but failed to parse (needs attention): {', '.join(unhealthy)}."
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
