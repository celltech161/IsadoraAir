# r0130 installation, verification and recovery

This runbook describes a later, separately authorized station installation. Publishing r0130 does not authorize
any step on KOGR or WRJE, a production reconciliation scan, a relocation, a Category change or any change to
station media.

## Release identity and trusted plan

* **Previous release:** r0129 at `032ba615140c7f20e1b380a606487162ab03fb8f`.
* **R:** the unique canonical commit introducing `deploy/releases/r0130.json`. Record its full SHA from the
  published repository and require the Update Center preview to show that exact commit.
* **No companion:** r0130 has no migration and therefore no `deploy/migration_authorizations/r0130.json`.
* **Expected one-release plan from r0129:** releases `[r0130]`; no migration, package, static, nginx, systemd or
  protected-runtime action; then restart `isadoraair-gunicorn` only.

Do not install from the feature branch, a rebuilt equivalent commit or a locally modified checkout.

## Read-only preflight

Perform independently for each station immediately before requesting installation authorization.

1. **Canonical identity:** fetch canonical `main`; confirm the target is R, r0129 is its release-chain
   predecessor, the station's installed source is the expected clean r0129 release, and the Update Center
   offers the reviewed plan and fingerprint.
2. **Update Center:** no job is running or awaiting manual intervention. Schema health is good, the last backup
   and recovery checks are acceptable, and protected generation/policy are unchanged.
3. **No migration:** `showmigrations --plan` and the trusted preflight show no unexpected pending or edited
   migration. Do not create a companion or run `migrate` for r0130.
4. **Reconciliation state:** no CLI or browser reconciliation is running and no launch lock is held. Record the
   latest report's scan id, completion time, status and counts. Do not run a scan merely to prepare
   installation.
5. **Filesystem prerequisites (read-only).** These checks decide whether relocation may be accepted for
   operational use; they are not an installation blocker.
   * `stat -f -c %T <LIBRARY_ROOT>` names a filesystem with `RENAME_NOREPLACE` support (ext4, xfs, btrfs or
     tmpfs).
   * `ldd --version` reports glibc 2.28 or later, and
     `python3 -c "import ctypes; ctypes.CDLL(None).renameat2"` exits 0.
   * `stat -c '%d %n' <LIBRARY_ROOT> <LIBRARY_ROOT>/*/` shows one device number for the root and every
     Category folder. A folder on another device makes reconciliation and bulk moves into or out of it refuse
     with a cross-filesystem message.
   * Record whether `<LIBRARY_ROOT>/.relocation-quarantine` exists. It should not yet; it is created on first
     use with mode 0700.

   Do not create, rename or write any file to test these.
6. **Backups:** confirm a current, verified database backup and media backup or snapshot exist. These are the
   restoration path for any relocation or Category change: r0130 cannot undo a committed one.
7. **Operator window:** no one is editing the Library or using reconciliation. Inform Track managers that
   Gunicorn will be briefly unavailable and in-flight web requests may fail during restart.
8. **Service baseline:** record Gunicorn's PID and `ActiveEnterTimestamp`. Record the engine, encoders,
   Monitoring, RBDS, Active Messages and spoken-render PIDs and timestamps, so unintended restarts can be
   detected. Confirm audio and monitoring are normal.
9. **Feature state:** record Hour Refill policy and arming, and native Active Messages airing. r0130 changes
   neither and must not be used to activate them.

Any identity, schema, scanner-state, permission, service-health or action-plan mismatch is a no-go. Stop and
review it rather than altering the manifest or station to make the release fit.

## Installation

Use only the normal trusted Update Center workflow after separate authorization for that station. Confirm the
release, exact R, action plan and fingerprint immediately before starting.

The updater advances source to R and restarts `isadoraair-gunicorn`, then verifies it. It does not:

* run a migration or `collectstatic`;
* install a package;
* create, enable or restart a service or timer;
* change protected-runtime generation or policy;
* restart the engine, encoders, Monitoring, RBDS, Active Messages or spoken rendering;
* run Library Reconciliation;
* move a file, change a Category or create the relocation quarantine;
* change Hour Refill, native Active Messages or any station configuration.

Do not manually restart services or invoke reconciliation or bulk-edit endpoints as part of installation.

## Post-install verification

1. **Job and source:** the Update Center job succeeded; the installed release is r0130; station `HEAD` is
   exactly R on clean `main`; Django checks remain clean.
2. **Restart inventory:** Gunicorn has a new PID/timestamp and is healthy. Every other service recorded during
   preflight keeps its prior PID/timestamp. Audio, encoders and monitoring remain normal.
3. **Access control:** an authorized **Manage tracks** operator sees the category-drift review controls on
   **Library → Reconciliation** and the file-location choice in the bulk editor. A user without that capability
   cannot reach the page, the findings or the drift and bulk-edit endpoints.
4. **Existing report transition:** the prior report remains readable. Its drift findings are review-only
   ("run reconciliation again"), because they lack the evidence r0130 records. Installation must not retrofit
   or infer that evidence.
5. **No automatic execution:** wait through a normal polling interval and confirm no scan, relocation,
   Category change or quarantine creation occurs automatically.
6. **Unchanged state:** confirm no Track, media, waveform, Category, log, rotation, playout, Hour Refill or
   native Active Messages state changed because of installation.

