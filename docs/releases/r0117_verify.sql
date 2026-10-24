-- r0117 post-install verification (read-only; everything is rolled back).
-- psql -f docs/releases/r0117_verify.sql  (as the application database user)
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '10s';
SET LOCAL lock_timeout = '2s';

-- 1. The three r0117 migrations are recorded.
SELECT app, name, applied FROM django_migrations
 WHERE (app, name) IN (('authz', '0009_playout_start_next_capability'),
                       ('library', '0091_playouttransportconfig'),
                       ('library', '0092_remote_dj_start_next_access'))
 ORDER BY app, name;

-- 2. playout.start_next exists and is schedule-restricted.
SELECT slug, requires_schedule FROM authz_capability WHERE slug = 'playout.start_next';

-- 3. Exactly Station Administrator and Remote Host hold it.
SELECT r.name AS r0117_start_next_role FROM authz_rolecapability rc
  JOIN authz_role r ON r.id = rc.role_id
  JOIN authz_capability c ON c.id = rc.capability_id
 WHERE c.slug = 'playout.start_next' ORDER BY r.name;

-- 4. Remote Host still does not hold deck transport or the Studio Mic.
SELECT c.slug AS r0117_must_be_empty FROM authz_rolecapability rc
  JOIN authz_role r ON r.id = rc.role_id
  JOIN authz_capability c ON c.id = rc.capability_id
 WHERE r.name = 'Remote Host' AND c.slug IN ('playout.control', 'studio.mic_ptt');

-- 5. remote_dj reaches the endpoint through exactly one exact path.
SELECT g.name, position('/api/engine/start-next/' IN ga.allowed_exact) > 0 AS r0117_path_present
  FROM library_groupaccess ga JOIN auth_group g ON g.id = ga.group_id WHERE g.name = 'remote_dj';

-- 6. The settings table exists (the singleton row is created on first use).
SELECT to_regclass('library_playouttransportconfig') IS NOT NULL AS r0117_config_table,
       (SELECT count(*) FROM library_playouttransportconfig) AS r0117_config_rows;

ROLLBACK;
