# r0140 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, apply the migration, restart a service, change a
Remote DJ setting or enable automatic reconnect merely because r0140 has been published.

**This release restarts the audio engine.** Installation interrupts program audio for the duration of the engine
restart and terminates any live Remote DJ session. Do not install during an active live broadcast, a live Remote DJ
show or a critical on-air transition (top of hour, scheduled live insert, Weather/AMBER announcement) without a
separately approved operational plan.

## Exact identity and trusted plan

- Previous published release: r0139 at `52e764ed0a8dab63a308214b0e85bc3918c8bf07`.
- Accepted engineering commits, in order:
  - `f83e21bc61b8757e5364751d64126264cec05372` (engine end reasons, slot state, reconnect policy, Admin switch);
  - `774ad5f1b6547b375b663d476282bc342e16628a` (browser reconnect, microphone cleanup, leave-page protection);
  - `3321ad412579ebc264a60b4d2e3acdbdecaa1a98` (contract fixes; reviewed candidate);
  - `31a7c7fe69a09db0b6c7f1b5a195c20cb09a212b` (truthful switch help text);
  - `0a75798444b87295408f33ff68cd167be4d3441e`, tree `8f99502c176e2639f2ce46b3305f8d925d983fde` (setup failures,
    sequence deadline, output tracks; accepted candidate).
- R is the unique canonical commit introducing `deploy/releases/r0140.json`, directly after the accepted engineering
  history, and adds only the four release files.
- Required migration: `library.0103_remotedjconfig_auto_reconnect_enabled`; migration file SHA-256
  `4c189e2421b6fe46a38eec46d8f1fd3d546a9506cf650f431bc4203330a84fa7`.
- A direct r0139 → r0140 plan advances source to R, applies that additive migration, then restarts
  `isadoraair-gunicorn`, then `isadoraair-engine`.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action.
- A station below r0139 must retain every intervening release, migration and restart in the cumulative trusted plan
  (for example, a station on r0138 also applies `library.0102_uitheme_log_display_appearance`; a station on r0136 or
  earlier also restarts Monitoring for r0137).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. Establish the installed release from the UpdateCenter installation record and the trusted updater identity, and
   verify the installed checkout is clean. Do not infer release identity from `HEAD` alone.
2. Confirm trusted UpdateCenter and protected-runtime health, the supported updater protocol, no running job and no
   failed job awaiting operator intervention.
3. Record schema health, applied migrations and pending migrations. The direct plan from r0139 must add only
   `library.0103_remotedjconfig_auto_reconnect_enabled`.
4. Confirm current backups and recovery readiness (including a successful recent backup and current recovery payload).
   Preserve the installation preview, action fingerprint and current release evidence.
5. Record service PIDs, activation times and health for Gunicorn, the audio engine, encoders, Monitoring, RBDS, Active
   Messages and spoken rendering. Record program audio and encoder connection state.
6. Record the current and next approved hourly logs, current track, queue order and playing decks. Confirm no log
   build, top-of-hour transition or live insert is underway or imminent.
7. Record pending urgent insertions, Active Messages and Weather/AMBER state and pending announcements.
8. Record Remote DJ state: active session, owning attempt and user, Remote Mic (PTT) state, Manual/Auto ownership and
   slot admission state. The engine restart will end any session; installation requires either no active session or
   the live DJ's informed agreement that it will end.
9. Record the current `RemoteDJConfig` row: `enabled`, `stun_server`, `ice_udp_min_port`, `ice_udp_max_port` and
   `reconnect_grace_seconds`, so they can be compared after installation.
10. Confirm Monitoring reports no pending critical finding, no Remote DJ quarantine and no restart-required finding.
    A pre-existing quarantine or restart-required condition is a reason to stop and obtain a separate decision, not
    to install.
11. Choose an operationally acceptable engine-restart window with an operator present.
12. Preview the exact canonical target R and plan immediately before installation. For a direct r0139 upgrade the
    action fingerprint must describe source advance, migration 0103 and the Gunicorn then engine restarts only.

Stop if identity, schema, fingerprint, migration, restart inventory or service health differs. Do not shorten or
manually reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0139 it must:

1. Advance source to the exact canonical r0140 release commit R.
2. Apply `library.0103_remotedjconfig_auto_reconnect_enabled` as an additive migration.
3. Restart `isadoraair-gunicorn`.
4. Restart `isadoraair-engine` (program audio is interrupted; any Remote DJ session ends).

No apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or protected-runtime
operation is expected. Do not apply migrations or restart services outside the authorized update job. Do not retry a
failed job automatically.

## KOGR preflight and acceptance

Preserve KOGR's existing Roadmap 2.27 Hour Refill observation-only configuration and evidence:

- `dry_run_only` remains checked;
- host installation arming remains disabled and `HOUR_REFILL_INSTALL_ARMED` remains false;
- cause/capture switches, policy rows, scheduled timers and accumulated observation evidence remain unchanged;
- Weather/AMBER configuration, snapshots, state and polling schedules remain unchanged;
- Active Messages configuration and queued messages remain unchanged.

Record Hour Refill policy, runner status, latest evidence and timers before installation and confirm they are
unchanged afterward. Installing r0140 must not enable Hour Refill, enable automatic Remote DJ reconnect, or change
Weather/AMBER, Active Messages, logs, playout settings, Library content or any unrelated operating setting.

## WRJE preflight and acceptance

WRJE requires independent evidence and authorization. Do not assume it is on r0139 or that it matches KOGR's
schema, settings, migration history, service health or operational readiness. Independently establish its installed
release and the complete trusted catch-up plan from that release, including every intervening migration and restart.
Verify its clean checkout, pending migrations, `RemoteDJConfig`, current and next log, engine queue, Remote DJ state,
service health, alert state and UpdateCenter status directly.

Success at KOGR is neither authorization nor acceptance evidence for WRJE. Do not alter WRJE's Remote DJ policy,
GroupAccess or permissions to test this release.

## Post-install verification

Acceptance on air is passive and must not disturb program audio beyond the declared engine restart.

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Verify the installed release is r0140 at the exact canonical R and the source checkout is clean.
3. Verify migration `library.0103_remotedjconfig_auto_reconnect_enabled` is applied through the trusted job, schema
   health is current and the existing `RemoteDJConfig` row has `auto_reconnect_enabled=False`.
4. Verify Gunicorn and then the engine restarted as declared, and that encoders, Monitoring, RBDS, Active Messages,
   spoken rendering and every unrelated service remained healthy without an unexpected restart.
5. Confirm program audio and encoders returned normally after the engine restart; the mixer, decks, current item and
   queue advancement are healthy; and the next top of hour transitions normally.
6. In Django Admin → Remote DJ Configuration → Reconnect / recovery, confirm **Enable automatic Remote DJ reconnect**
   is present and **Off**, and that `reconnect_grace_seconds` and every other recorded field is unchanged.
7. With an operator-approved Remote DJ account, confirm on the Console and on `/remote-dj/`: Connect, monitor return,
   and Disconnect. Exercise PTT only under an operator-approved on-air plan, because it can put the microphone on air.
   Confirm the Remote Mic is Off when the session is established.
8. While connected, confirm the leave-page warning on both pages: a same-tab link opens the **Stay on this page** /
   **Leave and disconnect** dialog, Stay keeps the session, and the browser's own prompt appears on Reload. Use
   **Leave and disconnect** (or Disconnect) to end the test session.
9. After Disconnect, confirm the browser's microphone-in-use indicator clears (explicit microphone release) and the
   station's Remote DJ slot returns to available.
10. Confirm Weather/AMBER, Hour Refill, Active Messages, scheduling and unrelated operational settings are unchanged.

Record KOGR and WRJE evidence independently.

### Not part of on-air acceptance

Deliberate network interruptions, forced terminal teardown, quarantine simulation, induced setup failures and
reconnect-sequence timing were qualified in the isolated browser harness and server tests. Do not perform them on a
live, on-air station. A later, separately approved field acceptance may enable automatic reconnect for supervised
testing and verify recovery across real network conditions; enabling the switch is an operating decision, not part of
installing r0140.

## Accepted limitations

- Browser behavior was qualified with a simulation harness; no real-engine WebRTC session or mobile browser was
  exercised during engineering.
- Browsers control the native leave-page prompt's wording and presence; some mobile exits show nothing. Navigating
  away still ends the page-owned connection; persistent cross-page ownership is Package C.
- Emergency Mute remains session-scoped; a latch at session end stops automatic reconnect.
- After an engine restart (including this installation's) no automatic reconnect follows; the DJ reconnects manually.

Do not change Remote DJ authorization, GroupAccess or Emergency Mute semantics during installation to work around
these limitations.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint, migration output, engine and Gunicorn journals,
engine state and browser evidence if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance, migration 0103 and the Gunicorn then engine restarts appears for a direct r0139
  upgrade;
- migration or schema verification fails, or `auto_reconnect_enabled` is not False after installation;
- Gunicorn or the engine fails to restart, or another service restarts unexpectedly;
- program audio, encoder connectivity, mixer, decks or queue advancement is unhealthy after the engine restart;
- Remote DJ cannot connect, the Remote Mic is not Off at establishment, the slot does not return to available after
  Disconnect, or a quarantine appears;
- Weather/AMBER, Hour Refill, Active Messages or another unrelated setting or service changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0140 is not rollback authority and does not authorize an
installation at either station.
