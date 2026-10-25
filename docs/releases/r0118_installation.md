# r0118 installation and verification

This is candidate guidance only. It does not authorize publication or a KOGR
or WRJE install. Each station requires its own approved Update Center action.

## Preconditions

1. Confirm the trusted target is the reviewed r0118 companion tip and that the
   release manifest resolves to its immediately preceding release commit.
2. Confirm no Remote DJ is connected. Stop rather than displacing a live
   session during installation.
3. Run the Update Center plan/probe and preserve its exact migration-plan
   digest. The plan must contain only
   `authz.0010_remote_dj_admin_capabilities`, operation 0 `RunPython`, as the
   manual operation for r0118.
4. Validate the r0118 trusted companion. Do not use a companion copied from a
   different target commit or manifest.

## Read-only production database preflight

Run as the normal application database role. This transaction does not mutate
station data:

```sql
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '5s';
SET LOCAL lock_timeout = '2s';

SELECT name
FROM authz_role
WHERE name = 'Station Administrator';

SELECT c.slug, c.label, c.requires_schedule,
       COALESCE(array_agg(r.name ORDER BY r.name)
                FILTER (WHERE r.name IS NOT NULL), ARRAY[]::varchar[]) AS existing_roles
FROM authz_capability AS c
LEFT JOIN authz_rolecapability AS rc ON rc.capability_id = c.id
LEFT JOIN authz_role AS r ON r.id = rc.role_id
WHERE c.slug IN ('remote_dj.emergency_mute', 'remote_dj.force_disconnect')
GROUP BY c.slug, c.label, c.requires_schedule
ORDER BY c.slug;

ROLLBACK;
```

Expected before migration: exactly one Station Administrator row. No capability
rows is the normal r0117 state. If either slug already exists, review its label,
schedule flag, and grants before proceeding; do not guess that locally-created
authority is equivalent.

## Install

Use the normal Update Center workflow against the reviewed trusted tip. Central
authorization must cover exactly the r0118 operation above. Keep the normal
database checkpoint and migration preflight. No collectstatic, package install,
nginx reconciliation, protected-runtime transition, or daemon reload is
required.

Restart only, in this order:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`
3. `isadoraair-monitoring`

## Post-install verification

Run `docs/releases/r0118_verify.sql` read-only and confirm:

- `authz.0010_remote_dj_admin_capabilities` is recorded once;
- both emergency capability rows exist with `requires_schedule = false`;
- only Station Administrator holds those two seeded grants.

Then verify through the normal UI/API without opening a production microphone:

- an authorized Station Administrator sees Emergency Mute and Kick only for a
  connected Remote DJ, while Remote Host does not receive those capabilities;
- ordinary Remote Mic authority remains owner- and session-bound;
- Monitoring remains active after restart and a controlled test notification
  uses the configured backend;
- Start Next is compact on desktop, mobile-sized on portrait view, keyboard
  focus is visible, and its existing queued/engine-confirmed behavior remains
  unchanged.

Record the installed release, exact trusted tip, migration-plan digest,
checkpoint, restart results, and verification output separately for KOGR and
WRJE.
