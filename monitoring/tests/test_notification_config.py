"""NotificationConfig.clean() -- recipient validation added in the
2026-08-11 SMTP diagnostics pass. Uses Django's own EmailValidator over
the exact parsing recipient_list() already does (one email per line,
or comma-separated) so a value that validates here behaves identically
at actual send time -- no separate/parallel parsing rule to drift."""
from django.core.exceptions import ValidationError
from django.test import TestCase

from monitoring.models import NotificationConfig


class RecipientParsingTests(TestCase):
    """recipient_list() itself is unchanged by this pass -- these tests
    just re-confirm its existing normalization semantics before the new
    clean() validation is layered on top, so a later failure clearly
    means "validation broke this," not "parsing broke this.\""""

    def test_single_email(self):
        config = NotificationConfig(recipients="ops@example.com")
        self.assertEqual(config.recipient_list(), ["ops@example.com"])

    def test_multiple_newline_separated(self):
        config = NotificationConfig(recipients="ops@example.com\nchief@example.org")
        self.assertEqual(config.recipient_list(), ["ops@example.com", "chief@example.org"])

    def test_comma_separated(self):
        config = NotificationConfig(recipients="ops@example.com,chief@example.org")
        self.assertEqual(config.recipient_list(), ["ops@example.com", "chief@example.org"])

    def test_mixed_comma_and_newline(self):
        config = NotificationConfig(recipients="ops@example.com,chief@example.org\nthird@example.net")
        self.assertEqual(
            config.recipient_list(),
            ["ops@example.com", "chief@example.org", "third@example.net"],
        )

    def test_surrounding_whitespace_stripped(self):
        config = NotificationConfig(recipients="  ops@example.com  \n   chief@example.org   ")
        self.assertEqual(config.recipient_list(), ["ops@example.com", "chief@example.org"])

    def test_blank_lines_and_blank_config_produce_empty_list(self):
        config = NotificationConfig(recipients="\n\n  \n")
        self.assertEqual(config.recipient_list(), [])
        self.assertEqual(NotificationConfig(recipients="").recipient_list(), [])

    def test_sms_gateway_style_address_is_plain_syntax(self):
        config = NotificationConfig(recipients="15551234567@carrier.example")
        self.assertEqual(config.recipient_list(), ["15551234567@carrier.example"])


class NotificationConfigValidationTests(TestCase):
    def test_blank_recipients_allowed(self):
        """Blank is current intended behavior -- notifications with no
        recipients are simply never sent (see notify.py's _send/
        send_test_email, both of which no-op on an empty list); it is
        not itself an error to save Notification Config with nothing
        configured yet."""
        config = NotificationConfig(recipients="")
        config.clean()  # must not raise

    def test_single_valid_email_passes(self):
        NotificationConfig(recipients="ops@example.com").clean()

    def test_multiple_valid_newline_separated_pass(self):
        NotificationConfig(recipients="ops@example.com\nchief@example.org").clean()

    def test_multiple_valid_comma_separated_pass(self):
        NotificationConfig(recipients="ops@example.com,chief@example.org").clean()

    def test_mixed_comma_and_newline_all_valid_passes(self):
        NotificationConfig(recipients="ops@example.com,chief@example.org\nthird@example.net").clean()

    def test_surrounding_whitespace_does_not_cause_false_rejection(self):
        NotificationConfig(recipients="  ops@example.com  \n  chief@example.org  ").clean()

    def test_sms_gateway_style_address_passes(self):
        NotificationConfig(recipients="15551234567@carrier.example").clean()

    def test_malformed_address_rejected(self):
        config = NotificationConfig(recipients="not-an-email")
        with self.assertRaises(ValidationError) as ctx:
            config.clean()
        self.assertIn("recipients", ctx.exception.message_dict)

    def test_incomplete_domain_rejected(self):
        config = NotificationConfig(recipients="foo@")
        with self.assertRaises(ValidationError) as ctx:
            config.clean()
        self.assertIn("recipients", ctx.exception.message_dict)

    def test_missing_local_part_rejected(self):
        config = NotificationConfig(recipients="@example.com")
        with self.assertRaises(ValidationError) as ctx:
            config.clean()
        self.assertIn("recipients", ctx.exception.message_dict)

    def test_one_bad_address_among_good_ones_still_rejected(self):
        config = NotificationConfig(recipients="ops@example.com\nnot-an-email\nchief@example.org")
        with self.assertRaises(ValidationError) as ctx:
            config.clean()
        self.assertIn("recipients", ctx.exception.message_dict)
        # Names the actual bad value, not a generic message.
        self.assertTrue(any("not-an-email" in msg for msg in ctx.exception.message_dict["recipients"]))

    def test_valid_config_full_clean_does_not_raise(self):
        """End-to-end: the same path Django admin's ModelForm actually
        calls on save (full_clean(), not just clean() in isolation)."""
        config = NotificationConfig(recipients="ops@example.com", cooldown_minutes=30)
        config.full_clean(exclude=["id"])

    def test_invalid_config_full_clean_raises(self):
        config = NotificationConfig(recipients="not-an-email", cooldown_minutes=30)
        with self.assertRaises(ValidationError):
            config.full_clean(exclude=["id"])
