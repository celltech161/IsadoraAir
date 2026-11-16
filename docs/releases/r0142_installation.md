# r0142 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service, delete a schedule override or
Rotation, or change a station setting merely because r0142 has been published.

r0142 corrects the r0141 Past Overrides HTTP 500. A direct r0141 → r0142 update **restarts Gunicorn only**: the audio
engine, encoders and every other service keep running and program audio is not interrupted.

## Exact identity and trusted plan

- Previous published release: r0141 at `529b84b79e154a0d370c9a87c78484716d4e6518`.
- Accepted corrective engineering commit: `355bd63b2aa9818e02caf8437341fff797d07f38`, tree
  `777d13fd930b92b7dbe70c9c0852caa6de198691`, the direct child of r0141.
- R is the unique canonical commit introducing `deploy/releases/r0142.json`, the direct child of the accepted
  correction, and adds only the four release files.
- There is **no migration**. A direct r0141 → r0142 plan advances source to R, then restarts `isadoraair-gunicorn`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action, and no engine, encoder or Monitoring restart for a direct r0141 update.
- If the trusted plan for a station directly on r0141 shows any other action, stop and investigate; do not install.
- A station below r0141 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0140 also restarts the engine for r0141 (interrupting program audio); a station on
  r0139 also applies `library.0103_remotedjconfig_auto_reconnect_enabled`; a station on r0136 or earlier also
  restarts Monitoring for r0137.

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity. Do not infer release identity from `HEAD` alone. Do not assume the
   station is on r0141.
2. **Clean checkout and healthy schema.** Verify the installed checkout is clean; record schema health, applied
   migrations and pending migrations. A direct r0141 plan must have no pending migration.
3. **Backups and recovery readiness.** Confirm a successful recent backup and a current recovery payload.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, the supported updater
   protocol, no running job and no failed job awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately
   before installation. For a direct r0141 upgrade the action fingerprint must describe source advance and the
   Gunicorn restart only.
6. **Services.** Record service PIDs, activation times and health for Gunicorn and the audio engine (the engine PID
   and activation time are the baseline for acceptance item 4), and for encoders, Monitoring, RBDS, Active Messages
   and spoken rendering.
7. **Program audio and encoders.** Record program audio and encoder connection state.
8. **Hourly logs.** Record the current and next approved hourly logs.
9. **Remote DJ.** Record any active Remote DJ session. A Gunicorn restart briefly interrupts web pages and requests;
   the engine-side session is not restarted, but confirm the live DJ is informed.
10. **Weather/AMBER and Active Messages.** Record Weather/AMBER state, active notices and pending announcements, and
    Active Messages configuration and pending messages.
11. **Hour Refill.** Record the existing Hour Refill policy (`dry_run_only`), host arming
    (`HOUR_REFILL_INSTALL_ARMED`), runner status and timers.

Stop if identity, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0141 it must:

1. Advance source to the exact canonical r0142 release commit R.
2. Restart `isadoraair-gunicorn`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected, and no engine, encoder or Monitoring restart. Do not restart services
outside the authorized update job. Do not retry a failed job automatically.

## Non-disruptive acceptance

Acceptance is read-only and must not disturb program audio.

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Confirm the installed source equals the exact canonical r0142 R, the checkout is clean and no migration is
   pending.
3. Verify Gunicorn restarted.
4. Verify the engine PID and service activation time **did not change**.
5. Verify program audio and encoders remain healthy, and that encoders, Monitoring, RBDS, Active Messages and spoken
   rendering did not restart.
6. Open Schedule → Date Override → **Past Overrides**.
7. Confirm `GET /api/schedule/past-overrides/` returns **HTTP 200** (no "Past overrides could not be loaded." and no
   "Request failed (500)").
8. Confirm actual historical overrides appear, if the profile has any; an empty profile shows "This profile has no
   date overrides before today."
9. Search for an existing historical Rotation, such as Jeff Memorial, by name.
10. Confirm profile selection, filtering, **Load older dates** and **Show date / Show hour** navigation work.
11. Confirm **no override removal** occurs during read-only acceptance: do not press Remove on a real override.
12. Verify Console NEXT and Swap remain present and unchanged (inspect only; do not perform a Swap on air for this
    acceptance).
13. Confirm no unexpected Hour Refill, Remote DJ, Weather/AMBER or Active Messages change.

**Do not remove Jeff Memorial as part of installation acceptance.** Finding and removing its historical override
with Past Overrides, and then deleting the Rotation, remain a **separate authorized operator action**.

## KOGR and WRJE

Each station requires **independent installation authorization** and its own **operational acceptance**, installed
release check, trusted plan and service-health preflight. **A successful installation at one station is not
authorization to install at the other.** Do not assume both stations are on r0141: a station on an older release
must apply its complete cumulative plan, preserving every intervening migration and service restart (including the
r0141 engine restart for a station on r0140 or earlier). Record KOGR and WRJE evidence independently.

Installing r0142 must not change Schedule Profiles, Rotations, Playlists, schedule overrides, logs, Hour Refill, Web
Requests, GroupAccess, Remote DJ policy, Weather/AMBER, Active Messages or any other station setting.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint, Gunicorn journal and browser evidence if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the Gunicorn restart appears for a direct r0141 upgrade, or a migration is
  pending;
- Gunicorn fails to restart, or the engine or another service restarts unexpectedly;
- program audio or encoder connectivity is unhealthy;
- Past Overrides still answers HTTP 500, or lists today's or future overrides;
- a schedule override, Rotation, Playlist, Hour Refill, Web Requests, Remote DJ, Weather/AMBER, Active Messages or
  another unrelated setting changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0142 is not rollback authority and does not authorize an
installation at either station.
