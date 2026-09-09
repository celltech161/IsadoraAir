"""Pass D (P1 1.15 / 2.4): Weather Configuration workflow completion --
announcer-persona management folded into the Weather Configuration
Admin workflow, alert-interval minutes presentation, FX Cart
autocomplete, routine/Advanced fieldset reorganization, and explicit
operator-triggered NWS discovery. See weather/admin.py,
weather/forms.py, and weather/nws_discovery.py."""
import json
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from isadoraair.tts.models import StationTTSVoice
from library.models import FXCart
from weather.forms import WeatherConfigForm
from weather.models import DEFAULT_ALERT_SOUND_TRIGGER_EVENTS, WeatherConfig, WeatherVoicePersona
from weather.nws_discovery import NWSDiscoveryError, fetch_nws_point


def make_voice(name="claira_sky", enabled=True):
    return StationTTSVoice.objects.create(
        name=name, enabled=enabled, engine=StationTTSVoice.Engine.KOKORO, provider_voice="af_jessica",
    )


@override_settings(SECURE_SSL_REDIRECT=False)
class PersonaWorkflowTests(TestCase):
    def setUp(self):
        self.super = User.objects.create_superuser("wxpersonaadmin", "x@example.invalid", "pw")
        self.client.force_login(self.super)
        self.claira = make_voice("claira_sky")
        self.max_voice = make_voice("max_weatherly")
        WeatherVoicePersona.objects.create(slot="day", tts_voice=self.claira, display_name="Claira Sky")
        WeatherVoicePersona.objects.create(slot="night", tts_voice=self.max_voice, display_name="Max Weatherly")
        self.config = WeatherConfig.objects.create(pk=1, voice_schedule=[["day", 6, 17], ["night", 18, 5]])

    def test_existing_day_night_personas_render_on_summary(self):
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        body = resp.content.decode()
        self.assertIn("Claira Sky", body)
        self.assertIn("Max Weatherly", body)
        self.assertIn("Manage Weather Announcers", body)

    def test_manage_announcers_page_lists_arbitrary_additional_slot(self):
        extra_voice = make_voice("morning_voice")
        WeatherVoicePersona.objects.create(slot="morning_host", tts_voice=extra_voice, display_name="Morgan")
        resp = self.client.get(reverse("admin:weather_weatherconfig_announcer_personas"))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("morning_host", body)
        self.assertIn("Morgan", body)

    def test_add_persona_via_real_weathervoicepersona_admin(self):
        weekend_voice = make_voice("weekend_voice")
        resp = self.client.post(reverse("admin:weather_weathervoicepersona_add"), {
            "slot": "weekend", "tts_voice": weekend_voice.pk,
            "display_name": "Weekend Wendy", "full_name": "", "signoff": "",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(WeatherVoicePersona.objects.filter(slot="weekend").exists())

    def test_edit_logical_tts_voice(self):
        persona = WeatherVoicePersona.objects.get(slot="day")
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_change", args=[persona.pk]),
            {"slot": "day", "tts_voice": self.max_voice.pk, "display_name": "Claira Sky", "full_name": "", "signoff": ""},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        persona.refresh_from_db()
        self.assertEqual(persona.tts_voice_id, self.max_voice.pk)

    def test_edit_display_full_signoff(self):
        persona = WeatherVoicePersona.objects.get(slot="day")
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_change", args=[persona.pk]),
            {
                "slot": "day", "tts_voice": self.claira.pk, "display_name": "Claira",
                "full_name": "Claira Sky, Chief Meteorologist", "signoff": "Stay safe out there.",
            },
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        persona.refresh_from_db()
        self.assertEqual(persona.display_name, "Claira")
        self.assertEqual(persona.full_name, "Claira Sky, Chief Meteorologist")
        self.assertEqual(persona.signoff, "Stay safe out there.")

    def test_existing_slot_is_readonly_on_change_form(self):
        persona = WeatherVoicePersona.objects.get(slot="day")
        resp = self.client.get(reverse("admin:weather_weathervoicepersona_change", args=[persona.pk]))
        body = resp.content.decode()
        # A readonly field renders its value as plain text, not an
        # editable <input name="slot" ...> control.
        self.assertNotIn('name="slot"', body)
        self.assertIn("day", body)

    def test_slot_is_editable_on_add_form(self):
        resp = self.client.get(reverse("admin:weather_weathervoicepersona_add"))
        self.assertContains(resp, 'name="slot"')

    def test_renaming_existing_slot_via_post_is_ignored_not_applied(self):
        persona = WeatherVoicePersona.objects.get(slot="day")
        self.client.post(
            reverse("admin:weather_weathervoicepersona_change", args=[persona.pk]),
            {"slot": "totally_different", "tts_voice": self.claira.pk, "display_name": "Claira Sky",
             "full_name": "", "signoff": ""},
            follow=True,
        )
        persona.refresh_from_db()
        self.assertEqual(persona.slot, "day")  # unchanged -- readonly field, tampered POST ignored

    def test_schedule_grid_sees_newly_created_persona(self):
        weekend_voice = make_voice("weekend_voice2")
        WeatherVoicePersona.objects.create(slot="weekend", tts_voice=weekend_voice, display_name="Wendy")
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        body = resp.content.decode()
        self.assertIn("weekend", body)  # available as an option in the 24 hour <select>s

    def test_provider_native_identity_not_exposed_on_persona_form(self):
        persona = WeatherVoicePersona.objects.get(slot="day")
        resp = self.client.get(reverse("admin:weather_weathervoicepersona_change", args=[persona.pk]))
        body = resp.content.decode()
        self.assertNotIn('name="provider_voice"', body)
        self.assertNotIn('name="piper_model"', body)
        self.assertNotIn("af_jessica", body)

    def test_tts_voice_field_uses_autocomplete(self):
        resp = self.client.get(reverse("admin:weather_weathervoicepersona_add"))
        body = resp.content.decode()
        self.assertIn("admin-autocomplete", body)

    def test_deletion_refused_when_slot_referenced_by_schedule(self):
        persona = WeatherVoicePersona.objects.get(slot="day")  # referenced by self.config.voice_schedule
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_delete", args=[persona.pk]), {"post": "yes"}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(WeatherVoicePersona.objects.filter(pk=persona.pk).exists())
        self.assertIn("still referenced", resp.content.decode())

    def test_deletion_allowed_when_slot_not_referenced(self):
        unused_voice = make_voice("unused_voice")
        unused = WeatherVoicePersona.objects.create(slot="unused", tts_voice=unused_voice)
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_delete", args=[unused.pk]), {"post": "yes"}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(WeatherVoicePersona.objects.filter(pk=unused.pk).exists())

    def test_delete_selected_bulk_action_is_unavailable(self):
        """r0053 review amendment: the guard in delete_view() alone
        never protected Django Admin's built-in bulk 'Delete selected'
        action -- it must be removed from the action list entirely."""
        resp = self.client.get(reverse("admin:weather_weathervoicepersona_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('value="delete_selected"', resp.content.decode())

        from django.contrib.admin.sites import site
        from django.test import RequestFactory
        admin_instance = site._registry[WeatherVoicePersona]
        request = RequestFactory().get(reverse("admin:weather_weathervoicepersona_changelist"))
        request.user = self.super
        self.assertNotIn("delete_selected", admin_instance.get_actions(request))

    def test_referenced_persona_survives_bulk_delete_selected_attempt(self):
        """Even if delete_selected were somehow still reachable, POSTing
        it directly must not delete a referenced persona -- belt and
        suspenders alongside removing the action from the UI."""
        persona = WeatherVoicePersona.objects.get(slot="day")
        self.client.post(reverse("admin:weather_weathervoicepersona_changelist"), {
            "action": "delete_selected", "_selected_action": [str(persona.pk)],
        })
        self.assertTrue(WeatherVoicePersona.objects.filter(pk=persona.pk).exists())

    def test_deletion_refused_when_schedule_is_malformed(self):
        """Fail closed: a schedule that cannot be interpreted must
        never be treated as 'references nothing'."""
        self.config.voice_schedule = "not-a-valid-schedule"
        self.config.save(update_fields=["voice_schedule"])
        persona = WeatherVoicePersona.objects.get(slot="night")  # would be UNreferenced under a valid schedule
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_delete", args=[persona.pk]), {"post": "yes"}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(WeatherVoicePersona.objects.filter(pk=persona.pk).exists())
        body = resp.content.decode()
        self.assertIn("malformed", body.lower())
        self.assertIn("repair the announcer schedule", body)

    def test_deletion_permitted_when_no_weatherconfig_exists(self):
        self.config.delete()
        unused_voice = make_voice("standalone_voice")
        standalone = WeatherVoicePersona.objects.create(slot="standalone", tts_voice=unused_voice)
        resp = self.client.post(
            reverse("admin:weather_weathervoicepersona_delete", args=[standalone.pk]), {"post": "yes"}, follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(WeatherVoicePersona.objects.filter(pk=standalone.pk).exists())


class AlertBeepTriggerEventsTests(TestCase):
    """Amendment to Pass D: the Weather Alert Beep's qualifying-event
    list (the repeating sonar/ping FX Cart, NOT the spoken WxAlert/
    AMBER statements) is now WeatherConfig.alert_sound_trigger_events,
    Django-configurable, defaulting to the four legacy hard-coded
    values so an upgrade changes nothing without operator action."""

    def test_fresh_weatherconfig_has_exactly_four_legacy_defaults(self):
        cfg = WeatherConfig.load()
        self.assertEqual(
            cfg.alert_sound_trigger_events,
            ["Tornado Warning", "Severe Thunderstorm Warning", "Tornado Watch", "Severe Thunderstorm Watch"],
        )
        self.assertEqual(cfg.alert_sound_trigger_events, DEFAULT_ALERT_SOUND_TRIGGER_EVENTS)

    def test_row_created_without_specifying_field_gets_legacy_defaults(self):
        """Proxy for 'an existing upgraded station receives equivalent
        defaults': any WeatherConfig row saved without an explicit
        value for this new field -- exactly what an already-migrated
        r0052 station's row looked like the instant r0053's AddField
        migration ran -- gets the model's default value. Django's own
        AddField-with-default machinery is what performs the actual
        one-time backfill of a pre-existing row at migration time;
        this proves the default it backfills WITH is correct."""
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        self.assertEqual(cfg.alert_sound_trigger_events, DEFAULT_ALERT_SOUND_TRIGGER_EVENTS)

    def test_default_list_is_a_fresh_object_each_time_not_shared_mutable_state(self):
        cfg1 = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        cfg1.alert_sound_trigger_events.append("Flash Flood Emergency")
        cfg2 = WeatherConfig(voice_schedule=[["default", 0, 23]])  # unsaved, but triggers the default too
        self.assertEqual(cfg2.alert_sound_trigger_events, DEFAULT_ALERT_SOUND_TRIGGER_EVENTS)
        self.assertNotIn("Flash Flood Emergency", cfg2.alert_sound_trigger_events)

    def test_dump_weather_config_emits_configured_trigger_list(self):
        cfg = WeatherConfig.load()
        cfg.alert_sound_trigger_events = ["Tornado Warning", "Flash Flood Emergency"]
        cfg.save()
        out = StringIO()
        call_command("dump_weather_config", stdout=out)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["alert_sound_trigger_events"], ["Tornado Warning", "Flash Flood Emergency"])
        # Existing keys' meanings are untouched by this amendment.
        self.assertIn("alert_sound_enabled", payload)
        self.assertIn("alert_sound_cart_id", payload)
        self.assertIn("alert_sound_interval_seconds", payload)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_admin_renders_distinguishing_help_text(self):
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        staff = User.objects.create_superuser("wxtriggeradmin", "t@example.invalid", "pw")
        self.client.force_login(staff)
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[cfg.pk]))
        body = resp.content.decode()
        self.assertIn("Alert types that trigger the repeating beep", body)
        self.assertIn("does not control generated spoken Weather or AMBER alert statements", body)
        self.assertIn('name="alert_sound_trigger_events_text"', body)
        self.assertNotIn('name="alert_sound_trigger_events"', body)  # raw JSONField widget not exposed

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_admin_saves_custom_trigger_configuration(self):
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        staff = User.objects.create_superuser("wxtriggeradmin2", "t2@example.invalid", "pw")
        self.client.force_login(staff)
        data = {
            "station_lat": 39.13, "station_lon": -97.70, "sun_alt_threshold_deg": 3.0,
            "nws_alert_zone": "KSC143", "nws_forecast_office": "TOP",
            "nws_forecast_grid_x": 10, "nws_forecast_grid_y": 53, "nws_cloud_stations": "KCNK,KSLN",
            "notify_email": "", "alert_sound_interval_minutes": "10",
            "alert_sound_trigger_events_text": "Tornado Warning\nFlash Flood Emergency\n",
            "_save": "Save",
        }
        for hour in range(24):
            data[f"voice_schedule_{hour}"] = "default"
        resp = self.client.post(reverse("admin:weather_weatherconfig_change", args=[cfg.pk]), data, follow=True)
        self.assertEqual(resp.status_code, 200)
        cfg.refresh_from_db()
        self.assertEqual(cfg.alert_sound_trigger_events, ["Tornado Warning", "Flash Flood Emergency"])

    def test_form_round_trips_stored_list_as_initial_text(self):
        cfg = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]],
            alert_sound_trigger_events=["Tornado Warning", "Flash Flood Emergency"],
        )
        form = WeatherConfigForm(instance=cfg)
        self.assertEqual(
            form.fields["alert_sound_trigger_events_text"].initial,
            "Tornado Warning\nFlash Flood Emergency",
        )

    def test_empty_textarea_saves_as_explicit_empty_list(self):
        """Preferred semantics: no NWS event triggers the repeating
        beep when the operator clears the field entirely."""
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        cfg = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], alert_sound_trigger_events=["Tornado Warning"],
        )
        data = {
            "station_lat": 39.13, "station_lon": -97.70, "sun_alt_threshold_deg": 3.0,
            "nws_alert_zone": "KSC143", "nws_forecast_office": "TOP",
            "nws_forecast_grid_x": 10, "nws_forecast_grid_y": 53, "nws_cloud_stations": "KCNK,KSLN",
            "notify_email": "", "alert_sound_interval_minutes": "10",
            "alert_sound_trigger_events_text": "",
        }
        for hour in range(24):
            data[f"voice_schedule_{hour}"] = "default"
        form = WeatherConfigForm(data=data, instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.alert_sound_trigger_events, [])

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_alert_beep_enable_cart_interval_unaffected_by_trigger_list_change(self):
        """Changing the trigger-event list in the same save must not
        disturb the pre-existing enable/cart/interval fields, and vice
        versa -- the four settings are independent form fields."""
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        cart = FXCart.objects.create(name="WX Beep 2", filepath="/tmp/beep2.wav")
        cfg = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
            alert_sound_interval_seconds=600, alert_sound_trigger_events=["Tornado Warning"],
        )
        staff = User.objects.create_superuser("wxtriggeradmin3", "t3@example.invalid", "pw")
        self.client.force_login(staff)
        data = {
            "station_lat": 39.13, "station_lon": -97.70, "sun_alt_threshold_deg": 3.0,
            "nws_alert_zone": "KSC143", "nws_forecast_office": "TOP",
            "nws_forecast_grid_x": 10, "nws_forecast_grid_y": 53, "nws_cloud_stations": "KCNK,KSLN",
            "notify_email": "", "alert_sound_interval_minutes": "10",
            "alert_sound_enabled": "on", "alert_sound_cart": str(cart.pk),
            "alert_sound_trigger_events_text": "Tornado Warning\nSevere Thunderstorm Warning",
            "_save": "Save",
        }
        for hour in range(24):
            data[f"voice_schedule_{hour}"] = "default"
        resp = self.client.post(reverse("admin:weather_weatherconfig_change", args=[cfg.pk]), data, follow=True)
        self.assertEqual(resp.status_code, 200)
        cfg.refresh_from_db()
        self.assertTrue(cfg.alert_sound_enabled)
        self.assertEqual(cfg.alert_sound_cart_id, cart.pk)
        self.assertEqual(cfg.alert_sound_interval_seconds, 600)
        self.assertEqual(cfg.alert_sound_trigger_events, ["Tornado Warning", "Severe Thunderstorm Warning"])


