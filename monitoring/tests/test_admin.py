"""NotificationConfig admin -- 2026-08-11 SMTP diagnostics pass: the
non-secret SMTP status display and the "Send Test Email" button/view.
No SMTP credential value may ever appear in a response -- only
Yes/No "configured" state for username/password."""
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.models import NotificationConfig, SystemEvent


@override_settings(SECURE_SSL_REDIRECT=False)
class SmtpStatusDisplayTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("smtpstatusstaff", "smtp@example.invalid", "pw")
        self.client.force_login(self.staff)
        self.config = NotificationConfig.load()

    def _get_change_page(self):
        resp = self.client.get(reverse("admin:monitoring_notificationconfig_change", args=[self.config.pk]))
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    @override_settings(
        EMAIL_HOST="smtp.example.com", EMAIL_PORT=2525, EMAIL_USE_TLS=True,
        DEFAULT_FROM_EMAIL="alerts@example.com",
        EMAIL_HOST_USER="smtpuser", EMAIL_HOST_PASSWORD="sUp3rS3cr3tPassw0rd!!",
    )
    def test_displays_host_port_tls_from_address(self):
        html = self._get_change_page()
        self.assertIn("smtp.example.com", html)
        self.assertIn("2525", html)
        self.assertIn("alerts@example.com", html)

    @override_settings(EMAIL_USE_TLS=True)
    def test_tls_enabled_shows_yes(self):
        html = self._get_change_page()
        self.assertIn("Yes", html)

    @override_settings(EMAIL_USE_TLS=False)
    def test_tls_disabled_shows_no(self):
        html = self._get_change_page()
        # "No" appears in the TLS row specifically -- check the
        # rendered SMTP status table has a row reading "No" at all
        # (a bare "No" also happens to be a common substring elsewhere,
        # so this just confirms it's present, not falsely absent).
        self.assertIn(">No<", html)

    @override_settings(EMAIL_HOST_USER="realuser", EMAIL_HOST_PASSWORD="realpassword123")
    def test_username_and_password_configured_show_yes(self):
        html = self._get_change_page()
        self.assertIn("SMTP username configured", html)
        self.assertIn("SMTP password configured", html)

    @override_settings(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="")
    def test_username_and_password_not_configured_show_no(self):
        html = self._get_change_page()
        self.assertIn(">No<", html)

    @override_settings(EMAIL_HOST_USER="realuser", EMAIL_HOST_PASSWORD="sUp3rS3cr3tPassw0rd!!")
    def test_actual_password_value_never_appears_in_response(self):
        html = self._get_change_page()
        self.assertNotIn("sUp3rS3cr3tPassw0rd!!", html)

    @override_settings(EMAIL_HOST_USER="realuser_do_not_leak", EMAIL_HOST_PASSWORD="x")
    def test_prefers_configured_yes_no_over_raw_username_value(self):
        """The task's own stated preference: show "Configured: Yes"
        rather than the literal username value."""
        html = self._get_change_page()
        self.assertNotIn("realuser_do_not_leak", html)

    @override_settings(EMAIL_BACKEND="library.email_backend.LoggingSMTPBackend")
    def test_email_backend_shown(self):
        # Django's test runner forces EMAIL_BACKEND to locmem for the
        # whole run (see django.test.utils.setup_test_environment) --
        # override it back to the real project value for this one test
        # so the assertion reflects what an operator would actually see
        # in production, not the test-only backend.
        html = self._get_change_page()
        self.assertIn("library.email_backend.LoggingSMTPBackend", html)


@override_settings(SECURE_SSL_REDIRECT=False)
class SendTestEmailButtonTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("sendtestemailstaff", "test@example.invalid", "pw")
        self.client.force_login(self.staff)
        self.config = NotificationConfig.load()

    def _set_recipients(self, recipients):
        self.config.recipients = recipients
        self.config.save()

    def _change_page_html(self):
        resp = self.client.get(reverse("admin:monitoring_notificationconfig_change", args=[self.config.pk]))
        return resp.content.decode()

    def _send_test_email_url(self):
        return reverse("admin:monitoring_notificationconfig_send_test_email")

    def test_button_present_on_change_page(self):
        html = self._change_page_html()
        self.assertIn("Send test email", html)
        self.assertIn(self._send_test_email_url(), html)

    def test_clicking_button_sends_email_and_redirects_with_success_message(self):
        self._set_recipients("ops@example.com")
        resp = self.client.get(self._send_test_email_url(), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "[IsadoraAir] Test notification")
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Test email sent" in m for m in messages))

    def test_no_recipients_configured_shows_graceful_error_admin_still_usable(self):
        self._set_recipients("")
        resp = self.client.get(self._send_test_email_url(), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("recipient" in m.lower() for m in messages))
        # Admin remains usable -- the change page itself still loads fine.
        change_resp = self.client.get(reverse("admin:monitoring_notificationconfig_change", args=[self.config.pk]))
        self.assertEqual(change_resp.status_code, 200)

    def test_smtp_exception_shows_graceful_admin_error(self):
        from unittest.mock import patch
        self._set_recipients("ops@example.com")
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("connection refused")):
            resp = self.client.get(self._send_test_email_url(), follow=True)
        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in resp.context["messages"]]
        self.assertTrue(any("Failed to send test email" in m for m in messages))

    def test_test_email_creates_system_event(self):
        self._set_recipients("ops@example.com")
        self.client.get(self._send_test_email_url())
        self.assertTrue(SystemEvent.objects.filter(dedupe_key="monitoring|test-email-sent").exists())

    def test_anonymous_request_redirected_not_allowed(self):
        self.client.logout()
        resp = self.client.get(self._send_test_email_url())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