## WRJE operational acceptance (separately authorized)

WRJE is the intended first field-acceptance station. Its last reported r0129 reconciliation showed 721
category-drift findings, 14 unregistered files and 66 uncertain findings. Those numbers are context, not
permission to change station media.

Each step below needs explicit station acceptance and media-change authorization. Stop at any unexpected result
(see "Stop procedure").

1. **Installed state.** Verify the installed r0130 identity and service health, as in Post-install
   verification.
2. **Playout.** Confirm normal playout and encoder operation, and that the engine state is fresh. Without a
   fresh engine state, every repair is refused.
3. **Fresh scan.** Run a fresh, read-only reconciliation scan. Record its scan id, completion time and counts.
4. **Choose a Track.** Inspect the category-drift findings, filtered and grouped by folder → Category. Choose a
   safe Track for which the operator has reviewed the Category and the folder and decided which is correct. It
   must not be on air, queued, or in the current or next hour; prefer one with no upcoming approved logs. A
   drift finding alone does not prove whether the database Category or the folder is wrong.
5. **Option A on that one Track.** Preview **A — Move file**, read every line, confirm, and apply.
6. **Verify Option A.**
   * The Track's filepath in Track Detail is the new folder and file.
   * The file exists at the new path and is playable.
   * The old path no longer exists.
   * The `relocation.track` audit event (Monitoring → Recent Events) shows `moved`, the old and new paths, and
     cleanup `removed`.
   * The quarantine is empty.
   * The waveform shows as unverified until Reanalyze Track, as documented.
7. **Rescan.** Run another read-only scan and confirm that Track's finding is gone.
8. **Option B on a different safe Track.** Preview **B — Change Category**. Check the old and new Category and
   any content-kind warning. Leave *also keep the old primary* unticked unless the operator wants it. Apply.
9. **Verify Option B.**
   * The primary Category changed.
   * The additional Categories are exactly as previewed.
   * The file's path, inode and timestamps are unchanged.
   * The audit shows `updated` with `file_moved: false`.
10. **Small batch.** Review and apply about five Tracks: one page, one explicitly chosen direction, each Track
    reviewed.
11. **Inspect the batch.** Check the partial-success results, every outcome and cleanup status, the waveform
    status of the moved Tracks, and the quarantine.
12. **Larger batches.** Only after the above is accepted, proceed with larger operator-reviewed batches of up to
    25. Rescan between batches.

Do not resolve all 721 findings automatically or without review. Do not reanalyze every moved Track; reanalyze
only those whose cue points must be edited.

The bulk editor's move option is accepted the same way: one Track first, then a small selection, with the
explicit confirmation and per-Track results checked.

KOGR acceptance is not evidence for WRJE, and vice versa. Repeat the preflight, authorization, installation and
verification independently at each station.

## Stop procedure

Stop all relocation work, record the evidence (the page's result panel, the Recent Events entries, Track
Detail), and do not retry, when any of these occurs:

* **`uncertain`**: the database did not confirm the commit. Inspect the Track in Track Detail and both file
  entries before anything else.
* **`committed_cleanup_required`**: the move committed and the Track is valid. The old entry or a retained
  object needs attention:
  * read the cleanup message and location;
  * inspect `RETAINED.json` if one was written;
  * resolve it by hand only after confirming which Track, if any, owns the file.
* **An unexpected file identity change:** a refusal saying a file was modified, replaced or no longer exists,
  where nothing should have touched it.
* **Unexpected silence:** no answer, or a 5xx, from the server. The page stops and marks the item unknown;
  inspect current state before a new preview.
* **Any playout or encoder anomaly** during the acceptance.

Do not delete or empty `.relocation-quarantine/` without resolving each retained object and its ownership.

## Failure and recovery boundaries

The Update Center makes no automatic rollback promise.

| Failure point | Expected state | Required handling |
|---|---|---|
| Before source advances | Production unchanged | Correct the external cause and request a reviewed retry |
| Source advances, Gunicorn restart fails | r0130 source present; web UI unavailable; audio path unchanged | Preserve job and journal evidence; do not reset Git manually |
| Repair or move is busy, stale, refused or a collision | Nothing changed for that Track | Review the reason; make a fresh preview |
| `committed_cleanup_required` | Track committed at its new path; an old entry or retained object remains | Stop; resolve by hand as above; never retry automatically |
| `uncertain`, or an unknown answer after a network error or 5xx | It may or may not have committed; both entries were kept | Stop; inspect the Track and both entries; never retry automatically |
| The operator chose the wrong direction | The committed move or Category change is not automatically reversible | Stop; correct it with a separately reviewed repair, or restore from backup |
| Defect found after a successful install | r0130 remains installed | Stop relocation work and prepare a forward corrective release |

Because r0130 has no schema change, r0129 code is database-compatible. A source rollback is outside the normal
Update Center workflow and requires separate reviewed recovery authorization. **Reverting application code cannot
undo already committed file moves or Category changes.** A source rollback and a media or metadata restoration
are different operations. Restoring moved files or changed Categories uses the backups confirmed in the preflight,
or new, separately reviewed repairs, and must never be represented as part of a source rollback. A source
rollback also leaves any `.relocation-quarantine/` contents in place; resolve them first.
