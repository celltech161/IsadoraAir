# r0128 installation, verification and recovery

This runbook describes a later, separately authorized station installation. Publishing r0128 does
not authorize any step on KOGR or WRJE, a production reconciliation scan, media decoding or a Track
deletion.

## Release identity and trusted plan

* **Previous release:** r0127 at `0eb739f6d2420d128a2548aa83a855d47bd475c7`.
* **R:** the unique canonical commit introducing `deploy/releases/r0128.json`; record its full SHA
  from the published repository and require the Update Center preview to show that exact commit.
* **No companion:** r0128 has no migration and therefore no
  `deploy/migration_authorizations/r0128.json`.
* **Expected one-release plan from r0127:** releases `[r0128]`, no migrations, packages, static,
  nginx, systemd or protected-runtime action, then restart `isadoraair-gunicorn` only.

Do not install from the feature branch, a rebuilt equivalent commit or a locally modified checkout.

## Read-only preflight

Perform independently for each station immediately before requesting installation authorization.

1. **Canonical identity:** fetch canonical `main`; confirm the target is R, r0127 is its release-chain
   predecessor, the station's installed source is the expected clean r0127 release, and the Update
   Center offers the reviewed plan and fingerprint.
2. **Update Center:** no job is running or awaiting manual intervention. Schema health is good, the
   last backup and recovery checks are acceptable, and protected generation/policy are unchanged.
3. **No migration:** `showmigrations --plan` and the trusted preflight show no unexpected pending or
   edited migration. Do not create a companion or run `migrate` for r0128.
4. **Runtime prerequisites:** as the reconciliation service account, confirm `/proc/self/fd` is
   available and FFprobe and FFmpeg are the expected station programs. This is verification of
   existing prerequisites; do not install a package or decode production media during preflight.
5. **Current reconciliation state:** no CLI or browser reconciliation is running and no launch lock
   is held. Record the latest report's scan id, completion time and status. Do not run a scan merely
   to prepare the installation.
6. **Operator window:** no one is doing Library or reconciliation work. Inform Track managers that
   Gunicorn will be briefly unavailable and in-flight web requests may fail during restart.
7. **Service baseline:** record Gunicorn's PID and `ActiveEnterTimestamp`; record the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken-render PIDs/timestamps so unintended
   restarts can be detected. Confirm audio and monitoring are normal.
8. **Feature state:** record Hour Refill policy/arming and native Active Messages airing. r0128
   changes neither and must not be used to activate them.

Any identity, schema, prerequisite, scanner-state, permission, service-health or plan mismatch is a
no-go. Stop and review it rather than altering the manifest or station to make the release fit.

## Installation

Use only the normal trusted Update Center workflow after separate authorization for that station.
Confirm the release, exact R, action plan and fingerprint immediately before starting.

The updater advances source to R and restarts `isadoraair-gunicorn`, then verifies it. It does not:

* run a migration or `collectstatic`;
* install a package;
* create, enable or restart a service or timer;
* change protected-runtime generation or policy;
* restart the engine, encoders, Monitoring, RBDS, Active Messages or spoken rendering;
* run Library Reconciliation or decode station media;
* resolve a duplicate or delete a Track;
* change Hour Refill, native Active Messages or any station configuration.

Do not manually restart services or invoke reconciliation endpoints as part of installation.

## Post-install verification

1. **Job and source:** the Update Center job succeeded; installed release is r0128; station `HEAD` is
   exactly R on clean `main`; Django checks remain clean.
2. **Restart inventory:** Gunicorn has a new PID/timestamp and is healthy. Every other service
   recorded during preflight retains its prior PID/timestamp. Audio and monitoring remain normal.
3. **Access control:** an authorized **Manage tracks** operator can open **Library →
   Reconciliation** and sees verification choices Off, 25, 50 and 100. A user without that
   capability cannot access the page, launch endpoint, findings, preview or apply endpoints.
4. **Default Off:** reload the page and confirm audio verification defaults to Off. Without pressing
   Run Reconciliation, no scan or decoder starts.
5. **Existing report transition:** confirm the previous report remains readable. It must not acquire
   new audio-equivalent deletion authority merely because r0128 was installed.
6. **No automatic execution:** wait through a normal polling interval and confirm no scan,
   verification, duplicate choice or deletion occurs automatically.
7. **Unchanged state:** confirm no Track, media, waveform, category, log, rotation, playout, Hour
   Refill or native Active Messages state changed because of installation.

### Separately authorized browser acceptance

The following is not part of installation. Perform it only with explicit station acceptance
authorization and an appropriate media-I/O window.

1. Deliberately run a new reconciliation scan with a small audio-verification limit. Record the scan
   id, selected limit, progress and completion evidence.
2. Confirm the report keeps exact-byte matches, verified decoded-audio matches and metadata-only
   likely findings visibly distinct. A skipped, timed-out, failed or incomplete verification must
   remain non-deletable.
3. For a disposable, non-airing two-Track test pair approved by the operator, compare both Track
   pages and preview the finding. Confirm the page identifies `decoded_audio` evidence and does not
   claim that metadata, cue markers, readiness or historical plays will be merged.
4. Prefer **No Action** or Keep Both. Apply a deletion only if that exact disposable Track has
   separate approval; then verify the audit record and source-cleanup outcome.
5. Run a fresh reconciliation scan before relying on any remaining findings. Never retry an
   uncertain apply outcome.

KOGR acceptance is not evidence for WRJE. Repeat the complete preflight, authorization, installation
and verification independently at WRJE.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise.

| Failure point | Expected state | Required handling |
|---|---|---|
| Before source advances | Production unchanged | Correct the external cause and request a reviewed retry |
| Source advances, Gunicorn restart fails | r0128 source present; web UI unavailable; audio path unchanged | Preserve job and journal evidence; do not reset Git manually |
| Verification prerequisite is missing | No scan should be started | Stop; correct through a separately reviewed station procedure, not the release job |
| Scan verification times out or reaches a bound | Completed report records non-authoritative leftovers | Do not treat skipped or partial evidence as equality; review the report |
| Old or incomplete report appears deletion-eligible | Safety invariant violated | Do not preview or apply; preserve evidence and prepare a corrective release |
| Pair reports busy, stale, blocked or refused | That pair did not change | Review the reason and make a new preview; never reuse its token |
| Pair reports Unknown after network/5xx | It may or may not have committed | Do not retry; inspect refreshed findings and audit evidence before a new preview |
| Database deletion committed but source cleanup retained/failed | Track row is gone; media may remain safely | Preserve the outcome and use a separately reviewed cleanup procedure |
| Defect found after successful install | r0128 remains installed | Stop using audio verification and duplicate resolution; prepare a forward corrective release |

Because r0128 has no schema change, r0127 code is database-compatible. A source rollback is outside
the normal Update Center workflow and requires separate reviewed recovery authorization. It cannot
undo a completed scan, Keep Both decision or Track deletion and must never be represented as data
rollback.
