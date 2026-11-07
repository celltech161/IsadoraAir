# r0127 installation, verification and recovery

This runbook describes a later, separately authorized station installation. Publishing r0127 does
not authorize any step on KOGR or WRJE, a production reconciliation scan or a Track deletion.

## Release identity and trusted plan

* **Previous release:** r0126 at `087a94873fc0b7c9161a2d8bfdb1a61f9565eb7a`.
* **R:** the unique canonical commit introducing `deploy/releases/r0127.json`; record its full SHA
  from the published repository and require the Update Center preview to show that exact commit.
* **No companion:** r0127 has no migration and therefore no
  `deploy/migration_authorizations/r0127.json`.
* **Expected one-release plan from r0126:** releases `[r0127]`, no migrations, packages, static,
  nginx, systemd or protected-runtime action, then restart `isadoraair-gunicorn` only.

Do not install from the feature branch, a rebuilt equivalent commit or a locally modified checkout.

## Read-only preflight

Perform independently for each station immediately before requesting installation authorization.

1. **Canonical identity:** fetch canonical `main`; confirm the target is R, r0126 is its release-chain
   predecessor, the station's installed source is the expected clean r0126 release, and the Update
   Center offers the reviewed plan and fingerprint.
2. **Update Center:** no job is running or awaiting manual intervention. Schema health is good, the
   last backup and recovery checks are acceptable, and protected generation/policy are unchanged.
3. **No migration:** `showmigrations --plan` and the trusted preflight show no unexpected pending or
   edited migration. Do not create a companion or run `migrate` for r0127.
4. **Current reconciliation state:** no CLI or browser reconciliation is running and no launch lock
   is held. Record the latest report's scan id, completion time and status. Do not run a scan merely
   to prepare the installation.
5. **Operator window:** no one is doing Library or reconciliation work. Inform Track managers that
   Gunicorn will be briefly unavailable and in-flight web requests may fail during restart.
6. **Service baseline:** record Gunicorn's PID and `ActiveEnterTimestamp`; record the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken-render PIDs/timestamps so unintended
   restarts can be detected. Confirm audio and monitoring are normal.
7. **Feature state:** record Hour Refill policy/arming and native Active Messages airing. r0127
   changes neither and must not be used to activate them.

Any identity, schema, scanner-state, permission, service-health or plan mismatch is a no-go. Stop and
review it rather than altering the manifest or station to make the release fit.

## Installation

Use only the normal trusted Update Center workflow after separate authorization for that station.
Confirm the release, exact R, action plan and fingerprint immediately before starting.

The updater advances source to R and restarts `isadoraair-gunicorn`, then verifies it. It does not:

* run a migration or `collectstatic`;
* install a package;
* create, enable or restart a service or timer;
* change protected-runtime generation or policy;
* restart the engine, encoders, Monitoring, RBDS, Active Messages or spoken rendering;
* run Library Reconciliation;
* resolve a duplicate or delete a Track;
* change Hour Refill, native Active Messages or any station configuration.

Do not manually restart services or invoke reconciliation endpoints as part of installation.

## Post-install verification

1. **Job and source:** the Update Center job succeeded; installed release is r0127; station `HEAD` is
   exactly R on clean `main`; Django checks remain clean.
2. **Restart inventory:** Gunicorn has a new PID/timestamp and is healthy. Every other service
   recorded during preflight retains its prior PID/timestamp. Audio and monitoring remain normal.
3. **Access control:** an authorized **Manage tracks** operator can open **Library →
   Reconciliation**. A user without that capability cannot access its page, findings, preview, Keep
   Both or apply endpoints. GET is refused on the state-changing endpoints and normal CSRF protection
   applies to POST.
4. **Existing report transition:** without starting a scan, confirm the previous report still renders.
   An r0126-or-earlier report must explain that it lacks the complete file witness and must not offer
   an executable deletion. Keep Both may remain available because it deletes nothing.
5. **No automatic execution:** wait through a normal polling interval and confirm no scan, duplicate
   choice or deletion occurs automatically.
6. **Unchanged state:** confirm no Track, media, waveform, category, log, rotation, playout, Hour
   Refill or native Active Messages state changed because of installation.

### Separately authorized browser acceptance

The following is not part of installation. Perform it only with explicit station acceptance
authorization and a maintenance window.

1. Run a new reconciliation scan and record its scan id and completion evidence. It must complete
   before its exact two-Track findings can authorize deletion.
2. In a real browser, select an exact pair and exercise **Stay and keep selections** and **Discard
   selections and continue** across page, filter and debounced search changes.
3. Preview the pair and verify Track links, category suggestions, log/playout blockers and VoiceTrack
   acknowledgment. Use **No action** or Keep Both unless the operator has separately approved a
   specific disposable, non-airing deletion test.
4. If a controlled test proxy is available, make the first request of a multi-pair batch return 5xx;
   verify the first result is Unknown, later pairs are Not attempted, findings reload and no request
   is retried. Do not induce an uncertain outcome against valuable media.
5. After any authorized resolution, verify the audit record and source-cleanup result, then run a new
   reconciliation scan before relying on remaining findings.

KOGR acceptance is not evidence for WRJE. Repeat the complete preflight, authorization, installation
and verification independently at WRJE.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise.

| Failure point | Expected state | Required handling |
|---|---|---|
| Before source advances | Production unchanged | Correct the external cause and request a reviewed retry |
| Source advances, Gunicorn restart fails | r0127 source present; web UI unavailable; audio path unchanged | Preserve job and journal evidence; do not reset Git manually |
| Old report appears deletion-eligible | Safety invariant violated | Do not preview or apply; preserve the report and browser response; prepare a corrective release |
| Preview is blocked by engine state | Nothing changed | Restore or diagnose engine observability through normal operations; do not weaken the check |
| Pair reports busy, stale, blocked or refused | That pair did not change | Review the reason and make a new preview; never reuse its token |
| Pair reports Unknown after network/5xx | It may or may not have committed | Do not retry; inspect reloaded findings and audit evidence before a new preview |
| Database deletion committed but source cleanup retained/failed | Track row is gone; media may remain safely | Preserve the reported outcome and use a separately reviewed cleanup procedure |
| Defect found after successful install | r0127 remains installed | Stop using duplicate resolution and prepare a forward corrective release |

Because r0127 has no schema change, r0126 code is database-compatible. A source rollback is outside
the normal Update Center workflow and requires separate reviewed recovery authorization. It cannot
undo a committed Keep Both decision or Track deletion and must never be represented as data rollback.
