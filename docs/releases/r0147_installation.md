# r0147 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0147 has been published.

r0147 adds first-audio stall recovery for ordinary unattended starts (roadmap 1.21-B1). A direct r0146 → r0147 update
**restarts the audio engine only**; the engine restart interrupts program audio briefly. Gunicorn, Monitoring, the
encoders, RBDS and the workers keep running.

## Exact identity and trusted plan

- Previous published release: r0146 at `bef47174cd8cc7e0064102890b396fb0b93d83b0`, tree
  `4987527b59ec8e268a08e5a081e942f6ccd0ba93`.
- Accepted engineering tip: `5dbbfa5ffc377bb4c615bffee60c6ee41deed71b`, tree
  `dfae26d2be2bff365df6c83bcb19128dec2646ef`: nine linear commits on r0146, `28c98b3`, `15bdc42`, `ce9c733`,
  `d5a368e`, `d12a2f7`, `9d4f8e0`, `96318b0`, `6bbaf9d`, `5dbbfa5`, published unchanged.
- R is the unique canonical commit introducing `deploy/releases/r0147.json`, the direct child of the engineering tip,
  and adds only the four release files.
- There is **no migration**. A direct r0146 → r0147 plan advances source to R, then restarts `isadoraair-engine`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap,
  station-configuration or protected-runtime action, and no Gunicorn, Monitoring, encoder, RBDS or worker restart for a
  direct r0146 update.
- If the trusted plan for a station directly on r0146 shows any other action, stop and investigate; do not install.
- A station below r0146 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0145 also restarts the engine for r0146 (still the engine only); a station on r0144 also
  restarts Gunicorn and Monitoring for r0145; a station on r0132 also applies
  `library.0101_uitheme_show_played_items`, `library.0102_uitheme_log_display_appearance` and
  `library.0103_remotedjconfig_auto_reconnect_enabled` (additive).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do not
manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter after
separate station authorization.

## Maintenance window and live-talent safety

The engine restart briefly interrupts program audio and ends any in-progress Voice Track, so schedule an **approved
operator maintenance window** away from live programs, Remote DJ shows and urgent-alert activity, and not within
minutes of a scheduled transition or while a Weather/AMBER alert is active.

- **On r0144 or later (KOGR and WRJE are on r0146):** the installation starts from an Update Center with the **engine
  restart safeguard**, which refuses it while a studio microphone is live, a Remote DJ is connected (even with its
  microphone Off), reconnecting, establishing or occupying its slot, or engine status is unknown. **Do not override the
  safeguard to install.** End the live session and preview again.
- **Below r0144:** the installed Update Center predates the safeguard and will not check. Before starting the job,
  confirm by direct observation that no studio microphone is live and no Remote DJ is connected, and that the operator
  agrees to the program-audio interruption.
- The safeguard checks admission, not the moment of the restart: keep the studio microphone off and Remote DJ
  disconnected until the job has finished.
- An operator-controlled session (Manual mode, Start Next or Eject in progress) makes an engine restart inappropriate:
  return to Auto first.

## GStreamer preflight: applicability to r0147

r0146 required Gate A (construct-time `force-live=true` deck mixer) and Gate B (fixed S16LE deck-bus caps probe) before
installation, because r0146 changed how the deck mixer and deck bus are built. **r0147 does not touch that code**: the
engine diff since r0146 adds first-audio recovery methods, one constant and one deck attribute, and changes no
audiomixer, capsfilter, pipeline construction or element property. r0147 installs no package and does not change the
system GStreamer runtime.

- **A station directly on accepted r0146 does not need to repeat Gate A and Gate B for r0147.** Instead confirm, from
  the engine journal since that station's last engine start, that `Deck mixer: audiomixer force-live=True` is present
  (the station is running the live mixer that r0146 installed). `force-live=False`, or a Monitoring warning "Deck mixer
  is not live (idle-start catch-up risk)", means the station does not have the r0146 correction: stop and report it; do
  not install r0147 until it is understood.
- **A station that is not on r0146** receives r0146 in its cumulative plan. For it, Gate A and Gate B in
  `r0146_installation.md` remain **mandatory** before installation, at that station's own sample rate and GStreamer
  runtime. KOGR's results never qualify WRJE.
- If the station's GStreamer runtime was changed after its r0146 acceptance, treat that as a change requiring the r0146
  gates again; r0147 does not authorize or perform it.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity, and confirm it is r0146 at `bef47174cd8cc7e0064102890b396fb0b93d83b0`. Do
   not infer release identity from `HEAD` alone.
