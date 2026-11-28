# r0150 station installation and recovery

Use the protected Update Center for application advancement. Install the
watcher and tmpfiles configuration separately. Production installation
requires separate station authorization.

## Verify station and recovery readiness

- Confirm the station's installed release, clean Git identity on `main`,
  database schema, protected updater readiness and exact target and plan
  fingerprint. The direct baseline is r0149 at
  `317c9a9a925caff958d238a2872832a78c2ef488`. An older station needs the full
  cumulative plan, including any other migrations, checkpoint or protected
  runtime steps the plan requires. Stage the watcher for any plan crossing r0150.
- On KOGR, reverify `jreed:jreed`, the application root
  `/home/jreed/isadoraair-django` through `/opt/isadoraair`, its
  `venv/bin/python` and `.env`, and the library and waveform roots
  `/srv/isadoraair/music` and `/srv/isadoraair/waveforms`. The recorded
  configuration had road processing, weather-scheduled TTS and transition
  sound enabled, with a 30-minute cadence and 120-minute stale threshold.
  Verify PostgreSQL, TTS assets, transition sound and KanDrive directory access.
- On WRJE, confirm its installed release, updater generation, actual
  service account, roots, dependencies, configuration and recipients.
  For enabled road processing, apply the same procedure with WRJE's
  own settings. If disabled, preserve that setting, install the watcher
  file and keep the watcher and both timers inactive. If WRJE policy
  previously kept a no-op sync timer active, record the change when
  disabling it. The r0150 sync unit requires an active watcher.
- Verify a compatible pre-install application/database backup and the
  approved P0 1.2 recovery payload. Record exact release and database
  identity, archive checksums, recovery access and the separately held
  recovery credentials. Verify the backup and payload agree on the
  protected runtime and station recovery configuration. See the
  [disaster-recovery contract](../DISASTER_RECOVERY.md) and
  [restore runbook](../DISASTER_RECOVERY_RESTORE.md).
- Additionally preserve rendered road service, timer and watcher files,
  unit fragment paths, drop-ins, `/etc/tmpfiles.d/isadoraair.conf`, and
  recorded enablement and activity. Preserve the KanDrive FLAC, script,
  provenance, pending publication journal and previous-audio copy when
  present, plus the waveform and matching Track readiness, timing and cue
  marker evidence. Capture a consistent media and database snapshot while
  the road jobs are drained. The standard archive excludes the music
  and waveform trees. Verify coverage of live units and local overrides
  independently. An updater migration checkpoint is a database artifact.

## Quiesce and stage

1. Record `systemctl cat` and the active/enabled state for the road units.
   Stop and disable the legacy generation and sync timers that exist.
   Stop and disable any existing road watcher. Let both oneshots finish
   and verify they are inactive. Investigate an unfinished job before
   taking the consistent snapshot. Disablement protects the cutover
   across an intervening reboot.
2. Save and remove obsolete legacy-timer scheduling drop-ins, including
   `10-toh-stagger.conf` if present. Keep the original timer fragment as
   a regular file for protected replacement. The updater refuses a
   symlink or mask at that destination.
3. In a separate verified checkout of the exact approved release, stage
   only the manual watcher and tmpfiles configuration. Set
   `release_checkout` to that separate checkout and `station_user` to
   the confirmed account before running the block. Use `jreed` at KOGR.

   ```bash
   : "${release_checkout:?set the verified separate release checkout}"
   : "${station_user:?set the verified station service account}"
   watcher=isadoraair-generate-road-condition-audio.path
   stage=$(mktemp -d)
   cp "$release_checkout/deploy/$watcher" "$stage/$watcher"
   sed -e "s|@@ISA_USER@@|$station_user|g" \
     "$release_checkout/deploy/isadoraair-tmpfiles.conf" > "$stage/isadoraair.conf"
   sudo install -o root -g root -m 0644 "$stage/$watcher" /etc/systemd/system/
   sudo install -o root -g root -m 0644 "$stage/isadoraair.conf" /etc/tmpfiles.d/isadoraair.conf
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/isadoraair.conf
   sudo systemctl daemon-reload
   systemctl show "$watcher" -p LoadState -p ActiveState -p UnitFileState
   ```

   Verify that the watcher is loaded, inactive and disabled, and
   `/run/isadoraair` and `/run/isadoraair/tts` have the expected station
   ownership and modes 0755 and 0700. The old application cannot accept
   the new generation service's `--dispatched` argument. Keep the
   watcher stopped and leave all managed templates to the updater.

