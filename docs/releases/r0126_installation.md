# r0126 installation, verification and recovery

This runbook describes a later, separately authorized station installation. Publishing r0126 does
not authorize any step on KOGR or WRJE.

## Release identity and trusted plan

* **Previous release:** r0125 at `cf0a17ec45f08dc26cc104a3a5d0275df5370f05`.
* **R:** the unique canonical commit introducing `deploy/releases/r0126.json`; record its full SHA
  from the published repository and require the Update Center preview to show that exact commit.
* **No companion:** r0126 has no migration and therefore no
  `deploy/migration_authorizations/r0126.json`.
* **Expected one-release plan from r0125:** releases `[r0126]`, no migrations, no package, static,
  nginx, systemd or protected-runtime action, then restart `isadoraair-gunicorn` only.

Do not install from a feature branch, rebuilt equivalent commit or locally modified checkout.

## Read-only preflight

Perform independently for each station immediately before requesting installation authorization.

1. **Canonical identity:** fetch canonical `main`; confirm the target is R, r0125 is its release-chain
   predecessor, the station's installed source is the expected clean trusted release, and the Update
   Center offers the reviewed plan and fingerprint.
2. **Update Center:** no other job is running or awaiting manual intervention. Schema health is good,
   the last backup and recovery checks are acceptable, and protected generation/policy are unchanged.
3. **No migration:** `showmigrations --plan` and the trusted preflight show no unexpected pending or
   edited migration. Do not create a migration companion or run `migrate` for r0126.
4. **GNU timeout prerequisite:** as the station service account, verify:

   ```bash
   test -x /usr/bin/gnutimeout
   /usr/bin/gnutimeout --version
   ```

   Record GNU coreutils 9.7 (already reported for KOGR and WRJE). Stop if the fixed path is absent,
   not executable or reports an unexpected implementation/version. Do not install or substitute a
   package during this release job.
5. **Configured paths:** read and record `REPORTS_ROOT`; confirm its existing
   `library-reconciliation/` directory, lock, progress and report files are owned and writable by the
   station service account. Do not start a scan.
6. **Current reconciliation state:** no CLI or browser scan is running, no launch lock is held, and
   the latest report/status is understood. Preserve its scan id and completion time as the baseline.
7. **Operator window:** no one is doing Library work. Inform Track managers that Gunicorn will be
   briefly unavailable and in-flight web requests may fail during restart.
8. **Service baseline:** record Gunicorn's PID and `ActiveEnterTimestamp`; record the engine,
   encoders, Monitoring, RBDS, Active Messages and spoken-render PIDs/timestamps so unintended
   restarts can be detected. Confirm audio and monitoring are normal.
9. **Feature state:** record Hour Refill policy/arming and native Active Messages airing. r0126 changes
   neither and must not be used to activate them.

Any identity, schema, dependency, path-permission, scanner-state or plan mismatch is a no-go. Stop and
review it; do not modify the manifest, fixed command or station to make the release fit.

## Installation

Use only the normal trusted Update Center workflow after separate authorization for that station.
Confirm the release, exact R, action plan and fingerprint immediately before starting.

The updater advances source to R and restarts `isadoraair-gunicorn`, then verifies it. It does not:

* run a migration or `collectstatic`;
* install a package;
* create, enable or restart a new service or timer;
* restart the engine, encoders, Monitoring, RBDS, Active Messages or spoken rendering;
* run Library Reconciliation;
* change any application feature flag or station configuration.

Do not manually restart services or invoke the reconciliation endpoint as part of installation.

## Post-install verification

1. **Job and source:** the Update Center job succeeded; installed release is r0126; station `HEAD` is
   exactly R on clean `main`; Django checks remain clean.
2. **Restart inventory:** Gunicorn has a new PID/timestamp and is healthy. Every service recorded in
   preflight step 8 retains its prior PID/timestamp. Audio and monitoring remain normal.
3. **Access control:**
   * an authorized Track manager can load **Library → Reconciliation** and sees **Run Reconciliation**;
   * a user without **Manage tracks** cannot access the page, summary, findings or launch endpoint;
   * GET to the launch endpoint is refused, and normal CSRF protection applies to POST.
4. **Existing evidence:** without starting a scan, confirm the previous completed report and findings
   still render and the status matches the preflight baseline.
5. **No automatic execution:** wait through at least one normal page polling interval and confirm no
   new scanner process, attempt, progress or report was created. r0126 adds no schedule.
6. **Operator-initiated acceptance scan:** this is not part of installation. Run it only under a
   separate explicit station acceptance authorization, outside busy hours. If authorized, record the
   initiating account, start and completion evidence, last-report preservation, duration, resource
   observation and the resulting read-only report. Do not act on findings merely to test the UI.
7. **Unchanged state:** confirm no Track row, media, waveform, category, schedule, playout state,
   Hour Refill setting or native Active Messages setting changed because of installation.

KOGR acceptance is not evidence for WRJE. Repeat the complete preflight, authorization, installation
and verification independently at WRJE.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise.

| Failure point | Expected state | Required handling |
|---|---|---|
| Before source advances | Production unchanged | Correct the external cause and request a reviewed retry |
| Source advances, Gunicorn restart fails | r0126 source present; web UI unavailable; audio path unchanged | Job requires manual intervention; collect status/journal evidence; do not reset Git manually |
| Gunicorn healthy, browser launch fails | Station otherwise on r0126 | Do not repeatedly submit; preserve browser/server evidence and report a corrective-release issue |
| Scan reaches two-hour limit | Scanner and ordinary descendants receive group-wide `KILL`; last completed report remains | Review status, lock and journal; do not change timeout or start another scan until exit is established |
| Scan or descendant stuck in uninterruptible I/O | Timeout cannot guarantee kernel-level interruption | Treat as an operating-system/storage incident; do not restart Gunicorn merely to supervise it |
| Defect found after successful install | r0126 remains installed | Stop using Run Reconciliation and prepare a forward corrective release |

Because r0126 has no schema or persistent application-data change, r0125 code is database-compatible.
A source rollback is nevertheless outside the normal Update Center workflow and requires a separate
reviewed recovery authorization. It does not undo a completed reconciliation report, which is a
read-only evidence artifact. Never remove report, lock or progress files merely to make status look
clean.
