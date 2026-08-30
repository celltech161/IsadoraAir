"""road_conditions/voice.py tests: KanDrive uses weather's OWN
VOICES dict and voice_for_hour() function (loaded by file path) plus
WeatherConfig.voice_schedule (read directly via the ORM) -- never a
road_conditions-local copy of either. No live Kokoro calls anywhere
here; this module never invokes Kokoro itself, only resolves which
voice a caller should use.

_cached_module is a module-level cache in road_conditions.voice --
reset it before/after every test so one test's load (real or faked)
can never leak into another's assertions about failure paths."""
import textwrap
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.test import TestCase, override_settings

from isadoraair.tts.models import StationTTSVoice
from road_conditions import voice as voice_module
from road_conditions.models import RoadConditionsConfiguration
from road_conditions.voice import VoiceResolutionError, available_slots, resolve_voice
from weather.models import WeatherConfig, WeatherVoicePersona


def _reset_cache():
    voice_module._cached_module = None


class RealWeatherVoiceModuleTests(TestCase):
    """Integration-style: exercises the ACTUAL weather-ingest/lib/voices.py
    on this box, confirming KanDrive really is reusing weather's own
    module object, not a copy. If weather-ingest ever changes its VOICES
    keys, these tests should fail loudly rather than silently drifting."""

    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_available_slots_matches_real_weather_voices(self):
        self.assertEqual(available_slots(), ["day", "night"])

    def test_slot_override_resolves_real_voice_identity(self):
        slot, voice = resolve_voice(slot_override="day")
        self.assertEqual(slot, "day")
        self.assertEqual(voice["engine"], "kokoro")
        self.assertEqual(voice["model"], "af_jessica")
        self.assertEqual(voice["name"], "Claira")

        slot, voice = resolve_voice(slot_override="night")
        self.assertEqual(slot, "night")
        self.assertEqual(voice["model"], "am_liam")
        self.assertEqual(voice["name"], "Max")

    def test_unknown_slot_override_raises(self):
        with self.assertRaises(VoiceResolutionError):
            resolve_voice(slot_override="afternoon")

    def test_schedule_resolution_matches_weather_config_for_the_hour(self):
        """The exact scenario Step 6 requires: KanDrive determines which
        voice WEATHER would use for a given scheduled slot, from
        WeatherConfig.voice_schedule -- not a hardcoded schedule of its
        own. Uses a schedule where day/night is unambiguous at the
        tested hours so this test doesn't depend on wall-clock time."""
        config = WeatherConfig.load()
        config.voice_schedule = [["day", 6, 17], ["night", 18, 5]]
        config.save()

        noon = datetime(2026, 8, 4, 12, 0, tzinfo=dt_timezone.utc)  # naive-local doesn't matter here,
        # localtime() is exercised for real -- see the separate localtime test below
        midnight = datetime(2026, 8, 4, 23, 0, tzinfo=dt_timezone.utc)

        # America/Chicago (this project's TIME_ZONE) is behind UTC, so
        # 12:00 UTC and 23:00 UTC land in different local hours than
        # their UTC clock time -- resolve via the same localtime() path
        # resolve_voice() itself uses, rather than asserting fixed UTC
        # hours, so this test can't silently drift from what the
        # production code actually does.
        from django.utils import timezone as dj_timezone
        noon_local_hour = dj_timezone.localtime(noon).hour
        midnight_local_hour = dj_timezone.localtime(midnight).hour
        expected_noon_slot = "day" if 6 <= noon_local_hour <= 17 else "night"
        expected_midnight_slot = "day" if 6 <= midnight_local_hour <= 17 else "night"

        slot, _voice = resolve_voice(now=noon)
        self.assertEqual(slot, expected_noon_slot)

        slot, _voice = resolve_voice(now=midnight)
        self.assertEqual(slot, expected_midnight_slot)

    def test_changing_weather_voice_schedule_changes_kandrive_resolution(self):
        """Directly demonstrates "no independent hard-coded voice
        rotation that can drift from the weather configuration" --
        editing ONLY WeatherConfig.voice_schedule changes what KanDrive
        resolves, with no road_conditions code or data touched."""
        config = WeatherConfig.load()
        fixed_time = datetime(2026, 8, 4, 15, 0, tzinfo=dt_timezone.utc)

        config.voice_schedule = [["day", 0, 23]]  # always day
        config.save()
        slot, _voice = resolve_voice(now=fixed_time)
        self.assertEqual(slot, "day")

        config.voice_schedule = [["night", 0, 23]]  # always night
        config.save()
        slot, _voice = resolve_voice(now=fixed_time)
        self.assertEqual(slot, "night")