2. **Release chain.** Confirm the protected updater verifies the complete chain from the installed release to r0147.
3. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed job
   awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately before
   installation. For a direct r0146 upgrade the action fingerprint must describe source advance and the engine restart
   only.
6. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers.
7. **Engine and encoder health, ordinary unattended playout.** The engine is running and healthy, the encoders are
   connected, program audio is airing, and the log is advancing unattended in Auto (no Manual mode, no Start Next, no
   Eject, no operator-forced item pending).
8. **Timing.** No scheduled transition is imminent, no urgent or Weather/AMBER alert is active, and no Voice Track or
   dedication is about to air.
9. **Deck mixer.** The r0146 mixer check above has been made on this station.
10. **Outstanding acceptance.** Confirm any open r0146 operational-acceptance observations are closed.

Stop if identity, chain, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the station, inside the approved window, use the protected UpdateCenter once. For a
station directly on r0146 it must:

1. Advance source to the exact canonical r0147 release commit R.
2. Restart `isadoraair-engine`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap,
configuration or protected-runtime operation is expected, and no Gunicorn, Monitoring, encoder, RBDS or worker restart.
Do not restart services outside the authorized update job. Do not retry a failed job automatically.

## Non-disruptive acceptance

Do **not** key a microphone on air, start a Remote DJ session, inject audio into the transmitter or streams, alter the
log or change station settings only to demonstrate acceptance. **Do not inject a stalled deck, corrupt or missing media
or deliberate silence into an operating station** to exercise the recovery: the isolated real-GStreamer engineering
evidence is the destructive-scenario evidence.

1. Verify the UpdateCenter job succeeded.
2. Confirm the installed source equals the exact canonical r0147 R, the checkout is clean and no migration is pending,
   and that the installed repository and runtime identity match the published release.
3. Verify the engine restarted (new PID and activation time) and Gunicorn, Monitoring, the encoders, RBDS and the
   workers kept their recorded PIDs and activation times **(no change)**.
4. **Deck mixer.** The engine journal since the restart contains `Deck mixer: audiomixer force-live=True`.
5. **Program audio and stream.** Program audio resumed after the restart, the encoders stayed connected, the stream and
   audio output are healthy, and the console shows the queue, NEXT, clocks and VU meters normally. Note the length of
   the interruption.
6. **Ordinary unattended playout.** Observe at least two ordinary song-to-song segues in Auto, and the transition
   between at least two scheduled occurrences: each is normal, with exactly one Play event per item and the expected
   play-count increment, no dead air and no delayed first audio.
7. **No unexpected recovery events.** The journal and Monitoring show no `FIRST-AUDIO DEADLINE` line, no
   "produced no audio; retrying" warning and no "No audio from ... occurrence skipped" critical event during normal
   operation, and no new Monitoring alarm, GStreamer critical, native crash, `vt2-teardown` event or engine restart.
8. **If a recovery does occur naturally** after installation, do not intervene. Preserve and record: the journal
   lines, the engine event(s) with `log_item_id`, `attempt`, `retry_opened` and `attempts`, whether the retry aired,
   whether exactly one Play event exists for the item, and whether the next item followed. This is additional
   operational evidence, not a failure of the installation; report it.
9. **No regressions.** Voice Tracks, urgent announcements, dedications, the studio microphone and Remote DJ behave as
   before when they occur in normal operation.

## Post-install Monitoring observation

For the rest of the day after installation, review Monitoring for engine restarts, dead air, first-audio recovery
events, deck-mixer warnings, GStreamer errors and encoder disconnects. Record any observation; do not remediate without
authorization.

## KOGR acceptance

KOGR is on r0146 (installed and operator-accepted 2026-11-23; Audio Pipeline 44100 Hz). Record KOGR evidence
independently: preflight items 1–10 with KOGR's PIDs and activation times, the safeguard assessment, the job, the plan
fingerprint, and acceptance items 1–9.

## WRJE acceptance

WRJE is on r0146 (installed and operator-accepted per the 2026-11-23 work order; Audio Pipeline documented as
48000 Hz). The protected updater must verify its actual release, schema and updater state and the complete chain; do
not assume them. Record WRJE evidence independently and do not reuse KOGR's results.

**A successful installation at one station is not authorization to install at the other.** Record KOGR and WRJE
evidence independently.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and the engine journal if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the engine restart appears for a direct r0146 upgrade, or a migration is
  pending;
- the engine fails to restart, logs `force-live=False`, or another service restarts unexpectedly;
- program audio does not resume, encoder connectivity is unhealthy, or the console fails to load.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0147 is not rollback authority and does not authorize an
installation at either station.
