from unittest.mock import MagicMock, patch

from django.test import TransactionTestCase

from monitoring.models import MonitorCheck, TransmitterConfig
from monitoring.services import monitor as monitor_module
from monitoring.services.transmitters import UnsupportedTransmitterParameter


class MonitorManagerTransmitterTests(TransactionTestCase):
    def setUp(self):
        MonitorCheck.objects.all().update(enabled=False)
        self.config = TransmitterConfig.load()
        self.config.host = "203.0.113.10"
        self.config.port = 23
        self.config.password = "test-only-placeholder"
        self.config.poll_interval_seconds = 30
        self.config.save()

    def _check(self, name, parameter, *, sort_order=1):
        check, _created = MonitorCheck.objects.update_or_create(
            name=name,
            defaults={
                "kind": "transmitter_param",
                "transmitter_parameter": parameter,
                "warning_threshold": 50,
                "critical_threshold": 25,
                "threshold_direction": "below",
                "consecutive_failures_required": 1,
                "sort_order": sort_order,
                "enabled": True,
            },
        )
        return check

    def _run(self, manager):
        written = []
        with (
            patch.object(manager, "_write_state", side_effect=written.append),
            patch.object(manager, "_poll_shoutcast_listeners"),
            patch("monitoring.services.monitor.maybe_notify") as notify,
        ):
            manager._run_cycle()
        return written[0], notify

    @patch("monitoring.services.monitor.create_transmitter_driver")
    def test_none_omits_transmitter_checks_clears_cache_and_never_notifies(
        self, factory
    ):
        check = self._check("TX Forward Power", "psu.fwd_power")
        self.config.transmitter_type = TransmitterConfig.TYPE_NONE
        self.config.save()
        manager = monitor_module.MonitorManager()
        manager._last_tx_result[check.id] = {
            "status": "ok", "detail": {"value": 250.0}
        }
        manager._current_status[check.id] = "critical"

        results, notify = self._run(manager)

        self.assertEqual(results, [])
        factory.assert_not_called()
        notify.assert_not_called()
        self.assertNotIn(check.id, manager._last_tx_result)
        self.assertNotIn(check.id, manager._current_status)

    @patch("monitoring.services.monitor.create_transmitter_driver")
    def test_bw_uses_one_driver_and_omits_unsupported_fan_check(self, factory):
        supported = self._check("TX Forward Power", "psu.fwd_power")
        unsupported = self._check(
            "TX Fan 1 Speed", "psu.fan_speed_measured_fan1", sort_order=2
        )
        self.config.transmitter_type = TransmitterConfig.TYPE_BW_TX300V3
        self.config.save()
        driver = MagicMock()
        driver.supports_reference.side_effect = (
            lambda reference: reference == "psu.fwd_power"
        )
        driver.__enter__.return_value = driver
        driver.__exit__.return_value = False
        driver.get.return_value = "245.5"
        factory.return_value = driver
        manager = monitor_module.MonitorManager()

        results, _notify = self._run(manager)

        factory.assert_called_once()
        factory_config = factory.call_args.args[0]
        self.assertEqual(factory_config.transmitter_type, "bw_tx300v3")
        self.assertEqual(factory_config.password, "test-only-placeholder")
        driver.__enter__.assert_called_once_with()
        driver.__exit__.assert_called_once_with(None, None, None)
        driver.get.assert_called_once_with("psu.fwd_power")
        transmitter_ids = [
            result["id"] for result in results
            if result["kind"].startswith("transmitter_")
        ]
        self.assertEqual(transmitter_ids, [supported.id])
        self.assertNotIn(unsupported.id, manager._last_tx_result)
        supported_result = next(
            result for result in results if result["id"] == supported.id
        )
        self.assertEqual(supported_result["detail"]["value"], 245.5)
        self.assertNotIn("test-only-placeholder", repr(results))

    @patch("monitoring.services.monitor.create_transmitter_driver")
    def test_cobalt_selection_continues_existing_raw_parameter_behavior(
        self, factory
    ):
        check = self._check("TX Forward Power", "psu.fwd_power")
        self.config.transmitter_type = TransmitterConfig.TYPE_COBALT_C300
        self.config.password = "must-not-be-needed-by-cobalt"
        self.config.save()
        driver = MagicMock()
        driver.supports_reference.return_value = True
        driver.__enter__.return_value = driver
        driver.__exit__.return_value = False
        driver.get.return_value = "249.42 W"
        factory.return_value = driver
        manager = monitor_module.MonitorManager()

        results, _notify = self._run(manager)

        factory.assert_called_once()
        driver.get.assert_called_once_with("psu.fwd_power")
        self.assertEqual(results[0]["id"], check.id)
        self.assertEqual(results[0]["detail"]["value"], 249.42)

    @patch("monitoring.services.monitor.create_transmitter_driver")
    def test_transmitter_connection_failure_does_not_block_other_checks(
        self, factory
    ):
        self._check("TX Forward Power", "psu.fwd_power")
        MonitorCheck.objects.create(
            name="Disk",
            kind="disk",
            disk_path="/",
            warning_threshold=80,
            critical_threshold=90,
            consecutive_failures_required=1,
            sort_order=2,
        )
        self.config.transmitter_type = TransmitterConfig.TYPE_BW_TX300V3
        self.config.save()
        driver = MagicMock()
        driver.supports_reference.return_value = True
        driver.__enter__.side_effect = OSError("connection refused")
        factory.return_value = driver
        manager = monitor_module.MonitorManager()

        with patch.dict(
            monitor_module.PROBE_DISPATCH,
            {"disk": lambda check: ("ok", {"percent": 10.0})},
        ):
            results, _notify = self._run(manager)

        self.assertEqual(len(results), 2)
        by_name = {result["name"]: result for result in results}
        self.assertEqual(by_name["TX Forward Power"]["status"], "unknown")
        self.assertEqual(by_name["Disk"]["status"], "ok")

    @patch("monitoring.services.monitor.create_transmitter_driver")
    def test_device_reported_unsupported_check_is_omitted_without_notification(
        self, factory
    ):
        check = self._check("TX Forward Power", "psu.fwd_power")
        self.config.transmitter_type = TransmitterConfig.TYPE_BW_TX300V3
        self.config.save()
        driver = MagicMock()
        driver.supports_reference.return_value = True
        driver.__enter__.return_value = driver
        driver.__exit__.return_value = False
        driver.get.side_effect = UnsupportedTransmitterParameter(
            "Unknown parameter"
        )
        factory.return_value = driver
        manager = monitor_module.MonitorManager()

        results, notify = self._run(manager)

        self.assertEqual(results, [])
        notify.assert_not_called()
        self.assertNotIn(check.id, manager._last_tx_result)
