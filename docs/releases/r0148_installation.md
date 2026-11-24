# r0148 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service, start or stop a recording or
change a station setting merely because r0148 has been published.

r0148 shows the Aircheck capture source truthfully and records it on new sessions (roadmap 2.0-A). A direct
r0147 → r0148 update **restarts Gunicorn only**: the audio engine, encoders, Monitoring, RBDS and every other service
keep running and program audio is not interrupted.

## Exact identity and trusted plan

- Previous published release: r0147 at `3027223c44ae7e7be6446c29ff6a2a74fa18cfd4`, tree
  `404ce679232b9aa7b532e0f80e1d3cd1aa3f8ab1`.
- Accepted engineering commit: `be7bc128d24f63bf7d147828dad3b7a8ea73919e`, tree
  `da1c190fdc1bd201fe43b4d3bc29d8199562b7cb`, the direct child of r0147, published unchanged.
- R is the unique canonical commit introducing `deploy/releases/r0148.json`, the direct child of the accepted
  commit, and adds only the four release files.
- There is **no migration**. A direct r0147 → r0148 plan advances source to R, then restarts `isadoraair-gunicorn`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap,
  station-configuration, Aircheck-data or protected-runtime action, and no engine, encoder, Monitoring, RBDS or worker
  restart for a direct r0147 update.
- If the trusted plan for a station directly on r0147 shows any other action, stop and investigate; do not install.
- A station below r0147 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0146 also restarts the engine for r0147 (interrupting program audio); a station on r0144
  also restarts Gunicorn and Monitoring for r0145; a station on r0132 also applies
  `library.0101_uitheme_show_played_items`, `library.0102_uitheme_log_display_appearance` and
  `library.0103_remotedjconfig_auto_reconnect_enabled` (additive). Do not assume a catch-up installation is
  Gunicorn-only because the direct transition is.

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do
not manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter
after separate station authorization.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity, and confirm it is r0147 at `3027223c44ae7e7be6446c29ff6a2a74fa18cfd4`.
   Do not infer release identity from `HEAD` alone.
2. **Release chain.** Confirm the protected updater verifies the complete chain from the installed release to r0148.
3. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed
   job awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately
   before installation. For a direct r0147 upgrade the action fingerprint must describe source advance and the
   Gunicorn restart only.
6. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers. The engine, encoder, Monitoring and RBDS values are the baseline for acceptance item 4.
7. **Program audio and encoders.** Program audio is airing and the encoders are connected.
8. **Aircheck state.** Record whether an Aircheck recording is running, and the Aircheck readiness shown on the
   Monitoring dashboard. **Do not start or stop a recording to prepare for or to prove this release.** A Gunicorn
   restart does not stop Liquidsoap's recorder, and a recording already running keeps its original session row; if
   the operator prefers, wait for it to end naturally before installing.
9. **Remote DJ and web use.** A Gunicorn restart briefly interrupts the web console and its requests. Record any
   active Remote DJ session and any operator using the console, and inform them; the engine-side session is not
   restarted.
10. **Outstanding acceptance.** Confirm any open r0147 operational-acceptance observations are closed.

Stop if identity, chain, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan. The engine restart safeguard does not apply to a Gunicorn-only plan, and the r0146
GStreamer gates are not relevant to r0148.

## Installation expectations

After separate authorization for the named station, use the protected UpdateCenter once. For a station directly on
r0147 it must:

1. Advance source to the exact canonical r0148 release commit R.
2. Restart `isadoraair-gunicorn`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap,
Aircheck-configuration or protected-runtime operation is expected, and no engine, encoder, Monitoring, RBDS or
worker restart. The updater may report that it processed a stage for an action that does nothing for this plan (for
example migrations or static collection): that milestone is not evidence that anything was applied or restarted;
confirm the effective restart set against the plan. Do not restart services outside the authorized update job. Do not
retry a failed job automatically.

## Non-disruptive acceptance

Acceptance is read-only. Do not start or stop an Aircheck recording, key a microphone, alter a log or change a
station setting only to demonstrate the correction, and do not interrupt a recording that is running.

1. Verify the UpdateCenter job succeeded and preserved its complete evidence.
2. Confirm the installed source equals the exact canonical r0148 R, the checkout is clean and no migration is
   pending.
3. Verify Gunicorn restarted (new PID and activation time).
4. Verify the audio engine, encoders, Monitoring and RBDS kept their recorded PIDs and activation times **(no
   change)**, and that the persistent workers did not restart.
5. Verify program audio, the stream and the encoders remain healthy and the console loads normally.
6. Open Django Admin → **Aircheck Configuration**. Confirm **Capture source** reads
   `airtap — shared default encoder-group source`, with the explanation that Aircheck records the same program feed
   used by the default Liquidsoap encoder group, and that there is **no** editable source-device field.
7. Confirm the unrelated settings (**Format**, **Bitrate**, **Output directory**, **Filename template**) are present
   and unchanged. Do not save the page merely to prove it works; if a save is separately authorized, confirm it
   changes only what was edited.
8. Open **Aircheck Sessions** (read-only). Confirm existing sessions are intact, including their original source
   labels (older sessions may show a value other than `airtap`; that is historical and is not corrected), and that
   any running session is still running.
9. Confirm Aircheck readiness on the Monitoring dashboard is as healthy as before the installation.
10. Confirm no unexpected Monitoring alarm, encoder disconnect, engine event or Aircheck error appeared.
11. **New-session attribution** (a recording started after installation records `airtap`) may be verified only when
    the operator separately authorizes a recording and it is operationally appropriate. It is not required for
    acceptance and must not be forced.

## KOGR acceptance

KOGR is on r0147 (installed and operator-accepted). Record KOGR evidence independently: preflight items 1–10 with
KOGR's PIDs and activation times, the job, the plan fingerprint and acceptance items 1–10 (11 only if separately
authorized).

## WRJE acceptance

WRJE is on r0147 (installed and operator-accepted). The protected updater must verify its actual release, schema and
updater state and the complete chain; do not assume them. Record WRJE evidence independently and do not reuse KOGR's
results. The Capture source line states the shared source identity; it is not evidence of WRJE's actual encoder
state. If WRJE's persisted last-known-good encoder scripts or running group are inspected, do so read-only and record
what is found for WRJE alone; KOGR's observations do not establish it.

**A successful installation at one station is not authorization to install at the other.**

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and the Gunicorn journal if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the Gunicorn restart appears for a direct r0147 upgrade, or a migration is
  pending;
- Gunicorn fails to restart, or the engine, an encoder, Monitoring, RBDS or a worker restarts unexpectedly;
- program audio does not continue, encoder connectivity is unhealthy, or the console or Admin fails to load;
- Aircheck Configuration still offers an editable source-device field, or shows a source other than `airtap`;
- an existing Aircheck session, recording or setting changes unexpectedly.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0148 is not rollback authority and does not authorize an
installation at either station.
