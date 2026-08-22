"""Station logical voice, feature-boundary, and migration-safety tests."""

import io
import json
from dataclasses import fields
from importlib import import_module
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import migrations
from django.test import TestCase

from isadoraair.tts import SynthesisRequest, TTSEngine, TTSVoiceUnavailable
from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice
from isadoraair.tts.station import StationTTSService, resolve_station_voice
from road_conditions.models import RoadConditionsConfiguration
from weather.models import WeatherConfig, WeatherVoicePersona
from webrequests.models import WebRequestConfig


class _RecordingResolvedService:
    def __init__(self):
        self.requests = []

    def synthesize(self, request):
        self.requests.append(request)
        return request.output_path


class StationVoiceResolutionTests(TestCase):
    def _piper_model(self):
        return PiperVoiceModel.objects.create(
            model_id="test-piper",
            model_filename="test-piper.onnx",
            config_filename="test-piper.onnx.json",
            model_sha256="1" * 64,
            config_sha256="2" * 64,
            language="en_US",
            sample_rate_hz=22050,
        )

    def test_no_station_specific_universal_defaults(self):
        self.assertEqual(StationTTSVoice.objects.count(), 0)
        self.assertEqual(PiperVoiceModel.objects.count(), 0)
        self.assertEqual(WeatherVoicePersona.objects.count(), 0)

    def test_logical_kokoro_voice_resolution(self):
        StationTTSVoice.objects.create(
            name="weather-day", enabled=True, engine="kokoro",
            provider_voice="provider-voice", language="en-us", speed=1.1,
        )
        resolved = resolve_station_voice("weather-day")
        self.assertEqual(resolved.engine, TTSEngine.KOKORO)
        self.assertEqual(resolved.provider_voice, "provider-voice")
        self.assertEqual(resolved.language, "en-us")
        self.assertEqual(resolved.speed, 1.1)
        self.assertIsNone(resolved.piper_spec)

    def test_logical_piper_voice_resolution(self):
        model = self._piper_model()
        StationTTSVoice.objects.create(
            name="weather-night", enabled=True, engine="piper",
            piper_model=model, language="en-us", speed=0.9,
        )
        resolved = resolve_station_voice("weather-night")
        self.assertEqual(resolved.engine, TTSEngine.PIPER)
        self.assertEqual(resolved.provider_voice, "test-piper")
        self.assertEqual(resolved.piper_spec.model_filename, "test-piper.onnx")
        self.assertEqual(resolved.piper_spec.sample_rate_hz, 22050)

    def test_missing_logical_voice(self):
        with self.assertRaisesRegex(TTSVoiceUnavailable, "not configured"):
            resolve_station_voice("absent")

    def test_disabled_logical_voice(self):
        StationTTSVoice.objects.create(
            name="disabled", enabled=False, engine="kokoro", provider_voice="native"
        )
        with self.assertRaisesRegex(TTSVoiceUnavailable, "disabled"):
            resolve_station_voice("disabled")

    def test_invalid_engine_is_rejected(self):
        voice = StationTTSVoice(
            name="invalid", enabled=True, engine="unknown", provider_voice="native"
        )
        with self.assertRaises(ValidationError):
            voice.full_clean()

    def test_high_level_service_resolves_defaults_and_hides_engine_from_caller(self):
        StationTTSVoice.objects.create(
            name="dedication", enabled=True, engine="kokoro",
            provider_voice="native", language="en-gb", speed=1.25,
        )
        low_level = _RecordingResolvedService()
        result = StationTTSService().synthesize(
            "Thank you",
            voice="dedication",
            output_path="/tmp/station-voice-test.wav",
            timeout_seconds=30,
            service=low_level,
        )
        request = low_level.requests[0]
        self.assertEqual(result, Path("/tmp/station-voice-test.wav"))
        self.assertEqual(request.engine, TTSEngine.KOKORO)
        self.assertEqual(request.voice, "native")
        self.assertEqual(request.speed, 1.25)
        self.assertEqual(request.language, "en-gb")
        self.assertEqual(request.timeout_seconds, 30)

    def test_callers_and_registry_cannot_supply_runtime_paths(self):
        self.assertEqual(
            {field.name for field in fields(SynthesisRequest)},
            {"text", "engine", "voice", "output_path", "speed", "language", "timeout_seconds"},
        )
        registry_fields = {field.name for field in StationTTSVoice._meta.fields}
        model_fields = {field.name for field in PiperVoiceModel._meta.fields}
        for forbidden in ("runtime_path", "executable", "model_path", "config_path", "venv"):
            self.assertNotIn(forbidden, registry_fields | model_fields)

    def test_persona_remains_outside_tts_resolution(self):
        voice = StationTTSVoice.objects.create(
            name="weather-day", enabled=True, engine="kokoro", provider_voice="native"
        )
        WeatherVoicePersona.objects.create(
            slot="day", tts_voice=voice, display_name="Presenter",
            full_name="Presenter Full", signoff="A feature-owned signoff",
        )
        resolved = resolve_station_voice("weather-day")
        self.assertEqual(
            {field.name for field in fields(resolved)},
            {"logical_name", "engine", "provider_voice", "language", "speed", "piper_spec"},
        )
        self.assertNotIn("Presenter", repr(resolved))


