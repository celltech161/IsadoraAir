"""monitoring/services/notify.py -- 2026-08-11 SMTP diagnostics pass.

Covers:
  * _send()'s new warning-SystemEvent-on-failure behavior: fail-safe
    (never raises out of maybe_notify), stable dedupe (a broken SMTP
    transport coalesces into one repeating row, not one per tick), no
    secret leakage, and -- structurally, not just by assertion -- no
    recursive email-send attempt triggered by the SystemEvent write.
  * send_test_email(): the "Send Test Email" admin action's service
    function. Success, no-recipients, and SMTP-exception paths, plus
    the SystemEvent each records.
  * maybe_notify()'s pre-existing cooldown/notify_on_warning/recovery
    gating, confirmed UNCHANGED by this pass (no prior test coverage
    existed for this function at all before this file).

Django's test runner forces EMAIL_BACKEND to the locmem backend for
the whole run (see django.test.utils.setup_test_environment) --
regardless of the project's own LoggingSMTPBackend setting -- so a
"successful send" here is a real send_mail() call landing in
django.core.mail.outbox, not a mock. Failure paths mock
django.core.mail.send_mail directly, since locmem never raises on its
own."""
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from monitoring.models import MonitorCheck, NotificationConfig, SystemEvent
from monitoring.services import notify


def make_config(**overrides):
    defaults = dict(enabled=True, recipients="ops@example.com", cooldown_minutes=30)
    defaults.update(overrides)
    config = NotificationConfig.load()
    for key, value in defaults.items():
        setattr(config, key, value)
    config.save()
    return config


def make_check(**overrides):
    defaults = dict(
        name="Test Check", kind="systemd", systemd_unit="isadoraair-test.service",
        notify_on_warning=True, notify_on_critical=True,
    )
    defaults.update(overrides)
    return MonitorCheck.objects.create(**defaults)


class SendSmtpFailureVisibilityTests(TestCase):
    """_send()'s own try/except -- the shared low-level sender used by
    both maybe_notify() (real alerts) and send_test_email()."""

    def test_smtp_exception_does_not_escape(self):
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("boom")):
            notify._send(config, "subject", "body")  # must not raise

    def test_smtp_failure_emits_warning_system_event(self):
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("boom")):
            notify._send(config, "subject", "body")
        event = SystemEvent.objects.get(dedupe_key="monitoring|notify-smtp-failed")
        self.assertEqual(event.level, "warning")
        self.assertEqual(event.title, "Notification email delivery failed")
        self.assertEqual(event.detail["exception"], "RuntimeError")
        self.assertIn("boom", event.detail["error"])

    def test_repeated_failures_coalesce_not_one_row_per_tick(self):
        """A broken SMTP server ticking every ~10s must not create a new
        database row every cycle -- the fixed dedupe_key means repeated
        failures within the coalesce window bump repeat_count on the
        SAME row instead."""
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("boom")):
            notify._send(config, "subject one", "body")
            notify._send(config, "subject two", "body")
            notify._send(config, "subject three", "body")
        events = SystemEvent.objects.filter(dedupe_key="monitoring|notify-smtp-failed")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().repeat_count, 3)

    def test_no_secret_leakage_in_failure_event(self):
        config = make_config()
        secret = "sUp3rS3cr3tSmtpPassw0rd!!"
        with override_settings(EMAIL_HOST_PASSWORD=secret):
            # Simulate an SMTP library exception that happens to echo
            # back something containing the configured password --
            # defense in depth, not assumed impossible.
            with patch("django.core.mail.send_mail", side_effect=RuntimeError(f"auth failed for {secret}")):
                notify._send(config, "subject", "body")
        event = SystemEvent.objects.get(dedupe_key="monitoring|notify-smtp-failed")
        self.assertNotIn(secret, event.detail["error"])
        self.assertNotIn(secret, event.title)

    def test_failure_event_cannot_trigger_a_recursive_send(self):
        """Structural proof, not just "it didn't happen to recurse
        today": emit_event() only ever writes a SystemEvent row (see
        monitoring/models.py) -- nothing subscribes to SystemEvent
        creation to send an email (confirmed by inspection: maybe_notify/
        _send are only ever called from monitor.py's own check-
        transition loop). This test locks that in operationally: even
        with send_mail mocked to always fail, exactly ONE send_mail call
        happens per _send() invocation -- never a second, recursive
        attempt provoked by the SystemEvent write that follows it."""
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("boom")) as mock_send:
            notify._send(config, "subject", "body")
        self.assertEqual(mock_send.call_count, 1)

    def test_console_logging_preserved(self):
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("boom")):
            with patch("builtins.print") as mock_print:
                notify._send(config, "my subject", "body")
        self.assertTrue(any("my subject" in str(call) for call in mock_print.call_args_list))

    def test_console_log_also_never_contains_secret(self):
        """The DB-persisted SystemEvent isn't the only surface this
        must be redacted from -- the console/journal print goes through
        the same sanitized text, not raw str(exc)."""
        config = make_config()
        secret = "sUp3rS3cr3tSmtpPassw0rd!!"
        with override_settings(EMAIL_HOST_PASSWORD=secret):
            with patch("django.core.mail.send_mail", side_effect=RuntimeError(f"auth failed for {secret}")):
                with patch("builtins.print") as mock_print:
                    notify._send(config, "subject", "body")
        self.assertFalse(any(secret in str(call) for call in mock_print.call_args_list))

    def test_no_recipients_never_attempts_send_or_event(self):
        config = make_config(recipients="")
        with patch("django.core.mail.send_mail") as mock_send:
            notify._send(config, "subject", "body")
        mock_send.assert_not_called()
        self.assertFalse(SystemEvent.objects.filter(dedupe_key="monitoring|notify-smtp-failed").exists())


