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
refuses `TransactionTestCase` fixture setup/teardown -- the code that flushes --
unless the active database name starts with `test_` (the prefix
`manage.py test` always uses). Run tests only as:

```bash
python manage.py test <label> --noinput
```

Never load test classes with `unittest` from `manage.py shell`, and never run
`flush` outside a disposable database.