## Advance and activate

1. Reconfirm the exact release and fingerprint and submit the protected
   Update Center job. Wait for success and verify the exact target HEAD,
   successful unit reconciliation, and Django and Gunicorn health.
   The direct plan has no migration. The protected executor advances
   source before installing the three revised managed unit templates.
2. Reconfirm that the legacy timer is stopped and disabled. Its
   replacement stays on disk under the original name, with no future
   deadline. The signed policy can enable a newly required timer in a
   cumulative plan. The compatibility unit is safe in that state;
   disable it again as part of station convergence. If an older plan
   explicitly lists the sync timer as newly required, see the cumulative
   plan caveat below.
3. If roads are enabled, activate the watcher first, verify it is active,
   then enable and start the sync timer.

   ```bash
   sudo systemctl disable --now isadoraair-generate-road-condition-audio.timer
   sudo systemctl enable --now isadoraair-generate-road-condition-audio.path
   systemctl is-active isadoraair-generate-road-condition-audio.path
   sudo systemctl enable --now isadoraair-sync-road-conditions.timer
   systemctl is-active isadoraair-sync-road-conditions.timer
   systemctl list-timers --all --no-pager
   ```

   If roads are disabled, leave both timers and the watcher disabled and
   inactive. Verify the station application setting remains disabled.
   For either configuration verify there is no legacy generation schedule,
   stray drop-in, or second automatic generation trigger.
4. For enabled stations, observe the next successful authoritative sync
   and matching generation check in the journal. Expect one initial
   regeneration when provenance is missing. Confirm Ready-to-Air and cue
   timing on the replacement, an unchanged report skips synthesis, and
   the watcher and timer are enabled for reboot. Repeat the checks after
   a separately authorized reboot. A missing or inactive watcher causes
   a systemd dependency failure when sync starts. Inspect the sync and
   watcher journal for that failure.
5. Confirm the station's recipients and cooldown and use the approved
   routing verification procedure. Supported SMS destinations use
   configured email-to-SMS gateway addresses. Feed warning, critical and
   recovery use that channel. Publication/recovery errors appear as
   Monitoring SystemEvents and require operator review. The generic
   SystemEvent feed has no automatic email/SMS delivery.

Fresh install and disaster-recovery stage 90 both install the watcher and
compatibility timer as files. Materialize tmpfiles before bring-up. Use
the same watcher-first activation and disabled-station checks. The
protected planner requires supported source and schema identities before
any subsequent ordinary release.

A cumulative plan that actually reintroduces the road timers as required
may start them during reconciliation, after the protected source advance.
The legacy timer is elapsed. Keep the watcher inactive unless the source
advance is confirmed, so an early sync remains blocked by its dependency.
For r0150's supported ordinary catch-up releases the road timers were
already introduced at r0023 and are changed or unchanged units.

## Failure and recovery

**Before source advance**, verify live HEAD and the protected job's
durable milestones. For a direct r0149 update, restore the recorded
r0149 road unit fragments, legitimate drop-ins and tmpfiles if changed.
Stop and disable the new watcher before removing its fragment. Reload
systemd and restore the recorded enablement and activity of the sync and
legacy timers. Verify r0149 starts the original generator service and
that the legacy timer is its only automatic generation authority. r0149
can retire readiness during an upstream failure. Preserve any evidence
and retry through the protected updater after correcting the failure.
For a cumulative plan with migrations, follow its checkpoint and
partial-migration rules before resuming jobs.

**After source advance**, preserve the job evidence and prefer fix-forward
through the protected Update Center. Keep road activation quiesced while
the unit state is being reconciled. Preserve pending publications and
their media and Track evidence. When appropriate under separate station
authorization, the r0150 `generate_road_condition_audio --recover-only`
command can settle its own journal. Do not run the old generation service
against r0150's pending state.

If fix-forward is unavailable, use the separately authorized P0 1.2
disaster-recovery procedure with a compatible pre-update backup and
payload. Restore application, database, runtime and station configuration
as a consistent recovery set. Reconcile the supplementary KanDrive and
live-unit artifacts before restoring service, and complete Recovery
Assurance. The protected updater is forward-only. See the
[Update Center recovery rules](../UPDATE_CENTER.md#failure-retry-and-rollback)
and the [r0113 precedent](r0113.md#forward-only-release-and-recovery).
