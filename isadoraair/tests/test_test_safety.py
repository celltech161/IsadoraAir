"""Regression coverage for the destructive-test guard (isadoraair/test_safety.py).

Incident of 2026-09-30: a TransactionTestCase run outside ``manage.py test``
flushed the real database. These tests prove the guard refuses BEFORE any flush
and that it is installed for every test package.

r0104/1.18 added role identity: a ``test_*`` database reached with a production
role (or a superuser, or a test database created by the production role) is
refused too, production roles are derived from the station's installation
config, and every failure to establish identity fails closed.
"""
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from django.db import connections
from django.test import SimpleTestCase, TransactionTestCase

from isadoraair import test_safety
from isadoraair.test_safety import (
    CI_OVERRIDE_NOT_PERMITTED,
    IDENTITY_UNESTABLISHED,
    PRODUCTION_ROLE_DETECTED,
    WRONG_DATABASE_NAME,
    ProductionInstallation,
    SessionIdentity,
    UnsafeTestDatabaseError,
    assert_safe_test_databases,
    check_alias,
)

SECRET = "s3cr3t-never-printed"

STATION = ProductionInstallation(
    station_markers=("/etc/isadoraair/station.json", "/opt/isadoraair/.env"),
    roles=frozenset({"isadoraair"}),
    database_names=frozenset({"isadoraair"}),
    sources=("/opt/isadoraair/.env",),
    problems=("/etc/isadoraair/station.json: PermissionError",),
)
NO_INSTALLATION = ProductionInstallation((), frozenset(), frozenset(), (), ())


def _identity(database="test_isadoraair_dev", user="isadoraair_dev", *, session_user=None,
              superuser=False, owner=None, memberships=()):
    return SessionIdentity(
        database=database,
        current_user=user,
        session_user=session_user or user,
        is_superuser=superuser,
        database_owner=owner or user,
        production_roles=("isadoraair",),
        production_memberships=tuple(memberships),
    )


def _fake_connections(**names):
    return {
        alias: SimpleNamespace(
            settings_dict={"NAME": name, "USER": "isadoraair_dev", "PASSWORD": SECRET},
            vendor="postgresql",
        )
        for alias, name in names.items()
    }


def _guarded_case():
    class _Dummy(TransactionTestCase):
        def runTest(self):  # pragma: no cover - never executed
            pass

    return _Dummy


class _GuardTestBase(SimpleTestCase):
    databases = {"default"}

    def setUp(self):
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(test_safety.ISOLATED_CI_ENV, None)

    def _with_database_name(self, name):
        return mock.patch.dict(connections["default"].settings_dict, {"NAME": name})

    def _installation(self, installation):
        return mock.patch.object(test_safety, "production_installation", return_value=installation)

    def _session(self, identity=None, *, side_effect=None):
        return mock.patch.object(
            test_safety, "query_session_identity",
            return_value=identity, side_effect=side_effect,
        )

    def _refusal(self, category, **check):
        with self.assertRaises(UnsafeTestDatabaseError) as raised:
            check_alias("default", **check)
        self.assertEqual(raised.exception.category, category, str(raised.exception))
        self.assertNotIn(SECRET, str(raised.exception))
        return raised.exception


class RealRunnerIdentityTests(_GuardTestBase):
    def test_the_runner_database_is_a_proven_test_database(self):
        # Under `manage.py test` Django has already switched to test_<name>,
        # and this asks the real server who we are.
        self.assertTrue(connections["default"].settings_dict["NAME"].startswith("test_"))
        test_safety.reset_cached_installation()
        self.addCleanup(test_safety.reset_cached_installation)
        identity = check_alias("default")
        self.assertLessEqual(
            test_safety.production_installation().roles, set(identity.production_roles)
        )
        self.assertTrue(identity.database.startswith("test_"))
        self.assertEqual(identity.current_user, connections["default"].settings_dict["USER"])
        self.assertFalse(identity.is_superuser)
        self.assertEqual(identity.production_memberships, ())
        self.assertNotIn(identity.database_owner, identity.production_roles)
        assert_safe_test_databases()


