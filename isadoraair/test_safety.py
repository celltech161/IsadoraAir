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
``_fixture_teardown`` so that, before anything is flushed, every database alias
the test would touch must prove BOTH:

1. Test-database identity. The configured NAME starts with ``test_`` (checked
   before any connection is opened, so a production NAME is never even
   connected to) AND the server-reported ``current_database()`` does too.
2. Non-production role identity. Neither ``current_user`` nor ``session_user``
   is, or is a member of, a production role; the role is not a superuser; and
   the test database is not owned by a production role (a ``test_*`` database
   created with production credentials). Production roles are derived from the
   station's own installation config -- see ``production_installation()`` --
   not from a hard-coded name.

It fails closed. If the identity query fails, or this is a station host whose
production role cannot be derived, the fixture is refused. The only relaxation
is ``ISADORAAIR_TEST_ISOLATED_CI=1``, which declares "this host has no
IsadoraAir installation, so there is no production role to derive"; it is
refused outright on any host carrying a station marker, and it relaxes nothing
else (both name checks, the superuser check and any derivable production role
still apply). There is deliberately no switch that disables the guard.

Refusals raise ``UnsafeTestDatabaseError`` with a ``category``:
``WRONG_DATABASE_NAME``, ``PRODUCTION_ROLE_DETECTED``,
``IDENTITY_UNESTABLISHED`` or ``CI_OVERRIDE_NOT_PERMITTED``. The message names
the alias, the actual database, the effective role, the failed rule and the
fix. It never includes passwords or connection strings.

