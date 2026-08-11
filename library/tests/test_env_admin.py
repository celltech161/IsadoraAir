"""Phase 2 (2026-08-11) admin-editable environment configuration --
library-app-owned settings: MUSICBRAINZ_CONTACT/LIBRARY_ROOT/
WAVEFORMS_DIR on CDRipConfig's "Library storage settings" sub-page, and
REPORTS_ROOT on StationInfo's "Reports storage settings" sub-page. Both
built on the shared isadoraair/env_admin.py helper introduced this
phase; see monitoring/tests/test_smtp_env_admin.py for the Phase 1
precedent this mirrors.

Every test patches env_config.ENV_FILE_PATH to a per-test temp file --
never the real project .env. No test creates/moves/deletes anything
under a real production path."""
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from isadoraair import env_config
from library.models import Artist, RoyaltyReport, Track
from monitoring.models import SystemEvent


@override_settings(SECURE_SSL_REDIRECT=False)
class EnvAdminTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="isadoraair-libenvtest-")
        self.addCleanup(self._tmpdir.cleanup)
        self.env_path = Path(self._tmpdir.name) / ".env"
        patcher = patch.object(env_config, "ENV_FILE_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.staff = User.objects.create_superuser("libenvadmin", "libenv@example.invalid", "pw")
        self.client.force_login(self.staff)

    def write_env(self, text):
        self.env_path.write_text(text, encoding="utf-8")


class CDRipConfigLibraryEnvAdminTests(EnvAdminTestCase):
    def setUp(self):
        super().setUp()
        from library.models import CDRipConfig
        self.config = CDRipConfig.load()

    def url(self):
        return reverse("admin:library_cdripconfig_library_env")

    def post_form(self, **overrides):
        data = {
            "musicbrainz_contact": "ops@example.com",
            "library_root": "/srv/isadoraair/music",
            "waveforms_dir": "/srv/isadoraair/waveforms",
        }
        data.update(overrides)
        return self.client.post(self.url(), data, follow=True)

    def test_superuser_can_access(self):
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_rejected(self):
        self.client.logout()
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 302)

    def test_link_present_on_cdripconfig_changeform(self):
        resp = self.client.get(reverse("admin:library_cdripconfig_change", args=[self.config.pk]))
        html = resp.content.decode()
        self.assertIn("Edit library storage settings", html)
        self.assertIn(self.url(), html)

    def test_disk_values_populate_fields(self):
        self.write_env(
            "MUSICBRAINZ_CONTACT=fromdisk@example.com\n"
            "LIBRARY_ROOT=/srv/fromdisk/music\n"
            "WAVEFORMS_DIR=/srv/fromdisk/waveforms\n"
        )
        html = self.client.get(self.url()).content.decode()
        self.assertIn("fromdisk@example.com", html)
        self.assertIn("/srv/fromdisk/music", html)
        self.assertIn("/srv/fromdisk/waveforms", html)

    def test_missing_file_shows_registered_defaults(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("/srv/isadoraair/music", html)
        self.assertIn("/srv/isadoraair/waveforms", html)

    def test_no_strand_warning_when_no_tracks_exist(self):
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn("split library", html)

    def test_strand_warning_shown_when_tracks_exist(self):
        artist = Artist.objects.create(name="Test Artist")
        Track.objects.create(filepath="/srv/isadoraair/music/x.flac", filename="x.flac", title="X", artist=artist)
        html = self.client.get(self.url()).content.decode()
        self.assertIn("split library", html)
        self.assertIn("does NOT move or break existing tracks", html)

    def test_valid_save_writes_env(self):
        self.write_env("LIBRARY_ROOT=/old/root\n")
        resp = self.post_form(library_root="/new/root")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("LIBRARY_ROOT=/new/root", self.env_path.read_text())
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Saved" in m for m in messages))

    def test_relative_path_rejected_gracefully(self):
        self.write_env("LIBRARY_ROOT=/old/root\n")
        resp = self.post_form(library_root="relative/path")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))
        self.assertIn("LIBRARY_ROOT=/old/root", self.env_path.read_text())

    def test_malformed_musicbrainz_contact_rejected(self):
        self.write_env("MUSICBRAINZ_CONTACT=old@example.com\n")
        resp = self.post_form(musicbrainz_contact="not-an-email")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))
        self.assertIn("MUSICBRAINZ_CONTACT=old@example.com", self.env_path.read_text())

    def test_no_op_save_generates_no_event(self):
        self.write_env(
            "MUSICBRAINZ_CONTACT=ops@example.com\n"
            "LIBRARY_ROOT=/srv/isadoraair/music\n"
            "WAVEFORMS_DIR=/srv/isadoraair/waveforms\n"
        )
        self.post_form()
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="library|env-updated").exists())

    def test_successful_save_records_changed_keys(self):
        self.write_env("LIBRARY_ROOT=/old/root\n")
        self.post_form(library_root="/new/root")
        event = SystemEvent.objects.get(dedupe_key="library|env-updated")
        self.assertEqual(event.level, "info")
        self.assertIn("LIBRARY_ROOT", event.detail["changed_keys"])
        self.assertEqual(event.detail["changed_by"], "libenvadmin")

    def test_restart_banner_reflects_saved_vs_running_mismatch(self):
        self.write_env("LIBRARY_ROOT=/on-disk\nMUSICBRAINZ_CONTACT=\nWAVEFORMS_DIR=/srv/isadoraair/waveforms\n")
        with patch.object(env_config.django_settings, "LIBRARY_ROOT", "/on-disk"), \
             patch.object(env_config.django_settings, "MUSICBRAINZ_CONTACT", ""), \
             patch.object(env_config.django_settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"):
            html = self.client.get(self.url()).content.decode()
        self.assertIn("Running configuration matches saved configuration.", html)


class StationInfoReportsEnvAdminTests(EnvAdminTestCase):
    def setUp(self):
        super().setUp()
        from library.models import StationInfo
        self.config = StationInfo.load()

    def url(self):
        return reverse("admin:library_stationinfo_reports_env")

    def post_form(self, **overrides):
        data = {"reports_root": "/var/lib/isadoraair/reports"}
        data.update(overrides)
        return self.client.post(self.url(), data, follow=True)

    def test_superuser_can_access(self):
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_rejected(self):
        self.client.logout()
        resp = self.client.get(self.url())
        self.assertEqual(resp.status_code, 302)

    def test_link_present_on_stationinfo_changeform(self):
        resp = self.client.get(reverse("admin:library_stationinfo_change", args=[self.config.pk]))
        html = resp.content.decode()
        self.assertIn("Edit reports storage settings", html)
        self.assertIn(self.url(), html)

    def test_disk_value_populates_field(self):
        self.write_env("REPORTS_ROOT=/srv/fromdisk/reports\n")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("/srv/fromdisk/reports", html)

    def test_no_move_warning_when_no_reports_exist(self):
        html = self.client.get(self.url()).content.decode()
        self.assertNotIn("stop being", html)

    def test_move_warning_shown_when_reports_exist(self):
        RoyaltyReport.objects.create(period_start=date(2026, 1, 1), period_end=date(2026, 1, 31), format="summary")
        html = self.client.get(self.url()).content.decode()
        self.assertIn("stop being", html)
        self.assertIn("moved to the new directory by hand", html)

    def test_valid_save_writes_env(self):
        self.write_env("REPORTS_ROOT=/old/reports\n")
        resp = self.post_form(reports_root="/new/reports")
        self.assertIn("REPORTS_ROOT=/new/reports", self.env_path.read_text())
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Saved" in m for m in messages))

    def test_relative_path_rejected_gracefully(self):
        self.write_env("REPORTS_ROOT=/old/reports\n")
        resp = self.post_form(reports_root="relative")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))
        self.assertIn("REPORTS_ROOT=/old/reports", self.env_path.read_text())

    def test_target_is_a_file_rejected(self):
        bad_target = Path(self._tmpdir.name) / "a_file"
        bad_target.write_text("x", encoding="utf-8")
        self.write_env("REPORTS_ROOT=/old/reports\n")
        resp = self.post_form(reports_root=str(bad_target))
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))

    def test_no_op_save_generates_no_event(self):
        self.write_env("REPORTS_ROOT=/var/lib/isadoraair/reports\n")
        self.post_form()
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="library|reports-env-updated").exists())

    def test_successful_save_records_changed_keys_and_audit_category(self):
        self.write_env("REPORTS_ROOT=/old/reports\n")
        self.post_form(reports_root="/new/reports")
        event = SystemEvent.objects.get(dedupe_key="library|reports-env-updated")
        self.assertEqual(event.category, "library")
        self.assertEqual(event.detail["changed_keys"], ["REPORTS_ROOT"])

    def test_does_not_touch_stationinfo_db_fields(self):
        self.config.legal_name = "Keep Me"
        self.config.save()
        self.write_env("REPORTS_ROOT=/old/reports\n")
        self.post_form(reports_root="/new/reports")
        from library.models import StationInfo
        refreshed = StationInfo.objects.get(pk=self.config.pk)
        self.assertEqual(refreshed.legal_name, "Keep Me")


class Phase2EnvAdminIsolationTests(EnvAdminTestCase):
    """Confirms the Phase 2 pages never touch the real .env, and that
    library.admin's other model changeform pages are unaffected."""

    def test_changeform_pages_do_not_read_env_file(self):
        from library.models import CDRipConfig, StationInfo
        cdrip = CDRipConfig.load()
        station = StationInfo.load()
        self.assertFalse(self.env_path.exists())
        self.client.get(reverse("admin:library_cdripconfig_change", args=[cdrip.pk]))
        self.client.get(reverse("admin:library_stationinfo_change", args=[station.pk]))
        self.assertFalse(self.env_path.exists())
