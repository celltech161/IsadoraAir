# r0143 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0143 has been published.

r0143 widens the console VU meter staleness window. A direct r0142 → r0143 update **restarts Gunicorn only**: the
audio engine, encoders and every other service keep running and program audio is not interrupted.

## Exact identity and trusted plan

- Previous published release: r0142 at `6b8d4b844de385e4f149b90cf30079d790d19fc1`.
- Engineering commit: `2fb09c824aa87e34ce6b0d677d89e06b94a8a9df`, tree `2bd3bfd62e99ac346fd882d4419d9b9071c84269`,
  the direct child of r0142.
- R is the unique canonical commit introducing `deploy/releases/r0143.json`, the direct child of the engineering
  commit, and adds only the four release files.
- There is **no migration**. A direct r0142 → r0143 plan advances source to R, then restarts `isadoraair-gunicorn`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action, and no engine, encoder or Monitoring restart for a direct r0142 update.
- If the trusted plan for a station directly on r0142 shows any other action, stop and investigate; do not install.
- A station below r0142 must retain every intervening release, migration and restart in the cumulative trusted plan.
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
   station is on r0142.
2. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
3. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed
   job awaiting operator intervention.
4. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately
   before installation. For a direct r0142 upgrade the action fingerprint must describe source advance and the
   Gunicorn restart only.
5. **Services.** Record the Gunicorn and audio engine PIDs and activation times (the engine PID and activation time
   are the baseline for acceptance item 4).
6. **Program audio and encoders.** Record program audio and encoder connection state.

Stop if identity, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0142 it must:

1. Advance source to the exact canonical r0143 release commit R.
2. Restart `isadoraair-gunicorn`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected, and no engine, encoder or Monitoring restart. Do not restart services outside
the authorized update job. Do not retry a failed job automatically.

## Non-disruptive acceptance

1. Verify the UpdateCenter job succeeded.
2. Confirm the installed source equals the exact canonical r0143 R, the checkout is clean and no migration is pending.
3. Verify Gunicorn restarted.
4. Verify the engine PID and service activation time **did not change**, and program audio and encoders are healthy.
5. **Reload** each open console page so it uses the new template.
6. Confirm the **Program Level** meter moves with program audio on the affected viewing device.
7. If a Remote DJ session is live, confirm the Remote Mic PTT button meter still follows the remote microphone; do not
   start a session or key the microphone on air only for this check.
8. Confirm the rest of the console (queue, NEXT, Swap, clocks, listener widget) is unchanged.

If the meter is still blank on a device, measure its lag in the browser console:
`fetch('/api/engine/levels/').then(r => r.json()).then(d => console.log((Date.now()/1000 - d.ts).toFixed(3)))`.
A value above 1.0 means that device's clock is more than a second ahead of the station: correct the device's time.

## KOGR and WRJE

Each station requires **independent installation authorization** and its own acceptance. **A successful
installation at one station is not authorization to install at the other.** Do not assume both stations are on r0142.
Record KOGR and WRJE evidence independently.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and Gunicorn journal if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the Gunicorn restart appears for a direct r0142 upgrade, or a migration is
  pending;
- Gunicorn fails to restart, or the engine or another service restarts unexpectedly;
- program audio or encoder connectivity is unhealthy, or the console fails to load.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0143 is not rollback authority and does not authorize an
installation at either station.
