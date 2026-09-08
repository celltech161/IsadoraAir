"""Regression coverage for the 2026-08 severe-weather alert beep
migration off direct-to-ALSA FFmpeg playback and onto IsadoraAir's own
FX Cart architecture (see WeatherConfig.alert_sound_cart,
weather/management/commands/dump_weather_config.py, and
library/management/commands/fire_fx_cart.py -- covered by its own
test_fire_fx_cart.py under library/tests/)."""
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings

from library.models import FXCart
from isadoraair.tts.models import StationTTSVoice
from weather.models import WeatherConfig, WeatherVoicePersona


class WeatherConfigCleanInstallTests(TestCase):
    def test_empty_database_gets_neutral_default_persona_and_schedule(self):
        cfg = WeatherConfig.load()

        self.assertEqual(cfg.voice_schedule, [["default", 0, 23]])
        persona = WeatherVoicePersona.objects.get(slot="default")
        self.assertEqual(persona.display_name, "Default announcer")
        self.assertEqual(persona.full_name, "")
        self.assertEqual(persona.signoff, "")
        self.assertIsNone(persona.tts_voice_id)
        self.assertFalse(StationTTSVoice.objects.exists())

    def test_load_is_idempotent_and_does_not_repair_existing_default(self):
        first = WeatherConfig.load()
        persona = WeatherVoicePersona.objects.get(slot="default")
        persona.display_name = "Operator label"
        persona.save(update_fields=["display_name"])

        second = WeatherConfig.load()

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WeatherConfig.objects.count(), 1)
        self.assertEqual(WeatherVoicePersona.objects.count(), 1)
        persona.refresh_from_db()
        self.assertEqual(persona.display_name, "Operator label")

    def test_existing_legacy_schedule_is_unchanged(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        WeatherConfig.objects.create(pk=1, voice_schedule=schedule)

        cfg = WeatherConfig.load()

        self.assertEqual(cfg.voice_schedule, schedule)
        self.assertFalse(WeatherVoicePersona.objects.filter(slot="default").exists())

    def test_existing_arbitrary_personas_and_schedule_are_unchanged(self):
        schedule = [["morning_host", 5, 11], ["evening_host", 12, 4]]
        WeatherConfig.objects.create(pk=1, voice_schedule=schedule)
        morning = WeatherVoicePersona.objects.create(
            slot="morning_host", display_name="Morgan", signoff="Morgan here.",
        )

        cfg = WeatherConfig.load()

        self.assertEqual(cfg.voice_schedule, schedule)
        morning.refresh_from_db()
        self.assertEqual(morning.display_name, "Morgan")

class WeatherConfigAlertSoundCartTests(TestCase):
    def test_accepts_fx_cart_selection(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        cfg = WeatherConfig.load()
        cfg.alert_sound_cart = cart
        cfg.save()

        cfg.refresh_from_db()
        self.assertEqual(cfg.alert_sound_cart_id, cart.id)

    def test_alert_sound_cart_may_be_null(self):
        cfg = WeatherConfig.load()
        # Never auto-populated from any prior config -- a fresh/migrated
        # singleton simply has no cart selected until an operator picks one.
        self.assertIsNone(cfg.alert_sound_cart_id)
        cfg.save()  # null FK must save cleanly, not just default-construct that way
        cfg.refresh_from_db()
        self.assertIsNone(cfg.alert_sound_cart_id)

    def test_deleting_selected_fx_cart_sets_weatherconfig_fk_to_null(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        cfg = WeatherConfig.load()
        cfg.alert_sound_cart = cart
        cfg.save()

        cart.delete()

        cfg.refresh_from_db()
        self.assertIsNone(cfg.alert_sound_cart_id)
        # WeatherConfig itself survives the cart's deletion intact.
        self.assertTrue(WeatherConfig.objects.filter(pk=cfg.pk).exists())


class DumpWeatherConfigCommandTests(TestCase):
    def _dump(self):
        out = StringIO()
        call_command("dump_weather_config", stdout=out)
        return json.loads(out.getvalue())

    def test_output_contains_alert_beep_fields(self):
        payload = self._dump()
        self.assertIn("alert_sound_enabled", payload)
        self.assertIn("alert_sound_interval_seconds", payload)
        self.assertIn("alert_sound_cart_id", payload)

    @override_settings(WEATHER_DATA_DIR="/srv/station/weather")
    def test_output_relays_configured_weather_data_dir(self):
        self.assertEqual(self._dump()["weather_data_dir"], "/srv/station/weather")

    @override_settings(WEATHER_DATA_DIR="/var/lib/isadoraair/weather")
    def test_output_relays_canonical_weather_data_dir(self):
        self.assertEqual(
            self._dump()["weather_data_dir"], "/var/lib/isadoraair/weather"
        )

    def test_output_no_longer_exposes_playback_specific_fields(self):
        payload = self._dump()
        self.assertNotIn("alert_sound_path", payload)
        self.assertNotIn("alert_sound_device", payload)
        self.assertNotIn("alert_sound_gain_db", payload)

    def test_no_selected_cart_serializes_as_json_null(self):
        payload = self._dump()
        self.assertIsNone(payload["alert_sound_cart_id"])

    def test_selected_cart_serializes_as_its_id_not_null(self):
        cart = FXCart.objects.create(name="Severe Wx Beep", filepath="/tmp/beep.wav")
        cfg = WeatherConfig.load()
        cfg.alert_sound_cart = cart
        cfg.save()

        payload = self._dump()
        self.assertEqual(payload["alert_sound_cart_id"], cart.id)

    def test_does_not_expose_fx_cart_filepath_or_gain(self):
        """WeatherConfig only ever hands out the cart's id -- filepath/
        gain/retrigger belong to FXCart itself, per the architectural
        boundary this migration establishes."""
        cart = FXCart.objects.create(
            name="Severe Wx Beep", filepath="/srv/isadoraair/carts/wx_beep.wav", gain_db=-6.0,
        )
        cfg = WeatherConfig.load()
        cfg.alert_sound_cart = cart
        cfg.save()

        payload = self._dump()
        self.assertNotIn(cart.filepath, json.dumps(payload))
        self.assertNotIn("filepath", payload)
        self.assertNotIn("gain_db", payload)
