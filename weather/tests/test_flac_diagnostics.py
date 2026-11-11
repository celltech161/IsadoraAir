"""Weather diagnostics across the MP3 -> FLAC standardization (r0134): the artifact is reported under the file its
Track currently points at, whichever extension that is, so a station is healthy before, during and after the first
FLAC publication adopts its Track."""
import tempfile
from pathlib import Path

from django.test import TestCase

from weather.diagnostics import GENERATED_ARTIFACTS, get_weather_diagnostics
from weather.models import WeatherConfig

from .test_diagnostics import NOW, make_category, make_track, write_json, write_matching_provenance


class DiagnosticsAcrossFormatsTests(TestCase):
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

    def _artifact(self, filename, track=True):
        path = self.library_root / "WxTemp" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        if track:
            make_track(path, self.category, ready2air=True)
        write_matching_provenance(self.data_dir, "WxTemp", path)
        return path

    def test_preferred_names_are_flac(self):
        self.assertEqual({name for _k, _c, name, _e in GENERATED_ARTIFACTS},
                         {"current_temp.flac", "current_obs.flac", "forecast.flac", "wx_alert.flac"})

    def test_flac_artifact_with_its_track_is_ready(self):
        self._artifact("current_temp.flac")
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertEqual(fact.state, "ready")
        self.assertIn("current_temp.flac", fact.summary)

    def test_station_still_on_mp3_is_ready_until_the_first_flac_publication(self):
        self._artifact("current_temp.mp3")
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertEqual(fact.state, "ready")
        self.assertIn("current_temp.mp3", fact.summary)

    def test_after_adoption_the_flac_is_reported_even_though_the_old_mp3_remains_on_disk(self):
        old = self._artifact("current_temp.mp3")
        from library.models import Track
        new = self._artifact("current_temp.flac", track=False)
        Track.objects.filter(filepath=str(old)).update(filepath=str(new), filename=new.name, format="flac")
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.path, str(new))

    def test_orphaned_mp3_with_a_flac_track_missing_its_file_is_flagged(self):
        """The Track points at the FLAC but the file is gone: needs attention, not silently satisfied by the old MP3."""
        self._artifact("current_temp.mp3", track=False)
        flac = self.library_root / "WxTemp" / "current_temp.flac"
        make_track(flac, self.category, ready2air=True)
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertEqual(fact.state, "needs_attention")

    def test_nothing_generated_yet_reports_the_preferred_name(self):
        fact = self.snapshot().get("generated_artifact:wx_temp")
        self.assertIn("current_temp.flac", fact.summary)

    def test_alert_messages_name_the_actual_file(self):
        category = make_category("WxAlert")
        write_json(self.data_dir / "active_watches_warnings.json", [])
        path = self.library_root / "WxAlert" / "wx_alert.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio")
        make_track(path, category, ready2air=True)
        write_matching_provenance(self.data_dir, "WxAlert", path, alert_family="nws_watch_warning")
        fact = self.snapshot().get("generated_artifact:wx_alert")
        self.assertEqual(fact.state, "not_applicable")
        self.assertIn("wx_alert.mp3", fact.summary)
