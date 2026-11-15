# r0139 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, apply the migration, restart a service or change a
station merely because r0139 has been published.

## Exact identity and trusted plan

- Previous published release: r0138 at `167a806ef97dd224d6f1182d2492ba3d908a5da5`.
- Accepted engineering commits, in order:
  - `810714dad568ae387971b3f79518ccf7e62fad7f` (seven UI refinements);
  - `7fdfdd9ab8e1749f59c270f8db616ffb9296971a` (NEXT pill);
  - `17ce2ecf91f097f50c407947681b8478404625c6`, tree `b3cc88c09d69271021c6b434933933eaee23011a` (color-contract and
    widget round-trip correction).
- R is the unique canonical commit introducing `deploy/releases/r0139.json`, directly after the accepted engineering
  history, and adds only the four release files.
- Required migration: `library.0102_uitheme_log_display_appearance`; migration file SHA-256
  `d4d65fc0787d57925ae4a24111af84e1bdeb9b3b2d10e914b7d4ae5aa2d0e265`.
- A direct r0138 → r0139 plan advances source to R, applies that additive migration, then restarts
  `isadoraair-gunicorn` only.
- There is no manual migration companion, engine restart, package, requirements, collectstatic, nginx, systemd-unit,
  manual-bootstrap or protected-runtime action.
- A station below r0138 must retain every intervening release, migration and restart in the cumulative trusted plan
  (for example, a station on r0137 also applies `library.0101_uitheme_show_played_items` and restarts the engine for
  r0138).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. Establish the installed release from the UpdateCenter installation record and verify the installed checkout is
   clean. Do not infer release identity from `HEAD` alone.
2. Confirm trusted UpdateCenter and protected-runtime health, the supported updater protocol, no running job and no
   failed job awaiting operator intervention.
3. Record schema health, applied migrations and pending migrations. The direct plan from r0138 must add only
   `library.0102_uitheme_log_display_appearance`.
4. Confirm current backups and recovery readiness. Preserve the installation preview, action fingerprint and current
   release evidence.
5. Record service PIDs, activation times and health for Gunicorn, engine, encoders, Monitoring, RBDS, Active Messages
   and spoken rendering. Record program audio and encoder connection state.
6. Record the current and next approved hourly logs, current track, queue order and playing decks. Confirm no log build
   or top-of-hour transition is underway.
7. Record pending urgent insertions, Active Messages and Weather/AMBER announcements.
8. Record Remote DJ connection state. The engine is not restarted by this release, but Gunicorn is, so a
   browser-based session should be informed that pages and API requests may briefly fail.
9. Record the existing `UITheme` row and settings, including the current Show played items value, the configured
   palette and every configured color and opacity (including `deck_transport_disabled_opacity`), so they can be
   compared after installation.
10. Choose an operationally acceptable Gunicorn restart window with an operator present.
11. Preview the exact canonical target R and plan immediately before installation. For a direct r0138 upgrade the
    action fingerprint must describe source advance, migration 0102 and a Gunicorn restart only.

Stop if identity, schema, fingerprint, migration, restart inventory or service health differs. Do not shorten or
manually reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0138 it must:

1. Advance source to the exact canonical r0139 release commit R.
2. Apply `library.0102_uitheme_log_display_appearance` as an additive migration.
3. Restart `isadoraair-gunicorn`.

No engine restart, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected. Do not apply migrations or restart services outside the authorized update
job. Do not retry a failed job automatically.

## KOGR preflight and acceptance

Preserve KOGR's existing Roadmap 2.27 Hour Refill observation-only configuration and evidence:

- `dry_run_only` remains checked;
- host installation arming remains disabled and `HOUR_REFILL_INSTALL_ARMED` remains false;
- cause/capture switches, policy rows, scheduled timers and accumulated observation evidence remain unchanged;
- Weather/AMBER configuration, snapshots, state and polling schedules remain unchanged.

Record Hour Refill policy, runner status, latest evidence and timers before installation and confirm they are
unchanged afterward. Do not change KOGR logs, playout settings, alert configuration, Active Messages or Library
content as part of this UI release.

## WRJE preflight and acceptance

WRJE requires independent evidence and authorization. Do not assume it matches KOGR. Verify its installed release,
clean checkout, current schema, pending migrations, `UITheme` configuration, current and next log, engine queue,
Remote DJ state, service health, alert state and UpdateCenter status directly.

Success at KOGR is neither authorization nor acceptance evidence for WRJE. Do not alter WRJE's Remote DJ policy or
permissions to test this release.

## Passive post-install acceptance

Acceptance on air is passive; do not disturb live playout or restart the engine to test queue operations.

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Verify the installed release is r0139 at the exact canonical R and the source checkout is clean.
3. Verify migration `library.0102_uitheme_log_display_appearance` is applied, schema health is current and existing
   `UITheme` rows received `played_item_darkening=0.55` and `current_item_outline_color="rgba(255, 255, 255, 1.00)"`.
4. Verify Gunicorn restarted as declared, and that the engine and every unrelated service remained healthy without an
   unexpected restart.
5. Confirm program audio, encoders, current item and queue advancement remain normal.
6. In Django Admin → UI Theme, confirm both new fields show their defaults. Confirm the **Played item darkening**
   slider renders, shows its percentage and saves, and that the **Current item outline color** picker renders and
   saves. Confirm `deck_transport_disabled_opacity` retains its configured value on the new slider.
7. Confirm that previously stored colors (including any shorthand or alpha-hex values) are unchanged after opening the
   UI Theme form and saving an unrelated setting; compare with the preflight record.
8. On the Console and Remote DJ pages, confirm log header alignment, the Play and drag-sort icons, the NEXT pill and the
   current-row outline (when Show played items is enabled in an appropriate window). Confirm the NEXT pill still acts
   only on upcoming rows, and that existing queue controls and connection-stale protection are unchanged.
9. Confirm the clock and the hamburger both open and close the navigation menu on a desktop and on a narrow phone
   layout, on the Console and on at least one other page, with the clock still updating.
10. Confirm Weather/AMBER, Hour Refill, Active Messages, scheduling and unrelated operational settings are unchanged.

## Accepted limitations

- Unusual custom category palettes can reduce the contrast of the NEXT pill because its background is translucent.
- Inherited from r0138: the server-side reorder API can accept played `LogItem` IDs from an otherwise authorized direct
  caller; default Remote DJ GroupAccess and `schedule.edit` may prevent Remote DJ reorder; an item can become claimed
  during a drag already in progress; narrow screens may need horizontal scrolling for the queue table.

Do not change authorization or queue semantics during installation to work around these limitations.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint, migration output, service journal and browser
evidence if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance, migration 0102 and the Gunicorn restart appears for a direct r0138 upgrade;
- migration or schema verification fails;
- Gunicorn fails to restart, or the engine or another service restarts unexpectedly;
- program audio, encoder connectivity or queue advancement is unhealthy;
- the new fields do not default as documented, or a stored color changes;
- played rows acquire queue controls or the history display changes the live queue;
- Weather/AMBER, Hour Refill, Active Messages or another unrelated service changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0139 is not rollback authority and does not authorize an
installation at either station.
