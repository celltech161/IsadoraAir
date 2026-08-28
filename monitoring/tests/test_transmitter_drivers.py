import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from monitoring.services.transmitter_client import TransmitterError
from monitoring.services.transmitters import (
    BWTx300v3Driver,
    CobaltTransmitterDriver,
    DRIVER_REGISTRY,
    TransmitterConfigurationError,
    TransmitterDriverRegistration,
    UnsupportedTransmitterParameter,
    compute_vswr,
    create_transmitter_driver,
)


class FakeSocket:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.timeouts = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        if not self.chunks:
            raise socket.timeout
        chunk = self.chunks.pop(0)
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


class BWProtocolTests(SimpleTestCase):
    password = "test-only-placeholder"

    def _connect(self, response_chunks, *, login_chunks=None):
        sock = FakeSocket((login_chunks or [
            b"BW Broadcast TX300v3\r\nPass",
            b"word:",
            b"\r\nTX-",
            b"V3>",
        ]) + response_chunks)
        patcher = patch(
            "monitoring.services.transmitters.bw_tx300v3.socket.create_connection",
            return_value=sock,
        )
        create_connection = patcher.start()
        self.addCleanup(patcher.stop)
        driver = BWTx300v3Driver(
            "203.0.113.10", 23, self.password, timeout=1.0
        )
        driver.__enter__()
        self.addCleanup(driver.__exit__, None, None, None)
        return driver, sock, create_connection

    def test_successful_password_only_login_and_split_prompt(self):
        driver, sock, create_connection = self._connect([])

        self.assertIsInstance(driver, BWTx300v3Driver)
        create_connection.assert_called_once_with(
            ("203.0.113.10", 23), timeout=1.0
        )
        self.assertEqual(sock.sent, [self.password.encode("ascii") + b"\r\n"])

    def test_successful_login_with_lowercase_password_prompt(self):
        """Real WRJE TX300v3 firmware emits a lowercase ``password:``
        prompt rather than the ``Password:`` this driver was originally
        written against. Password-prompt matching must be ASCII
        case-insensitive without changing the ordinary command prompt or
        telemetry parsing."""
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Welcome to the TX-V3!\r\n",
                b"password:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(sock.sent, [self.password.encode("ascii") + b"\r\n"])

    def test_lowercase_password_prompt_with_wrje_telnet_negotiation(self):
        """Reproduces the exact real-WRJE banner shape: a greeting line,
        an IAC WILL ECHO negotiation, then the lowercase password prompt.
        The driver must still decline the option (IAC DONT ECHO) and then
        authenticate."""
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Welcome to the TX-V3!\r\n",
                b"\xff\xfb\x01",
                b"password:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(
            sock.sent,
            [b"\xff\xfe\x01", self.password.encode("ascii") + b"\r\n"],
        )
        self.assertNotIn(b"\xff", driver._buf)

    def test_lowercase_password_prompt_split_across_recv_boundaries(self):
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Welcome to the TX-V3!\r\npass",
                b"word:",
                b"\r\nTX-",
                b"V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(sock.sent, [self.password.encode("ascii") + b"\r\n"])

    def test_mixed_case_password_prompt_is_matched_case_insensitively(self):
        """A deliberately mixed-case prompt establishes this is genuine
        ASCII case-insensitive matching, not a lowercase-only special
        case."""
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Welcome to the TX-V3!\r\n",
                b"pAsSwOrD:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(sock.sent, [self.password.encode("ascii") + b"\r\n"])

    def test_password_prompt_case_insensitivity_does_not_broaden_to_command_prompt(self):
        """The ordinary command prompt (``TX-V3>``) match must remain
        case-sensitive -- only password-prompt detection changed."""
        driver, sock, _create_connection = self._connect([
            b"get meters.pafwd\r\n250.5\r\ntx-v3>",
        ])

        with patch(
            "monitoring.services.transmitters.bw_tx300v3.time.monotonic",
            side_effect=[0.0, 0.0, 2.0],
        ):
            with self.assertRaisesMessage(TransmitterError, "Timed out"):
                driver.get("meters.pafwd")

    def test_iac_then_do_option_split_across_recv_boundaries(self):
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Banner before negotiation\xff",
                b"\xfd\x18Password:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(
            sock.sent,
            [b"\xff\xfc\x18", self.password.encode("ascii") + b"\r\n"],
        )
        self.assertNotIn(b"\xff", driver._buf)

    def test_iac_do_then_option_split_across_recv_boundaries(self):
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"Banner before negotiation\xff\xfd",
                b"\x18Password:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(
            sock.sent,
            [b"\xff\xfc\x18", self.password.encode("ascii") + b"\r\n"],
        )
        self.assertNotIn(b"\xff", driver._buf)

    def test_iac_will_then_option_split_sends_dont_and_preserves_prompt(self):
        driver, sock, _create_connection = self._connect(
            [],
            login_chunks=[
                b"\xff\xfb",
                b"\x01Password:",
                b"\r\nTX-V3>",
            ],
        )

        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(
            sock.sent,
            [b"\xff\xfe\x01", self.password.encode("ascii") + b"\r\n"],
        )
        self.assertNotIn(b"\xff", driver._buf)

    def test_split_iac_filter_preserves_only_surrounding_application_data(self):
        driver = BWTx300v3Driver(
            "203.0.113.10", 23, self.password, timeout=1.0
        )
        sock = FakeSocket([])
        driver._sock = sock

        filtered = (
            driver._strip_iac(b"Banner\xff")
            + driver._strip_iac(b"\xfd")
            + driver._strip_iac(b"\x18Password:")
        )

        self.assertEqual(filtered, b"BannerPassword:")
        self.assertNotIn(b"\xff", filtered)
        self.assertEqual(sock.sent, [b"\xff\xfc\x18"])

    def test_early_close_with_incomplete_iac_still_fails_and_closes(self):
        sock = FakeSocket([b"Banner\xff", b""])
        with patch(
            "monitoring.services.transmitters.bw_tx300v3.socket.create_connection",
            return_value=sock,
        ):
            driver = BWTx300v3Driver(
                "203.0.113.10", 23, self.password, timeout=1.0
            )
            with self.assertRaisesMessage(
                TransmitterError, "closed the connection"
            ):
                driver.__enter__()

        self.assertTrue(sock.closed)
        self.assertEqual(driver._iac_state, "data")

    def test_repeated_get_calls_reuse_one_connection(self):
        driver, sock, create_connection = self._connect([
            b"get meters.pafwd\r\n250.5\r\nTX-V3>",
            b"get system.uptime\r\n12 days 03:14:15\r\nTX-V3>",
        ])

        self.assertEqual(driver.get("meters.pafwd"), "250.5")
        self.assertEqual(driver.get("system.uptime"), "12 days 03:14:15")

        create_connection.assert_called_once()
        self.assertEqual(
            sock.sent[1:],
            [b"get meters.pafwd\r\n", b"get system.uptime\r\n"],
        )

    def test_unknown_parameter_response_is_explicitly_unsupported(self):
        driver, _sock, _create_connection = self._connect([
            b"get meters.pafwd\r\nUnknown parameter\r\nTX-V3>",
        ])

        with self.assertRaises(UnsupportedTransmitterParameter):
            driver.get("meters.pafwd")

    def test_rejected_login_without_final_prompt_fails_immediately(self):
        sock = FakeSocket([b"Password:", b"Login incorrect\r\n"])
        with patch(
            "monitoring.services.transmitters.bw_tx300v3.socket.create_connection",
            return_value=sock,
        ):
            driver = BWTx300v3Driver(
                "203.0.113.10", 23, self.password, timeout=30.0
            )
            with self.assertRaisesMessage(
                TransmitterError, "authentication failed"
            ):
                driver.__enter__()
        self.assertTrue(sock.closed)

    def test_password_never_appears_in_authentication_error(self):
        sock = FakeSocket([b"Password:", b"Access denied\r\n"])
        with patch(
            "monitoring.services.transmitters.bw_tx300v3.socket.create_connection",
            return_value=sock,
        ):
            driver = BWTx300v3Driver(
                "203.0.113.10", 23, self.password, timeout=1.0
            )
            with self.assertRaises(TransmitterError) as context:
                driver.__enter__()
        self.assertNotIn(self.password, str(context.exception))

    @patch("monitoring.services.transmitters.bw_tx300v3.socket.create_connection")
    def test_missing_password_fails_before_opening_socket(self, create_connection):
        driver = BWTx300v3Driver("203.0.113.10", 23, "", timeout=1.0)

        with self.assertRaisesMessage(
            TransmitterConfigurationError, "requires a password"
        ):
            driver.__enter__()

        create_connection.assert_not_called()

    def test_early_close_before_complete_command_response_raises(self):
        driver, _sock, _create_connection = self._connect([b"17", b""])

        with self.assertRaisesMessage(TransmitterError, "closed the connection"):
            driver.get("meters.pafwd")

    def test_wait_for_prompt_is_deadline_bounded(self):
        driver = BWTx300v3Driver(
            "203.0.113.10", 23, self.password, timeout=1.0
        )
        driver._sock = FakeSocket([socket.timeout()])
        with patch(
            "monitoring.services.transmitters.bw_tx300v3.time.monotonic",
            side_effect=[0.0, 0.0, 2.0],
        ):
            with self.assertRaisesMessage(TransmitterError, "Timed out"):
                driver._read_until(driver.prompt, "command response")


