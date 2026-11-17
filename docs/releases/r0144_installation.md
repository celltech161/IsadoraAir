# r0144 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0144 has been published.

r0144 adds the engine restart safeguard, the Library unique Artist count and Bulk Edit Related Artists. A direct
r0143 → r0144 update **restarts Gunicorn only**: the audio engine, encoders, Monitoring, RBDS and every other service
keep running and program audio is not interrupted.

## Exact identity and trusted plan

- Previous published release: r0143 at `538ed7e1c4a9a178e5d8186dc40ff27510dc0ce5`.
- Integrated engineering tip: `8137200dde516466e3bafca036887c3ba1e615db`, tree
  `6d71d23a6e40b2ed123a55796bbd2e94594b613c` (the independently reviewed combined tree), five linear commits on r0143:
  `9e70035`, `cdd23d4` (A), `36f1376`, `fe781a9` (B) and `8137200` (C).
- R is the unique canonical commit introducing `deploy/releases/r0144.json`, the direct child of the engineering tip,
  and adds only the four release files.
- There is **no migration**. A direct r0143 → r0144 plan advances source to R, then restarts `isadoraair-gunicorn`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action, and no engine, encoder, Monitoring or RBDS restart for a direct r0143 update.
- If the trusted plan for a station directly on r0143 shows any other action, stop and investigate; do not install.
- A station below r0143 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0140 also restarts the engine for r0141 (interrupting program audio); a station on r0139
  also applies `library.0103_remotedjconfig_auto_reconnect_enabled`; a station on r0136 or earlier also restarts
  Monitoring for r0137.

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do not
manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter after
separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity. Do not infer release identity from `HEAD` alone. Do not assume the station
   is on r0143.
2. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
3. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed job
   awaiting operator intervention.
4. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately before
   installation. For a direct r0143 upgrade the action fingerprint must describe source advance and the Gunicorn
   restart only.
5. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers (the non-Gunicorn values are the baseline for acceptance item 4).
6. **Program audio and encoders.** Record program audio and encoder connection state.

Stop if identity, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0143 it must:

1. Advance source to the exact canonical r0144 release commit R.
2. Restart `isadoraair-gunicorn`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected, and no engine, encoder, Monitoring or RBDS restart. Do not restart services
outside the authorized update job. Do not retry a failed job automatically.

The r0144 installation is started from the Update Center already running (r0143), so the new safeguard does not
apply to this installation; it applies to engine restarts requested after Gunicorn has restarted on r0144. A direct
r0143 plan restarts no engine either way.

## Non-disruptive acceptance

Do **not** initiate an engine restart, interrupt or start a Remote DJ session, key a microphone on air, or change
Library metadata only to demonstrate acceptance.

1. Verify the UpdateCenter job succeeded.
2. Confirm the installed source equals the exact canonical r0144 R, the checkout is clean and no migration is pending.
3. Verify Gunicorn restarted (new PID and activation time).
4. Verify the engine, encoders, Monitoring, RBDS and the workers kept their recorded PIDs and activation times **(no
   change)**, and that program audio was uninterrupted and encoders stayed connected.
5. **Restart safeguard (read-only).**
   - Confirm the engine's status file carries what the safeguard requires, without printing identity values: in
     `/run/isadoraair/engine_state.json`, `engine_session_id` is present and equals `state_revision.instance`,
     `state_revision.seq` is a positive integer, `transport` is `PLAYING`, `remote_dj_configured` is a boolean, and the
     `remote_dj_slot` and `remote_dj_connection` keys are present. If any is missing, every engine restart request will
     be assessed **unknown** (override required): record it and report it; do not work around it.
   - Django Admin → Audio Pipeline: open the change form and confirm the **Engine restart safeguard** section (override
     checkbox, superuser only, and reason). Leave without saving.
   - Monitoring: confirm the engine row still offers Restart. **Do not press it** (if pressed by mistake, cancel the
     browser's "Restart …?" confirmation): with the engine idle the safeguard admits the request and the engine
     restarts. A blocked or unknown request shows the refusal and, for a superuser,
     the override prompt with a reason; that presentation is covered by release qualification.
   - Update Center: with no pending plan that restarts the engine, no "Audio engine restart" assessment is shown (a
     Gunicorn-only plan reads no engine status). If a pending plan does restart the engine, the page shows its
     read-only assessment and level; do not start that install for this check.
   - Monitoring events: no `restart_safety` event should have been recorded by the installation.
6. **Library totals.** Reload `/library/`. Under several filter combinations (no filter, a search term, a Category, a
   status, and a combination), confirm the header shows `N tracks · M artists`, that both change together, and that
   typing a search quickly then changing it leaves totals and rows that match the final filter.
7. **Bulk Edit Related Artists (preview only).** Tick a few Tracks, open **Edit…**, and confirm **Related Artists** sits
   immediately below Artist with Leave unchanged / Set to / Append / Clear. Preview an **Append** of a name and a
   **Clear**, and confirm the before/after values in each preview are correct (duplicates and the primary Artist left
   out). Use **Cancel**; do **not** Apply on production metadata for this check.
8. Confirm the rest of `/library/`, the console (queue, NEXT, Swap, clocks, VU meters) and Monitoring are unchanged.

## KOGR acceptance

Record KOGR evidence independently: preflight items 1–6 with KOGR's PIDs and activation times, the job, the plan
fingerprint, and acceptance items 1–8. KOGR's live status file met every safeguard field requirement during release
qualification (assessment would have been **safe**); re-check it after installation (item 5).

## WRJE acceptance

Record WRJE evidence independently: preflight items 1–6 with WRJE's own PIDs and activation times, its own job and plan
fingerprint, and acceptance items 1–8. Do not reuse KOGR's results. If WRJE runs without Remote DJ configured,
`remote_dj_configured` is `false` and `remote_dj_slot` and `remote_dj_connection` must both be null; anything else is
contradictory and assessed **unknown**.

**A successful installation at one station is not authorization to install at the other.** Do not assume both stations
are on r0143. Record KOGR and WRJE evidence independently.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and Gunicorn journal if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the Gunicorn restart appears for a direct r0143 upgrade, or a migration is
  pending;
- Gunicorn fails to restart, or the engine or another service restarts unexpectedly;
- program audio or encoder connectivity is unhealthy, or `/library/`, Monitoring, the Update Center or the console
  fails to load.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0144 is not rollback authority and does not authorize an
installation at either station.
