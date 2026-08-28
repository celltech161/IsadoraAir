"""Foundation E station requirement resolution tests."""

from django.test import TestCase

from aircheck.models import AircheckConfig
from encoders.models import Encoder
from isadoraair.runtime_requirements import (
    PiperModelRequirement,
    StationSelection,
    VoiceRequirement,
    inspect_station_selection,
    resolve_runtime_requirements,
)
from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice
from road_conditions.models import RoadConditionsConfiguration
from weather.models import WeatherConfig, WeatherVoicePersona
from webrequests.models import WebRequestConfig


HASH_A = "a" * 64
HASH_B = "b" * 64


def kokoro_voice(*, reasons=("feature",), enabled=True):
    return VoiceRequirement(
        logical_name="station-day",
        engine="kokoro",
        provider_voice="af_voice",
        language="en-us",
        speed=1.0,
        reasons=reasons,
    )


def piper_model():
    return PiperModelRequirement(
        model_id="station-piper",
        model_filename="station-piper.onnx",
        config_filename="station-piper.onnx.json",
        model_sha256=HASH_A,
        config_sha256=HASH_B,
        language="en-us",
        sample_rate_hz=22050,
    )


def piper_voice():
    model = piper_model()
    return VoiceRequirement(
        logical_name="station-night",
        engine="piper",
        provider_voice=model.model_id,
        language="en-us",
        speed=1.0,
        reasons=("feature",),
        piper_model=model,
    )


class PureRequirementResolverTests(TestCase):
    def test_no_tts_or_native_selection_is_optional(self):
        result = resolve_runtime_requirements(StationSelection())
        self.assertFalse(result.components["kokoro"].required)
        self.assertFalse(result.components["piper"].required)
        self.assertFalse(result.components["fdkaac"].required)

    def test_selected_kokoro_voice_requires_kokoro_only(self):
        result = resolve_runtime_requirements(StationSelection(voices=(kokoro_voice(),)))
        self.assertTrue(result.components["kokoro"].required)
        self.assertFalse(result.components["piper"].required)
        self.assertEqual(result.components["kokoro"].voices[0].provider_voice, "af_voice")

    def test_multiple_feature_reasons_and_voices_are_deterministic(self):
        second = VoiceRequirement(
            logical_name="station-backup",
            engine="kokoro",
            provider_voice="am_voice",
            language="en-us",
            speed=1.0,
            reasons=("road fixed voice", "dedication feature"),
        )
        first_selection = StationSelection(
            voices=(kokoro_voice(reasons=("scheduled weather persona 'day'",)), second)
        )
        second_selection = StationSelection(voices=tuple(reversed(first_selection.voices)))
        first = resolve_runtime_requirements(first_selection).components["kokoro"]
        second_result = resolve_runtime_requirements(second_selection).components["kokoro"]
        self.assertEqual(first, second_result)
        self.assertEqual(
            first.reasons,
            ("dedication feature", "road fixed voice", "scheduled weather persona 'day'"),
        )
        self.assertEqual(
            [voice.logical_name for voice in first.voices],
            ["station-backup", "station-day"],
        )

    def test_selected_piper_carries_exact_station_model_metadata(self):
        result = resolve_runtime_requirements(StationSelection(voices=(piper_voice(),)))
        self.assertTrue(result.components["piper"].required)
        self.assertEqual(result.components["piper"].piper_models, (piper_model(),))
        self.assertEqual(result.components["piper"].piper_models[0].model_sha256, HASH_A)

    def test_he_aac_and_he_aac_v2_select_fdkaac(self):
        for reason in ("encoder selects he_aac", "encoder selects he_aac_v2"):
            with self.subTest(reason=reason):
                result = resolve_runtime_requirements(StationSelection(fdkaac_reasons=(reason,)))
                self.assertTrue(result.components["fdkaac"].required)

    def test_inconsistent_selection_errors_are_preserved_fail_closed(self):
        result = resolve_runtime_requirements(
            StationSelection(errors=("selected logical voice is invalid",))
        )
        self.assertEqual(result.errors, ("selected logical voice is invalid",))


