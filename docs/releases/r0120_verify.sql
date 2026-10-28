-- r0120 post-install verification. Read-only and always rolled back.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

-- Expect exactly the four hour-refill migrations, each once.
SELECT app, name
FROM django_migrations
WHERE app = 'library' AND name IN (
    '0093_hour_refill_cause_policy', '0094_logitem_origin',
    '0095_hour_refill_install', '0096_hour_refill_install_gate')
ORDER BY name;

-- Expect no row: 0096 removed the Phase A dry-run constraint.
SELECT c.conname
FROM pg_constraint AS c
JOIN pg_class AS t ON t.oid = c.conrelid
WHERE t.relname = 'library_hourrefillpolicy'
  AND c.conname = 'hour_refill_policy_dry_run_only_phase_a';

-- Expect no row after install. Any row must be enabled = false,
-- dry_run_only = true, both cause switches false.
SELECT id, enabled, capture_request_substitution, capture_operator_eject, dry_run_only
FROM library_hourrefillpolicy
ORDER BY id;

-- Expect 0, 0, 0 while the feature is Off.
SELECT
    (SELECT count(*) FROM library_hourrefillcause) AS causes,
    (SELECT count(*) FROM library_hourrefillinstall) AS installs,
    (SELECT count(*) FROM library_logitem WHERE origin = 'refill_installed') AS refill_rows;

ROLLBACK;
