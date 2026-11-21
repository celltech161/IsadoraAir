# r0145 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0145 has been published.

r0145 adds evergreen Voice Track transitions (roadmap 2.25), the Library filtered CSV export and NWS alert-fetch
failure visibility. A direct r0144 → r0145 update **restarts Gunicorn, then the audio engine, then Monitoring**. The
engine restart **interrupts program audio** briefly. The encoders, RBDS and the workers keep running.

## Exact identity and trusted plan

- Previous published release: r0144 at `e4003aa7f075b940ec5680bf6864f08530ceed05`.
- Integrated engineering tip: `724eed0882e58ed38aad40e8a76923e094673407`, tree
  `dd09ecf17ea149cd6cb7fb86c105b7ed53dd31ce`, 25 linear cherry-picks on r0144: 22 for roadmap 2.25 (`ca1fc40` …
  `d0a1af4`, reproducing the accepted 2.25 tree `09377e28748f909670405b81383bce3056cd873d`), `62649c5` (Library CSV
  export) and `50cb6c8`, `724eed0` (NWS fetch visibility).
- R is the unique canonical commit introducing `deploy/releases/r0145.json`, the direct child of the engineering tip,
  and adds only the four release files.
- There is **no migration**. A direct r0144 → r0145 plan advances source to R, then restarts `isadoraair-gunicorn`,
  `isadoraair-engine` and `isadoraair-monitoring`, in that order. Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action, and no encoder, RBDS or worker restart.
- If the trusted plan for a station directly on r0144 shows any other action, stop and investigate; do not install.
- A station below r0144 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0132 also applies `library.0101_uitheme_show_played_items`,
  `library.0102_uitheme_log_display_appearance` and `library.0103_remotedjconfig_auto_reconnect_enabled` (additive),
  and a station below r0144 installs from an Update Center **without** the engine restart safeguard (see below).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do not
manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter after
separate station authorization.

## Maintenance window and live-talent safety

The engine restart briefly interrupts program audio and ends any in-progress Voice Track, so schedule an **approved
operator maintenance window** for each station, away from live programs, Remote DJ shows and urgent-alert activity.

- **On r0144 (expected at KOGR):** the installation starts from the r0144 Update Center, so the **engine restart
  safeguard** assesses it. The install is refused while a studio microphone is live, a Remote DJ is connected (even
  with its microphone Off), reconnecting, establishing or occupying its slot, or engine status is unknown. **Do not
  override the safeguard to install.** End the live session and preview again. A warning (Manual mode, quarantined
  Remote DJ slot) is not a reason to proceed without the operator's agreement.
- **Below r0144 (possible at WRJE):** the installed Update Center predates the safeguard and will not check. Before
  starting the job, confirm by direct observation that no studio microphone is live and no Remote DJ is connected, and
  that the operator agrees to the program-audio interruption.
- The safeguard checks admission, not the moment of the restart: keep the studio microphone off and Remote DJ
  disconnected until the job has finished.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity. Do not infer release identity from `HEAD` alone. **Do not assume the
   station is on r0144.** WRJE was last independently documented on r0132.
2. **Release chain.** Confirm the protected updater verifies the complete chain from the installed release to r0145.
3. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed job
   awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately before
   installation. For a direct r0144 upgrade the action fingerprint must describe source advance and the three restarts
   only.
6. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers.
7. **Program audio and encoders.** Record program audio and encoder connection state.
8. **Weather Health baseline.** Record the Weather Health `watch_warnings` state and summary.

Stop if identity, chain, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the named station, inside the approved window, use the protected UpdateCenter once.
For a station directly on r0144 it must:

1. Advance source to the exact canonical r0145 release commit R.
2. Restart `isadoraair-gunicorn`.
3. Restart `isadoraair-engine`.
4. Restart `isadoraair-monitoring`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected, and no encoder, RBDS or worker restart. The NWS poller
(`wx-update-local-data`) is a scheduled one-shot process; its next run loads the new code. Do not restart services
outside the authorized update job. Do not retry a failed job automatically.

## Non-disruptive acceptance