class AlertIntervalFormTests(TestCase):
    def setUp(self):
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)

    def _base_data(self, **overrides):
        data = {
            "station_lat": 39.13, "station_lon": -97.70, "sun_alt_threshold_deg": 3.0,
            "nws_alert_zone": "KSC143", "nws_forecast_office": "TOP",
            "nws_forecast_grid_x": 10, "nws_forecast_grid_y": 53, "nws_cloud_stations": "KCNK,KSLN",
            "notify_email": "", "alert_sound_interval_minutes": "10",
        }
        for hour in range(24):
            data[f"voice_schedule_{hour}"] = "default"
        data.update(overrides)
        return data

    def test_600_seconds_displays_as_10_minutes(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_interval_seconds=600)
        form = WeatherConfigForm(instance=cfg)
        self.assertEqual(form.fields["alert_sound_interval_minutes"].initial, Decimal("10.00"))

    def test_10_minutes_post_stores_600_seconds(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_interval_seconds=999)
        form = WeatherConfigForm(data=self._base_data(alert_sound_interval_minutes="10"), instance=cfg)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.alert_sound_interval_seconds, 600)

    def test_non_integral_minute_existing_value_round_trips_exactly(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_interval_seconds=90)
        form = WeatherConfigForm(instance=cfg)
        minutes = form.fields["alert_sound_interval_minutes"].initial
        self.assertEqual(minutes, Decimal("1.50"))
        # Round-trip: submitting that exact displayed value back must
        # reproduce the original 90 seconds, not a rounded 60 or 120.
        form2 = WeatherConfigForm(data=self._base_data(alert_sound_interval_minutes=str(minutes)), instance=cfg)
        self.assertTrue(form2.is_valid(), form2.errors)
        self.assertEqual(form2.save().alert_sound_interval_seconds, 90)

    def test_odd_second_value_round_trips_exactly(self):
        """605 seconds isn't a whole number of centiseconds-of-a-minute
        either -- proves the 2-decimal-place minute display still has
        enough resolution to reconstruct the exact original seconds."""
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_interval_seconds=605)
        form = WeatherConfigForm(instance=cfg)
        minutes = form.fields["alert_sound_interval_minutes"].initial
        form2 = WeatherConfigForm(data=self._base_data(alert_sound_interval_minutes=str(minutes)), instance=cfg)
        self.assertTrue(form2.is_valid(), form2.errors)
        self.assertEqual(form2.save().alert_sound_interval_seconds, 605)

    def test_zero_minutes_rejected(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        form = WeatherConfigForm(data=self._base_data(alert_sound_interval_minutes="0"), instance=cfg)
        self.assertFalse(form.is_valid())
        self.assertIn("alert_sound_interval_minutes", form.errors)

    def test_negative_minutes_rejected(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        form = WeatherConfigForm(data=self._base_data(alert_sound_interval_minutes="-5"), instance=cfg)
        self.assertFalse(form.is_valid())
        self.assertIn("alert_sound_interval_minutes", form.errors)

    def test_seconds_field_not_rendered(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        form = WeatherConfigForm(instance=cfg)
        self.assertNotIn("alert_sound_interval_seconds", form.fields)

    def test_unrelated_save_does_not_alter_interval(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_interval_seconds=605)
        form = WeatherConfigForm(
            data=self._base_data(alert_sound_interval_minutes="10.08", notify_email="ops@example.invalid"),
            instance=cfg,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.alert_sound_interval_seconds, 605)
        self.assertEqual(saved.notify_email, "ops@example.invalid")


@override_settings(SECURE_SSL_REDIRECT=False)
class FXCartAutocompleteTests(TestCase):
    def setUp(self):
        self.super = User.objects.create_superuser("wxfxcartadmin", "y@example.invalid", "pw")
        self.client.force_login(self.super)
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)
        self.cart = FXCart.objects.create(name="WX Beep", filepath="/tmp/beep.wav")
        self.config = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=self.cart,
        )

    def test_alert_sound_cart_field_uses_autocomplete_widget(self):
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        body = resp.content.decode()
        self.assertIn("admin-autocomplete", body)
        self.assertIn('name="alert_sound_cart"', body)

    def test_autocomplete_endpoint_finds_cart_by_name(self):
        from django.contrib import admin as django_admin
        url = reverse("admin:autocomplete")
        resp = self.client.get(url, {
            "app_label": "weather", "model_name": "weatherconfig", "field_name": "alert_sound_cart", "term": "WX",
        })
        self.assertEqual(resp.status_code, 200)
        import json
        payload = json.loads(resp.content)
        self.assertTrue(any(r["text"] == "WX Beep" for r in payload["results"]))

    def test_existing_cart_persists_and_normal_submission_works(self):
        data = {
            "station_lat": 39.13, "station_lon": -97.70, "sun_alt_threshold_deg": 3.0,
            "nws_alert_zone": "KSC143", "nws_forecast_office": "TOP",
            "nws_forecast_grid_x": 10, "nws_forecast_grid_y": 53, "nws_cloud_stations": "KCNK,KSLN",
            "notify_email": "", "alert_sound_interval_minutes": "10",
            "alert_sound_enabled": "on", "alert_sound_cart": str(self.cart.pk),
            "_save": "Save",
        }
        for hour in range(24):
            data[f"voice_schedule_{hour}"] = "default"
        resp = self.client.post(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]), data, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.alert_sound_cart_id, self.cart.pk)