class IdentityMatrixTests(_GuardTestBase):
    def test_test_database_with_development_role_is_allowed(self):
        with self._installation(STATION), self._session(_identity()):
            identity = check_alias("default")
        self.assertEqual(identity.current_user, "isadoraair_dev")

    def test_non_test_database_with_development_role_is_refused_before_connecting(self):
        with self._installation(STATION), self._session(_identity()) as query, \
                self._with_database_name("isadoraair_dev"):
            error = self._refusal(WRONG_DATABASE_NAME)
        query.assert_not_called()
        self.assertIn("'isadoraair_dev' (configured)", str(error))

    def test_production_database_with_production_role_is_refused_before_connecting(self):
        with self._installation(STATION), \
                self._session(_identity("isadoraair", "isadoraair")) as query, \
                self._with_database_name("isadoraair"):
            self._refusal(WRONG_DATABASE_NAME)
        query.assert_not_called()

    def test_test_database_with_production_role_is_refused(self):
        # The pre-r0104 gap: NAME=test_isadoraair, USER=isadoraair passed.
        session = _identity("test_isadoraair", "isadoraair", memberships=["isadoraair"])
        with self._installation(STATION), self._session(session):
            error = self._refusal(PRODUCTION_ROLE_DETECTED)
        self.assertEqual(error.alias, "default")
        self.assertIn("effective role: isadoraair", str(error))
        self.assertIn("/opt/isadoraair/.env", str(error))

    def test_production_role_reached_through_set_role_is_refused(self):
        session = _identity(user="isadoraair_dev", session_user="isadoraair",
                            memberships=["isadoraair"])
        with self._installation(STATION), self._session(session):
            error = self._refusal(PRODUCTION_ROLE_DETECTED)
        self.assertIn("session role isadoraair", str(error))

    def test_server_reporting_a_non_test_database_is_refused(self):
        # Configured name looks right but the server says otherwise.
        with self._installation(STATION), self._session(_identity(database="isadoraair_dev")):
            self._refusal(WRONG_DATABASE_NAME)

    def test_superuser_is_refused(self):
        with self._installation(STATION), self._session(_identity(user="postgres", superuser=True)):
            error = self._refusal(PRODUCTION_ROLE_DETECTED)
        self.assertIn("superuser", error.rule)

    def test_test_database_created_by_the_production_role_is_refused(self):
        # The stale Codex-created test_isadoraair: owned by `isadoraair`.
        with self._installation(STATION), \
                self._session(_identity("test_isadoraair", owner="isadoraair")):
            error = self._refusal(PRODUCTION_ROLE_DETECTED)
        self.assertIn("owned by a production role", error.rule)

    def test_identity_query_failure_fails_closed_without_leaking_credentials(self):
        failure = RuntimeError(f"password={SECRET} host=localhost")
        with self._installation(STATION), self._session(side_effect=failure):
            error = self._refusal(IDENTITY_UNESTABLISHED)
        self.assertIn("identity query failed", error.rule)
        self.assertIn("RuntimeError", str(error))

    def test_unreadable_role_attributes_fail_closed(self):
        with self._installation(STATION), self._session(_identity(superuser=None)):
            self._refusal(IDENTITY_UNESTABLISHED)

    def test_station_host_without_a_derivable_production_role_fails_closed(self):
        unreadable = ProductionInstallation(
            ("/etc/isadoraair/station.json",), frozenset(), frozenset(), (),
            ("/etc/isadoraair/station.json: PermissionError",),
        )
        with self._installation(unreadable), self._session(_identity()) as query:
            error = self._refusal(IDENTITY_UNESTABLISHED)
        query.assert_not_called()
        self.assertIn("PermissionError", str(error))

    def test_host_without_any_installation_fails_closed_by_default(self):
        with self._installation(NO_INSTALLATION), self._session(_identity()) as query:
            error = self._refusal(IDENTITY_UNESTABLISHED)
        query.assert_not_called()
        self.assertIn("no IsadoraAir installation found", str(error))
        self.assertIn(test_safety.ISOLATED_CI_ENV, str(error))

    def test_multiple_aliases_with_one_unsafe_are_refused(self):
        fake = _fake_connections(default="test_isadoraair_dev", replica="isadoraair")
        with mock.patch.object(test_safety, "connections", fake), \
                self._installation(STATION), self._session(_identity()):
            with self.assertRaises(UnsafeTestDatabaseError) as raised:
                assert_safe_test_databases(["default", "replica"])
            assert_safe_test_databases(["default"])
        self.assertEqual(raised.exception.alias, "replica")
        self.assertEqual(raised.exception.category, WRONG_DATABASE_NAME)
        self.assertNotIn(SECRET, str(raised.exception))

    def test_diagnostics_name_alias_database_role_rule_and_fix(self):
        session = _identity("test_isadoraair", "isadoraair", memberships=["isadoraair"])
        with self._installation(STATION), self._session(session):
            error = self._refusal(PRODUCTION_ROLE_DETECTED)
        message = str(error)
        for expected in (
            "PRODUCTION_ROLE_DETECTED", "alias:          'default'",
            "database:       test_isadoraair", "effective role: isadoraair",
            "failed rule:", "fix:", "isadoraair_dev",
        ):
            self.assertIn(expected, message)


