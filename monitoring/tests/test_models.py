"""MonitorCheck model tests for the encoder_group kind added in the
2026-08-05 hardening pass."""
from django.core.exceptions import ValidationError
from django.test import TestCase

from monitoring.models import MonitorCheck


class EncoderGroupCleanTests(TestCase):
    def test_requires_encoder_group_slug(self):
        check = MonitorCheck(name="x", kind="encoder_group", encoder_group_systemd_unit="isadoraair-encoders.service")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("encoder_group_slug", ctx.exception.message_dict)

    def test_requires_systemd_unit(self):
        check = MonitorCheck(name="x", kind="encoder_group", encoder_group_slug="airtap")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("encoder_group_systemd_unit", ctx.exception.message_dict)

    def test_valid_when_both_present(self):
        check = MonitorCheck(
            name="x", kind="encoder_group",
            encoder_group_slug="airtap", encoder_group_systemd_unit="isadoraair-encoders.service",
        )
        check.clean()  # must not raise

    def test_other_kinds_unaffected_by_new_fields(self):
        check = MonitorCheck(name="x", kind="systemd", systemd_unit="isadoraair-engine.service")
        check.clean()  # must not raise -- encoder_group_slug/unit are blank and irrelevant here