class FakeWeatherVoiceModuleTests(TestCase):
    """Error-path/edge-case tests using a hand-built fake voices.py --
    doesn't touch or depend on the real weather-ingest file at all."""

    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def _write_fake_module(self, tmp_path, extra_voice_py=""):
        tmp_path.write_text(textwrap.dedent(f"""
            VOICES = {{
                "day": {{"engine": "kokoro", "model": "af_test", "name": "TestDay"}},
                "night": {{"engine": "kokoro", "model": "am_test", "name": "TestNight"}},
                {extra_voice_py}
            }}

            def voice_for_hour(hour, voice_schedule):
                for voice, start, end in voice_schedule:
                    if start <= end:
                        if start <= hour <= end:
                            return voice
                    else:
                        if hour >= start or hour <= end:
                            return voice
                return "day"
        """))

    def test_missing_weather_ingest_file_raises_clear_error(self):
        missing = Path("/nonexistent/definitely/not/here/voices.py")
        with self.settings():
            original = voice_module.WEATHER_INGEST_VOICES_PATH
            voice_module.WEATHER_INGEST_VOICES_PATH = missing
            try:
                with self.assertRaises(VoiceResolutionError) as ctx:
                    resolve_voice(slot_override="day")
                self.assertIn(str(missing), str(ctx.exception))
            finally:
                voice_module.WEATHER_INGEST_VOICES_PATH = original

    def test_non_kokoro_engine_raises_clear_error(self, tmp_path=None):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "voices.py"
            self._write_fake_module(
                fake_path,
                extra_voice_py='"afternoon": {"engine": "piper", "model": "/some/path.onnx", "name": "TestPiper"},',
            )
            original = voice_module.WEATHER_INGEST_VOICES_PATH
            voice_module.WEATHER_INGEST_VOICES_PATH = fake_path
            try:
                with self.assertRaises(VoiceResolutionError) as ctx:
                    resolve_voice(slot_override="afternoon")
                self.assertIn("kokoro", str(ctx.exception))
            finally:
                voice_module.WEATHER_INGEST_VOICES_PATH = original

    def test_available_slots_reflects_fake_module(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "voices.py"
            self._write_fake_module(fake_path)
            original = voice_module.WEATHER_INGEST_VOICES_PATH
            voice_module.WEATHER_INGEST_VOICES_PATH = fake_path
            try:
                self.assertEqual(available_slots(), ["day", "night"])
                slot, voice = resolve_voice(slot_override="day")
                self.assertEqual(voice["model"], "af_test")
            finally:
                voice_module.WEATHER_INGEST_VOICES_PATH = original


# ---------------------------------------------------------------------
# Shared weather-schedule mode -- WeatherConfig -> WeatherVoicePersona
# -> StationTTSVoice -> canonical shared TTS (resolve_voice() only;
# actual synthesis routing is covered in test_kandrive_synthesis.py).
# ---------------------------------------------------------------------
class SharedScheduleVoiceResolutionTests(TestCase):
    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)
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

        from django.utils import timezone as dj_timezone
        noon = datetime(2026, 8, 4, 18, 0, tzinfo=dt_timezone.utc)  # afternoon UTC
        noon_local_hour = dj_timezone.localtime(noon).hour
        expected_slot = "day" if 6 <= noon_local_hour <= 17 else "night"

        slot, voice = resolve_voice(now=noon)
        self.assertEqual(slot, expected_slot)
        self.assertEqual(voice["logical_voice_name"], "Claira_Sky" if expected_slot == "day" else "Max_Weatherly")

    def test_provider_mapping_change_changes_resolved_model_but_not_logical_name(self):
        """The exact scenario the task requires: repointing the SAME
        logical voice at a different provider identity must be visible
        in the resolved voice's "model" (fingerprint-relevant) field
        even though logical_voice_name is untouched."""
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


class LegacyModeRemainsDefaultTests(TestCase):
    """tts_use_weather_schedule=False (the model default -- and KOGR's
    real production state) must resolve exactly as before, with zero
    Weather Voice Persona/StationTTSVoice involvement."""

    def setUp(self):
        _reset_cache()
        self.addCleanup(_reset_cache)

    def test_default_config_is_legacy_and_untouched_config_row_stays_off(self):
        config = RoadConditionsConfiguration.load()
        self.assertFalse(config.tts_use_weather_schedule)
        self.assertIsNone(config.tts_voice_id)

        slot, voice = resolve_voice(slot_override="day")
        self.assertEqual(slot, "day")
        self.assertEqual(voice["model"], "af_jessica")
        self.assertNotIn("shared_tts", voice)
        self.assertNotIn("logical_voice_name", voice)

    def test_explicit_legacy_config_row_resolves_legacy_voice(self):
        config = RoadConditionsConfiguration.load()
        config.tts_use_weather_schedule = False
        config.save()

        slot, voice = resolve_voice(slot_override="night")
        self.assertEqual(voice["model"], "am_liam")
        self.assertFalse(voice.get("shared_tts"))
