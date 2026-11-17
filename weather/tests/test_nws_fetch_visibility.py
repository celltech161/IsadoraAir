"""r0145: Weather Health names an NWS alerts-fetch outage as the cause of a stale watch/warning snapshot, and the
Weather Health probe carries each degraded fact's own summary into the monitoring event detail.

Evidence only: no state, applicability or needs-attention rule changes, so every case also pins the unchanged state."""
import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from monitoring.services import probes
from monitoring.models import MonitorCheck
from weather.diagnostics import (
    NWS_ALERT_FETCH_STATUS_FILENAME, DiagnosticFact, WeatherDiagnosticsSnapshot, get_weather_diagnostics,
)
from weather.freshness import CURRENT_DERIVED_FRESHNESS_SECONDS
from weather.models import WeatherConfig

NOW = datetime(2026, 11, 17, 16, 20, 0, tzinfo=dt_timezone.utc)
STALE_SECONDS = CURRENT_DERIVED_FRESHNESS_SECONDS + 4 * 60          # 19 min old


def iso(dt):
    return dt.isoformat()


class FetchVisibilityBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="isadoraair-nws-visibility-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data_dir = Path(self.tmp)
        WeatherConfig.load()

    def write_snapshot(self, entries, age_seconds):
        path = self.data_dir / "active_watches_warnings.json"
        path.write_text(json.dumps(entries))
        stamp = (NOW - timedelta(seconds=age_seconds)).timestamp()
        os.utime(path, (stamp, stamp))

    def write_status(self, **fields):
        record = {"ok": False, "checked_at": iso(NOW - timedelta(seconds=30)), "status_code": 503,
                  "error": "HTTP 503", "failing_since": iso(NOW - timedelta(minutes=14))}
        record.update(fields)
        (self.data_dir / NWS_ALERT_FETCH_STATUS_FILENAME).write_text(json.dumps(record))

    def watch_fact(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir).get("watch_warnings")

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir)