class BWTelemetryTests(SimpleTestCase):
    def setUp(self):
        self.driver = BWTx300v3Driver(
            "203.0.113.10", 23, "test-only-placeholder"
        )

    def test_canonical_numeric_text_state_and_frequency_mapping(self):
        values = {
            "meters.pafwd": "100",
            "meters.parev": "4",
            "meters.patemp": "47.5",
            "metering.rf_out_status": "on",
            "transmitter.frequency": "99500000",
            "system.uptime": "12 days 03:14:15",
            "metering.power_control": "Locked",
            "status.VSWRLimitActive": "off",
            "status.TempLimitActive": "on",
            "meters.fallback": "off",
            "metering.fallback_cause": "None",
            "system.product.id": "TX300",
            "system.software.version": "3.2-test",
        }
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            status = self.driver.read_status()

        self.assertEqual(status["forward_power_watts"], 100.0)
        self.assertEqual(status["reflected_power_watts"], 4.0)
        self.assertEqual(status["pa_temperature_c"], 47.5)
        self.assertAlmostEqual(status["vswr"], 1.5)
        self.assertEqual(status["rf_output_state"], "on")
        self.assertEqual(status["frequency_hz"], 99500000)
        self.assertEqual(status["uptime_raw"], "12 days 03:14:15")
        self.assertEqual(status["power_control_state"], "Locked")
        self.assertIs(status["vswr_limit_active"], False)
        self.assertIs(status["temperature_limit_active"], True)
        self.assertIs(status["fallback_active"], False)
        self.assertEqual(status["fallback_cause"], "None")
        self.assertEqual(status["product_id"], "TX300")
        self.assertEqual(status["software_version"], "3.2-test")
        self.assertNotIn("fan_speed_rpm", status)

    def test_legacy_numeric_compatibility_mapping(self):
        values = {
            "meters.pafwd": "250.5",
            "meters.parev": "2.5",
            "meters.patemp": "44.25",
        }
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            self.assertEqual(self.driver.get("psu.fwd_power"), "250.5")
            self.assertEqual(self.driver.get("psu.rev_power"), "2.5")
            self.assertEqual(self.driver.get("psu.pa_temperature"), "44.25")

    def test_legacy_vswr_is_computed_from_forward_and_reflected_power(self):
        values = {"meters.pafwd": "100", "meters.parev": "4"}
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            self.assertAlmostEqual(float(self.driver.get("psu.vswr")), 1.5)

    def test_legacy_checks_share_native_values_within_one_poll_connection(self):
        self.driver._sock = FakeSocket([
            b"get meters.pafwd\r\n100\r\nTX-V3>",
            b"get meters.parev\r\n4\r\nTX-V3>",
        ])

        self.assertEqual(self.driver.get("psu.fwd_power"), "100")
        self.assertEqual(self.driver.get("psu.rev_power"), "4")
        self.assertAlmostEqual(float(self.driver.get("psu.vswr")), 1.5)

        self.assertEqual(
            self.driver._sock.sent,
            [b"get meters.pafwd\r\n", b"get meters.parev\r\n"],
        )

    def test_computed_vswr_is_a_compatibility_alias_for_psu_vswr(self):
        """r0015: the legacy WRJE reference computed:vswr must be a
        supported compatibility identifier that delegates to the exact
        same guarded forward/reflected-power calculation already
        exposed as psu.vswr -- never a second VSWR implementation."""
        self.assertTrue(self.driver.supports_reference("computed:vswr"))
        values = {"meters.pafwd": "247.0", "meters.parev": "4.0"}
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            computed_vswr = self.driver.get("computed:vswr")
        with patch.object(
            self.driver, "_get_native", side_effect=lambda name: values[name]
        ):
            psu_vswr = self.driver.get("psu.vswr")
        self.assertEqual(computed_vswr, psu_vswr)
        self.assertAlmostEqual(float(computed_vswr), 1.2916252451934438)

    def test_board_and_dsp_temperature_are_supported_read_only_native_references(self):
        """r0015: field-verified on WRJE's real TX300v3 (firmware
        2.0-R) -- aio.temp.board / aio.temp.dsp are native/compatibility
        references only, reached through the existing read-only command
        path, never a new canonical read_status() metric."""
        self.assertTrue(self.driver.supports_reference("aio.temp.board"))
        self.assertTrue(self.driver.supports_reference("aio.temp.dsp"))
        self.assertNotIn("aio.temp.board", self.driver.supported_canonical_metrics)
        self.assertNotIn("aio.temp.dsp", self.driver.supported_canonical_metrics)

        self.driver._sock = FakeSocket([
            b"get aio.temp.board\r\n49 (C)\r\nTX-V3>",
        ])
        self.assertEqual(self.driver.get("aio.temp.board"), "49 (C)")
        self.assertEqual(self.driver._sock.sent, [b"get aio.temp.board\r\n"])

        self.driver._sock = FakeSocket([
            b"get aio.temp.dsp\r\n57 (C)\r\nTX-V3>",
        ])
        self.assertEqual(self.driver.get("aio.temp.dsp"), "57 (C)")
        self.assertEqual(self.driver._sock.sent, [b"get aio.temp.dsp\r\n"])

    def test_psu_voltage_and_current_remain_explicitly_unsupported(self):
        """r0015: live WRJE queries returned meters.psuvoltage -> '342'
        and meters.psucurrent -> '934' with no established scaling/
        units. These must stay out of every allowlist/compatibility
        map -- this is an intentional exclusion, not an oversight."""
        for reference in ("meters.psuvoltage", "meters.psucurrent"):
            with self.subTest(reference=reference):
                self.assertFalse(self.driver.supports_reference(reference))
                self.assertNotIn(reference, self.driver.safe_native_parameters)
                self.assertNotIn(reference, self.driver.supported_canonical_metrics)
                self.assertIn(reference, self.driver._UNSUPPORTED_LEGACY)
                with self.assertRaises(UnsupportedTransmitterParameter):
                    self.driver.get(reference)

    def test_unsupported_psu_references_never_send_a_get_command(self):
        for reference in ("meters.psuvoltage", "meters.psucurrent"):
            with self.subTest(reference=reference):
                self.driver._sock = FakeSocket([])
                with self.assertRaises(UnsupportedTransmitterParameter):
                    self.driver.get(reference)
                self.assertEqual(self.driver._sock.sent, [])

    def test_vswr_guards_invalid_inputs(self):
        for forward, reflected in (
            (0, 0), (-1, 0), (100, -1), (100, 100), (100, 101),
            (float("nan"), 1), (100, float("inf")), ("bad", 1),
        ):
            with self.subTest(forward=forward, reflected=reflected):
                self.assertIsNone(compute_vswr(forward, reflected))

    def test_legacy_indicator_mapping_preserves_existing_fault_values(self):
        cases = (
            ("status.indicator.rf", "on", "G"),
            ("status.indicator.rf", "off", "R"),
            ("status.indicator.vswr", "on", "R"),
            ("status.indicator.vswr", "off", "G"),
            ("status.indicator.temp", "on", "R"),
            ("status.indicator.temp", "off", "G"),
        )
        for reference, raw, expected in cases:
            with self.subTest(reference=reference, raw=raw):
                with patch.object(self.driver, "_get_native", return_value=raw):
                    self.assertEqual(self.driver.get(reference), expected)

    def test_unfamiliar_legacy_indicator_values_remain_unavailable(self):
        for reference in (
            "status.indicator.rf",
            "status.indicator.vswr",
            "status.indicator.temp",
        ):
            with self.subTest(reference=reference):
                with patch.object(
                    self.driver, "_get_native", return_value="fault?"
                ):
                    self.assertIsNone(self.driver.get(reference))

    def test_unknown_bw_fan_units_are_not_mapped_to_legacy_rpm(self):
        self.assertFalse(
            self.driver.supports_reference("psu.fan_speed_measured_fan1")
        )
        self.assertTrue(self.driver.supports_reference("meters.fanspeed"))
        self.assertNotIn("fan_speed_rpm", self.driver.supported_canonical_metrics)
        with self.assertRaises(UnsupportedTransmitterParameter):
            self.driver.get("psu.fan_speed_measured_fan1")

    def test_rf_interlock_is_explicitly_unsupported(self):
        self.assertFalse(self.driver.supports_reference("status.rf_interlock"))

    def test_empty_canonical_request_performs_no_queries(self):
        with patch.object(self.driver, "_get_native") as get_native:
            self.assertEqual(self.driver.read_status([]), {})
        get_native.assert_not_called()


