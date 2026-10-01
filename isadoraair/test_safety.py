"""Developer/test-only safety: never run destructive test fixtures on a real database.

Why this exists
---------------
A Django ``TransactionTestCase`` ends every test by FLUSHING (truncating) every
table of the connection it runs on. Under ``manage.py test`` that connection is
the disposable ``test_<name>`` database Django creates. But if such a test class
is ever run any other way (for example loaded with ``unittest`` from
``manage.py shell``), Django's test-database setup is skipped and the flush hits
the real configured database. That is exactly how a production database was
wiped on 2026-09-30.

The guard
---------
``install()`` wraps ``TransactionTestCase._fixture_setup`` and
``_fixture_teardown`` so they refuse to run unless every database they would
touch is a Django-created test database (its name starts with ``test_``, the
prefix ``manage.py test`` always uses). It fails closed: with the guard active,
no test class can truncate a development or production database no matter how it
was launched.

It is imported only from the test packages (see ``<app>/tests/__init__.py``), so
it adds nothing to production runtime. It is the *second* line of defence; the
first is that feature worktrees use their own development database role with no
access to production (see docs/DEVELOPMENT_DATABASE.md).
"""
from django.db import connections
from django.test import testcases

TEST_DATABASE_PREFIX = "test_"
_INSTALLED_MARK = "_isadoraair_test_safety_installed"


class UnsafeTestDatabaseError(AssertionError):
    """A destructive test fixture was about to run on a non-test database."""


def assert_safe_test_databases(aliases=None):
    """Raise unless every given alias (default: all) is a Django test database."""
    for alias in (aliases if aliases is not None else list(connections)):
        name = str(connections[alias].settings_dict.get("NAME") or "")
        if not name.startswith(TEST_DATABASE_PREFIX):
            raise UnsafeTestDatabaseError(
                f"Refusing to run a destructive test fixture on database {name!r} "
                f"(alias {alias!r}): it is not a Django test database "
                f"(name must start with {TEST_DATABASE_PREFIX!r}). Run tests ONLY with "
                "`manage.py test <label>` from a feature worktree whose .env points at the "
                "development database; never load test classes through `manage.py shell` "
                "or a plain unittest runner."
            )


def install():
    """Idempotently guard ``TransactionTestCase`` fixture setup/teardown."""
    case = testcases.TransactionTestCase
    if getattr(case, _INSTALLED_MARK, False):
        return
    original_setup = case.__dict__["_fixture_setup"].__func__
    original_teardown = case._fixture_teardown

    def guarded_setup(cls):
        assert_safe_test_databases(cls._databases_names(include_mirrors=False))
        return original_setup(cls)

    def guarded_teardown(self):
        assert_safe_test_databases(self._databases_names(include_mirrors=False))
        return original_teardown(self)

    case._fixture_setup = classmethod(guarded_setup)
    case._fixture_teardown = guarded_teardown
    setattr(case, _INSTALLED_MARK, True)
