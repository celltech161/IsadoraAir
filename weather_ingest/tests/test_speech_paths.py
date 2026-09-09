"""Integration-level tests for the four speech-producing entry points
(current_temp.py, wx_forecast.py, wx_alert.py, amber_alert.py):
filenames/categories/postprocessing arguments unchanged, forecast
--voice auto works, and the Part 5 atomicity fix (one failed alert
clip prevents the whole publication, a successful set still
publishes). All subprocess/delivery/notification calls are mocked --
no live TTS, ffmpeg, or cross-venv Django calls anywhere here.

All four entry points resolve WEATHER_DATA_DIR at import time, and
wx_forecast.py also loads WeatherConfig there. Both shared helpers are
patched before import so test collection never makes a real cross-venv
Django call or touches production weather storage."""
import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

FAKE_CFG = {
    "station_lat": 39.13,
    "station_lon": -97.70,
    "sun_alt_threshold_deg": 3.0,
    "nws_alert_zone": "KSC143",
    "nws_forecast_office": "TOP",
    "nws_forecast_grid_x": 10,
    "nws_forecast_grid_y": 53,
    "nws_cloud_stations": ["KCNK", "KSLN"],
    "voice_schedule": [["day", 6, 17], ["night", 18, 5]],
    "voice_personas": {
        "day": {"logical_voice": "Claira_Sky", "display_name": "Claira",
                "full_name": "Claira Sky", "signoff": "I'm Claira Sky."},
        "night": {"logical_voice": "Max_Weatherly", "display_name": "Max",
                  "full_name": "Max Weatherly", "signoff": "I'm Max Weatherly."},
    },
    "notify_email": "",
    "alert_sound_enabled": True,
    "alert_sound_cart_id": None,
    "alert_sound_interval_seconds": 600,
}

# P1 2.4 Pass G: build_announcement() now returns (text, forecast_meta)
# instead of a bare string -- stand-in metadata for tests below that
# mock build_announcement() wholesale and only care about the text.
FAKE_FORECAST_META = {"source_kind": "live_nws", "source_age_seconds": 0.0, "used_fallback": False}


def _import_entry_points():
    """Import entry points without crossing into production Django."""
    import wxconfig
    with patch.object(wxconfig, "load_weather_config", return_value=FAKE_CFG), \
         patch.object(
             wxconfig,
             "resolve_weather_data_dir",
             return_value=PROJECT_ROOT / ".nonexistent-test-weather-data",
         ):
        return tuple(
            importlib.import_module(name)
            for name in ("current_temp", "wx_alert", "amber_alert", "wx_forecast")
        )


current_temp, wx_alert, amber_alert, wx_forecast = _import_entry_points()


class FilenameCategoryMetadataUnchangedTests(unittest.TestCase):
    """Proves the migration touched ONLY voice routing -- every
    filename/category/output-directory identity this project has
    always used is untouched."""

    def test_current_temp_category_and_filename(self):
        self.assertEqual(current_temp.CATEGORY_CODE, "WxTemp")
        self.assertEqual(current_temp.DEST_FILENAME, "current_temp.mp3")
        self.assertEqual(current_temp.ID3_ARTIST, "Oak Grove Radio")

    def test_wx_alert_category_and_filename(self):
        self.assertEqual(wx_alert.CATEGORY_CODE, "WxAlert")
        self.assertEqual(wx_alert.DEST_FILENAME, "wx_alert.mp3")

    def test_amber_alert_reuses_wxalert_category_and_filename(self):
        self.assertEqual(amber_alert.CATEGORY_CODE, "WxAlert")
        self.assertEqual(amber_alert.DEST_FILENAME, "wx_alert.mp3")

    def test_forecast_modes_category_and_filenames(self):
        self.assertEqual(wx_forecast.MODES["1day"]["category_code"], "WxObs")
        self.assertEqual(wx_forecast.MODES["1day"]["dest_filename"], "current_obs.mp3")
        self.assertEqual(wx_forecast.MODES["3day"]["category_code"], "WxForecast")
        self.assertEqual(wx_forecast.MODES["3day"]["dest_filename"], "forecast.mp3")