class IsolatedCiOverrideTests(_GuardTestBase):
    def _ci(self, value="1"):
        os.environ[test_safety.ISOLATED_CI_ENV] = value

    def test_override_is_off_by_default(self):
        self.assertNotIn(test_safety.ISOLATED_CI_ENV, os.environ)
        with self._installation(NO_INSTALLATION), self._session(_identity()):
            self._refusal(IDENTITY_UNESTABLISHED)

    def test_override_allows_a_development_role_on_a_host_with_no_installation(self):
        self._ci()
        with self._installation(NO_INSTALLATION), self._session(_identity()):
            self.assertEqual(check_alias("default").current_user, "isadoraair_dev")

    def test_override_is_refused_on_a_station_host(self):
        self._ci()
        with self._installation(STATION), self._session(_identity()) as query:
            error = self._refusal(CI_OVERRIDE_NOT_PERMITTED)
        query.assert_not_called()
        self.assertIn("/etc/isadoraair/station.json", str(error))

    def test_override_relaxes_nothing_else(self):
        self._ci()
        with self._installation(NO_INSTALLATION):
            with self._session(_identity()), self._with_database_name("isadoraair"):
                self._refusal(WRONG_DATABASE_NAME)
            with self._session(_identity(database="ci_real_db")):
                self._refusal(WRONG_DATABASE_NAME)
            with self._session(_identity(user="postgres", superuser=True)):
                self._refusal(PRODUCTION_ROLE_DETECTED)
            with self._session(side_effect=RuntimeError("down")):
                self._refusal(IDENTITY_UNESTABLISHED)

    def test_override_requires_an_exact_value(self):
        for value in ("true", "yes", "0", " 1"):
            with self.subTest(value=value):
                self._ci(value)
                with self._installation(NO_INSTALLATION), self._session(_identity()):
                    self._refusal(CI_OVERRIDE_NOT_PERMITTED)


