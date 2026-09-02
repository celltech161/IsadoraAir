"""r0028: Weather announcer schedule grid -- the Django form/widget/
admin layer built on weather.voice_schedule's pure expand/compress
functions (see test_voice_schedule.py for those). Covers required
items 5-12: coverage/gap/overlap/missing-persona/disabled-voice/
malformed-stored-data rejection, current-hour + on-duty summary
rendering, and preserved admin permissions/save behavior."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from isadoraair.tts.models import StationTTSVoice
from weather.forms import HourlyScheduleField, WeatherConfigForm, hour_label
from weather.models import WeatherConfig, WeatherVoicePersona
from weather.voice_schedule import compress_from_hours


def _hours(assignment):
    """{"day": [3,4,...], "night": [...]}-style shorthand -> the
    {hour: slot} dict HourlyScheduleField.to_python() expects, matching
    exactly what the 24 real <select> elements would post."""
    hours = {}
    for slot, hour_list in assignment.items():
        for hour in hour_list:
            hours[hour] = slot
    return hours


DAY_HOURS = list(range(3, 21))
NIGHT_HOURS = [h for h in range(24) if h not in DAY_HOURS]


class HourlyScheduleFieldValidationTests(TestCase):
    """Items 5-9: coverage/gap/overlap/missing-persona/disabled-voice."""

    def setUp(self):
        self.claira = StationTTSVoice.objects.create(
            name="claira_sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica",
        )
        self.max_voice = StationTTSVoice.objects.create(
            name="max_weatherly", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="am_michael",
        )
        WeatherVoicePersona.objects.create(slot="day", tts_voice=self.claira, display_name="Claira Sky")
        WeatherVoicePersona.objects.create(slot="night", tts_voice=self.max_voice, display_name="Max Weatherly")
        self.field = HourlyScheduleField()

    def test_valid_complete_schedule_cleans_to_canonical_json(self):
        value = self.field.clean(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS}))
        self.assertEqual(compress_from_hours({h: "day" for h in DAY_HOURS} | {h: "night" for h in NIGHT_HOURS}), value)

    def test_missing_hours_rejected(self):
        incomplete = _hours({"day": DAY_HOURS[:-1], "night": NIGHT_HOURS})  # hour 20 missing
        with self.assertRaises(Exception) as ctx:
            self.field.clean(incomplete)
        self.assertIn("missing", str(ctx.exception).lower())

    def test_overlap_rejected_at_the_pure_function_layer(self):
        # Overlap is structurally unreachable through this FIELD's own
        # to_python() -- 24 independent named <select> POST values can
        # only ever produce one hour -> one value (a dict, by
        # construction, cannot hold two values for the same key), so
        # there is no POST shape the grid (or a malicious duplicate-
        # key POST, which QueryDict.get() collapses to the last value
        # anyway) could use to express one. Real overlap can only ever
        # occur in STORED data (a hand-edited/legacy triple list) --
        # covered by WeatherConfigFormMalformedStoredDataTests below
        # (widget rendering) and directly by test_voice_schedule.py's
        # own ExpandToHoursTests.test_overlap_rejected (the pure
        # function both paths ultimately share).
        from weather.voice_schedule import ScheduleError, expand_to_hours
        with self.assertRaises(ScheduleError):
            expand_to_hours([["day", 0, 12], ["night", 10, 23]])

    def test_missing_persona_for_referenced_slot_rejected(self):
        WeatherVoicePersona.objects.filter(slot="night").delete()
        with self.assertRaises(Exception) as ctx:
            self.field.clean(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS}))
        self.assertIn("night", str(ctx.exception))

    def test_persona_missing_logical_voice_rejected(self):
        WeatherVoicePersona.objects.filter(slot="night").update(tts_voice=None)
        with self.assertRaises(Exception) as ctx:
            self.field.clean(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS}))
        self.assertIn("no logical station voice", str(ctx.exception))

    def test_disabled_logical_voice_rejected(self):
        self.max_voice.enabled = False
        self.max_voice.save()
        with self.assertRaises(Exception) as ctx:
            self.field.clean(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS}))
        self.assertIn("disabled", str(ctx.exception))

    def test_unreferenced_persona_with_disabled_voice_does_not_block_save(self):
        # A THIRD persona/voice pair that the schedule never actually
        # references must not gate saving -- only referenced slots are
        # validated.
        unused_voice = StationTTSVoice.objects.create(
            name="unused_voice", enabled=False, engine=StationTTSVoice.Engine.KOKORO, provider_voice="x",
        )
        WeatherVoicePersona.objects.create(slot="dawn", tts_voice=unused_voice, display_name="Dawn")
        self.field.clean(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS}))  # must not raise


class WeatherConfigFormMalformedStoredDataTests(TestCase):
    """Item 10: malformed EXISTING stored JSON (bad historical data, a
    hand-edited fixture, direct DB manipulation) must fail clearly and
    safely on render -- never silently reinterpreted -- and the form
    must still be usable to fix it."""

    def test_malformed_stored_schedule_does_not_crash_widget_rendering(self):
        cfg = WeatherConfig.load()
        cfg.voice_schedule = [["day", 0, 10]]  # a real gap, hours 11-23 uncovered
        cfg.save()

        form = WeatherConfigForm(instance=cfg)
        rendered = str(form["voice_schedule"])
        self.assertIn("could not be read", rendered)
        # Still renders 24 real, empty selects -- fixable, not a dead end.
        self.assertEqual(rendered.count("<select"), 24)

    def test_completely_non_list_stored_value_does_not_crash_widget_rendering(self):
        cfg = WeatherConfig.load()
        cfg.voice_schedule = {"not": "a list"}
        cfg.save()

        form = WeatherConfigForm(instance=cfg)
        rendered = str(form["voice_schedule"])
        self.assertIn("could not be read", rendered)

    def test_overlapping_stored_schedule_does_not_crash_widget_rendering(self):
        cfg = WeatherConfig.load()
        cfg.voice_schedule = [["day", 0, 12], ["night", 10, 23]]  # hours 10-12 double-covered
        cfg.save()

        form = WeatherConfigForm(instance=cfg)
        rendered = str(form["voice_schedule"])
        self.assertIn("could not be read", rendered)
        self.assertIn("overlapping", rendered)


class HourLabelTests(TestCase):
    def test_midnight_and_noon(self):
        self.assertEqual(hour_label(0), "12 AM")
        self.assertEqual(hour_label(12), "12 PM")

    def test_am_pm_boundaries(self):
        self.assertEqual(hour_label(1), "1 AM")
        self.assertEqual(hour_label(11), "11 AM")
        self.assertEqual(hour_label(13), "1 PM")
        self.assertEqual(hour_label(23), "11 PM")


@override_settings(SECURE_SSL_REDIRECT=False)
class WeatherConfigAdminGridIntegrationTests(TestCase):
    """Item 11 (current-hour/on-duty summary rendering) and item 12
    (existing admin permissions/save behavior preserved), exercised
    through the real admin view/client -- same pattern as
    test_env_admin.py."""

    def setUp(self):
        self.claira = StationTTSVoice.objects.create(
            name="claira_sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO, provider_voice="af_jessica",
        )
        self.max_voice = StationTTSVoice.objects.create(
            name="max_weatherly", enabled=True, engine=StationTTSVoice.Engine.KOKORO, provider_voice="am_michael",
        )
        WeatherVoicePersona.objects.create(slot="day", tts_voice=self.claira, display_name="Claira Sky")
        WeatherVoicePersona.objects.create(slot="night", tts_voice=self.max_voice, display_name="Max Weatherly")
        self.config = WeatherConfig.load()
        self.config.voice_schedule = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]
        self.config.save()

        self.staff = User.objects.create_user("wxstaff", "wxstaff@example.invalid", "pw", is_staff=True)
        self.super = User.objects.create_superuser("wxsuper", "wxsuper@example.invalid", "pw")

    def url(self):
        return reverse("admin:weather_weatherconfig_change", args=[self.config.pk])

    def _full_post_data(self, hours):
        data = {
            "station_lat": self.config.station_lat, "station_lon": self.config.station_lon,
            "sun_alt_threshold_deg": self.config.sun_alt_threshold_deg,
            "nws_alert_zone": self.config.nws_alert_zone, "nws_forecast_office": self.config.nws_forecast_office,
            "nws_forecast_grid_x": self.config.nws_forecast_grid_x, "nws_forecast_grid_y": self.config.nws_forecast_grid_y,
            "nws_cloud_stations": self.config.nws_cloud_stations,
            "notify_email": self.config.notify_email,
            "alert_sound_interval_seconds": self.config.alert_sound_interval_seconds,
            "_save": "Save",
        }
        if self.config.alert_sound_enabled:
            data["alert_sound_enabled"] = "on"
        for hour, slot in hours.items():
            data[f"voice_schedule_{hour}"] = slot
        return data

    def test_change_page_shows_grid_not_raw_json_textarea(self):
        self.client.force_login(self.super)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("wx-schedule-grid", body)
        self.assertEqual(body.count("wx-hour-select"), 24)  # 24 real selects, one per hour
        self.assertNotIn("<textarea", body)  # the old raw-JSON textarea is gone

    def test_current_hour_is_marked_in_rendered_page(self):
        self.client.force_login(self.super)
        response = self.client.get(self.url())
        current_hour = timezone.localtime(timezone.now()).hour
        self.assertContains(response, f'data-hour="{current_hour}"')
        self.assertContains(response, "wx-hour-current")

    def test_on_duty_summary_present(self):
        self.client.force_login(self.super)
        response = self.client.get(self.url())
        self.assertContains(response, "On duty now")

    def test_persona_range_summary_present(self):
        self.client.force_login(self.super)
        response = self.client.get(self.url())
        body = response.content.decode()
        self.assertIn("3 AM", body)
        self.assertIn("9 AM", body)

    def test_staff_without_change_permission_cannot_post(self):
        self.client.force_login(self.staff)
        original = list(self.config.voice_schedule)
        response = self.client.post(self.url(), self._full_post_data(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS})))
        self.assertNotEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.voice_schedule, original)

    def test_superuser_can_save_a_valid_schedule_change(self):
        self.client.force_login(self.super)
        new_hours = _hours({"day": list(range(6, 18)), "night": [h for h in range(24) if h not in range(6, 18)]})
        response = self.client.post(self.url(), self._full_post_data(new_hours), follow=True)
        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.voice_schedule, compress_from_hours(new_hours))

    def test_invalid_post_gap_does_not_persist_and_shows_form_error(self):
        self.client.force_login(self.super)
        broken = _hours({"day": DAY_HOURS})  # night hours omitted -- a real gap
        original = list(self.config.voice_schedule)
        response = self.client.post(self.url(), self._full_post_data(broken))
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not a redirect
        self.assertContains(response, "class=\"errorlist\"")
        self.config.refresh_from_db()
        self.assertEqual(self.config.voice_schedule, original)  # unchanged

    def test_csrf_required(self):
        from django.test import Client
        strict_client = Client(enforce_csrf_checks=True)
        strict_client.force_login(self.super)
        response = strict_client.post(self.url(), self._full_post_data(_hours({"day": DAY_HOURS, "night": NIGHT_HOURS})))
        self.assertEqual(response.status_code, 403)