class PostprocessingArgumentsUnchangedTests(unittest.TestCase):
    """The ffmpeg invocation itself (1s leading silence, 44.1kHz stereo
    320kbps MP3) is untouched by this migration -- verified directly
    against the real argv each conversion function builds."""

    def test_current_temp_ffmpeg_args(self):
        with patch.object(current_temp.os.path, "exists", return_value=True), \
             patch.object(current_temp, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock()
            current_temp.convert_to_mp3("/tmp/in.wav", "/tmp/out.mp3", "Title")
        argv = mock_subprocess.run.call_args.args[0]
        self.assertIn("adelay=1000:all=1", argv)
        self.assertIn("44100", argv)
        self.assertIn("320k", argv)
        self.assertIn("libmp3lame", argv)

    def test_wx_alert_concat_ffmpeg_args(self):
        with patch.object(wx_alert.os, "makedirs"), \
             patch.object(wx_alert, "open", create=True), \
             patch.object(wx_alert.os, "remove"), \
             patch.object(wx_alert, "subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = MagicMock()
            wx_alert.concat_clips(["/tmp/a.wav", "/tmp/b.wav"], "/tmp/out.mp3")
        argv = mock_subprocess.run.call_args.args[0]
        self.assertIn("adelay=1000:all=1", argv)
        self.assertIn("320k", argv)


class ForecastVoiceAutoTests(unittest.TestCase):
    """--voice auto must work correctly after the unit-template change
    (Part 2/Part 4's explicit requirement)."""

    def test_voice_argument_accepts_auto(self):
        parser_choices = None
        with patch.object(sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", "auto"]):
            args = wx_forecast.parse_args()
        self.assertEqual(args.voice, "auto")

    def test_explicit_day_and_night_remain_supported(self):
        for voice in ("day", "night"):
            with self.subTest(voice=voice):
                with patch.object(sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", voice]):
                    args = wx_forecast.parse_args()
                self.assertEqual(args.voice, voice)

    def test_forecast_accepts_arbitrary_persona_slot(self):
        with patch.object(
            sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", "morning_host"],
        ):
            args = wx_forecast.parse_args()
        self.assertEqual(args.voice, "morning_host")

    def test_current_temperature_accepts_arbitrary_persona_slot_and_auto(self):
        for voice in ("morning_host", "auto"):
            with self.subTest(voice=voice):
                with patch.object(
                    sys, "argv", ["current_temp.py", "--voice", voice],
                ):
                    args = current_temp.parse_args()
                self.assertEqual(args.voice, voice)

    def test_main_with_auto_resolves_and_synthesizes_with_resolved_voice(self):
        with patch.object(sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", "auto"]), \
             patch.object(wx_forecast, "build_announcement", return_value=("Test announcement.", FAKE_FORECAST_META)), \
             patch.object(wx_forecast, "generate_wav_with_piper", return_value=voices_ok()) as mock_gen, \
             patch.object(wx_forecast, "convert_to_mp3", return_value=True), \
             patch.object(wx_forecast, "deliver", return_value="/srv/dest.mp3"), \
             patch.object(wx_forecast.shutil, "rmtree"), \
             patch.object(wx_forecast.os, "makedirs"):
            ok = wx_forecast.main()
        self.assertTrue(ok)
        mock_gen.assert_called_once()
        # Second positional arg after (announcement, output_wav) is the
        # resolved voice dict -- must carry a logical_voice, proving
        # auto really resolved through resolve_voice(), not a static dict.
        called_voice = mock_gen.call_args.args[2]
        self.assertIn("logical_voice", called_voice)

    def test_main_with_explicit_day_and_night_resolves_and_synthesizes(self):
        """Production's 4 deployed forecast systemd units each pass an
        explicit --voice day/--voice night (unchanged by this migration
        -- see the corrective-review report), so this is the actual
        production-critical path, not auto. Mirrors the auto test
        above end-to-end through main(): resolve_voice -> generate_wav
        -> convert_to_mp3 -> deliver, for both explicit slots."""
        expected_logical = {"day": "Claira_Sky", "night": "Max_Weatherly"}
        for voice_arg in ("day", "night"):
            with self.subTest(voice=voice_arg):
                with patch.object(sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", voice_arg]), \
                     patch.object(wx_forecast, "build_announcement", return_value=("Test announcement.", FAKE_FORECAST_META)), \
                     patch.object(wx_forecast, "generate_wav_with_piper", return_value=voices_ok()) as mock_gen, \
                     patch.object(wx_forecast, "convert_to_mp3", return_value=True), \
                     patch.object(wx_forecast, "deliver", return_value="/srv/dest.mp3"), \
                     patch.object(wx_forecast.shutil, "rmtree"), \
                     patch.object(wx_forecast.os, "makedirs"):
                    ok = wx_forecast.main()
                self.assertTrue(ok)
                mock_gen.assert_called_once()
                called_voice = mock_gen.call_args.args[2]
                self.assertEqual(called_voice["logical_voice"], expected_logical[voice_arg])
                self.assertEqual(called_voice["slot"], voice_arg)


def voices_ok():
    import voices
    return voices.SynthesisResult(True)


def voices_fail(reason="nonzero_exit"):
    import voices
    return voices.SynthesisResult(False, reason)


class WxAlertAtomicityTests(unittest.TestCase):
    """Part 5: all-or-nothing publication. One failed segment must
    prevent concat/delivery/insert_urgent entirely; a fully-successful
    set must still publish normally."""

    def _entries(self, n):
        return [{"event": f"EVT{i}", "text": f"Alert text {i}"} for i in range(n)]

    def test_one_failed_clip_prevents_complete_publication(self):
        with patch.object(wx_alert, "load_watchwarn_entries", return_value=self._entries(3)), \
             patch.object(wx_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(wx_alert, "synthesize_clip", side_effect=[voices_ok(), voices_fail(), voices_ok()]), \
             patch.object(wx_alert, "concat_clips") as mock_concat, \
             patch.object(wx_alert, "deliver") as mock_deliver, \
             patch.object(wx_alert, "fire_insert_urgent") as mock_fire, \
             patch.object(wx_alert.os, "makedirs"):
            ok = wx_alert._main_body()
        self.assertFalse(ok)
        mock_concat.assert_not_called()
        mock_deliver.assert_not_called()
        mock_fire.assert_not_called()

    def test_successful_multi_alert_set_still_publishes_normally(self):
        with patch.object(wx_alert, "load_watchwarn_entries", return_value=self._entries(3)), \
             patch.object(wx_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(wx_alert, "synthesize_clip", return_value=voices_ok()), \
             patch.object(wx_alert, "concat_clips", return_value=True) as mock_concat, \
             patch.object(wx_alert, "deliver", return_value="/srv/dest.mp3") as mock_deliver, \
             patch.object(wx_alert, "fire_insert_urgent") as mock_fire, \
             patch.object(wx_alert.os, "makedirs"):
            ok = wx_alert._main_body()
        self.assertTrue(ok)
        mock_concat.assert_called_once()
        mock_deliver.assert_called_once()
        mock_fire.assert_called_once()

    def test_no_qualifying_entries_is_a_benign_success_no_op(self):
        with patch.object(wx_alert, "load_watchwarn_entries", return_value=[]), \
             patch.object(wx_alert, "concat_clips") as mock_concat:
            ok = wx_alert._main_body()
        self.assertTrue(ok)
        mock_concat.assert_not_called()

    def test_voice_resolution_failure_aborts_before_any_synthesis(self):
        with patch.object(wx_alert, "load_watchwarn_entries", return_value=self._entries(1)), \
             patch.object(wx_alert, "load_weather_config", return_value={"voice_schedule": [], "voice_personas": {}}), \
             patch.object(wx_alert, "synthesize_clip") as mock_synth:
            ok = wx_alert._main_body()
        self.assertFalse(ok)
        mock_synth.assert_not_called()


class AmberAlertAtomicityTests(unittest.TestCase):
    """Same all-or-nothing contract as WxAlertAtomicityTests, applied to
    amber_alert.py's own _main_body()."""

    def _entries(self, n):
        return [{"event": f"AMBER{i}", "text": f"Alert text {i}"} for i in range(n)]

    def test_one_failed_clip_prevents_complete_publication(self):
        with patch.object(amber_alert, "_load_active", return_value=self._entries(2)), \
             patch.object(amber_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(amber_alert.voices, "synthesize", side_effect=[voices_fail(), voices_ok()]), \
             patch.object(amber_alert, "_concat_clips") as mock_concat, \
             patch.object(amber_alert, "deliver") as mock_deliver, \
             patch.object(amber_alert, "_fire_insert_urgent") as mock_fire, \
             patch.object(amber_alert.os, "makedirs"):
            ok = amber_alert._main_body()
        self.assertFalse(ok)
        mock_concat.assert_not_called()
        mock_deliver.assert_not_called()
        mock_fire.assert_not_called()

    def test_successful_multi_alert_set_still_publishes_normally(self):
        with patch.object(amber_alert, "_load_active", return_value=self._entries(2)), \
             patch.object(amber_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(amber_alert.voices, "synthesize", return_value=voices_ok()), \
             patch.object(amber_alert, "_concat_clips", return_value=True) as mock_concat, \
             patch.object(amber_alert, "deliver", return_value="/srv/dest.mp3") as mock_deliver, \
             patch.object(amber_alert, "_fire_insert_urgent") as mock_fire, \
             patch.object(amber_alert.os, "makedirs"):
            ok = amber_alert._main_body()
        self.assertTrue(ok)
        mock_concat.assert_called_once()
        mock_deliver.assert_called_once()
        mock_fire.assert_called_once()

    def test_no_qualifying_entries_is_a_benign_success_no_op(self):
        with patch.object(amber_alert, "_load_active", return_value=[]), \
             patch.object(amber_alert, "_concat_clips") as mock_concat:
            ok = amber_alert._main_body()
        self.assertTrue(ok)
        mock_concat.assert_not_called()


class FailedCliOutputCannotProceedTests(unittest.TestCase):
    """A failed canonical-CLI result must never reach ffmpeg or
    delivery in ANY of the four callers."""

    def test_current_temp_failed_synthesis_never_reaches_ffmpeg_or_delivery(self):
        with patch.object(current_temp, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(current_temp, "load_weather", return_value={"tempf": "50", "humidity": "40"}), \
             patch.object(current_temp.voices, "synthesize", return_value=voices_fail()), \
             patch.object(current_temp, "convert_to_mp3") as mock_convert, \
             patch.object(current_temp, "deliver") as mock_deliver, \
             patch.object(current_temp, "notify"), \
             patch.object(current_temp.fcntl, "flock"), \
             patch("builtins.open", create=True), \
             patch.object(current_temp.shutil, "rmtree"), \
             patch.object(sys, "argv", ["current_temp.py", "--voice", "day"]):
            current_temp.main()
        mock_convert.assert_not_called()
        mock_deliver.assert_not_called()

    def test_wx_forecast_failed_synthesis_never_reaches_ffmpeg_or_delivery(self):
        with patch.object(sys, "argv", ["wx_forecast.py", "--mode", "1day", "--voice", "day"]), \
             patch.object(wx_forecast, "build_announcement", return_value=("Test.", FAKE_FORECAST_META)), \
             patch.object(wx_forecast, "generate_wav_with_piper", return_value=voices_fail()), \
             patch.object(wx_forecast, "convert_to_mp3") as mock_convert, \
             patch.object(wx_forecast, "deliver") as mock_deliver, \
             patch.object(wx_forecast, "notify"), \
             patch.object(wx_forecast.shutil, "rmtree"), \
             patch.object(wx_forecast.os, "makedirs"):
            ok = wx_forecast.main()
        self.assertFalse(ok)
        mock_convert.assert_not_called()
        mock_deliver.assert_not_called()


class ExistingFinalFileUntouchedTests(unittest.TestCase):
    """A synthesis/concat failure must never touch the previous final
    MP3 -- proven by never invoking any delivery/replace call on
    failure (deliver() is the ONLY thing that ever moves a file into
    the live srv path; see lib/delivery.py)."""

    def test_wx_alert_failure_never_calls_deliver(self):
        with patch.object(wx_alert, "load_watchwarn_entries",
                           return_value=[{"event": "E", "text": "t"}]), \
             patch.object(wx_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(wx_alert, "synthesize_clip", return_value=voices_fail()), \
             patch.object(wx_alert, "deliver") as mock_deliver, \
             patch.object(wx_alert.os, "makedirs"):
            wx_alert._main_body()
        mock_deliver.assert_not_called()

    def test_amber_alert_failure_never_calls_deliver(self):
        with patch.object(amber_alert, "_load_active",
                           return_value=[{"event": "E", "text": "t"}]), \
             patch.object(amber_alert, "load_weather_config", return_value=FAKE_CFG), \
             patch.object(amber_alert.voices, "synthesize", return_value=voices_fail()), \
             patch.object(amber_alert, "deliver") as mock_deliver, \
             patch.object(amber_alert.os, "makedirs"):
            amber_alert._main_body()
        mock_deliver.assert_not_called()


class ExitStatusTests(unittest.TestCase):
    """Real synthesis-generation failures return nonzero (False from
    main()); benign/no-change/lock-contention exits remain success."""

    def test_current_temp_main_returns_false_on_voice_resolution_failure(self):
        with patch.object(current_temp, "load_weather_config",
                           return_value={"voice_schedule": [], "voice_personas": {}}), \
             patch.object(current_temp, "notify"), \
             patch.object(current_temp.fcntl, "flock"), \
             patch("builtins.open", create=True), \
             patch.object(current_temp.shutil, "rmtree"), \
             patch.object(sys, "argv", ["current_temp.py", "--voice", "day"]):
            ok = current_temp.main()
        self.assertFalse(ok)

    def test_current_temp_lock_contention_is_benign_success(self):
        with patch.object(current_temp.fcntl, "flock", side_effect=BlockingIOError), \
             patch("builtins.open", create=True), \
             patch.object(sys, "argv", ["current_temp.py", "--voice", "day"]):
            ok = current_temp.main()
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