Do **not** key a microphone on air, start a Remote DJ session, inject audio into the transmitter or streams, change
Library metadata, edit Weather configuration or arm Hour Refill only to demonstrate acceptance.

1. Verify the UpdateCenter job succeeded.
2. Confirm the installed source equals the exact canonical r0145 R, the checkout is clean and no migration is pending.
3. Verify Gunicorn, the engine and Monitoring restarted (new PIDs and activation times), and the encoders, RBDS and
   the workers kept their recorded PIDs and activation times.
4. **Program audio.** Confirm program audio resumed after the engine restart, the encoders stayed connected, and the
   console shows the queue, NEXT, clocks and VU meters normally. Note the length of the interruption.
5. **Engine status.** In `/run/isadoraair/engine_state.json` confirm a `vt_transition` key is present, every r0144
   key remains, and `engine_session_id` equals `state_revision.instance`. Do not print identity values.
6. **Ordinary playout.** Observe at least two ordinary song-to-song transitions with no eligible Voice Track: each
   segues normally, with exactly one Play event per item and no dead air.
7. **Voice Track audio (observe scheduled programming only).** If the log contains a Track with an evergreen intro or
   outro VT during the observation period, listen on the station's off-air monitor: the VT speech is complete (head
   and tail), music is ducked under it, the incoming vocal does not collide with the VT, and the song becomes on air
   once. If none is scheduled, record "not observed". Do not add VTs or alter the log for this check.
8. **Monitoring.** Confirm there is no `engine|vt2-teardown|…` or `engine|vt-teardown-unavailable` event since the
   restart. If one appears, Voice Tracks are suspended until the next engine restart while program audio continues:
   record the event and report it; do not restart the engine outside an approved window.
9. **NWS Weather Health.** Open Weather Health. If `api.weather.gov` is healthy, `watch_warnings` is ready (or not
   applicable) and, after the next poll, `WEATHER_DATA_DIR/nws_alert_fetch_status.json` exists with `"ok": true`.
   If NWS is failing at that moment, the degraded summary names the cause (HTTP status or error) and the time since
   failures began. Do not block the network or edit the file to produce a failure.
10. **Library CSV export.** Reload `/library/`, apply a search or Category filter and a sort, and click **Export to
    .csv**. Confirm the file `isadoraair-library-YYYY-MM-DD.csv` opens with the header `Artist,Title,Album,Category,
    Duration`, contains the filtered rows in the shown order (not only the visible page), and that accented text
    displays correctly. Delete the downloaded file afterwards if it leaves the station.
11. Confirm the rest of `/library/` (Bulk Edit preview unchanged), Monitoring and Update Center pages load, and no
    `restart_safety` refusal or override event was recorded by the installation.

## Post-install Monitoring observation

For the rest of the day after installation, review Monitoring for `vt2-teardown` events, engine restarts, dead air,
encoder disconnects and Weather Health changes. Record any observation; do not remediate without authorization.

## KOGR acceptance

KOGR is expected on r0144. Record KOGR evidence independently: preflight items 1–8 with KOGR's PIDs and activation
times, the safeguard assessment, the job, the plan fingerprint, and acceptance items 1–11.

## WRJE acceptance

Do not assume WRJE's installed release. It was last independently documented on r0132; the protected updater must
verify its actual installed release and the complete chain before any installation authorization. A catch-up plan
from below r0144 includes the intervening migrations and restarts and is **not** gated by the engine restart safeguard
(apply the manual live-talent check above). Record WRJE evidence independently: preflight items 1–8 with WRJE's own
PIDs and activation times, its own job and plan fingerprint, and acceptance items 1–11. Do not reuse KOGR's results.

**A successful installation at one station is not authorization to install at the other.** Record KOGR and WRJE
evidence independently.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and the Gunicorn, engine and Monitoring journals if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the three restarts appears for a direct r0144 upgrade, or a migration is
  pending;
- a service fails to restart, or another service restarts unexpectedly;
- program audio does not resume, encoder connectivity is unhealthy, or the console, `/library/`, Monitoring or the
  Update Center fails to load.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0145 is not rollback authority and does not authorize an
installation at either station.
