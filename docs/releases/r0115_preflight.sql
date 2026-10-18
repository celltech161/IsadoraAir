-- r0115 read-only database preflight (docs/releases/r0115.md, "Production
-- database preflight"). Run ONLY when separately authorized, on the target
-- station, BEFORE starting the r0115 update -- never as a substitute for the
-- Update Center's guarded migration. Nothing here writes, locks beyond
-- ACCESS SHARE, cancels or kills anything; the transaction is read-only and
-- rolled back.
--
--   cd /opt/isadoraair
--   sudo -u <the User= of the installed isadoraair-gunicorn unit> \
--       venv/bin/python manage.py dbshell -- -X -v ON_ERROR_STOP=1 -f docs/releases/r0115_preflight.sql
--
-- (Before the update the station runs r0114 or older, so this file is not on
-- its disk yet: copy it from the published r0115 commit, or paste it.)

BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '2s';

\echo '== 1. server and database'
SELECT current_database() AS database, current_user AS role, version() AS server;

\echo '== 2. active_messages migration state (expect 0001..0004 applied, 0005 absent)'
SELECT name, applied FROM django_migrations WHERE app = 'active_messages' ORDER BY id;

\echo '== 3. execution table: planner estimate, exact rows, sizes'
SELECT c.reltuples::bigint AS estimated_rows,
       pg_size_pretty(pg_relation_size(c.oid)) AS heap,
       pg_size_pretty(pg_indexes_size(c.oid)) AS indexes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total
FROM pg_class c WHERE c.oid = 'active_messages_activebroadcastexecution'::regclass;
SELECT count(*) AS exact_rows FROM active_messages_activebroadcastexecution;
SELECT state, count(*) FROM active_messages_activebroadcastexecution GROUP BY state ORDER BY state;

\echo '== 4. existing execution-table indexes (the two r0115 names must be absent) and their validity'
SELECT c.relname AS index, i.indisvalid AS valid, i.indisready AS ready, pg_get_indexdef(i.indexrelid) AS definition
FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
WHERE i.indrelid = 'active_messages_activebroadcastexecution'::regclass ORDER BY c.relname;
SELECT relname AS r0115_name_already_present FROM pg_class
WHERE relname IN ('active_execution_recent', 'active_execution_members');

\echo '== 5. open transactions on this database (oldest first)'
SELECT pid, usename, application_name, state, now() - xact_start AS transaction_age,
       wait_event_type, wait_event, left(query, 100) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND xact_start IS NOT NULL AND pid <> pg_backend_pid()
ORDER BY xact_start;

\echo '== 6. locks held or awaited on the execution table (expect none besides this session)'
SELECT l.pid, l.mode, l.granted, a.application_name, a.state, now() - a.xact_start AS transaction_age
FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid
WHERE l.relation = 'active_messages_activebroadcastexecution'::regclass AND l.pid <> pg_backend_pid();

\echo '== 7. sessions waiting on any lock'
SELECT pid, application_name, wait_event_type, wait_event, now() - query_start AS waiting_for, left(query, 100) AS query
FROM pg_stat_activity
WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid();

ROLLBACK;
