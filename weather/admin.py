from pathlib import Path

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from isadoraair import env_admin, env_config

from .diagnostics import get_weather_diagnostics
from .forms import WeatherConfigForm
from .models import AmberAlertConfig, WeatherConfig, WeatherVoicePersona
from .nws_discovery import NWSDiscoveryError, fetch_nws_point
from .setup_status import render_setup_status
from .voice_schedule import ScheduleError, expand_to_hours

_WEATHER_ENV_KEYS = ["WEATHER_DATA_DIR"]
# The only fields nws-discovery/'s Apply action is ever allowed to
# write -- also the exact set validated via WeatherConfig.full_clean()
# before that write (see nws_discovery_view).
_NWS_APPLY_FIELDS = ["nws_forecast_office", "nws_forecast_grid_x", "nws_forecast_grid_y", "nws_alert_zone"]
# r0050: proves weather.diagnostics is genuinely reusable -- this page no
# longer maintains its own separate hardcoded file-presence list, it
# reads the same facts the weather_diagnostics management command does.
_WEATHER_DIAGNOSTIC_FILE_KEYS = [
    "weather_data_file:latest_weather.json",
    "weather_data_file:wind_history.json",
    "weather_data_file:smoothed_wind.json",
]


def _weather_schedule_reference_status():
    """The one place that decides whether the current Weather announcer
    schedule can be trusted to say a persona slot is safe to delete.
    Returns one of:

      ("missing_config", None)  -- no WeatherConfig row exists at all;
                                    nothing can reference any slot.
      ("malformed", None)       -- a WeatherConfig row exists but its
                                    stored voice_schedule cannot be
                                    interpreted (see voice_schedule.
                                    expand_to_hours()) -- deletion
                                    safety CANNOT be determined, so
                                    every caller here must fail closed
                                    (refuse deletion) rather than ever
                                    assuming "not referenced".
      ("ok", {slots...})        -- the schedule is well-formed; the set
                                    is every slot it currently
                                    references.

    Deliberately reuses voice_schedule.expand_to_hours() -- the SAME
    authority WeatherConfigForm's own validation uses -- rather than a
    separate raw scan, so "is this schedule interpretable at all" can
    never disagree between the two. Never touches WeatherConfig.load()
    -- a persona-admin action must not risk creating the WeatherConfig
    singleton just to check for references."""
    cfg = WeatherConfig.objects.filter(pk=1).first()
    if cfg is None:
        return "missing_config", None
    try:
        hour_to_slot = expand_to_hours(cfg.voice_schedule)
    except ScheduleError:
        return "malformed", None
    return "ok", set(hour_to_slot.values())


@admin.register(WeatherVoicePersona)
class WeatherVoicePersonaAdmin(admin.ModelAdmin):
    """r0053 (Pass D): `slot` is treated as a stable identity once a
    persona exists -- WeatherConfig.voice_schedule references it by
    that exact string, so a casual rename would silently orphan
    whatever hours currently point at it (see get_readonly_fields()).
    A new persona may still choose any valid unique slot on creation.

    Deletion safety (r0053 review amendment): the built-in bulk
    `delete_selected` action is removed entirely (see get_actions) --
    it bypasses delete_view()'s own guard otherwise. Individual
    deletion stays available through delete_view(), which refuses (with
    a clear message) both a slot still referenced by the current
    schedule AND a schedule that cannot currently be interpreted at all
    (fail closed -- see _weather_schedule_reference_status(); an
    uninterpretable schedule is never treated as "references nothing")."""

    list_display = ["slot", "display_name", "full_name", "tts_voice"]
    search_fields = ["slot", "display_name", "full_name", "tts_voice__name"]
    autocomplete_fields = ["tts_voice"]

    def get_readonly_fields(self, request, obj=None):
        return ["slot"] if obj is not None else []

    def get_actions(self, request):
        # The documented way to remove ONE built-in action while
        # leaving the action framework (and any other action) intact --
        # `actions = None`/`[]` would also disable per-object deletion
        # semantics this admin doesn't otherwise touch.
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def delete_view(self, request, object_id, extra_context=None):
        obj = self.get_object(request, object_id)
        if obj is not None:
            status, referenced_slots = _weather_schedule_reference_status()
            if status == "malformed":
                self.message_user(
                    request,
                    "The current Weather announcer schedule could not be read (it is "
                    f'malformed) -- repair the announcer schedule on the Weather '
                    f'Configuration page first, then retry deleting "{obj.slot}".',
                    level=messages.ERROR,
                )
                return HttpResponseRedirect(reverse("admin:weather_weathervoicepersona_change", args=[object_id]))
            if status == "ok" and obj.slot in referenced_slots:
                self.message_user(
                    request,
                    f'"{obj.slot}" is still referenced by the current Weather announcer '
                    f"schedule -- reassign those hours to a different persona first, then delete it.",
                    level=messages.ERROR,
                )
                return HttpResponseRedirect(reverse("admin:weather_weathervoicepersona_change", args=[object_id]))
            # status == "missing_config", or "ok" and not referenced -- permitted.
        return super().delete_view(request, object_id, extra_context)


