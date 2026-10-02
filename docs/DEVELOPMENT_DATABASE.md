# Development and test database isolation

**Feature worktrees must never use the production database.** On 2026-09-30 a
`TransactionTestCase` was loaded through `manage.py shell` with a plain
`unittest` runner. That skipped Django's test-database setup, so the test's
teardown flushed every table of the *production* database (restored from backup
afterwards). Two independent layers now prevent a repeat.

## Layer 1 -- a development role that cannot touch production

* Production: database `isadoraair`, role `isadoraair` (owner). Unchanged.
* Development: role `isadoraair_dev` (login, **no** superuser, createrole or
  replication; `CREATEDB` so Django can create/drop `test_isadoraair_dev`) and
  its own private database `isadoraair_dev`. It owns nothing in, and is granted
  nothing on, production.

One-time setup (PostgreSQL superuser, never the application role):

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -v dev_password='<password>' \
     -f deploy/dev/create_dev_database.sql
```

Optional hardening that removes PostgreSQL's default "any role may CONNECT"
from the production database (it only removes access; see the file's header):

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f deploy/dev/restrict_production_connect.sql
```

Each feature worktree has its **own** git-ignored `.env`, copied from
`.env.example`, with `DB_NAME=isadoraair_dev`, `DB_USER=isadoraair_dev` and the
dev password. `python-decouple` reads the `.env` in the worktree root, so the
worktree cannot reach production unless someone deliberately sources the
production `.env` into the shell -- **do not do that**. Never `source`, copy or
symlink the production `.env` into a worktree.

## Layer 2 -- the code guard

`isadoraair/test_safety.py` (installed by every `<app>/tests/__init__.py`)
wraps `TransactionTestCase` fixture setup and teardown -- the code that
flushes. Before anything runs, every database alias the test touches must
prove **both** of the following, or the fixture is refused:

1. **Test database identity.** The configured `NAME` starts with `test_` (the
   prefix `manage.py test` always uses). This is checked *before* any
   connection is opened, so a production `NAME` is never even connected to.
   The server-reported `current_database()` must start with `test_` too.
2. **Non-production role identity**, from one read-only catalog query:
   * neither `current_user` nor `session_user` is, or is a member of, a
     production role;
   * the role is not a PostgreSQL superuser;
   * the test database is not owned by a production role. This catches a
     `test_*` database created with production credentials, such as the stale
     `test_isadoraair` below.

Production roles are **derived from the station's own installation config**,
never hard-coded:

* `database.user` and `database.name` from `/etc/isadoraair/station.json`.
  That file is root-only, so a developer account usually records it as
  unreadable.
* `DB_USER` and `DB_NAME` from the application environment file that the
  station config names, and from `/opt/isadoraair/.env`. No other key is read.
* The owners of those production databases, read from `pg_database`.

It **fails closed**. The fixture is refused when the identity query fails,
when a role's attributes can't be read, or when this is a station host whose
production role can't be derived.

Every refusal raises `UnsafeTestDatabaseError` with one of these categories:

| Category | Meaning |
| --- | --- |
| `WRONG_DATABASE_NAME` | the configured or actual database is not `test_*` |
| `PRODUCTION_ROLE_DETECTED` | a production role or membership, a superuser, or a test database owned by production |
| `IDENTITY_UNESTABLISHED` | the guard could not prove who or where it is |
| `CI_OVERRIDE_NOT_PERMITTED` | see below |

The message names the alias, the actual database, the effective role, the rule
that failed and the fix. It never contains a password or connection string.

### Isolated CI hosts

A host with no IsadoraAir installation has no production role to derive, so it
is refused by default. Only there may the guard be told so explicitly:

```bash
ISADORAAIR_TEST_ISOLATED_CI=1 python manage.py test <label> --noinput
```

The value must be exactly `1`. The override is **refused outright** on any host
with a station marker: `/etc/isadoraair/station.json`, `/opt/isadoraair/.env`
or `/opt/isadoraair/manage.py`. It relaxes only "a production role must be
derivable". Both name checks, the superuser check and the identity query still
apply, so a CI database role must be a non-superuser `CREATEDB` role, like
`isadoraair_dev`. There is deliberately no switch that disables the guard.

### Running tests

Run tests only as:

```bash
python manage.py test <label> --noinput
```

Never load test classes with `unittest` from `manage.py shell`, and never run
`flush` outside a disposable database.

## Old worktrees and the stale `test_isadoraair` database

Worktrees created before this isolation existed may still have
`DB_NAME=isadoraair` / `DB_USER=isadoraair` in their `.env`, and their code has
no guard. Do not run tests from them; re-point their `.env` at the development
role or remove the worktree.

`test_isadoraair` is a leftover Django test database. A `manage.py test` run
with the production `.env` created it as the production role. Nothing
references it. Django would recreate it, and drop it, on any such run. It can
be dropped only by its owner or a superuser. This is an operator action; the
development role is deliberately not granted that privilege. Plain `DROP
DATABASE`, without `FORCE`, refuses if anything is still connected:

```bash
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres -c 'DROP DATABASE test_isadoraair;'
```
