-- OPTIONAL hardening, run as the PostgreSQL superuser:
--
--   sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f deploy/dev/restrict_production_connect.sql
--
-- By default PostgreSQL lets every role CONNECT to every database. This removes
-- that default for the production database so only its owner (the application
-- role) and superusers can connect. It only ever REMOVES access: the application
-- role keeps everything it has, and no credential changes.
--
-- Check first that no other role relies on the default:
--   SELECT rolname FROM pg_roles WHERE rolname !~ '^pg_' ORDER BY 1;
-- (at the time of writing: isadoraair, postgres, and the dev role).
--
-- Replace isadoraair below if the production database/role use another name.
REVOKE CONNECT ON DATABASE isadoraair FROM PUBLIC;
GRANT CONNECT ON DATABASE isadoraair TO isadoraair;
