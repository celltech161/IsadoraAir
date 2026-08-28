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

    def test_bw_native_forward_power_reference_also_receives_percent_of_max(self):
        """r0016: WRJE's native meters.pafwd forward-power check must
        get the same percent_of_max the established psu.fwd_power
        compatibility reference already gets -- the C300 forward-power
        renderer requires it."""
        check = MonitorCheck(
            name="TX Forward Power (native)",
            kind="transmitter_param",
            transmitter_parameter="meters.pafwd",
            warning_threshold=200,
            critical_threshold=150,
            threshold_direction="below",
        )
        with patch.object(self.driver, "_get_native", return_value="246"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 246.0)
        # WRJE's real configuration: full_power_watts=265, ~246W -> ~92.8%.
        self.assertAlmostEqual(detail["percent_of_max"], 98.4, places=1)

    def test_psu_fwd_power_and_meters_pafwd_produce_equal_percentages(self):
        """Equivalent readings/configuration must produce equal
        percentages for both established forward-power references --
        proves this is one calculation, not two."""
        config = TransmitterConfig.load()
        config.full_power_watts = 265.0
        config.save()
        check_legacy = MonitorCheck(
            name="TX Forward Power (legacy ref)",
            kind="transmitter_param",
            transmitter_parameter="psu.fwd_power",
            warning_threshold=200,
            critical_threshold=150,
            threshold_direction="below",
        )
        check_native = MonitorCheck(
            name="TX Forward Power (native ref)",
            kind="transmitter_param",
            transmitter_parameter="meters.pafwd",
            warning_threshold=200,
            critical_threshold=150,
            threshold_direction="below",
        )
        with patch.object(self.driver, "_get_native", return_value="246"):
            _legacy_status, legacy_detail = probe_transmitter_param(check_legacy, self.driver)
        with patch.object(self.driver, "_get_native", return_value="246"):
            _native_status, native_detail = probe_transmitter_param(check_native, self.driver)

        self.assertEqual(legacy_detail["percent_of_max"], native_detail["percent_of_max"])
        self.assertAlmostEqual(native_detail["percent_of_max"], 92.8, places=1)

    def test_informational_reflected_power_reads_ok_with_no_thresholds(self):
        """r0016: after the Part A validation fix, meters.parev with no
        configured thresholds is a valid informational check -- its
        status must be 'ok' whenever the read succeeds, and it must
        never receive percent_of_max (that's forward-power only)."""
        check = MonitorCheck(
            name="TX Reflected Power",
            kind="transmitter_param",
            transmitter_parameter="meters.parev",
        )
        with patch.object(self.driver, "_get_native", return_value="4"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 4.0)
        self.assertEqual(detail["raw"], "4")
        self.assertNotIn("percent_of_max", detail)

    def test_informational_transmitter_reads_preserve_numeric_value_and_raw(self):
        """Informational (thresholdless) transmitter reads must still
        produce a numeric detail['value'] suitable for the dashboard
        renderers/API consumers, and preserve detail['raw'] -- never
        a formatted display string used for comparison."""
        check = MonitorCheck(
            name="TX Fan Speed",
            kind="transmitter_param",
            transmitter_parameter="meters.fanspeed",
        )
        with patch.object(self.driver, "_get_native", return_value="3150 RPM"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 3150.0)
        self.assertEqual(detail["raw"], "3150 RPM")

    def test_bw_board_temperature_native_response_yields_numeric_detail(self):
        """r0015: live WRJE TX300v3 (firmware 2.0-R) returned
        'get aio.temp.board' -> '49 (C)'. The shared numeric parser
        already yields 49.0; probe_transmitter_param() stays unchanged
        and must never compare formatted display strings."""
        check = MonitorCheck(
            name="TX Board Temperature High",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.board",
            warning_threshold=55,
            critical_threshold=65,
            threshold_direction="above",
        )
        with patch.object(self.driver, "_get_native", return_value="49 (C)"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 49.0)
        self.assertEqual(detail["raw"], "49 (C)")

    def test_bw_dsp_temperature_native_response_yields_numeric_detail(self):
        check = MonitorCheck(
            name="TX DSP Temperature High",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.dsp",
            warning_threshold=58,
            critical_threshold=65,
            threshold_direction="above",
        )
        with patch.object(self.driver, "_get_native", return_value="57 (C)"):
            status, detail = probe_transmitter_param(check, self.driver)

        self.assertEqual(status, "ok")
        self.assertEqual(detail["value"], 57.0)
        self.assertEqual(detail["raw"], "57 (C)")

    def test_board_temperature_high_threshold_behavior(self):
        check = MonitorCheck(
            name="TX Board Temperature High",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.board",
            warning_threshold=55,
            critical_threshold=65,
            threshold_direction="above",
        )
        cases = (("49 (C)", "ok"), ("60 (C)", "warning"), ("70 (C)", "critical"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.object(self.driver, "_get_native", return_value=raw):
                    status, detail = probe_transmitter_param(check, self.driver)
                self.assertEqual(status, expected)
                self.assertEqual(detail["value"], float(raw.split()[0]))

    def test_board_temperature_low_threshold_behavior(self):
        check = MonitorCheck(
            name="TX Board Temperature Low",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.board",
            warning_threshold=5,
            critical_threshold=0,
            threshold_direction="below",
        )
        cases = (("49 (C)", "ok"), ("3 (C)", "warning"), ("-1 (C)", "critical"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.object(self.driver, "_get_native", return_value=raw):
                    status, detail = probe_transmitter_param(check, self.driver)
                self.assertEqual(status, expected)
                self.assertEqual(detail["value"], float(raw.split()[0]))

    def test_dsp_temperature_high_threshold_behavior(self):
        check = MonitorCheck(
            name="TX DSP Temperature High",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.dsp",
            warning_threshold=58,
            critical_threshold=65,
            threshold_direction="above",
        )
        cases = (("57 (C)", "ok"), ("60 (C)", "warning"), ("66 (C)", "critical"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.object(self.driver, "_get_native", return_value=raw):
                    status, detail = probe_transmitter_param(check, self.driver)
                self.assertEqual(status, expected)
                self.assertEqual(detail["value"], float(raw.split()[0]))

    def test_dsp_temperature_low_threshold_behavior(self):
        check = MonitorCheck(
            name="TX DSP Temperature Low",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.dsp",
            warning_threshold=5,
            critical_threshold=0,
            threshold_direction="below",
        )
        cases = (("57 (C)", "ok"), ("3 (C)", "warning"), ("-1 (C)", "critical"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with patch.object(self.driver, "_get_native", return_value=raw):
                    status, detail = probe_transmitter_param(check, self.driver)
                self.assertEqual(status, expected)
                self.assertEqual(detail["value"], float(raw.split()[0]))

    def test_computed_vswr_feeds_existing_numeric_probe_same_as_psu_vswr(self):
        check_computed = MonitorCheck(
            name="TX VSWR",
            kind="transmitter_param",
            transmitter_parameter="computed:vswr",
            warning_threshold=1.5,
            critical_threshold=1.65,
            threshold_direction="above",
        )
        check_legacy = MonitorCheck(
            name="TX VSWR (legacy ref)",
            kind="transmitter_param",
            transmitter_parameter="psu.vswr",
            warning_threshold=1.5,
            critical_threshold=1.65,
            threshold_direction="above",
        )
        values = {"meters.pafwd": "247.0", "meters.parev": "4.0"}
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            computed_status, computed_detail = probe_transmitter_param(
                check_computed, self.driver
            )
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            legacy_status, legacy_detail = probe_transmitter_param(
                check_legacy, self.driver
            )

        self.assertEqual(computed_status, legacy_status)
        self.assertEqual(computed_detail["value"], legacy_detail["value"])
        self.assertAlmostEqual(computed_detail["value"], 1.2916252451934438)

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
