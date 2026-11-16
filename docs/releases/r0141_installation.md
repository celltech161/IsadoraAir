# r0141 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service, delete a schedule override or
Rotation, or change a station setting merely because r0141 has been published.

> **This release restarts the audio engine.** Installation **interrupts program audio** for the duration of the
> engine restart and **may terminate any live Remote DJ session**. Do not install during an active live broadcast, a
> live Remote DJ show or a critical on-air transition (top of hour, scheduled live insert, Weather/AMBER
> announcement) without a separately approved operational plan and an operator-approved maintenance window.

## Exact identity and trusted plan

- Previous published release: r0140 at `64e8620ee5e90ffcd31bfca00369bd5cfb80fe3b`.
- Accepted engineering commits, in order:
  - `45ba3ff6ccfc327cf04a29e030a378e47f8aa9c0` (Schedule Past Overrides);
  - `ab08101f95493f75800cb6f9cd543c1d6ee121f9` (Console queue column order, NEXT relocation);
  - `e43481bb91853c075a80ff05d8d5405a8aded191` (guarded live queue Swap);
  - `9f230bf14906a02d6aa32effeb703ae875c907df` (test pins; Codex-reviewed candidate);
  - `7bfefa6d2e7a867b70d09f261d72edd0951109ba` (claim adoption, contention, live-fill identity);
  - `a44d1d60594ae3e590972f81ca99b590ea855189` (protected origins, effective airtime, unknown outcomes);
  - `9b83a45ce2783706651af76dfc1f969be6e18f5a` (query-parity test);
  - `d0aa0f212d1e5ef5ce6f4b0b2f4d7de80cbd4876`, tree `e5c96b3e3891c260ec0aa2f12d634c1005994d48` (unknown-outcome
    correction; accepted candidate).
- R is the unique canonical commit introducing `deploy/releases/r0141.json`, directly after the accepted engineering
  history, and adds only the four release files.
- There is **no migration**. A direct r0140 → r0141 plan advances source to R, then restarts `isadoraair-gunicorn`,
  then `isadoraair-engine`. Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action.
- A station below r0140 must retain every intervening release, migration and restart in the cumulative trusted plan
  (for example, a station on r0139 also applies `library.0103_remotedjconfig_auto_reconnect_enabled`; a station on
  r0136 or earlier also restarts Monitoring for r0137).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity. Do not infer release identity from `HEAD` alone. Confirm trusted
   UpdateCenter and protected-runtime health, the supported updater protocol, no running job and no failed job
   awaiting operator intervention.
2. **Clean checkout and healthy schema.** Verify the installed checkout is clean; record schema health, applied
   migrations and pending migrations. A direct r0140 plan must have no pending migration.
3. **Backups and recovery readiness.** Confirm a successful recent backup and a current recovery payload.
4. **Hourly logs.** Record the current and next approved hourly logs. Confirm no log build, top-of-hour transition or
   live insert is underway or imminent.
5. **Live queue and engine state.** Record the current track, queue order, playing decks, claimed occurrences
   (`playback_claimed_at`) and Manual/Auto state.
6. **Remote DJ.** Record any active Remote DJ session, its user and attempt, and the Remote Mic (PTT) state. The engine
   restart will end any session; installation requires either no active session or the live DJ's informed agreement.
7. **Encoders, Monitoring and services.** Record service PIDs, activation times and health for Gunicorn, the audio
   engine, encoders, Monitoring, RBDS, Active Messages and spoken rendering. Record program audio and encoder
   connection state.
8. **Weather/AMBER and Active Messages.** Record Weather/AMBER state, active notices and pending announcements, and
   Active Messages configuration and pending messages.
9. **Web Requests and Hour Refill.** Record pending and scheduled listener requests; record Hour Refill policy
   (`dry_run_only`), host arming (`HOUR_REFILL_INSTALL_ARMED`), runner status, latest evidence and timers.
10. **Findings.** Confirm Monitoring reports no critical quarantine and no outstanding restart-required finding. A
    pre-existing quarantine or restart-required condition is a reason to stop and obtain a separate decision, not to
    install.
11. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately
    before installation. For a direct r0140 upgrade the action fingerprint must describe source advance and the
    Gunicorn then engine restarts only.
12. **Maintenance window.** Obtain an operator-approved maintenance window for the engine restart, with an operator
    present.

Stop if identity, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0140 it must:

1. Advance source to the exact canonical r0141 release commit R.
2. Restart `isadoraair-gunicorn`.
3. Restart `isadoraair-engine` (program audio is interrupted; any Remote DJ session ends).

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected. Do not restart services outside the authorized update job. Do not retry a
failed job automatically.

## KOGR preflight and acceptance

Preserve, and confirm unchanged afterward:

- the existing Roadmap 2.27 Hour Refill observation-only state: `dry_run_only` remains checked, host installation
  arming remains disabled and `HOUR_REFILL_INSTALL_ARMED` remains false; cause/capture switches, policy rows, timers
  and accumulated observation evidence are unchanged;
