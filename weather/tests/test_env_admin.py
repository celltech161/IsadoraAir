"""Phase 2 (2026-08-11) admin-editable environment configuration --
weather-app-owned WEATHER_DATA_DIR on WeatherConfig's "Weather data
storage" sub-page. Built on the shared isadoraair/env_admin.py helper;
see monitoring/tests/test_smtp_env_admin.py (Phase 1) and
library/tests/test_env_admin.py (Phase 2) for the same pattern.

Every test patches env_config.ENV_FILE_PATH to a per-test temp file --
never the real project .env. No test writes into any real weather data
directory."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from isadoraair import env_config
from monitoring.models import SystemEvent
from weather.models import WeatherConfig


@override_settings(SECURE_SSL_REDIRECT=False)
class WeatherEnvAdminTests(TestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="isadoraair-wxenvtest-")
        self.addCleanup(self._tmpdir.cleanup)
        self.env_path = Path(self._tmpdir.name) / ".env"
        patcher = patch.object(env_config, "ENV_FILE_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.staff = User.objects.create_superuser("wxenvadmin", "wxenv@example.invalid", "pw")
        self.client.force_login(self.staff)
        self.config = WeatherConfig.load()

    def write_env(self, text):
        self.env_path.write_text(text, encoding="utf-8")

    def url(self):
        return reverse("admin:weather_weatherconfig_weather_env")

    def post_form(self, **overrides):
        data = {"weather_data_dir": "/var/lib/isadoraair/weather"}
        data.update(overrides)
        return self.client.post(self.url(), data, follow=True)

    def test_superuser_can_access(self):
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_rejected(self):
        self.client.logout()
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 302)

    def test_link_present_on_weatherconfig_changeform(self):
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        html = resp.content.decode()
        self.assertIn("Edit weather data storage", html)
        self.assertIn(self.url(), html)

    def test_disk_value_populates_field(self):
        self.write_env("WEATHER_DATA_DIR=/srv/fromdisk/weather\n")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("/srv/fromdisk/weather", html)

    def test_external_companion_project_warning_always_shown(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("weather-ingest companion project", html)
        self.assertIn("does not update that external project", html)

    def test_diagnostic_file_status_reports_none_yet_for_empty_directory(self):
        data_dir = Path(self._tmpdir.name) / "wxdata"
        data_dir.mkdir()
        self.write_env(f"WEATHER_DATA_DIR={data_dir}\n")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("none yet", html)

    def test_diagnostic_file_status_reports_present_files(self):
        data_dir = Path(self._tmpdir.name) / "wxdata2"
        data_dir.mkdir()
        (data_dir / "latest_weather.json").write_text("{}", encoding="utf-8")
        self.write_env(f"WEATHER_DATA_DIR={data_dir}\n")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("latest_weather.json", html)
        self.assertIn("wind_history.json", html)  # listed as missing

    def test_missing_directory_does_not_block_page_render(self):
        """No mandatory-file gate -- doesn't exist yet is not fatal."""
        nonexistent = Path(self._tmpdir.name) / "does" / "not" / "exist"
        self.write_env(f"WEATHER_DATA_DIR={nonexistent}\n")
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)

    def test_valid_save_writes_env(self):
        self.write_env("WEATHER_DATA_DIR=/old/weather\n")
        resp = self.post_form(weather_data_dir="/new/weather")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("WEATHER_DATA_DIR=/new/weather", self.env_path.read_text())
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Saved" in m for m in messages))

    def test_relative_path_rejected_gracefully(self):
        self.write_env("WEATHER_DATA_DIR=/old/weather\n")
        resp = self.post_form(weather_data_dir="relative/path")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))
        self.assertIn("WEATHER_DATA_DIR=/old/weather", self.env_path.read_text())

    def test_no_op_save_generates_no_event(self):
        self.write_env("WEATHER_DATA_DIR=/var/lib/isadoraair/weather\n")
        self.post_form()
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="weather|env-updated").exists())

    def test_successful_save_records_changed_keys_and_audit_category(self):
        self.write_env("WEATHER_DATA_DIR=/old/weather\n")
        self.post_form(weather_data_dir="/new/weather")
        event = SystemEvent.objects.get(dedupe_key="weather|env-updated")
        self.assertEqual(event.category, "weather")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.detail["changed_keys"], ["WEATHER_DATA_DIR"])
        self.assertEqual(event.detail["changed_by"], "wxenvadmin")

    def test_restart_banner_reflects_saved_vs_running_mismatch(self):
        self.write_env("WEATHER_DATA_DIR=/on-disk\n")
        with patch.object(env_config.django_settings, "WEATHER_DATA_DIR", "/still-running"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("Restart required", html)

    def test_does_not_touch_weatherconfig_db_fields(self):
        self.config.nws_alert_zone = "KSC999"
        self.config.save()
        self.write_env("WEATHER_DATA_DIR=/old/weather\n")
        self.post_form(weather_data_dir="/new/weather")
        refreshed = WeatherConfig.objects.get(pk=self.config.pk)
        self.assertEqual(refreshed.nws_alert_zone, "KSC999")

    def test_changeform_page_does_not_read_env_file(self):
        self.assertFalse(self.env_path.exists())
        self.client.get(reverse("admin:weather_weatherconfig_change", args=[self.config.pk]))
        self.assertFalse(self.env_path.exists())
