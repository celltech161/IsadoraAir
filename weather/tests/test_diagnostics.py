"""Pass B (P1 1.15 / 2.4): weather.diagnostics is the one reusable,
side-effect-free Weather readiness authority. These tests cover the
configuration/data/artifact evidence it collects, its safety
properties, and its two proving consumers (Admin subpage + management
command) -- see weather/diagnostics.py's own module docstring for the
design rules being enforced here."""
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from isadoraair.tts.models import StationTTSVoice
from library.models import Artist, Category, CategoryKind, FXCart, Track
from weather.diagnostics import FORECAST_CACHE_WARN_HOURS, get_weather_diagnostics
from weather.models import AmberAlertConfig, WeatherConfig, WeatherVoicePersona

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt_timezone.utc)


def make_voice(name="claira_sky", enabled=True):
    return StationTTSVoice.objects.create(
        name=name, enabled=enabled, engine=StationTTSVoice.Engine.KOKORO, provider_voice="af_jessica",
    )


def make_category(code, kind_code="imaging"):
    # get_or_create -- some category codes (e.g. WxAlert) are already
    # seeded by a real data migration (library/migrations/
    # 0041_add_wxalert_category.py); tests must not collide with that.
    kind, _ = CategoryKind.objects.get_or_create(code=kind_code, defaults={"name": kind_code.title()})
    category, _ = Category.objects.get_or_create(code=code, defaults={"name": code, "kind": kind})
    return category


def make_track(filepath, category, ready2air=True):
    artist, _ = Artist.objects.get_or_create(name="Oak Grove Radio")
    return Track.objects.create(
        filepath=str(filepath), filename=Path(filepath).name, title="Weather", artist=artist,
        category=category, ready2air=ready2air,
    )


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


class DiagnosticsConfigurationTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir)

    def test_clean_r0049_default_persona_with_no_voice_is_needs_attention(self):
        WeatherConfig.load()  # creates the neutral default/no-voice persona
        snap = self.snapshot()
        fact = snap.get("announcer_persona:default")
        self.assertEqual(fact.state, "needs_attention")
        self.assertIn("no logical station voice", fact.summary)

    def test_valid_arbitrary_persona_schedule_is_ready(self):
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="morning_host", tts_voice=voice, display_name="Morgan")
        WeatherConfig.objects.create(pk=1, voice_schedule=[["morning_host", 0, 23]])
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_schedule").state, "ready")
        self.assertEqual(snap.get("announcer_persona:morning_host").state, "ready")

    def test_piper_backed_persona_is_ready(self):
        voice = StationTTSVoice.objects.create(
            name="piper_voice", enabled=True, engine=StationTTSVoice.Engine.PIPER,
        )
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_persona:default").state, "ready")

    def test_malformed_schedule_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule="not-a-list")
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_schedule").state, "needs_attention")

    def test_incomplete_schedule_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 10]])
        snap = self.snapshot()
        fact = snap.get("announcer_schedule")
        self.assertEqual(fact.state, "needs_attention")
        self.assertIn("hour(s)", fact.detail)

    def test_overlapping_schedule_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["a", 0, 12], ["b", 6, 23]])
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_schedule").state, "needs_attention")

    def test_missing_persona_for_referenced_slot(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["ghost", 0, 23]])
        snap = self.snapshot()
        fact = snap.get("announcer_persona:ghost")
        self.assertEqual(fact.state, "needs_attention")
        self.assertIn("no Weather Voice Persona exists", fact.summary)

    def test_persona_with_no_voice_is_needs_attention(self):
        WeatherVoicePersona.objects.create(slot="unvoiced")
        WeatherConfig.objects.create(pk=1, voice_schedule=[["unvoiced", 0, 23]])
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_persona:unvoiced").state, "needs_attention")

    def test_disabled_logical_voice_is_needs_attention(self):
        voice = make_voice(enabled=False)
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        snap = self.snapshot()
        self.assertEqual(snap.get("announcer_persona:default").state, "needs_attention")

    def test_invalid_location_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], station_lat=200.0)
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("station_location").state, "needs_attention")

    def test_valid_location_is_ready(self):
        WeatherConfig.load()
        snap = self.snapshot()
        self.assertEqual(snap.get("station_location").state, "ready")

    def test_invalid_nws_zone_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], nws_alert_zone="???")
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("nws_config").state, "needs_attention")

    def test_valid_nws_config_is_ready(self):
        WeatherConfig.load()
        snap = self.snapshot()
        self.assertEqual(snap.get("nws_config").state, "ready")

    def test_alert_beep_disabled_is_optional_disabled(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=False)
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "optional_disabled")

    def test_alert_beep_enabled_no_cart_is_needs_attention(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True)
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "needs_attention")

    def test_alert_beep_enabled_valid_cart_is_ready(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            cart = FXCart.objects.create(name="Beep", filepath=f.name)
            WeatherConfig.objects.create(
                pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
            )
            WeatherVoicePersona.objects.create(slot="default")
            snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "ready")

    def test_alert_beep_cart_media_unavailable_is_needs_attention(self):
        cart = FXCart.objects.create(name="Beep", filepath="/nonexistent/beep.wav")
        WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
        )
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "needs_attention")

    def test_alert_beep_enabled_empty_trigger_list_is_optional_disabled(self):
        """r0053 review amendment: an intentional empty trigger list is
        a valid configuration ('will never fire'), not an error --
        checked BEFORE cart readiness, since cart state is moot if
        nothing can ever trigger it."""
        cart = FXCart.objects.create(name="Beep", filepath="/nonexistent/beep.wav")  # even a broken cart...
        WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
            alert_sound_trigger_events=[],
        )
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        fact = snap.get("alert_fx_cart")
        self.assertEqual(fact.state, "optional_disabled")  # ...never surfaces as needs_attention here
        self.assertIn("will not fire", fact.summary)

    def test_alert_beep_enabled_non_list_trigger_config_is_needs_attention(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True)
        WeatherVoicePersona.objects.create(slot="default")
        # Simulate malformed persisted JSON bypassing the Admin form's
        # own guarantees -- diagnostics must still handle it safely.
        WeatherConfig.objects.filter(pk=1).update(alert_sound_trigger_events="Tornado Warning")
        snap = self.snapshot()
        fact = snap.get("alert_fx_cart")
        self.assertEqual(fact.state, "needs_attention")
        self.assertIn("malformed", fact.summary.lower())

    def test_alert_beep_enabled_list_with_non_string_entry_is_needs_attention(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True)
        WeatherVoicePersona.objects.create(slot="default")
        WeatherConfig.objects.filter(pk=1).update(alert_sound_trigger_events=["Tornado Warning", 123])
        snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "needs_attention")

    def test_alert_beep_enabled_blank_string_only_list_is_needs_attention(self):
        cfg = WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True)
        WeatherVoicePersona.objects.create(slot="default")
        WeatherConfig.objects.filter(pk=1).update(alert_sound_trigger_events=["", "   "])
        snap = self.snapshot()
        self.assertEqual(snap.get("alert_fx_cart").state, "needs_attention")

    def test_alert_beep_valid_trigger_list_evidence_includes_count_and_events(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            cart = FXCart.objects.create(name="WXAlert Beeps", filepath=f.name)
            WeatherConfig.objects.create(
                pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
                alert_sound_trigger_events=["Tornado Warning", "Severe Thunderstorm Warning",
                                            "Tornado Watch", "Severe Thunderstorm Watch"],
            )
            WeatherVoicePersona.objects.create(slot="default")
            snap = self.snapshot()
        fact = snap.get("alert_fx_cart")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.evidence["trigger_event_count"], 4)
        self.assertEqual(len(fact.evidence["trigger_events"]), 4)
        self.assertIn("4 trigger event type", fact.summary)
        self.assertIn("WXAlert Beeps", fact.summary)

    def test_stored_none_trigger_list_evaluates_effective_four_defaults(self):
        """r0053 migration-compatibility correction: a stored NULL is
        deliberate (see normalize_alert_sound_trigger_events()), not
        malformed -- it must proceed straight into normal cart
        readiness using the effective four legacy defaults, never
        reported as needs_attention nor as optional_disabled."""
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            cart = FXCart.objects.create(name="WXAlert Beeps", filepath=f.name)
            WeatherConfig.objects.create(
                pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
                alert_sound_trigger_events=None,
            )
            WeatherVoicePersona.objects.create(slot="default")
            snap = self.snapshot()
        fact = snap.get("alert_fx_cart")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.evidence["trigger_event_count"], 4)
        self.assertIn("Tornado Warning", fact.evidence["trigger_events"])

    def test_generated_artifact_wx_alert_fact_is_not_mixed_with_alert_fx_cart(self):
        """Keeps the two facts distinct per the r0053 review amendment:
        alert_fx_cart is the repeating FX Cart beep; generated_artifact:
        wx_alert is the spoken urgent-alert artifact. Neither's evidence
        should leak the other's identity."""
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            cart = FXCart.objects.create(name="WXAlert Beeps", filepath=f.name)
            WeatherConfig.objects.create(
                pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=True, alert_sound_cart=cart,
            )
            WeatherVoicePersona.objects.create(slot="default")
            snap = self.snapshot()
        cart_fact = snap.get("alert_fx_cart")
        wx_alert_fact = snap.get("generated_artifact:wx_alert")
        self.assertIsNotNone(wx_alert_fact)
        self.assertNotEqual(cart_fact.key, wx_alert_fact.key)
        self.assertNotIn("trigger_events", wx_alert_fact.evidence or {})

    def test_blank_notify_email_is_optional_disabled(self):
        WeatherConfig.load()
        snap = self.snapshot()
        self.assertEqual(snap.get("notifications").state, "optional_disabled")

    def test_configured_notify_email_is_ready(self):
        WeatherConfig.objects.create(
            pk=1, voice_schedule=[["default", 0, 23]], notify_email="ops@example.invalid",
        )
        WeatherVoicePersona.objects.create(slot="default")
        snap = self.snapshot()
        self.assertEqual(snap.get("notifications").state, "ready")

    def test_amber_disabled_is_optional_disabled(self):
        WeatherConfig.load()
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_config").state, "optional_disabled")
        self.assertEqual(snap.get("amber_alerts_data").state, "optional_disabled")

    def test_amber_enabled_valid_is_ready(self):
        WeatherConfig.load()
        AmberAlertConfig.objects.create(pk=1, enabled=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_config").state, "ready")

    def test_amber_enabled_missing_codes_is_needs_attention(self):
        WeatherConfig.load()
        AmberAlertConfig.objects.create(pk=1, enabled=True, event_codes="", same_codes="")
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_config").state, "needs_attention")


class DiagnosticsDataDirectoryTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        WeatherConfig.load()

    def snapshot(self, data_dir=None, now=NOW):
        return get_weather_diagnostics(now=now, data_dir=data_dir or self.data_dir)

    def test_directory_missing(self):
        snap = self.snapshot(data_dir=self.data_dir / "does-not-exist")
        self.assertEqual(snap.get("weather_data_dir").state, "needs_attention")

    def test_directory_present_and_readable(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("weather_data_dir").state, "ready")

    def test_files_missing_reports_degraded_not_crash(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("weather_data_file:latest_weather.json").state, "degraded")
        self.assertEqual(snap.get("weather_data_file:wind_history.json").state, "degraded")
        self.assertEqual(snap.get("weather_data_file:smoothed_wind.json").state, "degraded")

    def test_malformed_json_is_needs_attention_not_silently_healthy(self):
        (self.data_dir / "latest_weather.json").write_text("{not json")
        snap = self.snapshot()
        fact = snap.get("weather_data_file:latest_weather.json")
        self.assertEqual(fact.state, "needs_attention")
        self.assertIn("malformed", fact.detail.lower())

    def test_valid_json_simple_file_is_ready_with_mtime_evidence(self):
        write_json(self.data_dir / "processed_weather.json", {"tempf": 72})
        snap = self.snapshot()
        fact = snap.get("weather_data_file:processed_weather.json")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.evidence["timestamp_source"], "file_mtime")
        self.assertIsNotNone(fact.age_seconds)

    def test_simple_json_file_age_uses_injected_now_deterministically(self):
        """r0051: _check_simple_json_file() previously called
        dj_timezone.now() internally instead of using the snapshot's own
        injected `now` -- this pins a file's mtime exactly and proves
        the resulting age_seconds is exact, not merely non-null, for
        BOTH files that function covers (processed_weather.json and
        sky_condition.json)."""
        import os
        for filename in ("processed_weather.json", "sky_condition.json"):
            path = self.data_dir / filename
            write_json(path, {"ok": True})
            fixed_mtime = (NOW - timedelta(hours=3)).timestamp()
            os.utime(path, (fixed_mtime, fixed_mtime))

        snap = self.snapshot(now=NOW)

        for filename in ("processed_weather.json", "sky_condition.json"):
            fact = snap.get(f"weather_data_file:{filename}")
            self.assertEqual(fact.age_seconds, 10800.0, filename)

    def test_semantic_timestamp_used_over_mtime_for_latest_weather(self):
        ts = (NOW - timedelta(seconds=437)).isoformat().replace("+00:00", "Z")
        write_json(self.data_dir / "latest_weather.json", {"tempf": 72, "timestamp": ts})
        snap = self.snapshot()
        fact = snap.get("weather_data_file:latest_weather.json")
        self.assertEqual(fact.evidence["timestamp_source"], "payload.timestamp")
        self.assertAlmostEqual(fact.age_seconds, 437, delta=1)

    def test_deterministic_age_seconds_given_injected_now(self):
        ts = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        write_json(self.data_dir / "smoothed_wind.json", {"time": ts, "speed": 5})
        snap = self.snapshot()
        fact = snap.get("weather_data_file:smoothed_wind.json")
        self.assertEqual(fact.age_seconds, 7200.0)

    def test_wind_history_uses_latest_entry_timestamp(self):
        older = (NOW - timedelta(minutes=20)).isoformat()
        newer = (NOW - timedelta(minutes=1)).isoformat()
        write_json(self.data_dir / "wind_history.json", [
            {"time": older, "dir": 10, "speed": 5, "gust": 6},
            {"time": newer, "dir": 12, "speed": 6, "gust": 7},
        ])
        snap = self.snapshot()
        fact = snap.get("weather_data_file:wind_history.json")
        self.assertEqual(fact.count, 2)
        self.assertAlmostEqual(fact.age_seconds, 60, delta=1)

    def test_mtime_fallback_clearly_labelled(self):
        write_json(self.data_dir / "smoothed_wind.json", {"speed": 5})  # no "time" key
        snap = self.snapshot()
        fact = snap.get("weather_data_file:smoothed_wind.json")
        self.assertEqual(fact.evidence["timestamp_source"], "file_mtime")


class DiagnosticsForecastCacheTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        WeatherConfig.load()

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir)

    def test_missing_cache_is_degraded(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("forecast_cache").state, "degraded")

    def test_malformed_cache_is_needs_attention(self):
        (self.data_dir / "wx_forecast_cache.json").write_text("not json")
        snap = self.snapshot()
        self.assertEqual(snap.get("forecast_cache").state, "needs_attention")

    def test_fresh_cache_under_six_hours_is_ready(self):
        path = self.data_dir / "wx_forecast_cache.json"
        write_json(path, [{"name": "Today", "detailedForecast": "Sunny"}])
        recent = (NOW - timedelta(hours=1)).timestamp()
        import os
        os.utime(path, (recent, recent))
        snap = self.snapshot()
        fact = snap.get("forecast_cache")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.count, 1)

    def test_stale_cache_over_existing_six_hour_boundary_is_degraded(self):
        path = self.data_dir / "wx_forecast_cache.json"
        write_json(path, [{"name": "Today", "detailedForecast": "Sunny"}])
        import os
        stale = (NOW - timedelta(hours=FORECAST_CACHE_WARN_HOURS + 1)).timestamp()
        os.utime(path, (stale, stale))
        snap = self.snapshot()
        fact = snap.get("forecast_cache")
        self.assertEqual(fact.state, "degraded")
        self.assertIn(f"{FORECAST_CACHE_WARN_HOURS}h warning", fact.summary)


class DiagnosticsAlertsTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        WeatherConfig.load()

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir)

    def test_watch_warning_file_absent_is_degraded_not_needs_attention(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("watch_warnings").state, "degraded")

    def test_valid_empty_list_is_ready(self):
        write_json(self.data_dir / "active_watches_warnings.json", [])
        snap = self.snapshot()
        fact = snap.get("watch_warnings")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.count, 0)

    def test_valid_populated_list_is_ready_with_count(self):
        write_json(self.data_dir / "active_watches_warnings.json", [
            {"event": "Tornado Warning", "text": "...", "text_core": "..."},
        ])
        snap = self.snapshot()
        fact = snap.get("watch_warnings")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.count, 1)
        self.assertEqual(fact.evidence["events"], ["Tornado Warning"])

    def test_malformed_watch_warning_json_is_needs_attention(self):
        (self.data_dir / "active_watches_warnings.json").write_text("{bad")
        snap = self.snapshot()
        self.assertEqual(snap.get("watch_warnings").state, "needs_attention")


class DiagnosticsAmberDataTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        WeatherConfig.load()

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir)

    def test_disabled_no_file_is_optional_disabled(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_data").state, "optional_disabled")

    def test_enabled_no_file_is_degraded(self):
        AmberAlertConfig.objects.create(pk=1, enabled=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_data").state, "degraded")

    def test_enabled_invalid_file_is_needs_attention(self):
        AmberAlertConfig.objects.create(pk=1, enabled=True)
        (self.data_dir / "active_amber_alerts.json").write_text("{bad")
        snap = self.snapshot()
        self.assertEqual(snap.get("amber_alerts_data").state, "needs_attention")

    def test_enabled_valid_active_list_is_ready(self):
        AmberAlertConfig.objects.create(pk=1, enabled=True)
        write_json(self.data_dir / "active_amber_alerts.json", [
            {"identifier": "x1", "event": "Child Abduction Emergency", "text": "...", "text_core": "..."},
        ])
        snap = self.snapshot()
        fact = snap.get("amber_alerts_data")
        self.assertEqual(fact.state, "ready")
        self.assertEqual(fact.count, 1)


class DiagnosticsGeneratedArtifactTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-lib-")
        self.addCleanup(self.tmp.cleanup)
        self.library_root = Path(self.tmp.name)
        self.data_tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-data-")
        self.addCleanup(self.data_tmp.cleanup)
        self.data_dir = Path(self.data_tmp.name)
        WeatherConfig.load()
        self.category = make_category("WxTemp")

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir, library_root=self.library_root)

    def _artifact_path(self):
        path = self.library_root / "WxTemp" / "current_temp.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_file_present_track_present_ready(self):
        path = self._artifact_path()
        path.write_bytes(b"id3")
        make_track(path, self.category, ready2air=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_temp").state, "ready")

    def test_file_present_track_missing_needs_attention(self):
        path = self._artifact_path()
        path.write_bytes(b"id3")
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_temp").state, "needs_attention")

    def test_track_present_file_missing_needs_attention(self):
        path = self._artifact_path()
        make_track(path, self.category, ready2air=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_temp").state, "needs_attention")

    def test_track_not_ready_to_air_needs_attention(self):
        path = self._artifact_path()
        path.write_bytes(b"id3")
        make_track(path, self.category, ready2air=False)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_temp").state, "needs_attention")

    def test_recurring_artifact_never_generated_is_degraded(self):
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_temp").state, "degraded")

    # generated_artifact:wx_alert (the shared, event-driven WxAlert
    # artifact) is NOT covered here -- its state depends on active-alert
    # applicability, not merely file/Track presence. See
    # DiagnosticsWxAlertApplicabilityTests below (r0051).


class DiagnosticsWxAlertApplicabilityTests(TestCase):
    """r0051: WxAlert/wx_alert.mp3 is the urgent spoken statement shared
    by BOTH ordinary NWS watch/warning alerts and AMBER-family alerts
    (never the FX "alert beep", which is `alert_fx_cart`). A historical
    file/Track surviving after an alert ends must not be reported as a
    currently `ready` artifact -- see diagnostics.py's
    _classify_alert_applicability() / _check_wx_alert_artifact()."""

    def setUp(self):
        self.lib_tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-alertlib-")
        self.addCleanup(self.lib_tmp.cleanup)
        self.library_root = Path(self.lib_tmp.name)
        self.data_tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-alertdata-")
        self.addCleanup(self.data_tmp.cleanup)
        self.data_dir = Path(self.data_tmp.name)
        WeatherConfig.load()
        self.category = make_category("WxAlert")

    def snapshot(self):
        return get_weather_diagnostics(now=NOW, data_dir=self.data_dir, library_root=self.library_root)

    def _write_watch_warnings(self, entries):
        write_json(self.data_dir / "active_watches_warnings.json", entries)

    def _configure_amber(self, enabled, entries=None):
        AmberAlertConfig.objects.create(pk=1, enabled=enabled)
        if entries is not None:
            write_json(self.data_dir / "active_amber_alerts.json", entries)

    def _make_artifact(self, ready2air=True):
        path = self.library_root / "WxAlert" / "wx_alert.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"id3")
        make_track(path, self.category, ready2air=ready2air)
        return path

    def test_no_active_alerts_and_artifact_absent_is_not_applicable(self):
        self._write_watch_warnings([])  # confirmed zero; AMBER left unconfigured (disabled)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "not_applicable")

    def test_no_active_alerts_and_old_file_track_ready_is_not_applicable(self):
        self._write_watch_warnings([])
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        fact = snap.get("generated_artifact:wx_alert")
        self.assertEqual(fact.state, "not_applicable")
        self.assertIn("historical", fact.summary.lower())
        # historical evidence is preserved, not discarded
        self.assertIsNotNone(fact.path)
        self.assertIsNotNone(fact.age_seconds)

    def test_active_weather_warning_and_artifact_ready_is_ready(self):
        self._write_watch_warnings([{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "ready")

    def test_active_amber_alert_and_artifact_ready_is_ready(self):
        self._write_watch_warnings([])
        self._configure_amber(True, entries=[
            {"identifier": "x1", "event": "Child Abduction Emergency", "text": "...", "text_core": "..."},
        ])
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "ready")

    def test_active_alert_and_artifact_missing_is_needs_attention(self):
        self._write_watch_warnings([{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        snap = self.snapshot()  # no file/Track created at all
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "needs_attention")

    def test_active_alert_and_track_missing_is_needs_attention(self):
        self._write_watch_warnings([{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        path = self.library_root / "WxAlert" / "wx_alert.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"id3")  # file present, no Track
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "needs_attention")

    def test_unknown_active_state_does_not_become_not_applicable(self):
        # active_watches_warnings.json is missing entirely (degraded --
        # pipeline hasn't run yet, NOT confirmed zero) and AMBER is left
        # unconfigured (confirmed zero) -- overall must stay unknown.
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        fact = snap.get("generated_artifact:wx_alert")
        self.assertNotEqual(fact.state, "not_applicable")
        self.assertEqual(fact.state, "degraded")

    def test_malformed_watch_warnings_does_not_become_not_applicable(self):
        (self.data_dir / "active_watches_warnings.json").write_text("{bad")
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        self.assertNotEqual(snap.get("generated_artifact:wx_alert").state, "not_applicable")

    def test_amber_disabled_and_zero_weather_alerts_is_not_applicable(self):
        self._write_watch_warnings([])
        self._configure_amber(False)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "not_applicable")

    def test_amber_disabled_and_active_weather_alert_is_active(self):
        self._write_watch_warnings([{"event": "Tornado Warning", "text": "...", "text_core": "..."}])
        self._configure_amber(False)
        self._make_artifact(ready2air=True)
        snap = self.snapshot()
        self.assertEqual(snap.get("generated_artifact:wx_alert").state, "ready")


class DiagnosticsSideEffectTests(TestCase):
    """Proves collection is genuinely side-effect-free (Pass B section
    10, strengthened r0051). No HTTP, no TTS, no subprocess, no writes,
    no Track/config mutation -- and specifically, no singleton
    auto-creation via WeatherConfig.load()/AmberAlertConfig.load() just
    because diagnostics ran against a database with none of that
    configured yet."""

    def test_no_subprocess_calls(self):
        WeatherConfig.load()
        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            get_weather_diagnostics(now=NOW)
        run.assert_not_called()
        popen.assert_not_called()

    def test_no_network_requests(self):
        WeatherConfig.load()
        with patch("requests.get") as rget, patch("requests.post") as rpost, \
             patch("urllib.request.urlopen") as uopen:
            get_weather_diagnostics(now=NOW)
        rget.assert_not_called()
        rpost.assert_not_called()
        uopen.assert_not_called()

    def test_no_config_or_track_mutation(self):
        # Deliberately NOT pre-creating WeatherConfig here -- a test
        # claiming "diagnostics doesn't mutate config" must not itself
        # create the very row it's proving isn't created.
        before = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(), Track.objects.count(),
        )
        get_weather_diagnostics(now=NOW)
        after = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(), Track.objects.count(),
        )
        self.assertEqual(before, after)

    def test_zero_configuration_rows_causes_no_mutation_and_returns_useful_facts(self):
        """The r0051 regression test for the critical fix: running
        diagnostics against a database with NO WeatherConfig, NO
        AmberAlertConfig, and NO WeatherVoicePersona must not create any
        of them (WeatherConfig.load()/AmberAlertConfig.load() are
        get_or_create and would have), and must still return a useful,
        non-exceptional snapshot."""
        self.assertFalse(WeatherConfig.objects.exists())
        self.assertFalse(AmberAlertConfig.objects.exists())
        self.assertFalse(WeatherVoicePersona.objects.exists())
        before = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(), Track.objects.count(),
        )

        snapshot = get_weather_diagnostics(now=NOW)  # must not raise

        after = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(), Track.objects.count(),
        )
        self.assertEqual(before, after)
        self.assertEqual(before, (0, 0, 0, 0))

        # Useful, clearly-actionable structured facts -- not a crash,
        # and AMBER is never reported as if it were actively configured.
        self.assertEqual(snapshot.get("station_location").state, "needs_attention")
        self.assertEqual(snapshot.get("nws_config").state, "needs_attention")
        self.assertEqual(snapshot.get("announcer_schedule").state, "needs_attention")
        self.assertEqual(snapshot.get("alert_fx_cart").state, "needs_attention")
        self.assertEqual(snapshot.get("notifications").state, "needs_attention")
        self.assertEqual(snapshot.get("amber_alerts_config").state, "optional_disabled")
        self.assertEqual(snapshot.get("amber_alerts_data").state, "optional_disabled")

    def test_does_not_write_into_weather_data_dir(self):
        with tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-safety-") as tmp:
            data_dir = Path(tmp)
            WeatherConfig.load()
            get_weather_diagnostics(now=NOW, data_dir=data_dir)
            self.assertEqual(list(data_dir.iterdir()), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class WeatherEnvAdminUsesDiagnosticsTests(TestCase):
    """Pass B section 12: the Admin "Weather data storage" subpage
    consumes weather.diagnostics instead of its own hardcoded file list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-admin-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.staff = User.objects.create_superuser("wxdiagadmin", "x@example.invalid", "pw")
        self.client.force_login(self.staff)
        WeatherConfig.load()
        from isadoraair import env_config
        patcher = patch.object(env_config, "ENV_FILE_PATH", Path(self.tmp.name) / ".env")
        patcher.start()
        self.addCleanup(patcher.stop)
        env_config.ENV_FILE_PATH.write_text(f"WEATHER_DATA_DIR={self.data_dir}\n")

    def test_page_reports_present_and_missing_via_diagnostics(self):
        write_json(self.data_dir / "latest_weather.json", {"tempf": 70})
        url = reverse("admin:weather_weatherconfig_weather_env")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("latest_weather.json", body)
        self.assertIn("wind_history.json", body)

    def test_malformed_but_present_file_is_not_described_as_missing(self):
        """r0051: a physically present but malformed file must be
        distinguished from a genuinely absent one -- both used to
        collapse into the same 'Missing' bucket because the page only
        checked `state == 'ready'`."""
        (self.data_dir / "latest_weather.json").write_text("{not json")
        url = reverse("admin:weather_weatherconfig_weather_env")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Present but failed to parse", body)
        # The malformed file must not appear in the "Missing" clause --
        # check the literal adjacency, not just "is the word present
        # somewhere on the page" (it legitimately is, in the other clause).
        self.assertNotIn(
            "Missing (optional -- not required to save this page): latest_weather.json",
            body,
        )


class WeatherDiagnosticsCommandTests(TestCase):
    def test_human_output_lists_facts_and_exits_zero_when_clean(self):
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=False)
        with tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-cmd-") as tmp, \
                tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-cmd-lib-") as lib, \
                override_settings(WEATHER_DATA_DIR=tmp, LIBRARY_ROOT=lib):
            out = StringIO()
            call_command("weather_diagnostics", stdout=out)
        self.assertIn("announcer_schedule", out.getvalue())

    def test_json_output_is_valid_and_matches_schema_keys(self):
        WeatherConfig.load()
        out = StringIO()
        try:
            call_command("weather_diagnostics", "--json", stdout=out)
        except SystemExit:
            pass
        payload = json.loads(out.getvalue())
        self.assertIn("generated_at", payload)
        self.assertIn("facts", payload)
        keys = {f["key"] for f in payload["facts"]}
        self.assertIn("station_location", keys)
        self.assertIn("weather_data_dir", keys)

    def test_needs_attention_present_causes_nonzero_exit(self):
        WeatherConfig.load()  # default persona, no voice -> needs_attention
        out = StringIO()
        with self.assertRaises(SystemExit) as ctx:
            call_command("weather_diagnostics", stdout=out)
        self.assertEqual(ctx.exception.code, 1)

    def test_all_optional_disabled_does_not_cause_failure_exit(self):
        voice = make_voice()
        WeatherVoicePersona.objects.create(slot="default", tts_voice=voice)
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]], alert_sound_enabled=False)
        with tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-cmd-") as tmp, \
                tempfile.TemporaryDirectory(prefix="isadoraair-wxdiag-cmd-lib-") as lib, \
                override_settings(WEATHER_DATA_DIR=tmp, LIBRARY_ROOT=lib):
            out = StringIO()
            try:
                call_command("weather_diagnostics", stdout=out)
            except SystemExit as exc:
                self.fail(f"unexpected nonzero exit with a fully-ready config: {exc.code}")
