"""Weather diagnostics after the MP3 -> FLAC standardization (r0134): the authoritative generated-audio filenames
are the .flac ones. A legacy MP3 left behind by the station is NOT evidence that the FLAC workflow is healthy."""
import tempfile
from pathlib import Path

from django.test import TestCase

from weather.diagnostics import GENERATED_ARTIFACTS, get_weather_diagnostics
from weather.models import WeatherConfig

from .test_diagnostics import NOW, make_category, make_track, write_json, write_matching_provenance


class FlacDiagnosticsTests(TestCase):
    def setUp(self):
        self.lib = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-flaclib-")
        self.addCleanup(self.lib.cleanup)
        self.data = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-flacdata-")
        self.addCleanup(self.data.cleanup)
        self.library_root, self.data_dir = Path(self.lib.name), Path(self.data.name)
        WeatherConfig.load()
        self.category = make_category("WxTemp")

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir, library_root=self.library_root)

    def _audio(self, category_code, filename, category, with_track=True):
        path = self.library_root / category_code / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        if with_track:
            make_track(path, category, ready2air=True)
        return path

    def test_the_authoritative_filenames_are_the_permanent_flac_names(self):
        self.assertEqual(
            {(category, name) for _key, category, name, _event in GENERATED_ARTIFACTS},
            {("WxTemp", "current_temp.flac"), ("WxObs", "current_obs.flac"),
             ("WxForecast", "forecast.flac"), ("WxAlert", "wx_alert.flac")},
        )

    def test_a_published_flac_with_its_track_and_provenance_is_ready(self):
        path = self._audio("WxTemp", "current_temp.flac", self.category)
        write_matching_provenance(self.data_dir, "WxTemp", path)
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.path, str(path))
        self.assertIn("current_temp.flac", fact.summary)

    def test_a_legacy_mp3_alone_does_not_make_the_routine_artifact_ready(self):
        mp3 = self._audio("WxTemp", "current_temp.mp3", self.category)
        write_matching_provenance(self.data_dir, "WxTemp", mp3)
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertNotEqual(fact.state, "ready")
        self.assertIn("current_temp.flac", fact.summary)

    def test_a_legacy_mp3_alone_does_not_make_an_active_alert_ready(self):
        category = make_category("WxAlert")
        write_json(self.data_dir / "active_watches_warnings.json",
                   [{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        self._audio("WxAlert", "wx_alert.mp3", category)
        fact = self.snapshot().get("generated_artifact:wx_alert")
        self.assertNotEqual(fact.state, "ready")
        self.assertIn("wx_alert.flac", fact.summary)

    def test_alert_is_not_applicable_when_none_is_active_even_without_a_flac(self):
        """WxAlert is event-driven: with no active alert the FLAC legitimately does not exist yet."""
        category = make_category("WxAlert")
        write_json(self.data_dir / "active_watches_warnings.json", [])
        self._audio("WxAlert", "wx_alert.mp3", category)
        fact = self.snapshot().get("generated_artifact:wx_alert")
        self.assertEqual(fact.state, "not_applicable")

    def test_alert_flac_is_ready_when_an_alert_is_active_and_it_is_published(self):
        category = make_category("WxAlert")
        write_json(self.data_dir / "active_watches_warnings.json",
                   [{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        path = self._audio("WxAlert", "wx_alert.flac", category)
        write_matching_provenance(self.data_dir, "WxAlert", path, alert_family="nws_watch_warning")
        fact = self.snapshot().get("generated_artifact:wx_alert")
        self.assertEqual(fact.state, "ready")
