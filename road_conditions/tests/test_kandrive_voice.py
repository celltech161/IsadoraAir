"""road_conditions/voice.py tests.

KanDrive resolves its day/night slot and voice identity ENTIRELY
through Django-owned configuration now -- WeatherConfig.voice_schedule
(via weather.voice_schedule.voice_for_hour(), a pure function) and
WeatherVoicePersona/StationTTSVoice (via
isadoraair.tts.station.resolve_station_voice()). This module no longer
imports the external weather-ingest project at all -- see
NoWeatherIngestImportDependencyTests below, which fails loudly if that
coupling is ever reintroduced.

No live Kokoro calls anywhere here; this module never invokes Kokoro
itself, only resolves which voice a caller should use."""
import sys
from datetime import datetime, timezone as dt_timezone

from django.test import TestCase
from django.utils import timezone as dj_timezone

from isadoraair.tts.models import StationTTSVoice
from road_conditions import voice as voice_module
from road_conditions.models import RoadConditionsConfiguration
from road_conditions.voice import VoiceResolutionError, available_slots, resolve_voice
from weather.models import WeatherConfig, WeatherVoicePersona
from weather.voice_schedule import voice_for_hour


class NoWeatherIngestImportDependencyTests(TestCase):
    """Codex's exact Part 1 requirement: road_conditions/voice.py must
    not import weather-ingest code. Proven two ways -- static source
    inspection (survives even if the external path happened to be
    importable in some environment) and confirming the external
    project's module name never appears in sys.modules after using
    every resolve_voice() path this test module exercises."""

    def test_source_contains_no_weather_ingest_filesystem_path(self):
        """Doc comments may legitimately mention "weather-ingest" by
        name to explain migration history -- what must never appear is
        an actual filesystem path into that external project."""
        import inspect
        source = inspect.getsource(voice_module)
        self.assertNotIn("/home/jreed/weather-ingest", source)

    def test_source_never_imports_by_file_path(self):
        import inspect
        source = inspect.getsource(voice_module)
        self.assertNotIn("importlib.util", source)
        self.assertNotIn("spec_from_file_location", source)

    def test_no_import_statement_references_weather_ingest(self):
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(voice_module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn("weather_ingest", alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertFalse(node.module and "weather_ingest" in node.module)

    def test_no_weather_ingest_module_loaded_after_full_resolution_cycle(self):
        before = set(sys.modules)
        RoadConditionsConfiguration.objects.all().delete()
        claira = StationTTSVoice.objects.create(
            name="Claira_Sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica", language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.create(slot="day", tts_voice=claira, display_name="Claira")
        resolve_voice(slot_override="day")  # legacy mode (default)
        config = RoadConditionsConfiguration.load()
        config.tts_use_weather_schedule = True
        config.save()
        resolve_voice(slot_override="day")  # shared mode
        after = set(sys.modules)
        new_modules = after - before
        self.assertFalse(
            any("weather_ingest" in name or "weather-ingest" in name for name in new_modules),
            f"unexpected weather-ingest-shaped module(s) loaded: {new_modules}",
        )


class ScheduleResolutionTests(TestCase):
    """weather.voice_schedule.voice_for_hour() -- pure, Django-owned,
    provider-free schedule resolution, and resolve_voice()'s use of it
    via _resolve_schedule_slot()."""

    def test_day_slot_resolution(self):
        self.assertEqual(voice_for_hour(10, [["day", 6, 17], ["night", 18, 5]]), "day")

    def test_night_slot_resolution(self):
        self.assertEqual(voice_for_hour(20, [["day", 6, 17], ["night", 18, 5]]), "night")

    def test_wrapped_midnight_range_resolution(self):
        schedule = [["day", 6, 17], ["night", 18, 5]]
        # The wrap: start(18) > end(5) means "hour >= 18 or hour <= 5".
        for hour in (18, 23, 0, 5):
            with self.subTest(hour=hour):
                self.assertEqual(voice_for_hour(hour, schedule), "night")
        for hour in (6, 12, 17):
            with self.subTest(hour=hour):
                self.assertEqual(voice_for_hour(hour, schedule), "day")

    def test_hour_covered_by_no_entry_defaults_to_day(self):
        self.assertEqual(voice_for_hour(10, [["night", 18, 5]]), "day")

    def test_schedule_resolution_matches_weather_config_for_the_hour_through_resolve_voice(self):
        """resolve_voice() determines which voice WEATHER would use for
        a given scheduled slot, from WeatherConfig.voice_schedule -- not
        a hardcoded schedule of its own."""
        RoadConditionsConfiguration.objects.all().delete()
        claira = StationTTSVoice.objects.create(
            name="Claira_Sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica", language="en-us", speed=1.0,
        )
        max_voice = StationTTSVoice.objects.create(
            name="Max_Weatherly", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="am_liam", language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.create(slot="day", tts_voice=claira, display_name="Claira")
        WeatherVoicePersona.objects.create(slot="night", tts_voice=max_voice, display_name="Max")

        config = WeatherConfig.load()
        config.voice_schedule = [["day", 6, 17], ["night", 18, 5]]
        config.save()

        noon = datetime(2026, 8, 4, 12, 0, tzinfo=dt_timezone.utc)
        midnight = datetime(2026, 8, 4, 23, 0, tzinfo=dt_timezone.utc)
        noon_local_hour = dj_timezone.localtime(noon).hour
        midnight_local_hour = dj_timezone.localtime(midnight).hour
        expected_noon_slot = "day" if 6 <= noon_local_hour <= 17 else "night"
        expected_midnight_slot = "day" if 6 <= midnight_local_hour <= 17 else "night"

        slot, _voice = resolve_voice(now=noon)
        self.assertEqual(slot, expected_noon_slot)
        slot, _voice = resolve_voice(now=midnight)
        self.assertEqual(slot, expected_midnight_slot)

    def test_unknown_slot_override_raises(self):
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(slot_override="afternoon")

    def test_available_slots(self):
        self.assertEqual(available_slots(), ["day", "night"])


# ---------------------------------------------------------------------
# Shared weather-schedule mode -- WeatherConfig -> WeatherVoicePersona
# -> StationTTSVoice -> canonical shared TTS (resolve_voice() only;
# actual synthesis routing is covered in test_kandrive_synthesis.py).
# ---------------------------------------------------------------------
class SharedScheduleVoiceResolutionTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.config.tts_use_weather_schedule = True
        self.config.tts_timeout_seconds = 45
        self.config.save()

        WeatherConfig.load()  # seeds the default day/night schedule via get_or_create

        self.claira = StationTTSVoice.objects.create(
            name="Claira_Sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica", language="en-us", speed=1.0,
        )
        self.max_voice = StationTTSVoice.objects.create(
            name="Max_Weatherly", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="am_liam", language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.create(
            slot="day", tts_voice=self.claira,
            display_name="Claira", full_name="Claira Sky", signoff="I'm Claira Sky.",
        )
        WeatherVoicePersona.objects.create(
            slot="night", tts_voice=self.max_voice,
            display_name="Max", full_name="Max Weatherly", signoff="I'm Max Weatherly.",
        )

    def test_day_slot_resolves_persona_and_only_logical_name_carried_for_synthesis(self):
        slot, voice = resolve_voice(slot_override="day")
        self.assertEqual(slot, "day")
        self.assertTrue(voice["shared_tts"])
        self.assertEqual(voice["logical_voice_name"], "Claira_Sky")
        self.assertEqual(voice["name"], "Claira")
        self.assertEqual(voice["full_name"], "Claira Sky")
        self.assertEqual(voice["signoff"], "I'm Claira Sky.")
        self.assertEqual(voice["engine"], "kokoro")
        self.assertEqual(voice["tts_timeout_seconds"], 45)
        # The resolved PROVIDER identity is carried for fingerprint
        # authenticity only -- never a technical id in listener-facing
        # fields, and the provider id itself never equals the logical name.
        self.assertEqual(voice["model"], "af_jessica")
        self.assertNotEqual(voice["logical_voice_name"], voice["model"])

    def test_night_slot_resolves_max_persona(self):
        slot, voice = resolve_voice(slot_override="night")
        self.assertEqual(slot, "night")
        self.assertEqual(voice["logical_voice_name"], "Max_Weatherly")
        self.assertEqual(voice["name"], "Max")
        self.assertEqual(voice["full_name"], "Max Weatherly")
        self.assertEqual(voice["model"], "am_liam")

    def test_schedule_resolution_selects_correct_slot_for_the_hour(self):
        wconfig = WeatherConfig.load()
        wconfig.voice_schedule = [["day", 6, 17], ["night", 18, 5]]
        wconfig.save()

        noon = datetime(2026, 8, 4, 18, 0, tzinfo=dt_timezone.utc)  # afternoon UTC
        noon_local_hour = dj_timezone.localtime(noon).hour
        expected_slot = "day" if 6 <= noon_local_hour <= 17 else "night"

        slot, voice = resolve_voice(now=noon)
        self.assertEqual(slot, expected_slot)
        self.assertEqual(voice["logical_voice_name"], "Claira_Sky" if expected_slot == "day" else "Max_Weatherly")

    def test_provider_mapping_change_changes_resolved_model_but_not_logical_name(self):
        """Repointing the SAME logical voice at a different provider
        identity must be visible in the resolved voice's "model"
        (fingerprint-relevant) field even though logical_voice_name is
        untouched."""
        slot, voice_before = resolve_voice(slot_override="day")
        self.assertEqual(voice_before["model"], "af_jessica")

        self.claira.provider_voice = "af_nicole"
        self.claira.save(update_fields=["provider_voice"])

        slot, voice_after = resolve_voice(slot_override="day")
        self.assertEqual(voice_after["logical_voice_name"], voice_before["logical_voice_name"])
        self.assertNotEqual(voice_after["model"], voice_before["model"])
        self.assertEqual(voice_after["model"], "af_nicole")

    def test_provider_mapping_change_changes_the_actual_report_fingerprint(self):
        """End-to-end version of the test above, through the real
        report.compute_report_fingerprint() -- proves the INTEGRATION,
        not just that resolve_voice() itself returns a different dict.
        report.py needed zero changes for this: it already fingerprints
        voice.get("model"), and resolve_voice()'s shared-mode branch
        populates "model" from the RESOLVED provider identity."""
        from road_conditions.report import compute_report_fingerprint

        _slot, voice_before = resolve_voice(slot_override="day")
        fp_before = compute_report_fingerprint("Same script text.", "day", voice_before, False)

        self.claira.provider_voice = "af_nicole"
        self.claira.save(update_fields=["provider_voice"])

        _slot, voice_after = resolve_voice(slot_override="day")
        fp_after = compute_report_fingerprint("Same script text.", "day", voice_after, False)

        self.assertEqual(voice_before["logical_voice_name"], voice_after["logical_voice_name"])
        self.assertNotEqual(fp_before, fp_after, "a provider remap for the same logical voice must invalidate the fingerprint")

    def test_missing_persona_row_fails_clearly(self):
        WeatherVoicePersona.objects.filter(slot="day").delete()
        with self.assertRaises(VoiceResolutionError) as ctx:
            resolve_voice(slot_override="day")
        self.assertIn("day", str(ctx.exception))

    def test_persona_with_no_tts_voice_selected_fails_clearly(self):
        WeatherVoicePersona.objects.filter(slot="day").update(tts_voice=None)
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(slot_override="day")

    def test_disabled_logical_voice_fails_clearly_through_shared_tts_error_semantics(self):
        self.claira.enabled = False
        self.claira.save(update_fields=["enabled"])
        with self.assertRaises(VoiceResolutionError) as ctx:
            resolve_voice(slot_override="day")
        self.assertIn("disabled", str(ctx.exception).lower())

    # No "unknown logical voice" case is tested here: WeatherVoicePersona.
    # tts_voice is on_delete=PROTECT specifically so a persona can never
    # be left pointing at a StationTTSVoice row that no longer exists --
    # resolve_station_voice()'s own "not configured" path is exercised
    # directly in isadoraair/tests/test_tts_station.py instead.

    def test_non_kokoro_persona_voice_rejected(self):
        from isadoraair.tts.models import PiperVoiceModel

        model = PiperVoiceModel.objects.create(
            model_id="test-piper", model_filename="test-piper.onnx",
            config_filename="test-piper.onnx.json",
            model_sha256="1" * 64, config_sha256="2" * 64,
            language="en-us", sample_rate_hz=22050,
        )
        piper_voice = StationTTSVoice.objects.create(
            name="Piper_Test", enabled=True, engine=StationTTSVoice.Engine.PIPER,
            provider_voice="", piper_model=model, language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.filter(slot="day").update(tts_voice=piper_voice)
        with self.assertRaises(VoiceResolutionError) as ctx:
            resolve_voice(slot_override="day")
        self.assertIn("kokoro", str(ctx.exception).lower())

    def test_blank_display_and_full_name_falls_back_to_slot_never_logical_id(self):
        WeatherVoicePersona.objects.filter(slot="day").update(display_name="", full_name="")
        slot, voice = resolve_voice(slot_override="day")
        self.assertEqual(voice["name"], "day")
        self.assertEqual(voice["full_name"], "day")
        self.assertNotIn("Claira_Sky", (voice["name"], voice["full_name"]))


# ---------------------------------------------------------------------
# Legacy (rollback-only) mode -- tts_use_weather_schedule=False, the
# model default. Historically resolved voice identity from weather-
# ingest's own external VOICES dict; now resolves through the SAME
# WeatherVoicePersona/StationTTSVoice chain shared mode uses (there is
# no more separate provider dictionary anywhere to source it from --
# see this module's own docstring), pulling the resolved provider_voice
# into voice["model"] for synthesis.py's direct-Kokoro fallback and
# never setting shared_tts=True.
# ---------------------------------------------------------------------
class LegacyModeVoiceResolutionTests(TestCase):
    def setUp(self):
        self.config = RoadConditionsConfiguration.load()
        self.assertFalse(self.config.tts_use_weather_schedule, "must default to legacy/off")

        WeatherConfig.load()

        self.claira = StationTTSVoice.objects.create(
            name="Claira_Sky", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="af_jessica", language="en-us", speed=1.0,
        )
        self.max_voice = StationTTSVoice.objects.create(
            name="Max_Weatherly", enabled=True, engine=StationTTSVoice.Engine.KOKORO,
            provider_voice="am_liam", language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.create(
            slot="day", tts_voice=self.claira,
            display_name="Claira", full_name="Claira Sky", signoff="I'm Claira Sky.",
        )
        WeatherVoicePersona.objects.create(
            slot="night", tts_voice=self.max_voice,
            display_name="Max", full_name="Max Weatherly", signoff="I'm Max Weatherly.",
        )

    def test_default_config_is_legacy(self):
        self.assertIsNone(self.config.tts_voice_id)

    def test_day_slot_identical_claira_identity_never_shared_tts(self):
        slot, voice = resolve_voice(slot_override="day")
        self.assertEqual(slot, "day")
        self.assertEqual(voice["engine"], "kokoro")
        self.assertEqual(voice["model"], "af_jessica")
        self.assertEqual(voice["name"], "Claira")
        self.assertEqual(voice["full_name"], "Claira Sky")
        self.assertEqual(voice["signoff"], "I'm Claira Sky.")
        self.assertNotIn("shared_tts", voice)
        self.assertNotIn("logical_voice_name", voice)

    def test_night_slot_identical_max_identity(self):
        slot, voice = resolve_voice(slot_override="night")
        self.assertEqual(slot, "night")
        self.assertEqual(voice["model"], "am_liam")
        self.assertEqual(voice["name"], "Max")
        self.assertEqual(voice["full_name"], "Max Weatherly")

    def test_explicit_legacy_config_row_resolves_legacy_voice(self):
        self.config.tts_use_weather_schedule = False
        self.config.save()

        slot, voice = resolve_voice(slot_override="night")
        self.assertEqual(voice["model"], "am_liam")
        self.assertFalse(voice.get("shared_tts"))

    def test_legacy_mode_fails_clearly_when_persona_missing(self):
        WeatherVoicePersona.objects.filter(slot="day").delete()
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(slot_override="day")

    def test_legacy_mode_rejects_non_kokoro_persona_voice(self):
        from isadoraair.tts.models import PiperVoiceModel

        model = PiperVoiceModel.objects.create(
            model_id="test-piper-legacy", model_filename="test-piper.onnx",
            config_filename="test-piper.onnx.json",
            model_sha256="3" * 64, config_sha256="4" * 64,
            language="en-us", sample_rate_hz=22050,
        )
        piper_voice = StationTTSVoice.objects.create(
            name="Piper_Legacy_Test", enabled=True, engine=StationTTSVoice.Engine.PIPER,
            provider_voice="", piper_model=model, language="en-us", speed=1.0,
        )
        WeatherVoicePersona.objects.filter(slot="day").update(tts_voice=piper_voice)
        with self.assertRaises(VoiceResolutionError) as ctx:
            resolve_voice(slot_override="day")
        self.assertIn("kokoro", str(ctx.exception).lower())