class TransmitterFactoryTests(SimpleTestCase):
    def _config(self, transmitter_type):
        return SimpleNamespace(
            transmitter_type=transmitter_type,
            host="203.0.113.10",
            port=23,
            password="test-only-placeholder",
            timeout_seconds=2.5,
        )

    @patch("monitoring.services.transmitters.bw_tx300v3.socket.create_connection")
    def test_none_selects_no_driver_and_opens_no_connection(self, connect):
        self.assertIsNone(create_transmitter_driver(self._config("none")))
        connect.assert_not_called()

    def test_cobalt_selects_adapter_without_password(self):
        driver = create_transmitter_driver(self._config("cobalt_c300"))
        self.assertIsInstance(driver, CobaltTransmitterDriver)
        self.assertTrue(driver.supports_reference("psu.fwd_power"))
        self.assertFalse(hasattr(driver, "password"))

    def test_bw_selects_password_authenticated_driver(self):
        driver = create_transmitter_driver(self._config("bw_tx300v3"))
        self.assertIsInstance(driver, BWTx300v3Driver)
        self.assertEqual(driver.password, "test-only-placeholder")

    def test_factory_delegates_construction_to_registered_driver(self):
        future_driver = MagicMock()
        built_driver = object()
        future_driver.from_config.return_value = built_driver
        with patch.dict(
            DRIVER_REGISTRY,
            {
                "future_tx": TransmitterDriverRegistration(
                    "future_tx", "Future transmitter", future_driver
                ),
            },
            clear=True,
        ):
            config = self._config("future_tx")
            self.assertIs(create_transmitter_driver(config), built_driver)

        future_driver.from_config.assert_called_once_with(config)

    def test_unrecognized_type_fails_safely(self):
        with self.assertRaises(TransmitterConfigurationError):
            create_transmitter_driver(self._config("unknown-vendor"))

    @patch("monitoring.services.transmitters.cobalt.TransmitterClient")
    def test_cobalt_adapter_delegates_to_existing_client_unchanged(self, client_cls):
        client = MagicMock()
        client.get.return_value = "249.42 W"
        client_cls.return_value = client
        driver = CobaltTransmitterDriver("203.0.113.10", 23, timeout=3.0)

        with driver:
            self.assertEqual(driver.get("psu.fwd_power"), "249.42 W")

        client_cls.assert_called_once_with("203.0.113.10", 23, 3.0)
        client.__enter__.assert_called_once_with()
        client.get.assert_called_once_with("psu.fwd_power")
        client.__exit__.assert_called_once_with(None, None, None)
