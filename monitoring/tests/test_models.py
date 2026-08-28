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


class TransmitterParamInformationalThresholdTests(TestCase):
    """r0016: a transmitter_param check may legitimately be an
    informational-only reading (TX Reflected Power, TX Fan Speed) with
    no alert thresholds at all -- probe_transmitter_param() already
    handles warning_threshold=critical_threshold=None correctly.
    disk/cpu/memory/temperature are unaffected -- those still require
    at least one threshold."""

    def test_transmitter_param_with_no_thresholds_is_valid(self):
        check = MonitorCheck(
            name="x", kind="transmitter_param", transmitter_parameter="meters.parev",
        )
        check.full_clean()  # must not raise

    def test_transmitter_param_still_requires_transmitter_parameter(self):
        check = MonitorCheck(name="x", kind="transmitter_param")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("transmitter_parameter", ctx.exception.message_dict)

    def test_transmitter_param_with_thresholds_still_valid(self):
        check = MonitorCheck(
            name="x", kind="transmitter_param", transmitter_parameter="meters.pafwd",
            warning_threshold=200, critical_threshold=150, threshold_direction="below",
        )
        check.full_clean()  # must not raise -- existing thresholded behavior unchanged

    def test_disk_with_no_thresholds_remains_invalid(self):
        check = MonitorCheck(name="x", kind="disk", disk_path="/")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("critical_threshold", ctx.exception.message_dict)

    def test_cpu_with_no_thresholds_remains_invalid(self):
        check = MonitorCheck(name="x", kind="cpu")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("critical_threshold", ctx.exception.message_dict)

    def test_memory_with_no_thresholds_remains_invalid(self):
        check = MonitorCheck(name="x", kind="memory")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("critical_threshold", ctx.exception.message_dict)

    def test_temperature_with_no_thresholds_remains_invalid(self):
        check = MonitorCheck(name="x", kind="temperature")
        with self.assertRaises(ValidationError) as ctx:
            check.clean()
        self.assertIn("critical_threshold", ctx.exception.message_dict)

    def test_disk_cpu_memory_temperature_still_valid_with_a_threshold(self):
        for kind, extra in (
            ("disk", {"disk_path": "/"}),
            ("cpu", {}),
            ("memory", {}),
            ("temperature", {}),
        ):
            with self.subTest(kind=kind):
                check = MonitorCheck(
                    name=f"x-{kind}", kind=kind, warning_threshold=80, **extra
                )
                check.clean()  # must not raise
