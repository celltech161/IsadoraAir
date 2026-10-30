-- r0121 pre/post-install verification. Read-only and always rolled back.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

-- Post-install: expect exactly these two rows, once each.
SELECT app, name
FROM django_migrations
WHERE app = 'library' AND name IN (
    '0097_hour_refill_start_next_cause',
    '0098_hour_refill_automatic_failure_cause')
ORDER BY name;

-- Post-install: expect all four named constraints. Pre-install: only the
-- known-type constraint exists. Definitions are returned for review.
SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
FROM pg_constraint AS c
JOIN pg_class AS t ON t.oid = c.conrelid
WHERE t.relname = 'library_hourrefillcause'
  AND c.conname IN (
      'hour_refill_cause_known_type',
      'hour_refill_cause_start_next_fields',
      'hour_refill_cause_automatic_skip_fields',
      'hour_refill_cause_media_failure_fields')
ORDER BY c.conname;

-- At 0096 this returns every policy field available before r0121. At 0098 it
-- also returns the three new capture switches. Expect no row; if one exists,
-- inspect the complete JSON object for the schema actually installed.
SELECT to_jsonb(p) AS policy
FROM library_hourrefillpolicy AS p
ORDER BY p.id;

-- Report evidence; do not assume zero on an existing r0120 installation.
SELECT cause_type, count(*) AS evidence_rows
FROM library_hourrefillcause
GROUP BY cause_type
ORDER BY cause_type;

SELECT
    (SELECT count(*) FROM library_hourrefillinstall) AS installs,
    (SELECT count(*) FROM library_logitem WHERE origin = 'refill_installed') AS refill_rows;

ROLLBACK;
