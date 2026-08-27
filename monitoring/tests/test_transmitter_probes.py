from unittest.mock import patch

from django.test import TestCase

from monitoring.models import MonitorCheck, TransmitterConfig
from monitoring.services.probes import (
    probe_transmitter_indicator,
    probe_transmitter_param,
)
from monitoring.services.transmitters import (
    BWTx300v3Driver,
    UnsupportedTransmitterParameter,
)


class TransmitterProbeCompatibilityTests(TestCase):
    def setUp(self):
        config = TransmitterConfig.load()
        config.full_power_watts = 250.0
        config.save()
        self.driver = BWTx300v3Driver(
            "203.0.113.10", 23, "test-only-placeholder"
        )

    def test_bw_forward_power_feeds_existing_numeric_probe(self):
        check = MonitorCheck(
            name="TX Forward Power",
            kind="transmitter_param",
            transmitter_parameter="psu.fwd_power",
            warning_threshold=200,
            critical_threshold=150,
            threshold_direction="below",
        )
        with patch.object(self.driver, "_get_native", return_value="245.5"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 245.5)
        self.assertEqual(detail["percent_of_max"], 98.2)

    def test_bw_protection_state_feeds_existing_indicator_fault_values(self):
        check = MonitorCheck(
            name="TX VSWR Indicator",
            kind="transmitter_indicator",
            transmitter_indicator="status.indicator.vswr",
            fault_values="R",
            warn_values="O",
        )
        with patch.object(self.driver, "_get_native", return_value="on"):
            status, detail = probe_transmitter_indicator(check, self.driver)

        self.assertEqual(status, "critical")
        self.assertEqual(detail["value"], "R")

    def test_unfamiliar_bw_indicator_states_fail_unknown_not_ok(self):
        for reference in (
            "status.indicator.rf",
            "status.indicator.vswr",
            "status.indicator.temp",
        ):
            with self.subTest(reference=reference):
                check = MonitorCheck(
                    name=f"Unfamiliar {reference}",
                    kind="transmitter_indicator",
                    transmitter_indicator=reference,
                    fault_values="R",
                    warn_values="O",
                )
                with patch.object(
                    self.driver, "_get_native", return_value="fault?"
                ):
                    status, detail = probe_transmitter_indicator(
                        check, self.driver
                    )

                self.assertEqual(status, "unknown")
                self.assertEqual(detail, {"error": "no response"})

    def test_device_reported_unknown_parameter_is_explicitly_unsupported(self):
        check = MonitorCheck(
            name="TX Forward Power",
            kind="transmitter_param",
            transmitter_parameter="psu.fwd_power",
            warning_threshold=200,
            critical_threshold=150,
            threshold_direction="below",
        )
        with patch.object(
            self.driver,
            "get",
            side_effect=UnsupportedTransmitterParameter("Unknown parameter"),
        ):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "unsupported")
        self.assertIn("Unknown parameter", detail["reason"])