class StationInspectionTests(TestCase):
    def _voice(self, *, name="station-day", engine="kokoro", enabled=True, model=None):
        return StationTTSVoice.objects.create(
            name=name,
            enabled=enabled,
            engine=engine,
            provider_voice="af_voice" if engine == "kokoro" else "",
            piper_model=model,
            language="en-us",
            speed=1.0,
        )

    def _encoder(self, **overrides):
        values = {
            "name": "stream",
            "enabled": True,
            "protocol": "icecast",
            "host": "example.invalid",
            "port": 8000,
            "mount": "/stream",
            "username": "source",
            "password": "not-inspected",
            "format": "mp3",
            "bitrate_kbps": 128,
            "input_device": "other",
        }
        values.update(overrides)
        return Encoder.objects.create(**values)

    def test_inspection_does_not_create_missing_singleton_rows(self):
        inspect_station_selection()
        self.assertFalse(WeatherConfig.objects.exists())
        self.assertFalse(WebRequestConfig.objects.exists())
        self.assertFalse(RoadConditionsConfiguration.objects.exists())
        self.assertFalse(AircheckConfig.objects.exists())

    def test_inspection_does_not_add_update_or_delete_existing_rows(self):
        voice = self._voice()
        web = WebRequestConfig.objects.create(
            pk=1,
            enabled=True,
            dedication_tts_voice=voice,
            dedication_tts_timeout_seconds=37,
        )
        encoder = self._encoder(format="aac", bitrate_kbps=64)
        before_counts = (
            StationTTSVoice.objects.count(),
            WebRequestConfig.objects.count(),
            Encoder.objects.count(),
        )

        inspect_station_selection()

        voice.refresh_from_db()
        web.refresh_from_db()
        encoder.refresh_from_db()
        self.assertEqual(
            (
                StationTTSVoice.objects.count(),
                WebRequestConfig.objects.count(),
                Encoder.objects.count(),
            ),
            before_counts,
        )
        self.assertTrue(voice.enabled)
        self.assertEqual(web.dedication_tts_timeout_seconds, 37)
        self.assertEqual(encoder.bitrate_kbps, 64)

    def test_enabled_feature_reference_selects_enabled_kokoro(self):
        voice = self._voice()
        WebRequestConfig.objects.create(pk=1, enabled=True, dedication_tts_voice=voice)
        result = resolve_runtime_requirements(inspect_station_selection())
        self.assertTrue(result.components["kokoro"].required)
        self.assertIn("enabled web-request dedications", result.components["kokoro"].reasons)

    def test_malformed_weather_schedule_fails_closed(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["day", 0, 10]])
        selection = inspect_station_selection()
        self.assertIn("weather voice schedule must cover every hour exactly once", selection.errors)

    def test_enabled_road_weather_schedule_requires_every_persona_voice(self):
        voice = self._voice()
        WeatherConfig.objects.create(
            pk=1,
            voice_schedule=[["day", 0, 11], ["night", 12, 23]],
        )
        WeatherVoicePersona.objects.create(slot="day", tts_voice=voice)
        RoadConditionsConfiguration.objects.create(pk=1, enabled=True, tts_use_weather_schedule=True)
        selection = inspect_station_selection()
        self.assertIn("enabled road conditions weather TTS schedule is incomplete", selection.errors)

    def test_disabled_unreferenced_voice_does_not_select_runtime(self):
        self._voice(enabled=False)
        result = resolve_runtime_requirements(inspect_station_selection())
        self.assertFalse(result.components["kokoro"].required)
        self.assertEqual(result.errors, ())

    def test_disabled_selected_voice_fails_closed_and_keeps_engine_required(self):
        voice = self._voice(enabled=False)
        WebRequestConfig.objects.create(pk=1, enabled=True, dedication_tts_voice=voice)
        result = resolve_runtime_requirements(inspect_station_selection())
        self.assertTrue(result.components["kokoro"].required)
        self.assertIn("selected logical voice 'station-day' is disabled", result.errors)

    def test_selected_piper_model_metadata_comes_from_station_database(self):
        model = PiperVoiceModel.objects.create(
            model_id="station-piper",
            model_filename="station-piper.onnx",
            config_filename="station-piper.onnx.json",
            model_sha256=HASH_A,
            config_sha256=HASH_B,
            language="en-us",
            sample_rate_hz=22050,
        )
        voice = self._voice(name="station-night", engine="piper", model=model)
        WebRequestConfig.objects.create(pk=1, enabled=True, dedication_tts_voice=voice)
        result = resolve_runtime_requirements(inspect_station_selection())
        self.assertEqual(result.components["piper"].piper_models[0].to_dict(), piper_model().to_dict())

    def test_enabled_aac_encoder_selects_real_fdkaac_profile_boundaries(self):
        for bitrate, profile in (
            (64, "he_aac_v2"),
            (65, "he_aac"),
            (96, "he_aac"),
            (97, "aac_lc"),
            (192, "aac_lc"),
        ):
            with self.subTest(bitrate=bitrate):
                Encoder.objects.all().delete()
                self._encoder(bitrate_kbps=bitrate, format="aac")
                selection = inspect_station_selection()
                self.assertTrue(any(profile in reason for reason in selection.fdkaac_reasons))

    def test_disabled_aac_encoder_does_not_select_fdkaac(self):
        self._encoder(enabled=False, format="aac", bitrate_kbps=64)
        self.assertEqual(inspect_station_selection().fdkaac_reasons, ())

    def test_enabled_non_aac_encoder_does_not_select_fdkaac(self):
        self._encoder(enabled=True, format="mp3", bitrate_kbps=192)
        self.assertEqual(inspect_station_selection().fdkaac_reasons, ())

    def test_aircheck_requires_fdkaac_only_when_its_encoder_group_is_active(self):
        AircheckConfig.objects.create(pk=1, audio_format="he_aac")
        self._encoder(input_device="other")
        self.assertNotIn("active aircheck output selects he_aac", inspect_station_selection().fdkaac_reasons)
        Encoder.objects.all().delete()
        self._encoder(input_device="airtap")
        selection = inspect_station_selection()
        self.assertEqual(selection.fdkaac_reasons, ("active aircheck output selects he_aac",))
        self.assertTrue(resolve_runtime_requirements(selection).components["fdkaac"].required)