class StationVoiceModelValidationTests(TestCase):
    def test_piper_model_requires_plain_paired_filenames_and_valid_hashes(self):
        for model in (
            PiperVoiceModel(
                model_id="path", model_filename="../voice.onnx",
                config_filename="voice.onnx.json", model_sha256="1" * 64,
                config_sha256="2" * 64, language="en-us", sample_rate_hz=22050,
            ),
            PiperVoiceModel(
                model_id="pair", model_filename="voice.onnx",
                config_filename="other.onnx.json", model_sha256="1" * 64,
                config_sha256="2" * 64, language="en-us", sample_rate_hz=22050,
            ),
            PiperVoiceModel(
                model_id="hash", model_filename="voice.onnx",
                config_filename="voice.onnx.json", model_sha256="not-a-hash",
                config_sha256="2" * 64, language="en-us", sample_rate_hz=22050,
            ),
        ):
            with self.subTest(model=model.model_id):
                with self.assertRaises(ValidationError):
                    model.full_clean()

    def test_engine_specific_model_pairing(self):
        model = PiperVoiceModel.objects.create(
            model_id="piper", model_filename="piper.onnx", config_filename="piper.onnx.json",
            model_sha256="1" * 64, config_sha256="2" * 64,
            language="en-us", sample_rate_hz=22050,
        )
        invalid = (
            StationTTSVoice(name="k1", engine="kokoro"),
            StationTTSVoice(name="k2", engine="kokoro", provider_voice="native", piper_model=model),
            StationTTSVoice(name="p1", engine="piper"),
            StationTTSVoice(name="p2", engine="piper", provider_voice="native", piper_model=model),
            StationTTSVoice(name="p3", engine="piper", piper_model=model, language="en-gb"),
            StationTTSVoice(name="speed", engine="kokoro", provider_voice="native", speed=0),
        )
        for voice in invalid:
            with self.subTest(voice=voice.name):
                with self.assertRaises(ValidationError):
                    voice.full_clean()


class FeatureMigrationPreparationTests(TestCase):
    def test_existing_feature_configs_are_safe_and_unconfigured(self):
        web = WebRequestConfig.load()
        road = RoadConditionsConfiguration.load()
        self.assertIsNone(web.dedication_tts_voice)
        self.assertEqual(web.dedication_tts_timeout_seconds, 30)
        self.assertIsNone(road.tts_voice)
        self.assertFalse(road.tts_use_weather_schedule)
        self.assertEqual(road.tts_timeout_seconds, 600)

    def test_weather_dump_exposes_persona_to_logical_mapping_without_provider_paths(self):
        WeatherConfig.load()
        voice = StationTTSVoice.objects.create(
            name="logical-day", enabled=True, engine="kokoro", provider_voice="native"
        )
        WeatherVoicePersona.objects.create(
            slot="day", tts_voice=voice, display_name="Day", full_name="Day Person", signoff="Bye",
        )
        output = io.StringIO()
        call_command("dump_weather_config", stdout=output)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["voice_personas"]["day"]["logical_voice"], "logical-day")
        self.assertNotIn("engine", payload["voice_personas"]["day"])
        self.assertNotIn("model", payload["voice_personas"]["day"])
        self.assertNotIn("path", json.dumps(payload["voice_personas"]))

    def test_schema_migrations_seed_no_rows_and_access_no_assets(self):
        modules = (
            "isadoraair.tts.migrations.0001_initial",
            "weather.migrations.0007_weathervoicepersona",
            "webrequests.migrations.0008_webrequestconfig_dedication_tts",
            "road_conditions.migrations.0010_roadconditionsconfiguration_tts",
        )
        source = ""
        for module_name in modules:
            module = import_module(module_name)
            self.assertFalse(
                any(isinstance(operation, migrations.RunPython) for operation in module.Migration.operations)
            )
            source += Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("/opt/", "/var/lib/", "/home/", "RunPython", "open("):
            self.assertNotIn(forbidden, source)
