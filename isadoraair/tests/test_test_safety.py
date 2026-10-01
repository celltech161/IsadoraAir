"""Regression coverage for the destructive-test guard (isadoraair/test_safety.py).

Incident of 2026-09-30: a TransactionTestCase run outside ``manage.py test``
flushed the real database. These tests prove the guard refuses BEFORE any flush
and that it is installed for every test package.
"""
import importlib
from unittest import mock

from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase

from isadoraair import test_safety
from isadoraair.test_safety import UnsafeTestDatabaseError, assert_safe_test_databases


def _guarded_case():
    class _Dummy(TransactionTestCase):
        def runTest(self):  # pragma: no cover - never executed
            pass

    return _Dummy


class TestDatabaseGuardTests(SimpleTestCase):
    databases = {"default"}

    def _with_database_name(self, name):
        return mock.patch.dict(connections["default"].settings_dict, {"NAME": name})

    def test_the_runner_database_is_a_test_database(self):
        # Under `manage.py test` Django has already switched to test_<name>.
        self.assertTrue(connections["default"].settings_dict["NAME"].startswith("test_"))
        assert_safe_test_databases()

    def test_production_and_development_database_names_are_refused(self):
        for name in ("isadoraair", "isadoraair_dev", "postgres", ""):
            with self.subTest(name=name), self._with_database_name(name):
                with self.assertRaises(UnsafeTestDatabaseError):
                    assert_safe_test_databases()

    def test_teardown_refuses_before_any_flush_on_a_non_test_database(self):
        case = _guarded_case()
        with self._with_database_name("isadoraair"), \
                mock.patch("django.test.testcases.call_command") as call_command:
            with self.assertRaises(UnsafeTestDatabaseError):
                case("runTest")._fixture_teardown()
        call_command.assert_not_called()

    def test_setup_refuses_on_a_non_test_database(self):
        case = _guarded_case()
        with self._with_database_name("isadoraair"), \
                mock.patch("django.test.testcases.call_command") as call_command:
            with self.assertRaises(UnsafeTestDatabaseError):
                case._fixture_setup()
        call_command.assert_not_called()

    def test_a_test_database_still_flushes_normally(self):
        case = _guarded_case()
        with self._with_database_name("test_something"), \
                mock.patch("django.test.testcases.call_command") as call_command:
            case("runTest")._fixture_teardown()
        self.assertEqual(call_command.call_args.args[0], "flush")

    def test_installation_is_idempotent(self):
        before = TransactionTestCase._fixture_teardown
        test_safety.install()
        test_safety.install()
        self.assertIs(TransactionTestCase._fixture_teardown, before)

    def test_every_test_package_installs_the_guard(self):
        for app in (
            "aircheck", "authz", "encoders", "hardware", "isadoraair", "library", "monitoring",
            "road_conditions", "updatecenter", "weather", "weather_ingest", "webrequests",
        ):
            with self.subTest(app=app):
                module = importlib.import_module(f"{app}.tests")
                self.assertIs(vars(module).get("_test_safety"), test_safety)
        self.assertTrue(getattr(TransactionTestCase, test_safety._INSTALLED_MARK))