- Weather/AMBER alert configuration, snapshots, polling schedules and active notices;
- Active Messages configuration and pending messages;
- Remote DJ configuration, GroupAccess and permissions;
- every current Schedule Profile, Rotation and Playlist;
- the existing current and next logs and the queue state.

Installation must **not automatically remove the Jeff Memorial Rotation or any historical override**. Finding and
removing the dated override that still references it (with Past Overrides) and then deleting the Rotation are a
**separate operator action**, not part of installing r0141.

Installing r0141 must not enable Hour Refill, change Web Requests priority, grant Swap to Remote Hosts, or change
Weather/AMBER, Active Messages, logs, playout settings, Library content or any unrelated operating setting.

## WRJE preflight and acceptance

WRJE requires an **independent installed-release check**, its own complete trusted **catch-up plan**, its own
**service-health preflight**, separate **installation authorization** and its own **operational acceptance**. Do not
assume it is on r0140 or that it matches KOGR's installed release, schema, settings, Schedule Profiles, migration
history, service health or operational readiness. Independently establish its installed release and every intervening
migration and restart from that release.

Success at KOGR is neither authorization nor acceptance evidence for WRJE. Do not alter WRJE's Remote DJ policy,
GroupAccess or permissions to test this release.

## Post-install acceptance

Acceptance on air is passive and must not disturb program audio beyond the declared engine restart.

**Release and services**

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Verify the installed release is r0141 at the exact canonical R, the source checkout is clean and no migration is
   pending.
3. Verify Gunicorn and then the engine restarted as declared, and that encoders, Monitoring, RBDS, Active Messages,
   spoken rendering and every unrelated service remained healthy without an unexpected restart.

**Schedule**

4. In Schedule → Date Override, **Past Overrides** appears beside the date picker controls.
5. Historical entries display correctly for the selected profile, newest date first, including minute transitions.
6. Name filtering finds actual historical assignments (for example, the Rotation an operator is investigating).
7. **Show date** / **Show hour** navigation returns to the correct date and hour.
8. Deletion and authorization checks, if performed, use **only a safe test fixture** (for example a past override
   created for the purpose on a non-production profile) and do not affect production history. Do not remove a real
   historical override as part of acceptance.

**Console**

9. **NEXT** appears in the intended column (Drag | NEXT | Swap | # | …).
10. Drag-to-reorder and NEXT still work.
11. Swap search, preview and confirmation work, and the correct replacement appears in the queue, on a music slot
    chosen with the operator's agreement.
12. Protected occurrences (live fill, Hour Refill, alerts, Active Messages, intros, request-reserved, on-deck) are
    not replaceable: Swap is refused with the reason shown.
13. Network uncertainty is represented truthfully (unknown, never a false "not swapped"); do not induce it on air.
14. Keyboard use (focus survives the one-second redraw) and narrow-screen rendering remain usable.
15. On `/remote-dj/`, Swap is shown but unavailable for Remote Hosts; NEXT, insert and reorder are unchanged.

**Engine**

16. Normal program audio resumes after the authorized restart, and encoder connections remain healthy.
17. Normal queue advancement and crossfades work.
18. No unexpected track skip, duplicate occurrence or wrong-track history is observed (including for a swapped row,
    which must play the chosen track).
19. Standard live fill and the top-of-hour transition remain healthy at the next top of hour.
20. No unexpected Hour Refill activation occurs; Weather/AMBER, Active Messages, Web Requests and scheduling are
    unchanged.

Record KOGR and WRJE evidence independently.

### Not part of on-air acceptance

**Do not induce live queue races, lock timeouts, media errors or deliberate playout gaps on an on-air station.** Claim
contention, concurrent Swaps, stale live-fill proposals, lost HTTP replies and unknown outcomes were qualified in
isolated database and browser tests. Any real-engine verification of them requires isolated testing, separately
approved.

## Accepted limitations

- Claim contention is bounded: after 8 consecutive contended attempts an occurrence is skipped with a Monitoring error
  event.
- Operator Insert, Reorder and NEXT do not take the `PlaylistLog` lock (inherited); a trailing poison-guarded row can
  collide with a live-fill append, which then rolls back (inherited).
- A later eligible listener request can replace an operator's Swap before the occurrence is claimed.
- No real-engine / GStreamer field acceptance was performed during engineering; the browser harnesses used simulated
  API responses.

Do not change Web Requests priority, GroupAccess or Hour Refill settings during installation to work around these
limitations.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint, engine and Gunicorn journals, engine state and
browser evidence if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the Gunicorn then engine restarts appears for a direct r0140 upgrade, or a
  migration is pending;
- Gunicorn or the engine fails to restart, or another service restarts unexpectedly;
- program audio, encoder connectivity, mixer, decks or queue advancement is unhealthy after the engine restart;
- a track is skipped, duplicated or recorded with the wrong track, or a "persistent claim contention" error event
  appears;
- a protected occurrence can be swapped, or Swap reports success for a track that does not play;
- Weather/AMBER, Hour Refill, Active Messages, Web Requests, a schedule override, Rotation or Playlist, or another
  unrelated setting or service changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0141 is not rollback authority and does not authorize an
installation at either station.
