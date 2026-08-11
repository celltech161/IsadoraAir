"""NotificationConfigAdmin's SMTP settings sub-page (Phase 1 admin-
editable environment configuration layer) -- monitoring/admin.py's
smtp_env_view/_handle_smtp_env_post/_smtp_env_context, backed by
isadoraair/env_config.py.

Every test patches env_config.ENV_FILE_PATH to a per-test temp file
(never the real project .env) -- see EnvAdminTestCase below. No real
SMTP send happens anywhere in this file (nothing here calls
send_test_email/maybe_notify)."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from isadoraair import env_config
from monitoring.models import NotificationConfig, SystemEvent


@override_settings(SECURE_SSL_REDIRECT=False)
class EnvAdminTestCase(TestCase):
    """Shared setup: an isolated temp .env (env_config.ENV_FILE_PATH
    patched for the lifetime of each test method), a superuser client,
    and the loaded NotificationConfig singleton."""

    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory(prefix="isadoraair-smtpadmintest-")
        self.addCleanup(self._tmpdir.cleanup)
        self.env_path = Path(self._tmpdir.name) / ".env"
        patcher = patch.object(env_config, "ENV_FILE_PATH", self.env_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.staff = User.objects.create_superuser("smtpenvadmin", "smtpenv@example.invalid", "pw")
        self.client.force_login(self.staff)
        self.config = NotificationConfig.load()

    def write_env(self, text):
        self.env_path.write_text(text, encoding="utf-8")

    def smtp_url(self):
        return reverse("admin:monitoring_notificationconfig_smtp_env")

    def get_form(self):
        return self.client.get(self.smtp_url())

    def post_form(self, **overrides):
        data = {
            "email_host": "smtp.example.com",
            "email_port": "587",
            "email_host_user": "",
            "default_from_email": "alerts@example.com",
            "email_host_password": "",
            # email_use_tls deliberately omitted here (as a real
            # unchecked HTML checkbox would) -- callers that need TLS
            # checked pass email_use_tls="on" explicitly.
        }
        data.update(overrides)
        return self.client.post(self.smtp_url(), data, follow=True)

    # Baseline used by the "no real change" / "saved matches running"
    # tests -- covers all six managed keys so nothing left implicit
    # (e.g. the always-omitted-by-default email_use_tls checkbox, or a
    # key this test doesn't care about) can smuggle in an unintended
    # diff and flip a no-op test into a real write, or a "matches"
    # assertion into a false mismatch from an unrelated key.
    BASELINE_ENV = dict(
        EMAIL_HOST="baseline.example.com", EMAIL_PORT="587", EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="", EMAIL_USE_TLS="False", DEFAULT_FROM_EMAIL="baseline@example.com",
    )
    BASELINE_SETTINGS = dict(
        EMAIL_HOST="baseline.example.com", EMAIL_PORT=587, EMAIL_HOST_USER="",
        EMAIL_HOST_PASSWORD="", EMAIL_USE_TLS=False, DEFAULT_FROM_EMAIL="baseline@example.com",
    )
    BASELINE_POST = dict(
        email_host="baseline.example.com", email_port="587", email_host_user="",
        default_from_email="baseline@example.com", email_host_password="",
        # email_use_tls omitted -> unchecked -> "False", matching BASELINE_ENV above
    )

    def write_baseline_env(self, **overrides):
        values = dict(self.BASELINE_ENV, **overrides)
        self.write_env("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")

    def matching_settings(self, **overrides):
        return override_settings(**dict(self.BASELINE_SETTINGS, **overrides))


# ---------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------
class AccessControlTests(EnvAdminTestCase):
    def test_superuser_can_access(self):
        resp = self.get_form()
        self.assertEqual(resp.status_code, 200)

    def test_anonymous_rejected(self):
        self.client.logout()
        resp = self.get_form()
        self.assertEqual(resp.status_code, 302)

    def test_anonymous_post_rejected_and_does_not_write(self):
        self.write_env("EMAIL_HOST=untouched.example.com\n")
        self.client.logout()
        resp = self.client.post(self.smtp_url(), {"email_host": "hacked.example.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("untouched.example.com", self.env_path.read_text())


# ---------------------------------------------------------------------
# GET: disk values populate the form (requirement 13)
# ---------------------------------------------------------------------
class DiskValuePopulationTests(EnvAdminTestCase):
    def test_disk_values_shown_on_get(self):
        self.write_env(
            "EMAIL_HOST=fromdisk.example.com\nEMAIL_PORT=2525\n"
            "DEFAULT_FROM_EMAIL=disk@example.com\n"
        )
        html = self.get_form().content.decode()
        self.assertIn("fromdisk.example.com", html)
        self.assertIn("2525", html)
        self.assertIn("disk@example.com", html)

    def test_missing_file_shows_registered_defaults(self):
        html = self.get_form().content.decode()
        self.assertIn("localhost", html)
        self.assertIn("587", html)

    def test_saved_value_immediately_visible_even_with_stale_running_settings(self):
        """Core requirement 13 -- a save to .env must show up on GET
        even though this test process's own django.conf.settings (what
        a NOT-yet-restarted Gunicorn worker would still be using) is
        deliberately mocked to something completely different."""
        self.write_env("EMAIL_HOST=freshly-saved.example.com\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST", "stale-running-value.example.com"):
            html = self.get_form().content.decode()
        self.assertIn("freshly-saved.example.com", html)
        self.assertNotIn("stale-running-value.example.com", html)

    def test_password_field_is_always_blank_on_get_even_when_configured(self):
        self.write_env("EMAIL_HOST_PASSWORD=realsecretvalue123\n")
        html = self.get_form().content.decode()
        self.assertNotIn("realsecretvalue123", html)
        self.assertIn('name="email_host_password" value=""', html)

    def test_password_configured_status_shown_without_value(self):
        self.write_env("EMAIL_HOST_PASSWORD=realsecretvalue123\n")
        html = self.get_form().content.decode()
        self.assertIn("Currently configured", html)

    def test_password_not_configured_status_shown(self):
        self.write_env("EMAIL_HOST_PASSWORD=\n")
        html = self.get_form().content.decode()
        self.assertIn("Currently not configured", html)

    def test_duplicate_key_shows_actionable_error_and_no_form(self):
        self.write_env("EMAIL_HOST=first.example.com\nEMAIL_HOST=second.example.com\n")
        html = self.get_form().content.decode()
        self.assertIn("more than once", html)
        self.assertNotIn("Save SMTP settings", html)


# ---------------------------------------------------------------------
# Restart-required banner
# ---------------------------------------------------------------------
class RestartRequiredBannerTests(EnvAdminTestCase):
    """Every test here writes/patches ALL SIX managed keys via the
    shared BASELINE_ENV/BASELINE_SETTINGS fixtures (see EnvAdminTestCase)
    so an assertion about ONE key's match/mismatch can't be accidentally
    satisfied (or defeated) by an unrelated key that this test doesn't
    otherwise care about."""

    def test_matching_disk_and_running_shows_no_restart_needed(self):
        self.write_baseline_env()
        with self.matching_settings():
            html = self.get_form().content.decode()
        self.assertIn("Running configuration matches saved configuration.", html)

    def test_mismatched_disk_and_running_shows_restart_required(self):
        self.write_baseline_env(EMAIL_HOST="on-disk.example.com")
        with self.matching_settings(EMAIL_HOST="still-running.example.com"):
            html = self.get_form().content.decode()
        self.assertIn("Restart required", html)

    def test_port_int_vs_text_representation_does_not_false_trigger_banner(self):
        self.write_baseline_env(EMAIL_PORT="587")
        with self.matching_settings(EMAIL_PORT=587):
            html = self.get_form().content.decode()
        self.assertIn("Running configuration matches saved configuration.", html)

    def test_tls_bool_vs_text_representation_does_not_false_trigger_banner(self):
        self.write_baseline_env(EMAIL_USE_TLS="True")
        with self.matching_settings(EMAIL_USE_TLS=True):
            html = self.get_form().content.decode()
        self.assertIn("Running configuration matches saved configuration.", html)

    def test_password_mismatch_triggers_restart_required_without_revealing_either_value(self):
        self.write_baseline_env(EMAIL_HOST_PASSWORD="newondisk123")
        with self.matching_settings(EMAIL_HOST_PASSWORD="oldrunning456"):
            html = self.get_form().content.decode()
        self.assertIn("Restart required", html)
        self.assertNotIn("newondisk123", html)
        self.assertNotIn("oldrunning456", html)


# ---------------------------------------------------------------------
# Save flow
# ---------------------------------------------------------------------
class SaveFlowTests(EnvAdminTestCase):
    def test_valid_save_writes_env_and_redirects_with_success_message(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        resp = self.post_form(email_host="new.example.com")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("EMAIL_HOST=new.example.com", self.env_path.read_text())
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("SMTP settings saved" in m for m in messages))

    def test_no_op_save_shows_no_changes_message_and_no_write(self):
        self.write_baseline_env()
        before_mtime = self.env_path.stat().st_mtime_ns
        resp = self.client.post(self.smtp_url(), self.BASELINE_POST, follow=True)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("No changes to save" in m for m in messages))
        self.assertEqual(self.env_path.stat().st_mtime_ns, before_mtime)

    def test_invalid_value_shows_graceful_error_admin_still_usable(self):
        self.write_env("EMAIL_PORT=587\n")
        resp = self.post_form(email_port="not-a-number")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Could not save" in m for m in messages))
        self.assertIn("EMAIL_PORT=587", self.env_path.read_text())
        # Admin remains usable.
        self.assertEqual(self.get_form().status_code, 200)

    def test_duplicate_key_on_save_shows_actionable_error(self):
        self.write_env("EMAIL_HOST=a.example.com\nEMAIL_HOST=b.example.com\n")
        resp = self.post_form(email_host="c.example.com")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("more than once" in m for m in messages))

    def test_use_tls_checkbox_unchecked_writes_false(self):
        self.write_env("EMAIL_USE_TLS=True\n")
        data = {
            "email_host": "smtp.example.com", "email_port": "587",
            "email_host_user": "", "default_from_email": "alerts@example.com",
            "email_host_password": "",
            # email_use_tls omitted -- an unchecked HTML checkbox sends nothing
        }
        self.client.post(self.smtp_url(), data)
        self.assertIn("EMAIL_USE_TLS=False", self.env_path.read_text())

    def test_use_tls_checkbox_checked_writes_true(self):
        self.write_env("EMAIL_USE_TLS=False\n")
        self.post_form(email_use_tls="on")
        self.assertIn("EMAIL_USE_TLS=True", self.env_path.read_text())

    def test_does_not_touch_notificationconfig_db_fields(self):
        self.config.recipients = "keepme@example.com"
        self.config.cooldown_minutes = 42
        self.config.save()
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.post_form(email_host="new.example.com")
        refreshed = NotificationConfig.objects.get(pk=self.config.pk)
        self.assertEqual(refreshed.recipients, "keepme@example.com")
        self.assertEqual(refreshed.cooldown_minutes, 42)

    def test_no_model_field_or_migration_needed_write_lands_only_on_disk(self):
        """Confirms this whole feature is genuinely filesystem-only --
        no NotificationConfig column was added for any SMTP value."""
        field_names = {f.name for f in NotificationConfig._meta.get_fields()}
        self.assertNotIn("email_host", field_names)
        self.assertNotIn("email_host_password", field_names)


# ---------------------------------------------------------------------
# Secret (password) write-only semantics
# ---------------------------------------------------------------------
class PasswordSecretHandlingTests(EnvAdminTestCase):
    def test_blank_password_field_preserves_current_password(self):
        self.write_env("EMAIL_HOST_PASSWORD=originalsecret123\n")
        self.post_form(email_host_password="")
        self.assertIn("EMAIL_HOST_PASSWORD=originalsecret123", self.env_path.read_text())

    def test_nonblank_password_field_replaces_it(self):
        self.write_env("EMAIL_HOST_PASSWORD=originalsecret123\n")
        self.post_form(email_host_password="brandnewsecret456")
        content = self.env_path.read_text()
        self.assertIn("EMAIL_HOST_PASSWORD=brandnewsecret456", content)
        self.assertNotIn("originalsecret123", content)

    def test_clear_checkbox_clears_even_though_field_blank(self):
        self.write_env("EMAIL_HOST_PASSWORD=originalsecret123\n")
        self.post_form(email_host_password="", clear_email_host_password="on")
        self.assertIn("EMAIL_HOST_PASSWORD=\n", self.env_path.read_text())

    def test_without_clear_checkbox_blank_field_never_accidentally_clears(self):
        """The core write-only invariant: an accidentally-submitted
        blank password field must NEVER clear a stored credential."""
        self.write_env("EMAIL_HOST_PASSWORD=mustsurvive789\n")
        self.post_form(email_host_password="")
        self.assertIn("EMAIL_HOST_PASSWORD=mustsurvive789", self.env_path.read_text())

    def test_password_value_never_in_get_response(self):
        self.write_env("EMAIL_HOST_PASSWORD=neverleaked000\n")
        html = self.get_form().content.decode()
        self.assertNotIn("neverleaked000", html)

    def test_submitted_new_password_never_echoed_back_in_post_response(self):
        self.write_env("EMAIL_HOST_PASSWORD=old\n")
        resp = self.post_form(email_host_password="brandnewsecret456")
        self.assertNotIn("brandnewsecret456", resp.content.decode())

    def test_password_value_never_in_success_message(self):
        self.write_env("EMAIL_HOST_PASSWORD=old\n")
        resp = self.post_form(email_host_password="totallysecretvalue")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertFalse(any("totallysecretvalue" in m for m in messages))

    def test_password_value_never_in_system_event(self):
        self.write_env("EMAIL_HOST_PASSWORD=old\n")
        self.post_form(email_host_password="hiddenvalue999")
        event = SystemEvent.objects.get(dedupe_key="monitoring|smtp-env-updated")
        self.assertNotIn("hiddenvalue999", str(event.detail))

    def test_username_shown_and_editable_unlike_password(self):
        """EMAIL_HOST_USER is sensitive but not secret -- unlike the
        password, its value IS shown/editable."""
        self.write_env("EMAIL_HOST_USER=visible_username\n")
        html = self.get_form().content.decode()
        self.assertIn("visible_username", html)


# ---------------------------------------------------------------------
# Audit event
# ---------------------------------------------------------------------
class AuditEventTests(EnvAdminTestCase):
    def test_successful_save_records_changed_key_names(self):
        self.write_env("EMAIL_HOST=old.example.com\nEMAIL_PORT=587\n")
        self.post_form(email_host="new.example.com", email_port="2525")
        event = SystemEvent.objects.get(dedupe_key="monitoring|smtp-env-updated")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.title, "SMTP environment configuration updated")
        self.assertIn("EMAIL_HOST", event.detail["changed_keys"])
        self.assertIn("EMAIL_PORT", event.detail["changed_keys"])

    def test_event_records_requesting_admin_username(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        self.post_form(email_host="new.example.com")
        event = SystemEvent.objects.get(dedupe_key="monitoring|smtp-env-updated")
        self.assertEqual(event.detail["changed_by"], "smtpenvadmin")

    def test_event_records_restart_required_flag(self):
        self.write_env("EMAIL_HOST=old.example.com\n")
        with patch.object(env_config.django_settings, "EMAIL_HOST", "old.example.com"):
            self.post_form(email_host="new.example.com")
        event = SystemEvent.objects.get(dedupe_key="monitoring|smtp-env-updated")
        self.assertTrue(event.detail["restart_required"])

    def test_no_op_save_generates_no_event(self):
        self.write_baseline_env()
        self.client.post(self.smtp_url(), self.BASELINE_POST)
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="monitoring|smtp-env-updated").exists())

    def test_failed_save_does_not_falsely_log_success(self):
        self.write_env("EMAIL_PORT=587\n")
        self.post_form(email_port="not-a-number")
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="monitoring|smtp-env-updated").exists())

    def test_secret_key_change_recorded_as_key_name_only(self):
        self.write_env("EMAIL_HOST_PASSWORD=old\n")
        self.post_form(email_host_password="newvalue123")
        event = SystemEvent.objects.get(dedupe_key="monitoring|smtp-env-updated")
        self.assertIn("EMAIL_HOST_PASSWORD", event.detail["changed_keys"])
        self.assertNotIn("newvalue123", str(event.detail))


# ---------------------------------------------------------------------
# Main NotificationConfig changeform integration -- link + static note
# (must not disturb any pre-existing test in test_admin.py)
# ---------------------------------------------------------------------
class ChangeformIntegrationTests(EnvAdminTestCase):
    def _change_page_html(self):
        resp = self.client.get(reverse("admin:monitoring_notificationconfig_change", args=[self.config.pk]))
        return resp.content.decode()

    def test_edit_smtp_settings_link_present(self):
        html = self._change_page_html()
        self.assertIn("Edit SMTP settings", html)
        self.assertIn(self.smtp_url(), html)

    def test_test_email_mismatch_note_present(self):
        """Static, always-shown note (does not require a live disk
        comparison on the main changeform page -- see admin.py's own
        docstring on why the disk-reading logic is confined to the
        dedicated SMTP settings sub-page only)."""
        html = self._change_page_html()
        self.assertIn("restart", html.lower())

    def test_changeform_does_not_touch_env_file_on_disk(self):
        """The main changeform's own smtp_status/send_test_email_button
        must never read .env -- confirms the isolation this test suite
        as a whole depends on (existing pre-Phase-1 tests in
        test_admin.py never patch ENV_FILE_PATH at all)."""
        self.assertFalse(self.env_path.exists())
        self._change_page_html()
        self.assertFalse(self.env_path.exists())
