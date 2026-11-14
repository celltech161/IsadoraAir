# r0138 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, apply the migration, restart a service or change a
station merely because r0138 has been published.

## Exact identity and trusted plan

- Previous published release: r0137 at `ac22f4fd8445b22342fbaa334c5cf3f697ea644c`.
- Accepted engineering commits, in order:
  - `9f4e20f164349c408943fb19540fea1876cf7588`;
  - `95a15d67de1f38a6e14bc8f290081c6924cb0d12`;
  - `edc208346d2f2981dfcc6cd591a1678f197ba49d`, tree
    `785c6b2270631d72c7881c0594c08bc43d59d3df`.
- R is the unique canonical commit introducing `deploy/releases/r0138.json`, directly after the accepted engineering
  history.
- Required migration: `library.0101_uitheme_show_played_items`; migration file SHA-256
  `497d32112dc713f2dc486e8c608b5786912694cd3e646f17351f4cf30ded7f94`.
- A direct r0137 → r0138 plan advances source to R, applies that additive migration, then restarts
  `isadoraair-gunicorn` followed by `isadoraair-engine`.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action.
- A station below r0137 must retain every intervening release, migration and restart in the cumulative trusted plan.

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. Establish the installed release from the UpdateCenter installation record and verify the installed checkout is
   clean. Do not infer release identity from `HEAD` alone.
2. Confirm trusted UpdateCenter and protected-runtime health, the supported updater protocol, no running job and no
   failed job awaiting operator intervention.
3. Record schema health, applied migrations and pending migrations. The direct plan from r0137 must add only
   `library.0101_uitheme_show_played_items`.
4. Confirm current backups and recovery readiness. Preserve the installation preview, action fingerprint and current
   release evidence.
5. Record service PIDs, activation times and health for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages
   and spoken rendering. Record program audio and encoder connection state.
6. Record the current and next approved hourly logs, current track, queue order and playing decks. Confirm no log build
   or top-of-hour transition is underway.
7. Record pending urgent insertions, Active Messages and Weather/AMBER announcements. Defer if an urgent item would be
   disrupted by the engine restart.
8. Confirm whether a Remote DJ is connected. An engine restart can end that session; defer or obtain the presenter's
   explicit agreement.
9. Record the existing `UITheme` row and Console/Remote DJ settings, including the absence of the new field before
   migration where applicable.
10. Choose an operationally appropriate engine-restart window with an operator present. An engine restart can briefly
    interrupt program audio.
11. Preview the exact canonical target R and plan immediately before installation. For a direct r0137 upgrade the
    action fingerprint must describe source advance, migration 0101, Gunicorn restart and engine restart only.

Stop if identity, schema, fingerprint, migration, restart inventory or service health differs. Do not shorten or
manually reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0137 it must:

1. Advance source to the exact canonical r0138 release commit R.
2. Apply `library.0101_uitheme_show_played_items` as an additive migration.
3. Restart `isadoraair-gunicorn`.
4. Restart `isadoraair-engine`.

No apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or protected-runtime
operation is expected. Do not apply migrations or restart services outside the authorized update job. Do not retry a
failed job automatically.

## KOGR preflight and acceptance

Preserve KOGR's existing Roadmap 2.27 Hour Refill observation-only configuration and evidence:

- `dry_run_only` remains checked;
- host installation arming remains disabled and `HOUR_REFILL_INSTALL_ARMED` remains false;
- cause/capture switches, policy rows, scheduled timers and accumulated observation evidence remain unchanged;
- r0137 Weather/AMBER configuration, snapshots, state and polling schedules remain unchanged.

Record Hour Refill policy, runner status, latest evidence and timers before installation. The engine restart is a
temporary interruption of observation, not authorization to arm Hour Refill, reset its evidence or alter scheduling.
After installation, verify that observation resumes with the same policy and that the next scheduled evidence task
succeeds.

Do not change KOGR logs, playout settings, alert configuration, Active Messages or Library content as part of this UI
release.

## WRJE preflight and acceptance

WRJE requires independent evidence and authorization. Do not assume it matches KOGR. Verify its installed release,
clean checkout, current schema, pending migrations, `UITheme` configuration, current and next log, engine queue,
Remote DJ session, service health, alert state and UpdateCenter status directly.

Determine whether an active Remote DJ session or station-specific queue activity makes the engine restart unsafe at
the proposed time. Success at KOGR is neither authorization nor acceptance evidence for WRJE. Do not alter WRJE's
Remote DJ policy or permissions to test this release.

## Passive post-install acceptance

Acceptance on air is passive. Potentially disruptive queue interaction should be exercised only in an isolated or
non-airing environment.

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Verify the installed release is r0138 at the exact canonical R and the source checkout is clean.
3. Verify migration `library.0101_uitheme_show_played_items` is applied, schema health is current and existing
   `UITheme` rows received `show_played_items=False`.
4. Verify Gunicorn and engine restarted in that order. Confirm unrelated services remained healthy and were not
   unnecessarily restarted.
5. Confirm program audio, encoders, current item and queue advancement are healthy after the engine restart.
6. With Show played items still Off, confirm the Coming Up table retains its ordinary upcoming-only behavior.
7. Confirm Intro values display as `M:SS`, null is blank and explicit zero is `0:00` in both Console and Remote DJ.
8. In an appropriate non-airing window, enable Show played items through Django Admin and reload the page. Confirm the
   current hour's played, current/on-deck and upcoming rows appear with the documented shading and outline.
9. Confirm played and on-deck rows have no drag or Next controls; ordinary queue controls still name only upcoming
   items and existing connection-stale protections remain effective.
10. Confirm a normal hour adoption displays the new active log and no late previous-hour history appears.
11. Confirm the history display recovers after a transient endpoint failure without disturbing the authoritative live
    queue. Do not deliberately blackhole production traffic to test the six-second timeout.
12. Confirm Console and Remote DJ remain accessible only under their existing authorization rules.
13. Confirm Weather/AMBER, Hour Refill, Active Messages, scheduling and unrelated operational settings are unchanged.

## Accepted inherited limitations

- The server-side reorder API can accept played `LogItem` IDs from an otherwise authorized direct caller. The r0138
  browser limits its payload to upcoming IDs.
- Default Remote DJ GroupAccess and the existing `schedule.edit` requirement may prevent actual Remote DJ reorder even
  though the shared UI displays a control.
- An item can become claimed during a drag already in progress before the reorder reaches the server.
- Mobile queue tables may require horizontal scrolling.

Do not change authorization or queue semantics during installation to work around these inherited limitations.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint, migration output, service journal, engine state,
queue snapshot and browser evidence if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance, migration 0101 and the two declared restarts appears for a direct r0137 upgrade;
- migration or schema verification fails;
- Gunicorn or the engine fails to restart;
- program audio, encoder connectivity or queue advancement is unhealthy;
- the new field does not default Off;
- played rows acquire queue controls or history changes the live queue;
- stale history crosses an hour boundary;
- Weather/AMBER, Hour Refill, Active Messages or another unrelated service changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0138 is not rollback authority and does not authorize an
installation at the other station.