class ProductionInstallationDerivationTests(SimpleTestCase):
    """production_installation() reads real files; paths point at a temp dir."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="isa-guard-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.station = self.root / "station.json"
        self.app = self.root / "app"
        self.app.mkdir()
        self.env = self.app / ".env"
        for name, value in (
            ("STATION_CONFIG_PATH", self.station),
            ("PRODUCTION_APP_ROOT", self.app),
            ("PRODUCTION_ENVIRONMENT_FILE", self.env),
        ):
            patcher = mock.patch.object(test_safety, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        test_safety.reset_cached_installation()
        self.addCleanup(test_safety.reset_cached_installation)

    def test_roles_come_from_the_production_environment_file_only_db_keys(self):
        self.env.write_text(
            f"SECRET_KEY={SECRET}\nDB_NAME=isadoraair\nexport DB_USER='isadoraair'\n"
            f"DB_PASSWORD={SECRET}\n",
            encoding="utf-8",
        )
        installation = test_safety.production_installation()
        self.assertEqual(installation.roles, {"isadoraair"})
        self.assertEqual(installation.database_names, {"isadoraair"})
        self.assertIn(str(self.env), installation.station_markers)
        self.assertNotIn(SECRET, repr(installation))

    def test_station_config_and_the_environment_file_it_names_are_both_used(self):
        other_env = self.root / "other.env"
        other_env.write_text("DB_USER=isadoraair_app\nDB_NAME=isadoraair_live\n", encoding="utf-8")
        self.station.write_text(json.dumps({
            "database": {"name": "isadoraair", "user": "isadoraair"},
            "application_environment_file": str(other_env),
        }), encoding="utf-8")
        installation = test_safety.production_installation()
        self.assertEqual(installation.roles, {"isadoraair", "isadoraair_app"})
        self.assertEqual(installation.database_names, {"isadoraair", "isadoraair_live"})

    @unittest.skipIf(os.geteuid() == 0, "root can read a 0000 file")
    def test_unreadable_station_config_is_a_problem_and_a_marker_not_a_role(self):
        self.station.write_text(json.dumps({"database": {"user": "isadoraair"}}), encoding="utf-8")
        self.station.chmod(0)
        installation = test_safety.production_installation()
        self.assertEqual(installation.roles, frozenset())
        self.assertIn(str(self.station), installation.station_markers)
        self.assertTrue(any("PermissionError" in problem for problem in installation.problems))

    def test_no_installation_has_no_markers_and_no_roles(self):
        installation = test_safety.production_installation()
        self.assertEqual(installation.station_markers, ())
        self.assertEqual(installation.roles, frozenset())


class FixtureHookTests(_GuardTestBase):
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

    def test_setup_and_teardown_refuse_a_production_role_before_any_flush(self):
        case = _guarded_case()
        session = _identity("test_isadoraair", "isadoraair", memberships=["isadoraair"])
        with self._installation(STATION), self._session(session), \
                mock.patch("django.test.testcases.call_command") as call_command:
            for hook in (case._fixture_setup, case("runTest")._fixture_teardown):
                with self.subTest(hook=hook.__name__):
                    with self.assertRaises(UnsafeTestDatabaseError) as raised:
                        hook()
                    self.assertEqual(raised.exception.category, PRODUCTION_ROLE_DETECTED)
        call_command.assert_not_called()

    def test_setup_and_teardown_fail_closed_when_identity_is_unestablished(self):
        case = _guarded_case()
        with self._installation(STATION), self._session(side_effect=RuntimeError("down")), \
                mock.patch("django.test.testcases.call_command") as call_command:
            for hook in (case._fixture_setup, case("runTest")._fixture_teardown):
                with self.subTest(hook=hook.__name__):
                    with self.assertRaises(UnsafeTestDatabaseError) as raised:
                        hook()
                    self.assertEqual(raised.exception.category, IDENTITY_UNESTABLISHED)
        call_command.assert_not_called()

    def test_a_proven_test_database_still_flushes_normally(self):
        case = _guarded_case()
        with mock.patch("django.test.testcases.call_command") as call_command:
            case("runTest")._fixture_teardown()
        self.assertEqual(call_command.call_args.args[0], "flush")

    def test_simple_test_case_never_consults_the_guard_or_a_database(self):
        class _Simple(SimpleTestCase):
            def test_nothing(self):
                pass

        result = unittest.TestResult()
        with mock.patch.object(test_safety, "check_alias") as check, \
                mock.patch.object(test_safety, "query_session_identity") as query:
            unittest.TestSuite([_Simple("test_nothing")]).run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        check.assert_not_called()
        query.assert_not_called()

    def test_installation_is_idempotent(self):
        setup = TransactionTestCase.__dict__["_fixture_setup"]
        teardown = TransactionTestCase._fixture_teardown
        test_safety.install()
        test_safety.install()
        self.assertIs(TransactionTestCase.__dict__["_fixture_setup"], setup)
        self.assertIs(TransactionTestCase._fixture_teardown, teardown)

    def test_every_test_package_installs_the_guard(self):
        for app in (
            "aircheck", "authz", "encoders", "hardware", "isadoraair", "library", "monitoring",
            "road_conditions", "updatecenter", "weather", "weather_ingest", "webrequests",
        ):
            with self.subTest(app=app):
                module = importlib.import_module(f"{app}.tests")
                self.assertIs(vars(module).get("_test_safety"), test_safety)
        self.assertTrue(getattr(TransactionTestCase, test_safety._INSTALLED_MARK))
