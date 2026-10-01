-- Developer/test database isolation for IsadoraAir feature worktrees.
--
-- Run ONCE per host, as the PostgreSQL superuser, never as the application role:
--
--   sudo -u postgres psql -X -v ON_ERROR_STOP=1 -v dev_password='<password>' \
--        -f deploy/dev/create_dev_database.sql
--
-- It creates a dedicated, unprivileged login role and a private database for
-- development. The role owns nothing in, and is granted nothing on, the
-- production database, so even a mistaken test run from a feature worktree
-- cannot read, flush or drop production data. `manage.py test` still works:
-- CREATEDB lets Django create and drop its disposable `test_<name>` database.
--
-- Production credentials, the production role and the production database's
-- contents are NOT touched by this script.

\set ON_ERROR_STOP on

SELECT format(
    'CREATE ROLE isadoraair_dev LOGIN NOSUPERUSER NOCREATEROLE NOREPLICATION CREATEDB PASSWORD %L',
    :'dev_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'isadoraair_dev') \gexec

SELECT 'CREATE DATABASE isadoraair_dev OWNER isadoraair_dev'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'isadoraair_dev') \gexec

-- The development database is private to its owner.
REVOKE ALL ON DATABASE isadoraair_dev FROM PUBLIC;

-- Report the resulting boundary.
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication
FROM pg_roles WHERE rolname = 'isadoraair_dev';
