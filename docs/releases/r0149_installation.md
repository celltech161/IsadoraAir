# r0149 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0149 has been published.

r0149 corrects Hour Refill Start Next eligibility with a connected Remote DJ (roadmap 2.27). A direct r0148 → r0149
update **restarts Gunicorn and the audio engine**. **The engine restart interrupts program audio.** This is not a
Gunicorn-only, non-disruptive release. The encoders, Monitoring, RBDS and the workers keep running.

**Installation is not Hour Refill activation.** Installing r0149 changes no Hour Refill policy value, arms no host and
clears no Observation only. Installation acceptance below says nothing about activation acceptance. Host arming, and
clearing Observation only, each need their own later authorization.

## Exact identity and trusted plan

- Previous published release: r0148 at `4e13350940d5a4b6670dc319d6804e5249906352`, tree
  `89aa7226c3bc3a094e8512a89f227b1c350dc22d`.
- Accepted engineering tip: `a86eec5bd8a3aef3134ea562e0249788e09b75ec`, tree
  `feef3890f432f3ef417213fe1813336f60c30646`: five linear commits on r0148, `e710899`, `63d7849`, `8ae4f4b`,
  `4a89b64`, `a86eec5`, published unchanged.
- R is the unique canonical commit introducing `deploy/releases/r0149.json`, the direct child of the engineering tip,
  and adds only the four release files.
- There is **no migration**. A direct r0148 → r0149 plan advances source to R, then restarts `isadoraair-gunicorn` and
  then `isadoraair-engine`. Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap,
  station-configuration, Hour Refill policy-data or protected-runtime action, no GStreamer upgrade, and no encoder,
  Liquidsoap, Monitoring, RBDS or worker restart for a direct r0148 update.
- If the trusted plan for a station directly on r0148 shows any other action, stop and investigate; do not install.
- A station below r0148 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0147 receives r0148 and r0149 (the same two restarts); a station on r0146 also receives
  r0147; a station on r0144 also restarts Monitoring for r0145; a station on r0132 also applies
  `library.0101_uitheme_show_played_items`, `library.0102_uitheme_log_display_appearance` and
  `library.0103_remotedjconfig_auto_reconnect_enabled` (additive). A station further behind may have still more.

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do not
manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter after
separate station authorization.

## Engine restart safety and maintenance window

The engine restart **interrupts program audio** and ends any in-progress Voice Track. Do not treat it as inaudible.
Schedule an **approved operator maintenance window** away from live programs, Remote DJ shows and urgent-alert
activity, and not within minutes of a scheduled transition or while a Weather/AMBER alert is active.

- **On r0144 or later:** the installation starts from an Update Center with the **engine restart safeguard**, which
  refuses it while a studio microphone is live, a Remote DJ is connected (even with its microphone Off),
  reconnecting, establishing or occupying its slot, or engine status is unknown. **r0149 does not change the
  safeguard.** That r0149 lets a monitoring Remote DJ stop excluding a Start Next from Hour Refill has no bearing on
  restarting the engine. **Do not override the safeguard to install.** End the live session and preview again.
- **Below r0144:** the installed Update Center predates the safeguard and will not check. Before starting the job,
  confirm by direct observation that no studio microphone is live and no Remote DJ is connected, and that the operator
  agrees to the program-audio interruption.
- The safeguard checks admission, not the moment of the restart: keep the studio microphone off and Remote DJ
  disconnected until the job has finished.
- An operator-controlled session (Manual mode, a Start Next or an Eject in progress) makes an engine restart
  inappropriate: return to Auto and let the transition finish first.

## GStreamer runtime preflight: applicability to r0149

r0149 does **not** change the deck mixer, deck bus, pipeline construction, engine startup, shutdown, resume or the
r0147 first-real-audio recovery. In `engine.py` it changes only the Start Next ownership snapshot and adds one
read-only microphone helper. It installs no package and does not change the system GStreamer runtime.