@override_settings(SECURE_SSL_REDIRECT=False)
class AdvancedFieldLayoutTests(TestCase):
    def setUp(self):
        self.super = User.objects.create_superuser("wxlayoutadmin", "z@example.invalid", "pw")
        self.client.force_login(self.super)
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)
        self.config = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])

    def _get(self):
        return self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))

    def test_routine_fields_visible(self):
        body = self._get().content.decode()
        self.assertIn('name="station_lat"', body)
        self.assertIn('name="station_lon"', body)
        self.assertIn('name="notify_email"', body)

    def test_advanced_fields_present_and_editable(self):
        body = self._get().content.decode()
        for field in ("sun_alt_threshold_deg", "nws_alert_zone", "nws_forecast_office",
                      "nws_forecast_grid_x", "nws_forecast_grid_y", "nws_cloud_stations"):
            self.assertIn(f'name="{field}"', body)

    def test_advanced_fieldset_is_collapsible(self):
        body = self._get().content.decode()
        self.assertIn("Advanced Weather Settings", body)
        self.assertIn("collapse", body)

    def test_underlying_seconds_field_never_rendered(self):
        body = self._get().content.decode()
        self.assertNotIn('name="alert_sound_interval_seconds"', body)


class NWSDiscoveryClientTests(TestCase):
    """weather/nws_discovery.py -- pure HTTP-parsing layer, mocked."""

    def _response(self, status=200, json_body=None, raise_json=False):
        class FakeResponse:
            def __init__(self):
                self.status_code = status
                self.ok = 200 <= status < 300
            def json(self):
                if raise_json:
                    raise ValueError("bad json")
                return json_body
        return FakeResponse()

    def _points_payload(self, grid_id="TOP", grid_x=10, grid_y=53,
                         county="https://api.weather.gov/zones/county/KSC143",
                         forecast_zone="https://api.weather.gov/zones/forecast/KSZ004"):
        return {"properties": {
            "gridId": grid_id, "gridX": grid_x, "gridY": grid_y,
            "county": county, "forecastZone": forecast_zone,
        }}

    def test_valid_points_response_parsed(self):
        session = type("S", (), {"get": lambda self, url, timeout: self.resp})()
        session.resp = self._response(200, self._points_payload())
        result = fetch_nws_point(39.13, -97.70, session=session)
        self.assertEqual(result["grid_id"], "TOP")
        self.assertEqual(result["grid_x"], 10)
        self.assertEqual(result["grid_y"], 53)
        self.assertEqual(result["county_ugc"], "KSC143")

    def test_county_ugc_and_forecast_zone_ugc_differ_and_county_is_used(self):
        session = type("S", (), {"get": lambda self, url, timeout: self.resp})()
        session.resp = self._response(200, self._points_payload(
            county="https://api.weather.gov/zones/county/KSC143",
            forecast_zone="https://api.weather.gov/zones/forecast/KSZ004",
        ))
        result = fetch_nws_point(39.13, -97.70, session=session)
        self.assertEqual(result["county_ugc"], "KSC143")
        self.assertEqual(result["forecast_zone_ugc"], "KSZ004")
        self.assertNotEqual(result["county_ugc"], result["forecast_zone_ugc"])

    def test_timeout_raises_discovery_error(self):
        class TimeoutSession:
            def get(self, url, timeout):
                raise requests.exceptions.Timeout("slow")
        with self.assertRaises(NWSDiscoveryError):
            fetch_nws_point(39.13, -97.70, session=TimeoutSession())

    def test_non_2xx_raises_discovery_error(self):
        session = type("S", (), {"get": lambda self, url, timeout: self.resp})()
        session.resp = self._response(500)
        with self.assertRaises(NWSDiscoveryError):
            fetch_nws_point(39.13, -97.70, session=session)

    def test_malformed_json_raises_discovery_error(self):
        session = type("S", (), {"get": lambda self, url, timeout: self.resp})()
        session.resp = self._response(200, raise_json=True)
        with self.assertRaises(NWSDiscoveryError):
            fetch_nws_point(39.13, -97.70, session=session)

    def test_incomplete_payload_raises_discovery_error(self):
        session = type("S", (), {"get": lambda self, url, timeout: self.resp})()
        session.resp = self._response(200, {"properties": {"gridId": "TOP"}})  # missing grid X/Y/county
        with self.assertRaises(NWSDiscoveryError):
            fetch_nws_point(39.13, -97.70, session=session)


