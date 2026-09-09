"""Regression coverage for wx_forecast.py's six-hour forecast-cache
boundary (P1 2.4 Pass G). weather.diagnostics.py's own
FORECAST_CACHE_WARN_HOURS exposes this same six-hour value as
`degraded` evidence on the READING side; this proves the PRODUCER
side is now authoritative -- a cache older than six hours is refused
outright (no new forecast is synthesized from it), rather than merely
logged as a warning and broadcast indefinitely as it was before P1 2.4
Pass G.

wx_forecast.py computes CFG/DATA_DIR/FORECAST_CACHE_FILE at import
time via wxconfig.load_weather_config()/resolve_weather_data_dir() --
the same import-time ceremony as update_local_wx_data.py (see
test_alert_beep_trigger.py's own docstring). Several OTHER files in
this test suite (see test_speech_paths.py) also import wx_forecast at
their own module scope under their own fake config, and Python caches
a module the first time any file in this shared test process imports
it by name -- whichever test file runs first "wins" wx_forecast's real
DATA_DIR for the rest of the run. Rather than depend on that ordering,
every test below patches `wx_forecast.FORECAST_CACHE_FILE` directly to
a private temp file (see _ForecastCacheTestCase), so behavior here is
correct regardless of which file imports wx_forecast first."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
sys.path.insert(0, str(PROJECT_ROOT))

import wxconfig  # noqa: E402

_FAKE_CFG = {
    "weather_data_dir": tempfile.mkdtemp(prefix="isadoraair-wxingest-forecast-test-"),
    "station_lat": 39.13,
    "station_lon": -97.70,
    "sun_alt_threshold_deg": 3.0,
    "nws_alert_zone": "KSC143",
    "nws_forecast_office": "TOP",
    "nws_forecast_grid_x": 10,
    "nws_forecast_grid_y": 53,
    "nws_cloud_stations": [],
}

# Only takes effect if THIS file is the first to import wx_forecast in
# the current test process -- see module docstring. Harmless no-op
# otherwise (Python does not re-run an already-cached module's
# top-level code on a later `import`).
with patch.object(wxconfig, "load_weather_config", Mock(return_value=_FAKE_CFG)):
    import wx_forecast  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _ForecastCacheTestCase(unittest.TestCase):
    """Shared fixture: points wx_forecast.FORECAST_CACHE_FILE at a
    private temp file for the duration of each test, independent of
    whatever DATA_DIR the module actually ended up with at import time
    -- see module docstring."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxforecast-cache-")
        self.addCleanup(self._tmp.cleanup)
        self.cache_path = Path(self._tmp.name) / "wx_forecast_cache.json"
        patcher = patch.object(wx_forecast, "FORECAST_CACHE_FILE", self.cache_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_cache(self, periods, age_seconds):
        self.cache_path.write_text(json.dumps(periods))
        ts = time.time() - age_seconds
        os.utime(self.cache_path, (ts, ts))


class FreshLiveNWSTests(_ForecastCacheTestCase):
    def test_successful_live_fetch_returns_live_source_kind_and_writes_cache(self):
        periods = [{"name": "Today", "detailedForecast": "Sunny"}]
        with patch.object(
            wx_forecast.requests, "get",
            return_value=FakeResponse({"properties": {"periods": periods}}),
        ):
            result_periods, source_kind, age, used_fallback = wx_forecast.get_periods()

        self.assertEqual(result_periods, periods)
        self.assertEqual(source_kind, "live_nws")
        self.assertEqual(age, 0.0)
        self.assertFalse(used_fallback)
        self.assertEqual(json.loads(self.cache_path.read_text()), periods)


class CachedFallbackTests(_ForecastCacheTestCase):
    def test_allowable_cached_fallback_under_six_hours(self):
        periods = [{"name": "Today", "detailedForecast": "Cloudy"}]
        self.write_cache(periods, age_seconds=3600)  # 1h old
        with patch.object(wx_forecast.requests, "get", side_effect=RuntimeError("network down")):
            result_periods, source_kind, age, used_fallback = wx_forecast.get_periods()

        self.assertEqual(result_periods, periods)
        self.assertEqual(source_kind, "cached_fallback")
        self.assertTrue(used_fallback)
        self.assertAlmostEqual(age, 3600, delta=5)

    def test_just_under_six_hour_boundary_is_still_allowed(self):
        # Deliberately 2s under the boundary rather than exactly at it,
        # to avoid clock-precision flakiness right at the edge -- the
        # actual check is a strict `>`, so anything not measurably past
        # six hours must be accepted as a legitimate fallback.
        periods = [{"name": "Today", "detailedForecast": "Windy"}]
        self.write_cache(periods, age_seconds=6 * 3600 - 2)
        with patch.object(wx_forecast.requests, "get", side_effect=RuntimeError("network down")):
            result_periods, source_kind, age, used_fallback = wx_forecast.get_periods()

        self.assertEqual(result_periods, periods)
        self.assertEqual(source_kind, "cached_fallback")

    def test_over_six_hour_cache_is_refused_not_broadcast(self):
        periods = [{"name": "Today", "detailedForecast": "Stormy"}]
        self.write_cache(periods, age_seconds=6 * 3600 + 60)
        with patch.object(wx_forecast.requests, "get", side_effect=RuntimeError("network down")):
            with self.assertRaises(wx_forecast.ForecastUnavailableError):
                wx_forecast.get_periods()

    def test_no_cache_at_all_and_live_fetch_fails_is_refused(self):
        with patch.object(wx_forecast.requests, "get", side_effect=RuntimeError("network down")):
            with self.assertRaises(wx_forecast.ForecastUnavailableError):
                wx_forecast.get_periods()

    def test_empty_live_response_falls_back_to_cache(self):
        periods = [{"name": "Today", "detailedForecast": "Fair"}]
        self.write_cache(periods, age_seconds=120)
        with patch.object(
            wx_forecast.requests, "get",
            return_value=FakeResponse({"properties": {"periods": []}}),
        ):
            result_periods, source_kind, age, used_fallback = wx_forecast.get_periods()

        self.assertEqual(result_periods, periods)
        self.assertEqual(source_kind, "cached_fallback")


class LastKnownGoodPreservationTests(_ForecastCacheTestCase):
    """Proves main() aborts BEFORE any synthesis/publication when the
    forecast is unavailable -- the previous broadcast artifact is left
    completely untouched (deliver() is never invoked at all), and
    failure is still reported through the normal notify() path."""

    def setUp(self):
        super().setUp()
        self.write_cache([{"name": "Today", "detailedForecast": "Old"}], age_seconds=6 * 3600 + 60)

        self.argv_patcher = patch.object(sys, "argv", ["wx_forecast.py", "--mode", "3day", "--voice", "auto"])
        self.argv_patcher.start()
        self.addCleanup(self.argv_patcher.stop)

        voice = {"name": "Claira Sky", "signoff": "stay safe."}
        self.resolve_voice_patcher = patch.object(wx_forecast, "resolve_voice", return_value=("day", voice))
        self.resolve_voice_patcher.start()
        self.addCleanup(self.resolve_voice_patcher.stop)

        self.requests_patcher = patch.object(wx_forecast.requests, "get", side_effect=RuntimeError("network down"))
        self.requests_patcher.start()
        self.addCleanup(self.requests_patcher.stop)

    def test_main_returns_false_and_never_synthesizes_or_delivers(self):
        with patch.object(wx_forecast, "generate_wav_with_piper") as mock_synth, \
             patch.object(wx_forecast, "deliver") as mock_deliver, \
             patch.object(wx_forecast, "notify") as mock_notify:
            result = wx_forecast.main()

        self.assertFalse(result)
        mock_synth.assert_not_called()
        mock_deliver.assert_not_called()
        mock_notify.assert_called_once()
        self.assertIn("FAILED", mock_notify.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
