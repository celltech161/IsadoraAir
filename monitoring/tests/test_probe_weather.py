"""probe_weather tests (P1 2.4 Pass G) -- the probe must delegate
entirely to weather.diagnostics.get_weather_diagnostics() (the one
Weather readiness/provenance authority) rather than reimplementing any
Weather rule, map its states onto Monitoring's ok/warning/critical
vocabulary, and fail safe (unknown) on an unexpected exception instead
of crashing the Monitoring poll loop."""
from unittest.mock import patch

from django.test import TestCase

from monitoring.models import MonitorCheck
from monitoring.services import probes
from weather.diagnostics import DiagnosticFact, WeatherDiagnosticsSnapshot


def make_check(**overrides):
    defaults = dict(name="Weather Health (test)", kind="weather")
    defaults.update(overrides)
    return MonitorCheck(**defaults)


def make_snapshot(*facts):
    return WeatherDiagnosticsSnapshot(generated_at="2026-09-09T00:00:00Z", facts=tuple(facts))


class ProbeWeatherTests(TestCase):
    def test_all_ready_is_ok(self):
        snapshot = make_snapshot(
            DiagnosticFact(key="station_location", state="ready", summary="ok"),
            DiagnosticFact(key="alert_fx_cart", state="optional_disabled", summary="disabled"),
            DiagnosticFact(key="generated_artifact:wx_alert", state="not_applicable", summary="no active alert"),
        )
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "ok")
        self.assertEqual(detail["fact_count"], 3)

    def test_degraded_fact_is_warning_not_critical(self):
        snapshot = make_snapshot(
            DiagnosticFact(key="forecast_cache", state="degraded", summary="stale cache"),
            DiagnosticFact(key="station_location", state="ready", summary="ok"),
        )
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "warning")
        self.assertEqual(detail["degraded_keys"], ["forecast_cache"])

    def test_needs_attention_fact_is_critical(self):
        snapshot = make_snapshot(
            DiagnosticFact(key="station_location", state="needs_attention", summary="bad config"),
            DiagnosticFact(key="forecast_cache", state="degraded", summary="stale cache"),
        )
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "critical")
        self.assertEqual(detail["needs_attention_keys"], ["station_location"])
        self.assertEqual(detail["degraded_keys"], ["forecast_cache"])

    def test_needs_attention_takes_priority_over_degraded(self):
        snapshot = make_snapshot(
            DiagnosticFact(key="a", state="degraded", summary="x"),
            DiagnosticFact(key="b", state="needs_attention", summary="y"),
        )
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, _ = probes.probe_weather(make_check())
        self.assertEqual(status, "critical")

    def test_unexpected_exception_is_unknown_not_raised(self):
        with patch("weather.diagnostics.get_weather_diagnostics", side_effect=RuntimeError("boom")):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "unknown")
        self.assertIn("boom", detail["error"])

    def test_probe_dispatch_includes_weather(self):
        self.assertIs(probes.PROBE_DISPATCH["weather"], probes.probe_weather)

    def test_kind_choice_exists_and_requires_no_extra_fields(self):
        """No unnecessary per-row configuration fields -- a plain
        MonitorCheck(kind='weather') must validate cleanly, same as
        the existing 'rbds' kind (see MonitorCheck.clean())."""
        check = make_check()
        check.full_clean()  # must not raise
        self.assertIn(("weather", "Weather Health"), MonitorCheck.KIND_CHOICES)
