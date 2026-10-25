-- r0118 post-install verification. Read-only and always rolled back.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

SELECT app, name
FROM django_migrations
WHERE app = 'authz' AND name = '0010_remote_dj_admin_capabilities';

SELECT slug, requires_schedule
FROM authz_capability
WHERE slug IN ('remote_dj.emergency_mute', 'remote_dj.force_disconnect')
ORDER BY slug;

SELECT c.slug, r.name
FROM authz_rolecapability AS rc
JOIN authz_capability AS c ON c.id = rc.capability_id
JOIN authz_role AS r ON r.id = rc.role_id
WHERE c.slug IN ('remote_dj.emergency_mute', 'remote_dj.force_disconnect')
ORDER BY c.slug, r.name;

SELECT c.slug, r.name
FROM authz_rolecapability AS rc
JOIN authz_capability AS c ON c.id = rc.capability_id
JOIN authz_role AS r ON r.id = rc.role_id
WHERE c.slug IN ('remote_dj.emergency_mute', 'remote_dj.force_disconnect')
  AND r.name <> 'Station Administrator'
ORDER BY c.slug, r.name;

ROLLBACK;