- **A station already accepted on r0148 with an unchanged qualified runtime does not repeat the r0146 Gate A and
  Gate B.** Instead verify the installed runtime and the initialization evidence: from the engine journal since that
  station's last engine start, `Deck mixer: audiomixer force-live=True` is present, and the `Pipeline sample rate -> N`
  line shows the station's configured rate. `force-live=False`, or a Monitoring warning "Deck mixer is not live
  (idle-start catch-up risk)", means the station does not have the r0146 correction: stop and report it.
- **If the station's GStreamer runtime was changed after its acceptance**, the r0146 Gate A and Gate B apply again
  before installation. r0149 does not authorize or perform a runtime change.
- **A station that is not yet on r0146** receives r0146 in its cumulative plan: Gate A and Gate B in
  `r0146_installation.md` remain **mandatory** before installation, at that station's own sample rate and runtime.
- A station below r0148 must also satisfy every other requirement of each intervening release's installation guide.
- KOGR's results never qualify WRJE.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity, and confirm it is r0148 at `4e13350940d5a4b6670dc319d6804e5249906352`.
   Do not infer release identity from `HEAD` alone.
2. **Release chain.** Confirm the protected updater verifies the complete chain from the installed release to r0149.
3. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed
   job awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and the **full** action fingerprint
   immediately before installation. For a direct r0148 upgrade it must describe source advance, then the Gunicorn
   restart, then the engine restart, and nothing else.
6. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers.
7. **Engine runtime identity.** Record the `runtime_commit` the engine publishes. It names the commit the engine
   process loaded when it last started, which can be older than the installed checkout: a release that restarted only
   Gunicorn (r0148) leaves it at the earlier engine start.
8. **Engine initialization evidence and ordinary playout.** Make the deck-mixer and sample-rate check above. The
   engine is running and healthy, the encoders are connected, program audio and the stream are healthy, and the log is
   advancing unattended in Auto (no Manual mode, no Start Next, no Eject, no operator-forced item pending).
9. **Restart window.** The window is approved; no studio microphone is live; no Remote DJ is connected; no scheduled
   transition is imminent; no urgent or Weather/AMBER alert is active; no Voice Track or dedication is about to air.
   Record the safeguard assessment.
10. **Hour Refill state (read-only).** On the Hour Refill Policy page and its status panels record: the mode (it must
    read *Observation*, or *Off* where the master switch is off); **Observation only (dry run)** checked; both
    **host armed** rows *no*; the master switch and **each cause switch**; any drift from the policy the operator has
    approved for the station; and the latest Hour Refill install, if any (none is expected).
11. **Outstanding acceptance.** Confirm any open r0148 operational-acceptance observations are closed.

Stop if identity, chain, schema, fingerprint, restart inventory, service health, Observation only or host arming
differs. Do not shorten or manually reconstruct a catch-up plan.

## Installation expectations

After separate authorization for the station, inside the approved window, use the protected UpdateCenter once. For a
station directly on r0148 it must:

1. Advance source to the exact canonical r0149 release commit R.
2. Restart `isadoraair-gunicorn`.
3. Restart `isadoraair-engine`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap,
configuration, Hour Refill policy or protected-runtime operation is expected, and no encoder, Monitoring, RBDS or
worker restart. The updater may report that it processed a stage for an action that does nothing for this plan (for
example migrations or static collection): that milestone is not evidence that anything was applied or restarted;
confirm the effective restart set against the plan. Do not restart services outside the authorized update job. Do not
retry a failed job automatically.

## Installation acceptance

Acceptance observes normal operation. Do **not** key a microphone on air, start a Remote DJ session, press Start Next,
alter the log, change a station or Hour Refill setting, or inject any fault only to demonstrate acceptance.

1. Verify the UpdateCenter job succeeded and preserve its complete evidence.
2. Confirm the installed source equals the exact canonical r0149 R and tree, the checkout is clean and no migration is
   pending.
3. Verify Gunicorn **and** the audio engine restarted (new PIDs and activation times).
4. Verify the encoders, Monitoring, RBDS and the workers kept their recorded PIDs and activation times
   **(no change)**.