@override_settings(SECURE_SSL_REDIRECT=False)
class NWSDiscoveryAdminViewTests(TestCase):
    def setUp(self):
        self.super = User.objects.create_superuser("wxnwsadmin", "n@example.invalid", "pw")
        self.client.force_login(self.super)
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)
        self.config = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]],
            nws_forecast_office="OLD", nws_forecast_grid_x=1, nws_forecast_grid_y=2, nws_alert_zone="KSC999",
        )
        self.discovery_url = reverse("admin:weather_weatherconfig_nws_discovery")

    def _counts(self):
        return (WeatherConfig.objects.get(pk=1).nws_forecast_office,
                WeatherConfig.objects.get(pk=1).nws_forecast_grid_x,
                WeatherConfig.objects.get(pk=1).nws_forecast_grid_y,
                WeatherConfig.objects.get(pk=1).nws_alert_zone)

    def test_get_discovery_page_makes_no_network_call(self):
        with patch("weather.nws_discovery.requests.Session.get") as get:
            resp = self.client.get(self.discovery_url)
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()

    def test_ordinary_weather_page_get_makes_zero_network_calls(self):
        with patch("weather.nws_discovery.requests.Session.get") as get:
            resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        self.assertEqual(resp.status_code, 200)
        get.assert_not_called()

    def test_discover_post_previews_without_mutating(self):
        before = self._counts()
        with patch("weather.admin.fetch_nws_point", return_value={
            "grid_id": "TOP", "grid_x": 10, "grid_y": 53, "county_ugc": "KSC143", "forecast_zone_ugc": "KSZ004",
        }):
            resp = self.client.post(self.discovery_url, {"action": "discover"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("KSC143", resp.content.decode())
        self.assertEqual(self._counts(), before)

    def test_apply_saves_intended_fields_only(self):
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "53", "county_ugc": "KSC143",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._counts(), ("TOP", 10, 53, "KSC143"))

    def test_timeout_causes_no_mutation(self):
        before = self._counts()
        with patch("weather.admin.fetch_nws_point", side_effect=NWSDiscoveryError("Timed out calling api.weather.gov.")):
            resp = self.client.post(self.discovery_url, {"action": "discover"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Timed out", resp.content.decode())
        self.assertEqual(self._counts(), before)

    def test_malformed_json_causes_no_mutation(self):
        before = self._counts()
        with patch("weather.admin.fetch_nws_point", side_effect=NWSDiscoveryError("api.weather.gov returned malformed JSON.")):
            resp = self.client.post(self.discovery_url, {"action": "discover"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._counts(), before)

    def test_incomplete_apply_payload_causes_no_mutation(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "", "grid_x": "", "grid_y": "", "county_ugc": "",
        })
        self.assertEqual(resp.status_code, 302)  # redirected back with an error message
        self.assertEqual(self._counts(), before)

    def test_manual_edit_remains_possible_after_discovery_preview(self):
        with patch("weather.admin.fetch_nws_point", return_value={
            "grid_id": "TOP", "grid_x": 10, "grid_y": 53, "county_ugc": "KSC143", "forecast_zone_ugc": "KSZ004",
        }):
            self.client.post(self.discovery_url, {"action": "discover"})
        # Operator ignores the discovered value and edits nws_alert_zone by hand instead.
        self.config.nws_alert_zone = "KSC777"
        self.config.save(update_fields=["nws_alert_zone"])
        self.config.refresh_from_db()
        self.assertEqual(self.config.nws_alert_zone, "KSC777")

    def test_forecast_zone_never_substituted_for_county_ugc_on_apply(self):
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "53", "county_ugc": "KSC143",
        }, follow=True)
        self.config.refresh_from_db()
        self.assertEqual(self.config.nws_alert_zone, "KSC143")

    # ---- Apply payload validation (r0053 review amendment) ----

    def test_overlong_grid_id_rejected_no_mutation_no_500(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "WAY_TOO_LONG_OFFICE_CODE", "grid_x": "10", "grid_y": "53",
            "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), before)

    def test_overlong_county_ugc_rejected_no_mutation_no_500(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "53",
            "county_ugc": "THIS_COUNTY_UGC_IS_DEFINITELY_WAY_TOO_LONG_TO_BE_REAL",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), before)

    def test_non_numeric_grid_values_rejected_no_mutation_no_500(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "not-a-number", "grid_y": "also-not-a-number",
            "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), before)

    def test_grid_x_zero_is_accepted(self):
        """r0053 final review correction: NWS's own /points schema
        declares gridX/gridY as 'integer, minimum 0' -- zero is a
        legitimate coordinate, not an invalid sentinel."""
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "0", "grid_y": "53", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), ("TOP", 0, 53, "KSC143"))

    def test_grid_y_zero_is_accepted(self):
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "0", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), ("TOP", 10, 0, "KSC143"))

    def test_both_grid_coordinates_zero_is_accepted(self):
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "0", "grid_y": "0", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), ("TOP", 0, 0, "KSC143"))

    def test_negative_grid_values_rejected(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "-5", "grid_y": "-9", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), before)

    def test_malformed_apply_shows_admin_error_and_preserves_previous_values(self):
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "", "grid_x": "abc", "grid_y": "-1", "county_ugc": "",
        }, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("failed validation", resp.content.decode())
        self.assertEqual(self._counts(), before)