class SendTestEmailTests(TestCase):
    def test_no_recipients_configured_returns_graceful_error(self):
        config = make_config(recipients="")
        ok, message = notify.send_test_email(config)
        self.assertFalse(ok)
        self.assertIn("recipient", message.lower())
        self.assertEqual(len(mail.outbox), 0)

    def test_successful_send_uses_expected_recipients_and_from(self):
        config = make_config(recipients="ops@example.com\nchief@example.org")
        ok, message = notify.send_test_email(config)
        self.assertTrue(ok)
        self.assertIn("2", message)
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.subject, "[IsadoraAir] Test notification")
        self.assertEqual(sorted(sent.to), ["chief@example.org", "ops@example.com"])
        from django.conf import settings
        self.assertEqual(sent.from_email, settings.DEFAULT_FROM_EMAIL)
        self.assertIn("IsadoraAir", sent.body)

    def test_successful_send_records_info_system_event(self):
        config = make_config(recipients="ops@example.com\nchief@example.org")
        notify.send_test_email(config)
        event = SystemEvent.objects.get(dedupe_key="monitoring|test-email-sent")
        self.assertEqual(event.level, "info")
        self.assertEqual(event.title, "Notification test email sent")
        self.assertEqual(event.detail["recipient_count"], 2)
        from django.conf import settings
        self.assertEqual(event.detail["from"], settings.DEFAULT_FROM_EMAIL)

    def test_success_event_never_stores_recipient_addresses(self):
        config = make_config(recipients="ops@example.com")
        notify.send_test_email(config)
        event = SystemEvent.objects.get(dedupe_key="monitoring|test-email-sent")
        self.assertNotIn("ops@example.com", str(event.detail))

    def test_smtp_exception_returns_graceful_admin_error(self):
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("connection refused")):
            ok, message = notify.send_test_email(config)
        self.assertFalse(ok)
        self.assertIn("connection refused", message)
        self.assertEqual(len(mail.outbox), 0)

    def test_smtp_exception_records_warning_system_event(self):
        config = make_config()
        with patch("django.core.mail.send_mail", side_effect=RuntimeError("connection refused")):
            notify.send_test_email(config)
        event = SystemEvent.objects.get(dedupe_key="monitoring|test-email-failed")
        self.assertEqual(event.level, "warning")
        self.assertEqual(event.title, "Notification test email failed")
        self.assertEqual(event.detail["exception"], "RuntimeError")

    def test_no_secret_leakage_on_failure(self):
        config = make_config()
        secret = "sUp3rS3cr3tSmtpPassw0rd!!"
        with override_settings(EMAIL_HOST_PASSWORD=secret):
            with patch("django.core.mail.send_mail", side_effect=RuntimeError(f"auth failed for {secret}")):
                ok, message = notify.send_test_email(config)
        self.assertNotIn(secret, message)
        event = SystemEvent.objects.get(dedupe_key="monitoring|test-email-failed")
        self.assertNotIn(secret, str(event.detail))

    def test_never_sends_real_email_in_test_environment(self):
        """Sanity guard on the test setup itself -- confirms the locmem
        backend is actually in effect, so nothing in this file risks a
        real outbound SMTP connection."""
        from django.conf import settings
        self.assertEqual(settings.EMAIL_BACKEND, "django.core.mail.backends.locmem.EmailBackend")


class MaybeNotifyExistingBehaviorTests(TestCase):
    """Light regression lock on maybe_notify()'s pre-existing gating --
    unchanged by this pass, but had no test coverage at all before it."""

    def test_disabled_config_suppresses_all_notifications(self):
        config = make_config(enabled=False)
        check = make_check()
        with patch.object(notify, "_send") as mock_send:
            notify.maybe_notify(check, "critical", {}, "ok", {})
        mock_send.assert_not_called()

    def test_warning_respects_notify_on_warning_false(self):
        make_config()
        check = make_check(notify_on_warning=False)
        with patch.object(notify, "_send") as mock_send:
            notify.maybe_notify(check, "warning", {}, "ok", {})
        mock_send.assert_not_called()

    def test_critical_respects_notify_on_critical_false(self):
        make_config()
        check = make_check(notify_on_critical=False)
        with patch.object(notify, "_send") as mock_send:
            notify.maybe_notify(check, "critical", {}, "ok", {})
        mock_send.assert_not_called()

    def test_cooldown_suppresses_repeat_notification(self):
        make_config(cooldown_minutes=30)
        check = make_check()
        cooldowns = {}
        with patch.object(notify, "_send") as mock_send:
            notify.maybe_notify(check, "critical", {}, "ok", cooldowns)
            notify.maybe_notify(check, "critical", {}, "critical", cooldowns)
        self.assertEqual(mock_send.call_count, 1)

    def test_recovery_notification_sent_and_clears_cooldown(self):
        make_config()
        check = make_check()
        cooldowns = {str(check.id): 0}
        with patch.object(notify, "_send") as mock_send:
            notify.maybe_notify(check, "ok", {}, "critical", cooldowns)
        mock_send.assert_called_once()
        self.assertNotIn(str(check.id), cooldowns)