@admin.register(WeatherConfig)
class WeatherConfigAdmin(admin.ModelAdmin):
    """weather_env_link/weather_env_view (Phase 2, 2026-08-11): Weather
    data directory (WEATHER_DATA_DIR) is a separate, .env-backed setting
    -- surfaced here via a sub-page (isadoraair/env_admin.py's shared
    helper), not injected into this ModelAdmin's own save_model().

    r0053 (Pass D) adds two more of the same shape: announcer-personas/
    (a summary + links into WeatherVoicePersonaAdmin's own real add/
    change views -- never a second persona form) and nws-discovery/ (an
    explicit, operator-triggered NWS /points lookup -- see
    weather/nws_discovery.py). Both follow the same "no nested form
    inside WeatherConfig's own POST" shape weather_env_view already
    established."""

    form = WeatherConfigForm
    fieldsets = [
        ("Weather Setup Status", {
            "fields": ["setup_status"],
            "description": "Read-only snapshot from weather.diagnostics, generated fresh on "
                            "every page load -- reload the page to recheck. This panel never "
                            "changes anything; the ordinary form below is unaffected by it.",
        }),
        ("Station Location", {
            "fields": ["station_lat", "station_lon"],
        }),
        ("NWS Setup", {
            "fields": ["nws_summary"],
            "description": "The manual office/grid/alert-zone values below (Advanced Weather "
                            "Settings) are what weather_ingest actually reads. Discovery here "
                            "only assists filling them in from your station's coordinates -- it "
                            "never runs automatically and never saves without an explicit Apply.",
        }),
        ("Announcer Voices", {
            "fields": ["announcer_summary", "voice_schedule"],
            "description": "Click an hour to assign the announcer on duty for that hour, "
                            "station-local time.",
        }),
        ("Alert Beep", {
            "fields": ["alert_sound_enabled", "alert_sound_cart", "alert_sound_interval_minutes",
                       "alert_sound_trigger_events_text"],
            "description": "The repeating sonar/ping Weather Alert Beep -- fires the selected "
                            "FX Cart while a qualifying NWS event remains active, through "
                            "IsadoraAir's normal FX/program bus, present on air and in studio/"
                            "remote-DJ monitoring, same as any other cart fire. This is "
                            "completely separate from the generated spoken Weather (WxAlert) "
                            "and AMBER-family statements -- those have their own independent "
                            "selection rules and are unaffected by the trigger list below. The "
                            "cart's own filepath, gain, and retrigger mode are set on the FX "
                            "Cart itself (Library -> FX Carts), not here.",
        }),
        ("Notifications", {
            "fields": ["notify_email"],
        }),
        ("Weather data storage", {
            "fields": ["weather_env_link"],
            "description": "Where GW3000/Ecowitt weather JSON files land -- stored in "
                            ".env, not this database record.",
        }),
        ("Advanced Weather Settings", {
            "fields": ["sun_alt_threshold_deg", "nws_alert_zone", "nws_forecast_office",
                       "nws_forecast_grid_x", "nws_forecast_grid_y", "nws_cloud_stations"],
            "classes": ["collapse"],
            "description": "Manual overrides -- fully editable by hand at any time. NWS Setup's "
                            "discovery action can fill nws_alert_zone/nws_forecast_office/"
                            "nws_forecast_grid_x/nws_forecast_grid_y in for you, but never "
                            "touches sun_alt_threshold_deg or nws_cloud_stations (METAR station "
                            "selection stays entirely operator-controlled).",
        }),
    ]
    readonly_fields = ["setup_status", "announcer_summary", "nws_summary", "weather_env_link"]
    autocomplete_fields = ["alert_sound_cart"]

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

    @admin.display(description="Configured announcers")
    def announcer_summary(self, obj):
        """A plain directory of existing personas plus a link into the
        real announcer-personas/ subpage -- never a second copy of
        WeatherVoicePersonaAdmin's own add/change form."""
        personas = list(WeatherVoicePersona.objects.select_related("tts_voice").order_by("slot"))
        manage_url = reverse("admin:weather_weatherconfig_announcer_personas")
        if not personas:
            body = format_html('<p style="color:#888;">No Weather Voice Personas exist yet.</p>')
        else:
            items = format_html_join(
                "", "<li>{} ({}): {}</li>",
                (
                    (
                        p.display_name or p.full_name or p.slot,
                        p.slot,
                        p.tts_voice.name if p.tts_voice_id else "no logical voice selected",
                    )
                    for p in personas
                ),
            )
            body = format_html("<ul>{}</ul>", items)
        return format_html('{}<p><a class="button" href="{}">Manage Weather Announcers</a></p>', body, manage_url)

    @admin.display(description="NWS setup")
    def nws_summary(self, obj):
        if obj is None or obj.pk is None:
            return "(save the configuration first)"
        discovery_url = reverse("admin:weather_weatherconfig_nws_discovery")
        return format_html(
            "<p>Forecast office/grid: <strong>{} {},{}</strong><br>"
            "Alert county UGC: <strong>{}</strong></p>"
            '<p><a class="button" href="{}">Discover / Validate from Station Location</a> '
            '<span style="color:#888;font-size:0.85em;">Calls api.weather.gov only when you '
            "click this -- never on an ordinary page load, and never saves without an "
            "explicit Apply.</span></p>",
            obj.nws_forecast_office, obj.nws_forecast_grid_x, obj.nws_forecast_grid_y,
            obj.nws_alert_zone, discovery_url,
        )

    def get_urls(self):
        return [
            path(
                "weather-data-settings/",
                self.admin_site.admin_view(self.weather_env_view),
                name="weather_weatherconfig_weather_env",
            ),
            path(
                "announcer-personas/",
                self.admin_site.admin_view(self.announcer_personas_view),
                name="weather_weatherconfig_announcer_personas",
            ),
            path(
                "nws-discovery/",
                self.admin_site.admin_view(self.nws_discovery_view),
                name="weather_weatherconfig_nws_discovery",
            ),
            *super().get_urls(),
        ]

    @admin.display(description="Weather data storage")
    def weather_env_link(self, obj):
        if obj is None or obj.pk is None:
            return "(save the configuration first)"
        url = reverse("admin:weather_weatherconfig_weather_env")
        try:
            saved = env_config.read_managed_values(_WEATHER_ENV_KEYS)
            path_value = saved["WEATHER_DATA_DIR"].display_value or "(not set)"
        except env_config.EnvConfigError:
            path_value = "(could not read .env)"
        return format_html(
            "<p>Weather data path: <code>{}</code><br>"
            '<span style="color:#888;font-size:0.85em;">Status: see Weather Setup Status above.</span></p>'
            '<p><a class="button" href="{}">Edit weather data storage</a></p>',
            path_value, url,
        )

    # ---------------------------------
    # announcer-personas/ -- summary + links into the real
    # WeatherVoicePersonaAdmin add/change/delete views. This view itself
    # never edits a persona; it only reads for display.
    # ---------------------------------

    def announcer_personas_view(self, request):
        obj = WeatherConfig.load()
        change_url = reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        status, referenced_slots = _weather_schedule_reference_status()
        personas = []
        for p in WeatherVoicePersona.objects.select_related("tts_voice").order_by("slot"):
            # Fail closed here too: a malformed schedule means "cannot
            # confirm this is safe to delete", displayed the same as an
            # actually-referenced slot -- never treated as unreferenced.
            blocked = status == "malformed" or (status == "ok" and p.slot in referenced_slots)
            personas.append({
                "slot": p.slot,
                "display_name": p.display_name,
                "full_name": p.full_name,
                "tts_voice_name": p.tts_voice.name if p.tts_voice_id else None,
                "tts_voice_enabled": p.tts_voice.enabled if p.tts_voice_id else None,
                "referenced": blocked,
                "change_url": reverse("admin:weather_weathervoicepersona_change", args=[p.pk]),
                "delete_url": reverse("admin:weather_weathervoicepersona_delete", args=[p.pk]),
            })
        context = {
            **self.admin_site.each_context(request),
            "title": "Manage Weather Announcers",
            "opts": self.model._meta,
            "change_url": change_url,
            "personas": personas,
            "add_url": reverse("admin:weather_weathervoicepersona_add"),
        }
        return TemplateResponse(request, "admin/weather/announcer_personas.html", context)

    # ---------------------------------
    # nws-discovery/ -- explicit operator action only. GET never
    # contacts NWS; POST action=discover does (and does not save);
    # POST action=apply saves ONLY the 4 fields discovery covers, from
    # values carried in this same request's hidden fields (never a
    # second live lookup).
    # ---------------------------------

    def nws_discovery_view(self, request):
        # r0053 review amendment: admin_site.admin_view() (wrapping this
        # in get_urls()) only establishes "is this an authenticated
        # staff user", the same as any other admin page -- it is NOT a
        # substitute for this specific model's own change permission.
        # Viewing requires view-or-change (Django's own default
        # has_view_permission semantics); any POST that can trigger a
        # real NWS call or write WeatherConfig requires change.
        if not self.has_view_permission(request):
            raise PermissionDenied
        obj = WeatherConfig.load()
        change_url = reverse("admin:weather_weatherconfig_change", args=[obj.pk])
        discovery_url = reverse("admin:weather_weatherconfig_nws_discovery")
        discovered = None
        discovery_error = None

        if request.method == "POST":
            if not self.has_change_permission(request):
                raise PermissionDenied
            action = request.POST.get("action")
            if action == "discover":
                try:
                    discovered = fetch_nws_point(obj.station_lat, obj.station_lon)
                except NWSDiscoveryError as exc:
                    discovery_error = str(exc)
            elif action == "apply":
                # Validate BEFORE touching `obj` for real -- these are
                # hidden POST values (carrying an already-reviewed
                # discovery result forward, so Apply never needs a
                # second live NWS call), and must cross the same
                # validation boundary the ordinary WeatherConfigForm
                # save would, not bypass it. full_clean() converts an
                # overlong/malformed value into a caught
                # ValidationError instead of a DB-level exception.
                office = (request.POST.get("grid_id") or "").strip()
                county_ugc = (request.POST.get("county_ugc") or "").strip()
                try:
                    grid_x = int((request.POST.get("grid_x") or "").strip())
                except (TypeError, ValueError):
                    grid_x = None
                try:
                    grid_y = int((request.POST.get("grid_y") or "").strip())
                except (TypeError, ValueError):
                    grid_y = None

                obj.nws_forecast_office = office
                obj.nws_forecast_grid_x = grid_x
                obj.nws_forecast_grid_y = grid_y
                obj.nws_alert_zone = county_ugc

                problems = []
                exclude_fields = [f.name for f in WeatherConfig._meta.fields if f.name not in _NWS_APPLY_FIELDS]
                try:
                    obj.full_clean(exclude=exclude_fields)
                except ValidationError as exc:
                    for field, errs in exc.message_dict.items():
                        problems.extend(f"{field}: {e}" for e in errs)
                # No additional zero-rejection here: NWS's own /points
                # schema declares gridX/gridY as "integer, minimum 0" --
                # zero is a legitimate coordinate, not a sentinel for
                # "missing". PositiveIntegerField's own validator (via
                # full_clean() above) already establishes the correct
                # >= 0 boundary; negative/non-numeric/null values are
                # caught there (non-numeric/null were already coerced to
                # None above, which full_clean rejects as required).

                if problems:
                    self.message_user(
                        request,
                        "Discovered values failed validation -- nothing changed: " + "; ".join(problems),
                        level=messages.ERROR,
                    )
                    return HttpResponseRedirect(discovery_url)

                obj.save(update_fields=_NWS_APPLY_FIELDS)
                self.message_user(
                    request,
                    f"Applied discovered NWS values: office {office}, grid {grid_x},{grid_y}, "
                    f"alert county UGC {county_ugc}.",
                )
                return HttpResponseRedirect(change_url)

        context = {
            **self.admin_site.each_context(request),
            "title": "NWS Setup Discovery",
            "opts": self.model._meta,
            "change_url": change_url,
            "discovery_url": discovery_url,
            "station_lat": obj.station_lat,
            "station_lon": obj.station_lon,
            "current": {
                "office": obj.nws_forecast_office,
                "grid_x": obj.nws_forecast_grid_x,
                "grid_y": obj.nws_forecast_grid_y,
                "alert_zone": obj.nws_alert_zone,
            },
            "discovered": discovered,
            "discovery_error": discovery_error,
        }
        return TemplateResponse(request, "admin/weather/nws_discovery.html", context)

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