class WatchWarningCauseTests(FetchVisibilityBase):
    def test_stale_snapshot_with_a_failing_fetch_names_the_cause(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status()
        fact = self.watch_fact()
        self.assertEqual(fact.state, "degraded")                     # unchanged
        self.assertIn("Stale: 19 min old", fact.summary)             # the existing staleness text is kept
        self.assertIn("The NWS alerts fetch has been failing for 14 min (HTTP 503)", fact.summary)
        self.assertIn("kept until it succeeds", fact.summary)
        evidence = fact.evidence["nws_alert_fetch"]
        self.assertEqual((evidence["status_code"], evidence["error"]), (503, "HTTP 503"))
        self.assertEqual(evidence["failing_seconds"], 14 * 60)
        self.assertEqual(fact.count, 0)                              # existing evidence retained
        self.assertEqual(fact.evidence["events"], [])
        json.dumps(fact.to_dict())                                   # JSON-serialisable for the admin/API views

    def test_a_connection_error_is_named_by_its_error_text(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(status_code=None, error="ConnectionError: no route to host")
        self.assertIn("(ConnectionError: no route to host)", self.watch_fact().summary)

    def test_missing_snapshot_with_a_failing_fetch_names_the_cause_too(self):
        self.write_status()
        fact = self.watch_fact()
        self.assertEqual(fact.state, "degraded")
        self.assertIn("No watch/warning snapshot exists yet", fact.summary)
        self.assertIn("failing for 14 min (HTTP 503)", fact.summary)

    def test_the_cause_never_changes_state_or_applicability(self):
        self.write_snapshot([], STALE_SECONDS)
        baseline = self.snapshot()
        self.write_status()
        with_status = self.snapshot()
        self.assertEqual([(f.key, f.state) for f in baseline.facts], [(f.key, f.state) for f in with_status.facts])
        self.assertEqual(with_status.get("generated_artifact:wx_alert").state, "degraded")
        self.assertNotEqual(with_status.get("generated_artifact:wx_alert").state, "not_applicable")

    def test_a_fresh_snapshot_is_never_degraded_or_annotated_by_a_failing_fetch(self):
        self.write_snapshot([], 120)
        self.write_status()
        fact = self.watch_fact()
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.summary, "No active watches/warnings.")
        self.assertNotIn("nws_alert_fetch", fact.evidence)
        self.assertEqual(self.snapshot().get("generated_artifact:wx_alert").state, "not_applicable")

    def test_a_successful_fetch_record_adds_nothing(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(ok=True, status_code=200, error=None, failing_since=None)
        fact = self.watch_fact()
        self.assertNotIn("NWS alerts fetch", fact.summary)
        self.assertNotIn("nws_alert_fetch", fact.evidence)

    def test_an_old_failure_record_means_the_updater_stopped_not_an_nws_outage(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(checked_at=iso(NOW - timedelta(seconds=CURRENT_DERIVED_FRESHNESS_SECONDS + 60)))
        fact = self.watch_fact()
        self.assertEqual(fact.state, "degraded")
        self.assertNotIn("NWS alerts fetch", fact.summary)
        self.assertNotIn("nws_alert_fetch", fact.evidence)

    def test_a_record_at_the_freshness_boundary_still_counts(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(checked_at=iso(NOW - timedelta(seconds=CURRENT_DERIVED_FRESHNESS_SECONDS)))
        self.assertIn("NWS alerts fetch", self.watch_fact().summary)

    def test_absent_malformed_or_wrongly_typed_records_are_ignored_without_error(self):
        self.write_snapshot([], STALE_SECONDS)
        status_path = self.data_dir / NWS_ALERT_FETCH_STATUS_FILENAME
        plain = self.watch_fact().summary
        for text in ("{not json", "[]", "null", '"x"', "{}", json.dumps({"ok": False}),
                     json.dumps({"ok": False, "checked_at": 12345}),
                     json.dumps({"ok": False, "checked_at": "yesterday"}),
                     json.dumps({"ok": "no", "checked_at": iso(NOW)})):
            status_path.write_text(text)
            fact = self.watch_fact()
            self.assertEqual(fact.state, "degraded", text)
            self.assertEqual(fact.summary, plain, text)

    def test_odd_field_types_do_not_leak_or_crash(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(status_code="503", error={"a": 1}, failing_since="garbage")
        fact = self.watch_fact()
        self.assertIn("NWS alerts fetch has been failing", fact.summary)
        self.assertIsNone(fact.evidence["nws_alert_fetch"]["status_code"])
        self.assertIsNone(fact.evidence["nws_alert_fetch"]["error"])
        self.write_status(error="x" * 5000)
        self.assertLessEqual(len(self.watch_fact().evidence["nws_alert_fetch"]["error"]), 200)

    def test_a_malformed_watch_snapshot_stays_needs_attention_and_unannotated(self):
        (self.data_dir / "active_watches_warnings.json").write_text("{bad")
        self.write_status()
        fact = self.watch_fact()
        self.assertEqual(fact.state, "needs_attention")
        self.assertNotIn("NWS alerts fetch", fact.summary)

    def test_a_failure_started_just_now_reads_zero_minutes(self):
        self.write_snapshot([], STALE_SECONDS)
        self.write_status(failing_since=iso(NOW - timedelta(seconds=10)))
        self.assertIn("failing for 0 min", self.watch_fact().summary)


def make_check():
    return MonitorCheck(name="Weather Health (test)", kind="weather")


class ProbeDetailTests(FetchVisibilityBase):
    def test_degraded_details_map_each_degraded_key_to_its_own_summary(self):
        snapshot = WeatherDiagnosticsSnapshot(generated_at="x", facts=(
            DiagnosticFact(key="forecast_cache", state="degraded", summary="stale cache"),
            DiagnosticFact(key="station_location", state="ready", summary="ok"),
            DiagnosticFact(key="generated_artifact:wx_alert", state="not_applicable", summary="n/a"),
        ))
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "warning")
        self.assertEqual(detail["degraded_details"], {"forecast_cache": "stale cache"})
        self.assertEqual(detail["reason"], "1 Weather fact(s) degraded: forecast_cache")      # unchanged
        self.assertEqual(detail["degraded_keys"], ["forecast_cache"])

    def test_critical_carries_both_detail_maps(self):
        snapshot = WeatherDiagnosticsSnapshot(generated_at="x", facts=(
            DiagnosticFact(key="a", state="needs_attention", summary="bad config"),
            DiagnosticFact(key="b", state="degraded", summary="stale"),
        ))
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "critical")
        self.assertEqual(detail["needs_attention_details"], {"a": "bad config"})
        self.assertEqual(detail["degraded_details"], {"b": "stale"})
        self.assertEqual(detail["needs_attention_keys"], ["a"])

    def test_ok_has_no_detail_maps(self):
        snapshot = WeatherDiagnosticsSnapshot(generated_at="x", facts=(
            DiagnosticFact(key="a", state="ready", summary="ok"),))
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        self.assertEqual(status, "ok")
        self.assertNotIn("degraded_details", detail)

    def test_end_to_end_an_nws_outage_shows_its_cause_in_the_probe_detail(self):
        """Real diagnostics over a real (temporary) data dir: the probe detail -- which becomes the Recent Events
        row's probe_detail and the notification body -- says the NWS alerts fetch is failing."""
        self.write_snapshot([], STALE_SECONDS)
        self.write_status()
        with override_settings(WEATHER_DATA_DIR=str(self.data_dir)), \
                patch("django.utils.timezone.now", return_value=NOW):
            status, detail = probes.probe_weather(make_check())
        # (an empty test database leaves unrelated config facts needing attention, hence "critical" here; the
        # degraded facts and their summaries are carried either way)
        self.assertIn(status, ("warning", "critical"))
        self.assertIn("watch_warnings", detail["degraded_keys"])
        self.assertIn("generated_artifact:wx_alert", detail["degraded_keys"])
        self.assertIn("HTTP 503", detail["degraded_details"]["watch_warnings"])
        self.assertIn("failing for 14 min", detail["degraded_details"]["watch_warnings"])
        json.dumps(detail)                                              # JSON-serialisable event detail

    def test_the_transition_event_records_the_details(self):
        from monitoring.models import SystemEvent
        from monitoring.services.monitor import MonitorManager
        snapshot = WeatherDiagnosticsSnapshot(generated_at="x", facts=(
            DiagnosticFact(key="watch_warnings", state="degraded", summary="stale. NWS fetch failing (HTTP 503)"),))
        with patch("weather.diagnostics.get_weather_diagnostics", return_value=snapshot):
            status, detail = probes.probe_weather(make_check())
        check = MonitorCheck.objects.create(name="Weather Health (evt)", kind="weather")
        MonitorManager()._emit_transition_event(check, "ok", status, detail)
        event = SystemEvent.objects.filter(category="monitor").latest("id")
        self.assertIn("HTTP 503", event.detail["probe_detail"]["degraded_details"]["watch_warnings"])
