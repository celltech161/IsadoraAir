# r0129 installation, verification and recovery

This runbook describes a later, separately authorized station installation. Publishing r0129 does
not authorize any step on KOGR or WRJE, a production reconciliation scan, a duplicate decision or
deletion of production media.

## Release identity and trusted plan

* **Previous release:** r0128 at `10fe0938e6362dea961ac817d485451ce4c9a3c7`.
* **R:** the unique canonical commit introducing `deploy/releases/r0129.json`; record its full SHA
  from the published repository and require the Update Center preview to show that exact commit.
* **No companion:** r0129 has no migration and therefore no
  `deploy/migration_authorizations/r0129.json`.
* **Expected one-release plan from r0128:** releases `[r0129]`, no migrations, packages, static,
  nginx, systemd or protected-runtime action, then restart `isadoraair-gunicorn` only.

Do not install from the feature branch, a rebuilt equivalent commit or a locally modified checkout.

## Read-only preflight

Perform independently for each station immediately before requesting installation authorization.

1. **Canonical identity:** fetch canonical `main`; confirm the target is R, r0128 is its release-chain
   predecessor, the station's installed source is the expected clean r0128 release, and the Update
   Center offers the reviewed plan and fingerprint.
2. **Update Center:** no job is running or awaiting manual intervention. Schema health is good, the
   last backup and recovery checks are acceptable, and protected generation/policy are unchanged.
3. **No migration:** `showmigrations --plan` and the trusted preflight show no unexpected pending or
   edited migration. Do not create a companion or run `migrate` for r0129.
4. **Reconciliation state:** no CLI or browser reconciliation is running and no launch lock is held.
   Record the latest report's scan id, completion time and status. Do not run a scan merely to prepare
   installation.
5. **Operator window:** no one is editing the Library or using reconciliation. Inform Track managers
   that Gunicorn will be briefly unavailable and in-flight web requests may fail during restart.
6. **Service baseline:** record Gunicorn's PID and `ActiveEnterTimestamp`; record the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken-render PIDs/timestamps so unintended
   restarts can be detected. Confirm audio and monitoring are normal.
7. **Feature state:** record Hour Refill policy/arming and native Active Messages airing. r0129
   changes neither and must not be used to activate them.

Any identity, schema, scanner-state, permission, service-health or action-plan mismatch is a no-go.
Stop and review it rather than altering the manifest or station to make the release fit.

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
* resolve a duplicate, alter a Track or delete media;
* change Hour Refill, native Active Messages or any station configuration.

Do not manually restart services or invoke reconciliation endpoints as part of installation.

## Post-install verification

1. **Job and source:** the Update Center job succeeded; installed release is r0129; station `HEAD` is
   exactly R on clean `main`; Django checks remain clean.
2. **Restart inventory:** Gunicorn has a new PID/timestamp and is healthy. Every other service
   recorded during preflight retains its prior PID/timestamp. Audio and monitoring remain normal.
3. **Access control:** an authorized **Manage tracks** operator can open **Library →
   Reconciliation** and sees the manual review control for eligible likely and uncertain findings. A
   user without that capability cannot access the page, findings, preview or apply endpoints.
4. **Existing report transition:** confirm the prior report remains readable but cannot authorize a
   manual deletion if it lacks complete `member_files` evidence. Installation must not retrofit or
   infer that evidence.
5. **No automatic execution:** wait through a normal polling interval and confirm no scan, duplicate
   choice or deletion occurs automatically.
6. **Unchanged state:** confirm no Track, media, waveform, category, log, rotation, playout, Hour
   Refill or native Active Messages state changed because of installation.

### Separately authorized browser acceptance

The following is not part of installation. Perform it only with explicit station acceptance and
media-change authorization.

1. Run a fresh reconciliation scan and record its scan id, progress and completion evidence. Existing
   pre-r0129 scans cannot authorize the manual route.
2. Review a disposable, non-airing finding approved by the operator. Confirm exact-byte,
   decoded-audio and manual-editorial authorities remain visibly distinct.
3. Confirm **No action** is the default; Keep Both remains available; a destructive choice requires
   the manual acknowledgment, and a Track with VoiceTracks requires its separate acknowledgment.
4. Confirm the preview states that the recordings may be different edits, masters or performances,
   the survivor is selected by the operator, and only explicitly selected additional categories are
   transferred. The survivor's primary category and all other Track-specific data are not merged.
5. Prefer **No action** or Keep Both. Apply a deletion only with separate authorization for that exact
   disposable Track. Verify its audit authority, category outcome and source-cleanup status.
6. Run a fresh reconciliation scan before another destructive decision. After any busy, stale,
   blocked, refused or uncertain response, inspect current state and create a new preview. Never retry
   an uncertain apply request automatically.

KOGR acceptance is not evidence for WRJE. Repeat the complete preflight, authorization, installation
and verification independently at WRJE.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise.

| Failure point | Expected state | Required handling |
|---|---|---|
| Before source advances | Production unchanged | Correct the external cause and request a reviewed retry |
| Source advances, Gunicorn restart fails | r0129 source present; web UI unavailable; audio path unchanged | Preserve job and journal evidence; do not reset Git manually |
| Old/incomplete report offers manual deletion | Safety invariant violated | Do not preview or apply; preserve evidence and prepare a corrective release |
| Group reports busy, stale, blocked or refused | No partial change for that pair | Review the reason and make a fresh preview; never reuse its token |
| Pair reports Unknown after network/5xx | It may or may not have committed | Do not retry; inspect refreshed findings and audit evidence before a new preview |
| Database deletion committed but source cleanup retained/failed | Track row is gone; media may remain safely | Preserve the outcome and use a separately reviewed cleanup procedure |
| Operator selected the wrong survivor | The committed deletion is not automatically reversible | Stop; preserve evidence and use a separately reviewed data/media recovery procedure |
| Defect found after successful install | r0129 remains installed | Stop manual duplicate disposition and prepare a forward corrective release |

Because r0129 has no schema change, r0128 code is database-compatible. A source rollback is outside
the normal Update Center workflow and requires separate reviewed recovery authorization. It cannot
undo a Keep Both decision, committed Track deletion, category addition, audit event or removed audio,
and must never be represented as data rollback.