@override_settings(SECURE_SSL_REDIRECT=False)
class NWSDiscoveryPermissionTests(TestCase):
    """r0053 review amendment: admin_site.admin_view() only proves 'is
    an authenticated staff user' -- it is not WeatherConfigAdmin's own
    has_change_permission(). A staff user with no model permissions at
    all must never be able to trigger a live NWS call or mutate
    WeatherConfig through this view."""

    def setUp(self):
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)
        self.config = WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]],
            nws_forecast_office="OLD", nws_forecast_grid_x=1, nws_forecast_grid_y=2, nws_alert_zone="KSC999",
        )
        self.discovery_url = reverse("admin:weather_weatherconfig_nws_discovery")

    def _counts(self):
        cfg = WeatherConfig.objects.get(pk=1)
        return (cfg.nws_forecast_office, cfg.nws_forecast_grid_x, cfg.nws_forecast_grid_y, cfg.nws_alert_zone)

    def _staff_with_perms(self, username, *codenames):
        from django.contrib.auth.models import Permission
        user = User.objects.create_user(username, f"{username}@example.invalid", "pw", is_staff=True)
        for codename in codenames:
            user.user_permissions.add(Permission.objects.get(content_type__app_label="weather", codename=codename))
        return user

    def test_staff_with_no_permissions_get_denied(self):
        staff = self._staff_with_perms("wxnoperm")
        self.client.force_login(staff)
        resp = self.client.get(self.discovery_url)
        self.assertEqual(resp.status_code, 403)

    def test_staff_with_view_only_can_get_page(self):
        staff = self._staff_with_perms("wxviewonly", "view_weatherconfig")
        self.client.force_login(staff)
        resp = self.client.get(self.discovery_url)
        self.assertEqual(resp.status_code, 200)

    def test_staff_with_view_only_discover_post_denied(self):
        staff = self._staff_with_perms("wxviewonly2", "view_weatherconfig")
        self.client.force_login(staff)
        with patch("weather.admin.fetch_nws_point") as fetch:
            resp = self.client.post(self.discovery_url, {"action": "discover"})
        self.assertEqual(resp.status_code, 403)
        fetch.assert_not_called()

    def test_staff_with_view_only_apply_denied_and_no_mutation(self):
        staff = self._staff_with_perms("wxviewonly3", "view_weatherconfig")
        self.client.force_login(staff)
        before = self._counts()
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "53", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._counts(), before)

    def test_staff_with_change_permission_can_apply(self):
        staff = self._staff_with_perms("wxchangeperm", "view_weatherconfig", "change_weatherconfig")
        self.client.force_login(staff)
        resp = self.client.post(self.discovery_url, {
            "action": "apply", "grid_id": "TOP", "grid_x": "10", "grid_y": "53", "county_ugc": "KSC143",
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._counts(), ("TOP", 10, 53, "KSC143"))
        self.assertNotEqual(self.config.nws_alert_zone, "KSZ004")


@override_settings(SECURE_SSL_REDIRECT=False)
class WeatherDataPathPresentationTests(TestCase):
    def setUp(self):
        self.super = User.objects.create_superuser("wxdatapathadmin", "p@example.invalid", "pw")
        self.client.force_login(self.super)
        self.voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=self.voice)
        self.config = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])

    def test_saved_path_displayed_and_edit_link_present(self):
        import tempfile
        from unittest.mock import patch as mockpatch
        from pathlib import Path as P
        from isadoraair import env_config
        with tempfile.TemporaryDirectory() as tmp:
            env_path = P(tmp) / ".env"
            env_path.write_text("WEATHER_DATA_DIR=/var/lib/isadoraair/weather\n")
            with mockpatch.object(env_config, "ENV_FILE_PATH", env_path):
                resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        body = resp.content.decode()
        self.assertIn("/var/lib/isadoraair/weather", body)
        self.assertIn(reverse("admin:weather_weatherconfig_weather_env"), body)

    def test_weather_env_link_source_does_not_reimplement_file_health(self):
        """The main page's weather_env_link must not itself stat/parse
        any Weather data file -- that stays inside weather.diagnostics,
        surfaced only via the Setup Status panel. A static source check
        (rather than a runtime mock) proves this directly regardless of
        whether the current saved path happens to exist on this box."""
        import inspect
        from weather.admin import WeatherConfigAdmin
        source = inspect.getsource(WeatherConfigAdmin.weather_env_link)
        self.assertNotIn("is_file", source)
        self.assertNotIn("json.load", source)
        self.assertNotIn("Track.objects", source)