5. **Shutdown and resume.** The engine journal shows the old process ending normally (`Shutting down...`,
   `Engine stopped.`). If an item was airing, the new process either resumes it (`Restoring interrupted log_item`) or
   states why it did not (`Resume state rejected: ...`); record which, and investigate an unexplained rejection.
6. **Engine initialization.** The journal since the restart contains `Deck mixer: audiomixer force-live=True` and the
   station's `Pipeline sample rate`, with no GStreamer critical or native crash, and the engine's published
   `runtime_commit` equals R.
7. **Program audio and stream.** Program audio resumed, the encoders stayed connected, the stream and audio output
   are healthy, and the console shows the queue, NEXT, clocks and VU meters normally. Note the length of the
   interruption.
8. **Subsequent playback.** Observe at least two ordinary song-to-song segues in Auto and the next scheduled
   occurrence transition: each is normal, with exactly one Play event per item, no dead air and no delayed first
   audio. The journal and Monitoring show no unexpected `FIRST-AUDIO DEADLINE`, "produced no audio" or "occurrence
   skipped" event, and no new alarm or unplanned engine restart.
9. **Admin help text.** On the Hour Refill Policy page, the **Capture start next** help reads: "A completed Start
   Next fade that cut a scheduled music song short and started the next item. Overlap, start-only, fade to silence,
   Manual and live-microphone (studio or Remote DJ) transitions are never causes; a connected Remote DJ with the mic
   off does not prevent one." Do not save the page to prove it.
10. **Observation preserved.** The mode and every switch are as recorded in preflight item 10; **Observation only
    (dry run) remains checked**; both **host armed** rows still read *no*.
11. **No installs.** No new Hour Refill install record and no refill-origin log item exists.

## Post-install Monitoring observation

For the rest of the day after installation, review Monitoring for engine restarts, dead air, first-audio recovery
events, deck-mixer warnings, GStreamer errors, encoder disconnects and Hour Refill runner errors. Record any
observation; do not remediate without authorization.

## KOGR installation checklist

When r0149 was prepared, KOGR's UpdateCenter record showed r0148 installed. **Verify it again on the station**; do not
rely on this document. Because r0148 restarted Gunicorn only, KOGR's engine can still publish the r0147
`runtime_commit` until an engine restart.

- [ ] Preflight items 1–11, with KOGR's own PIDs, activation times, sample rate and GStreamer version.
- [ ] The installed release is r0148, the checkout is clean, the schema and updater are healthy.
- [ ] The trusted plan and its full fingerprint are recorded and show only source advance, the Gunicorn restart and
      the engine restart.
- [ ] The engine restart preflight passes and an appropriate restart window is confirmed.
- [ ] Hour Refill is **Observation only**; host arming is **false** in both rows.
- [ ] The current cause switches and any policy drift from the approved Start Next-only pilot policy are recorded.
      Reconciling them is a later activation prerequisite: **do not change them during installation.**
- [ ] Explicit installation authorization for KOGR has been given.

After the separately authorized installation, complete installation acceptance items 1–11 and record them for KOGR.

## WRJE installation and acceptance