It is imported only from the test packages (see ``<app>/tests/__init__.py``), so
it adds nothing to production runtime. It is the *second* line of defence; the
first is that feature worktrees use their own development database role with no
access to production (see docs/DEVELOPMENT_DATABASE.md).
"""
from dataclasses import dataclass
import functools
import json
import os
from pathlib import Path

from django.db import connections
from django.test import testcases

from isadoraair.db_credentials import DEFAULT_STATION_CONFIG

TEST_DATABASE_PREFIX = "test_"
_INSTALLED_MARK = "_isadoraair_test_safety_installed"

# Where the station's production identity lives. station.json is root-only on
# a station, so the application environment file it names -- at the canonical
# app root checked by isadoraair/deploy_baseline.py -- is the source a
# developer account can usually read. Only DB_USER/DB_NAME are ever read.
STATION_CONFIG_PATH = DEFAULT_STATION_CONFIG
PRODUCTION_APP_ROOT = Path("/opt/isadoraair")
PRODUCTION_ENVIRONMENT_FILE = PRODUCTION_APP_ROOT / ".env"

ISOLATED_CI_ENV = "ISADORAAIR_TEST_ISOLATED_CI"

WRONG_DATABASE_NAME = "WRONG_DATABASE_NAME"
PRODUCTION_ROLE_DETECTED = "PRODUCTION_ROLE_DETECTED"
IDENTITY_UNESTABLISHED = "IDENTITY_UNESTABLISHED"
CI_OVERRIDE_NOT_PERMITTED = "CI_OVERRIDE_NOT_PERMITTED"

_FIXES = {
    WRONG_DATABASE_NAME: (
        "Run tests ONLY with `manage.py test <label>` (which creates and uses test_<NAME>) "
        "from a feature worktree whose .env points at the development database; never load "
        "test classes through `manage.py shell` or a plain unittest runner."
    ),
    PRODUCTION_ROLE_DETECTED: (
        "Point the worktree .env at the development role (DB_NAME=isadoraair_dev, "
        "DB_USER=isadoraair_dev; see docs/DEVELOPMENT_DATABASE.md). Never use production "
        "credentials, a superuser, or a database created by the production role for tests."
    ),
    IDENTITY_UNESTABLISHED: (
        "The guard could not prove which role and database it is connected to, or which "
        "role is production on this host, so it refuses. Check the database is reachable "
        "with the worktree .env and that the production DB_USER can be read from "
        f"{STATION_CONFIG_PATH} or {PRODUCTION_ENVIRONMENT_FILE}. Only on an isolated CI host "
        f"with no IsadoraAir installation may {ISOLATED_CI_ENV}=1 be set."
    ),
    CI_OVERRIDE_NOT_PERMITTED: (
        f"Unset {ISOLATED_CI_ENV}. It is only for isolated CI hosts with no IsadoraAir "
        "installation; on this host use the development role instead."
    ),
}


class UnsafeTestDatabaseError(AssertionError):
    """A destructive test fixture was about to run on an unproven database."""

    def __init__(self, category, *, alias, rule, database=None, role=None, detail=None):
        self.category = category
        self.alias = alias
        self.rule = rule
        self.database = database
        self.role = role
        lines = [
            f"Refusing to run a destructive test fixture (TransactionTestCase flush): {category}",
            f"  alias:          {alias!r}",
            f"  database:       {database if database is not None else '<not established>'}",
            f"  effective role: {role if role is not None else '<not established>'}",
            f"  failed rule:    {rule}",
        ]
        if detail:
            lines.append(f"  detail:         {detail}")
        lines.append(f"  fix:            {_FIXES[category]}")
        super().__init__("\n".join(lines))


@dataclass(frozen=True)
class ProductionInstallation:
    """What this host says production is. Never holds a password."""

    station_markers: tuple
    roles: frozenset
    database_names: frozenset
    sources: tuple
    problems: tuple


@dataclass(frozen=True)
class SessionIdentity:
    database: str
    current_user: str
    session_user: str
    is_superuser: object  # bool, or None when the role row could not be read
    database_owner: object
    production_roles: tuple
    production_memberships: tuple


def _read_environment_identity(path):
    """Return {DB_USER, DB_NAME} from an env file; nothing else is retained."""
    wanted = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            key, separator, value = line.strip().partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if separator and key in ("DB_USER", "DB_NAME"):
                wanted[key] = value.strip().strip("'\"")
    return wanted


@functools.lru_cache(maxsize=1)
def production_installation():
    """Derive production role/database names from this host's installation config.

    Sources, in order: the station config (root-only on a station, so usually
    unreadable to a developer) and the application environment file it names,
    then the canonical production environment file. Unreadable sources are
    recorded as problems, not errors; the caller decides whether what *was*
    derived is enough. Cached per process; see ``reset_cached_installation``.
    """
    markers = tuple(
        str(path)
        for path in (STATION_CONFIG_PATH, PRODUCTION_ENVIRONMENT_FILE, PRODUCTION_APP_ROOT / "manage.py")
        if os.path.lexists(path)
    )
    roles, names, sources, problems = set(), set(), [], []
    environment_files = []
    if os.path.lexists(STATION_CONFIG_PATH):
        try:
            station = json.loads(Path(STATION_CONFIG_PATH).read_text(encoding="utf-8"))
            database = station.get("database") or {}
            if database.get("user"):
                roles.add(str(database["user"]))
            if database.get("name"):
                names.add(str(database["name"]))
            if station.get("application_environment_file"):
                environment_files.append(Path(station["application_environment_file"]))
            sources.append(str(STATION_CONFIG_PATH))
        except (OSError, ValueError, AttributeError) as exc:
            problems.append(f"{STATION_CONFIG_PATH}: {type(exc).__name__}")
    environment_files.append(PRODUCTION_ENVIRONMENT_FILE)
    for path in dict.fromkeys(environment_files):
        if not os.path.lexists(path):
            continue
        try:
            identity = _read_environment_identity(path)
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: {type(exc).__name__}")
            continue
        if identity.get("DB_USER"):
            roles.add(identity["DB_USER"])
        if identity.get("DB_NAME"):
            names.add(identity["DB_NAME"])
        sources.append(str(path))
    return ProductionInstallation(
        station_markers=markers,
        roles=frozenset(roles),
        database_names=frozenset(names),
        sources=tuple(sources),
        problems=tuple(problems),
    )


def reset_cached_installation():
    production_installation.cache_clear()


_IDENTITY_SQL = """
WITH production AS (
    SELECT unnest(%s::text[]) AS rolname
    UNION
    SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = ANY(%s::text[])
)
SELECT current_database(),
       current_user,
       session_user,
       (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
       (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()),
       ARRAY(SELECT rolname FROM production ORDER BY 1),
       ARRAY(
           SELECT r.rolname FROM pg_roles r JOIN production p USING (rolname)
           WHERE pg_has_role(current_user, r.oid, 'MEMBER')
              OR pg_has_role(session_user, r.oid, 'MEMBER')
           ORDER BY 1
       )
"""


def query_session_identity(alias, installation):
    """Ask the server who and where this alias really is (one read-only query)."""
    connection = connections[alias]
    if connection.vendor != "postgresql":
        raise RuntimeError(f"unsupported database vendor {connection.vendor!r}")
    with connection.cursor() as cursor:
        cursor.execute(
            _IDENTITY_SQL,
            [sorted(installation.roles), sorted(installation.database_names)],
        )
        row = cursor.fetchone()
    return SessionIdentity(
        database=row[0],
        current_user=row[1],
        session_user=row[2],
        is_superuser=row[3],
        database_owner=row[4],
        production_roles=tuple(row[5]),
        production_memberships=tuple(row[6]),
    )


def _isolated_ci_requested():
    value = os.environ.get(ISOLATED_CI_ENV)
    if value is None or value == "":
        return False
    if value != "1":
        raise UnsafeTestDatabaseError(
            CI_OVERRIDE_NOT_PERMITTED, alias="*",
            rule=f"{ISOLATED_CI_ENV} must be exactly '1' when set (got an unrecognised value)",
        )
    return True


def check_alias(alias, installation=None):
    """Raise ``UnsafeTestDatabaseError`` unless ``alias`` is a proven test database."""
    configured = str(connections[alias].settings_dict.get("NAME") or "")
    if not configured.startswith(TEST_DATABASE_PREFIX):
        # Refuse before connecting: a production NAME is never even opened.
        raise UnsafeTestDatabaseError(
            WRONG_DATABASE_NAME, alias=alias, database=f"{configured!r} (configured)",
            rule=f"configured database name must start with {TEST_DATABASE_PREFIX!r}",
        )

    installation = installation or production_installation()
    isolated_ci = _isolated_ci_requested()
    if isolated_ci and installation.station_markers:
        raise UnsafeTestDatabaseError(
            CI_OVERRIDE_NOT_PERMITTED, alias=alias, database=configured,
            rule=f"{ISOLATED_CI_ENV}=1 is refused on a host with an IsadoraAir installation",
            detail="station markers present: " + ", ".join(installation.station_markers),
        )
    if not installation.roles and not isolated_ci:
        detail = "; ".join(installation.problems) or "no readable station config or production .env"
        if not installation.station_markers:
            detail += " (no IsadoraAir installation found on this host)"
        raise UnsafeTestDatabaseError(
            IDENTITY_UNESTABLISHED, alias=alias, database=configured,
            rule="the production database role could not be derived, so the effective role "
                 "cannot be proven non-production",
            detail=detail,
        )

    try:
        identity = query_session_identity(alias, installation)
    except Exception as exc:  # noqa: BLE001 -- any failure here means "unproven"
        code = getattr(exc, "pgcode", None)
        raise UnsafeTestDatabaseError(
            IDENTITY_UNESTABLISHED, alias=alias, database=configured,
            rule="the database/role identity query failed",
            detail=f"{type(exc).__name__}" + (f" (SQLSTATE {code})" if code else ""),
        ) from None

    role = identity.current_user
    if identity.session_user != identity.current_user:
        role = f"{identity.current_user} (session role {identity.session_user})"
    where = f"{identity.database} (configured {configured})"
    if not str(identity.database or "").startswith(TEST_DATABASE_PREFIX):
        raise UnsafeTestDatabaseError(
            WRONG_DATABASE_NAME, alias=alias, database=where, role=role,
            rule=f"server-reported current_database() must start with {TEST_DATABASE_PREFIX!r}",
        )
    if identity.is_superuser is None:
        raise UnsafeTestDatabaseError(
            IDENTITY_UNESTABLISHED, alias=alias, database=where, role=role,
            rule="the effective role's attributes could not be read from pg_roles",
        )
    derived_from = ", ".join(installation.sources) or f"{ISOLATED_CI_ENV}=1 (no installation)"
    if identity.production_memberships:
        raise UnsafeTestDatabaseError(
            PRODUCTION_ROLE_DETECTED, alias=alias, database=where, role=role,
            rule="the effective role is, or is a member of, a production role",
            detail=f"production roles {list(identity.production_roles)} derived from {derived_from}; "
                   f"matched {list(identity.production_memberships)}",
        )
    if identity.is_superuser:
        raise UnsafeTestDatabaseError(
            PRODUCTION_ROLE_DETECTED, alias=alias, database=where, role=role,
            rule="the effective role is a PostgreSQL superuser (it can reach production)",
        )
    if identity.database_owner in identity.production_roles:
        raise UnsafeTestDatabaseError(
            PRODUCTION_ROLE_DETECTED, alias=alias, database=where, role=role,
            rule="the test database is owned by a production role (created with production "
                 "credentials)",
            detail=f"owner {identity.database_owner!r}; production roles derived from {derived_from}",
        )
    return identity


def assert_safe_test_databases(aliases=None):
    """Raise unless every given alias (default: all) is a proven test database."""
    for alias in (aliases if aliases is not None else list(connections)):
        check_alias(alias)


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
