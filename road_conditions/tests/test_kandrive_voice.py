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

from road_conditions import voice as voice_module
from road_conditions.voice import VoiceResolutionError, available_slots, resolve_voice
from weather.models import WeatherConfig


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