WRJE needs its own installation preflight, authorization and acceptance: **do not assume** its installed release,
schema, runtime, sample rate or Hour Refill state. The protected updater must verify the actual release and the
complete chain. A catch-up plan keeps every intervening migration and restart, and every intervening installation
requirement applies (including Gate A and Gate B if WRJE is not yet on accepted r0146, at WRJE's own sample rate).
Below r0144 the safeguard does not check; apply the manual live-talent check. Complete preflight items 1–11 and
acceptance items 1–11 with WRJE's own evidence.

WRJE's Hour Refill activation is qualified **independently**: its own category audit, shadow observation, timing and
proposal review, cause evidence, arming and install authorizations. KOGR's evidence does not replace any of it.

**A successful installation at one station is not authorization to install, arm or activate the other.**

## Post-installation shadow verification of the newly permitted path

**Run this only under its own operator authorization, after installation acceptance.** It is a shadow (observation)
check of the path r0149 newly permits. It installs nothing.

**Conditions (all of them):**

- Auto mode.
- Ordinary eligible scheduled music: the song to be cut is a **scheduled** occurrence in a `music`-kind category,
  on air and not paused, early enough in the song that the cut shortens it; not a fill, request, forced, urgent or
  operator-inserted item; and not close to the end of the hour.
- A Remote DJ **connected**, with the **remote microphone Off** (not requested, not reconnecting).
- The studio microphone Off.
- No unsafe ownership or transition state: no Voice Track, seek, earlier Start Next, live fill, urgent item or
  alert in progress.
- **Observation only (dry run) checked** and the **host unarmed**.
- The master switch and the Start Next cause switch On.

**Action.** The operator deliberately presses one Start Next that fades the current song out and starts the next item
(overlap, start-only and fade-to-silence are never causes). Record the time and the song.

**Verify, read-only:**

1. **Exactly one durable cause** appears for that Start Next (Hour Refill Causes in Admin).
2. Its type is Start Next, and its occurrence identity matches: log date and hour, log item, Track, deck slot and
   generation, engine instance and request.
3. The Remote DJ connection **does not block** an otherwise eligible projection: the evaluation (the
   `Hour-refill evaluation` journal line and the status panel's latest outcome with its reasons) is not
   `blocked_transport` with reason `remote_dj`.
4. The net shortage and threshold are computed freshly for that hour and are correct for a Start Next-only set
   (more than 30 s qualifies). If other cause types are recorded for the same hour and switched on, the set is mixed
   and the connection **is** expected to block it; choose a clean hour.
5. If a qualifying deficit exists, the advisory proposal is structurally acceptable: category, recency, request and
   dedication adjacency, protected closer, at most two tracks, insertion lead and safety reserve.
6. **No live installation occurs:** no install record, no refill-origin log item, the mode still *Observation* and the
   host still unarmed.

**Reading the result.**

- **Capture verified:** items 1–3 and 6 hold.
- **Full proposal path verified:** capture verified **and** a qualifying deficit produced an acceptable proposal
  (items 4 and 5).
- A `below_threshold` or `already_satisfied` outcome is **not** a capture defect: capture can be verified while the
  proposal path stays unverified. Report which of the two was verified.
- No cause at all, or a connection block on a Start Next-only set, is a finding: keep Observation only checked,
  preserve the journal and status, and report it.

## Remaining pilot prerequisites

Each is separate. Installing r0149 satisfies none of those still open:

1. The original 14-day observation: for KOGR it was completed and independently verified. **Installing r0149 does not
   restart the observation clock.**
2. Current category and protected-closer decisions, including: **Sign On**; the empty **New Music Friday** category;
   and **ArloG** with the Thanksgiving direct-track hour.
3. Reconciliation to the approved **Start Next-only pilot policy**: master and Start Next On; request substitution,
   operator Eject, automatic skip and media failure Off; residual PSA Off.
4. The focused post-installation Remote DJ shadow verification above.
5. Separate operator authorization for **host arming**.
6. Separate operator authorization to **clear Observation only**.
7. Supervision of the **first actual refill**.
8. **Seven days** of limited-install observation.
9. **At least one actual refill successfully observed airing** before operational acceptance.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and the engine and Gunicorn journals if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the two restarts appears for a direct r0148 upgrade, or a migration is pending;
- Gunicorn or the engine fails to restart, the engine logs `force-live=False`, or another service restarts
  unexpectedly;
- program audio does not resume, subsequent playback is abnormal, encoder or stream health is bad, or the console or
  Admin fails to load;
- Observation only is no longer checked, a host reads as armed, or an Hour Refill install or refill-origin log item
  appears.

If an Hour Refill write is ever suspected, check **Observation only (dry run)** or switch the master Off first (no
restart needed), then preserve the evidence.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0149 is not rollback authority and does not authorize an
installation or activation at either station.
