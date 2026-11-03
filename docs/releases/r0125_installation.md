# r0125 installation, verification and recovery

Publication does not authorize installation. KOGR is installed first. WRJE is installed only after
KOGR's results have been reviewed, under its own approval, preview and evidence. Do not assume
that either station is on r0124, or that the two stations are on the same release.

## Identity

* **R:** the unique commit introducing `deploy/releases/r0125.json`. It is the installable release.
* **No companion:** r0125 has no migration, so there is no `deploy/migration_authorizations/r0125.json`
  and no companion commit C.
* **After publication:** verify that the trusted remote `main` tip is exactly R and that the manifest
  resolves to that one introducing commit. Record R's full SHA in the station record.

## Expected trusted plan

From a clean station at the r0124 release commit `69d53bc4f40e96a47359dce817375aa415cd068f`:

| Field | Expected |
|---|---|
| releases | `[r0125]` |
| migrations / manual operations / companion | none / none / none |
| collectstatic, Python requirements, APT packages | no / no / none |
| systemd units changed, new or removed; nginx | none; no |
| protected runtime | no transition (stays at the current generation) |
| restarts, in this order | `isadoraair-gunicorn`, then `isadoraair-active-message-worker` |
| blockers | none |

* **From r0123** (`1a7c5b113a31…`): the plan is `[r0124, r0125]` with the same empty action set and
  the same two restarts.
* **From r0122:** the plan is `[r0123, r0124, r0125]`. It includes r0123's migration
  `library.0099_track_cue_marker_authority`, whose two manual `AddField` operations are covered by
  the r0123 companion. Follow r0123's own verification (`docs/releases/r0123_installation.md`,
  `r0123_verify.sql`) as part of that plan.
* **Any older baseline:** review its actual multi-release plan. A station before r0116 also crosses
  r0116's protected-runtime handoff and engine restarts. Never substitute this one-release plan.
* **Stop if the preview differs in any field.**

## Process transition (why these two restarts, and no others)

r0125 changes `library/models.py` (path-registration locks in Track writes) and `library/apps.py`
(Track delete receivers). These modules are loaded by every Django process. The changed write paths
are actually used only by:

* **Gunicorn:** every Library page, API, deletion, bulk edit, relink and review.
* **The active-message worker:** it imports `sync_track_file` (registration under the path lock),
  `waveform_cache` (the deletion marker) and the Track delete receivers in-process when it
  publishes or retires an Active Messages Track. Publication runs only while native airing is open;
  it is closed on both stations until Phase G (verify).

The engine, encoders, Monitoring, RBDS, the spoken-render worker and the render and validation
services never create, relocate or delete a Track and never write a waveform cache. Their old
in-memory code is safe indefinitely and they are not restarted. **No audio interruption is
expected.** Oneshot timers and management commands load r0125 on their next ordinary start.

**Transition window.** Between source advancement and the restarts, the old Gunicorn and worker run
alongside new short-lived processes. Any such combination behaves like r0124 or like r0125 for a
given operation; none weakens either. The window is a few seconds and is not used for Library
work. Do not run deletions, imports, recalls or `reconcile_library` during the installation.

## Before installation (each station, read-only)

1. **Identity:** the installed release, exact HEAD and branch of `/opt/isadoraair`, and
   `git status --porcelain` empty.
2. **Migrations:** Update Center reports current schema health and no unapplied migration.
3. **Protected updater:**
   * healthy and armed;
   * the active generation and runtime version recorded;
   * protected generation 15 / runtime 13 (delivered by r0116; no later release changed it) or
     newer, whose signed policy names the active-message worker, so its restart is authorized;
   * no update job running or awaiting manual intervention.
4. **Trusted plan:** the Update Center preview exactly matches the table above (or the reviewed
   multi-release plan). Record its fingerprint.
5. **Backup and recovery:**
   * the last nightly backup and its round-trip verification succeeded;
   * Recovery Assurance is `ok` for the active protected generation.

   The music library is not in any backup. See "Recovery boundaries".
6. **Running services:** record `systemctl show -p ActiveEnterTimestamp,MainPID` for Gunicorn, the
   active-message worker, the engine, encoders, Monitoring, RBDS and the spoken-render worker.
7. **Activation state (record only; r0125 changes none of it):**
   * Hour Refill policy (master, five causes, Observation only) and `HOUR_REFILL_INSTALL_ARMED`;
   * Active Messages `native_airing_enabled` (expected False);
   * any Remote DJ session in progress.
8. **Active Messages:** no execution is READY, being published or engine-held. If one is, wait.
9. **Library baseline:**
   * Track count;
   * number of files in `WAVEFORMS_DIR`;
   * `WAVEFORMS_DIR/.locks/*.deleted` count (expected 0 before r0125);
   * the last 24 h of `journalctl -u isadoraair-gunicorn | grep track_cleanup` (expected none on
     r0124).
10. **Maintenance window:**
    * a quiet period with no Remote DJ show in progress and no operator Library work;
    * the Gunicorn restart makes the web UI unavailable for a few seconds and fails in-flight
      requests.

## Installation

Use only the normal trusted Update Center workflow. Confirm the displayed release, plan and
fingerprint. The updater checks out R, then restarts Gunicorn and then the active-message worker,
each with its own start/completion milestone and health verification.

Do not:
* run migrations;
* create a migration companion;
* edit the manifest;
* restart the engine or any other service;
* run `reconcile_library`;
* bypass a planner or executor refusal.

## After installation

1. **Job and source:**
   * the job succeeded;
   * the installed release is r0125;
   * HEAD is exactly R, on `main`, clean;
   * `manage.py check` reports no new issue.
2. **Restarts:** Gunicorn and the active-message worker have new `ActiveEnterTimestamp`s and are
   healthy; the worker passed its start gate. The engine, encoders, Monitoring, RBDS and the
   spoken-render worker keep their pre-install timestamps and PIDs. The protected runtime
   generation is unchanged.
3. **Playout:**
   * audio continued throughout;
   * the next hour transition, legal ID and log advance normally;
   * Monitoring shows no new engine, encoder or stream alarm.
4. **Library and permissions:** reload any Library page that was open before the restart, because
   a page loaded under r0124 still offers the retired direct bulk actions, which now answer 410. Then:
   * Library, Track Detail and **Library → Reconciliation** load for a manager;
   * a user without **Manage tracks** gets no bulk-edit controls and no Reconciliation access;
   * Remote DJ and contributor accounts are unchanged.
5. **Bulk edit (preview required, apply optional):**
   * **Required:** on an authorized, non-critical, non-airing test Track only, preview a harmless
     change and confirm the preview writes nothing.
   * A preview records no audit event. Do not expect a **Library bulk edit** event from a preview.
   * **Optional apply:** only with the station operator's explicit approval, apply that change to the
     same test Track. Use a reversible field (for example energy).
   * After an apply that succeeded and committed, and only then, expect one
     **Library bulk edit completed** event in **Monitoring → Recent Events**, with the operator's
     name. If no apply was approved, no such event is expected and its absence is not a failure.
6. **Reconciliation:** only if separately approved for this window, run `reconcile_library` once,
   as the service account, outside busy hours.
   * Review the page. Confirm duplicate groups (including unregistered byte-identical copies) are
     visible, and that no delete or merge control exists.
   * Do not relink a production Track merely to test the button.
7. **Workers and analyzer:**
   * the active-message worker heartbeat is current;
   * the next `isadoraair-analyze.timer` run completes normally;
   * the dedication and web-request timers run without new errors.
8. **Media protection:** compare against the pre-installation baseline (step 9 above).
   * **Expected changes.** Track count, waveform files and source files may change through normal
     station jobs during the window. These include scheduled syndicated ingestion, weather
     publication and the analyzer timer.
   * **Attribute every difference.** Each observed difference must be attributed to specific
     evidence:
     * an operator action recorded in the window; or
     * the scheduled job's own evidence: its unit's journal entry or run time, and the changed file
       or waveform matching that job's source path and timestamp.

     The mere existence of a background job does not explain a difference.
   * **Stop conditions.** Any of these is a stop condition and must be reported before further
     Library work:
     * an unattributed Track, source-file or waveform change;
     * any `.locks/*.deleted` marker or `track_cleanup` line without a corresponding deletion;
     * an Active Messages or spoken file change not attributed to that domain's own recorded
       activity.

   **Do not delete a real Track to demonstrate cleanup.**

**Go / no-go for continuing to operate on r0125:** steps 1–4 and 7–8 must pass. Any unexpected
restart, alarm, missing permission or unexplained media change is a stop. Report it before
further Library work.

Record the station, the prior release, R, the preview and fingerprint, the job result, the restart
evidence and these checks.

## WRJE (separately, after KOGR's review)

Repeat every step above with WRJE's own evidence: its installed baseline (it may differ from KOGR's),
its own trusted plan, its protected runtime and policy generation, its media storage and service
account permissions on `LIBRARY_ROOT`, `WAVEFORMS_DIR/.locks` and `REPORTS_ROOT`, its backup and
recovery state, and its own installation approval. KOGR's result is not evidence for WRJE.

## Recovery boundaries

Update Center makes no automatic-rollback promise (`docs/UPDATE_CENTER.md`, "Failure, retry, and
rollback").

| When it fails | State | Supported handling |
|---|---|---|
| Fetch, validation, staging, checkpoint (before the source advances) | production unchanged | fix the cause, retry |
| After the source advances, Gunicorn restart fails | source at R, web UI down, audio unaffected | the job is `manual_intervention_required`; collect `systemctl status`/journal and escalate. Do not reset Git by hand. |
| Gunicorn restarted, worker restart fails | Library on r0125; Active Messages paused; audio unaffected | as above; the completed Gunicorn milestone is not repeated |
| An interrupted restart | ambiguous milestone | manual by design; never retried blindly |
| A defect found after a successful install | r0125 running | stop using the affected feature; a forward corrective release is the supported path |

* **Source rollback to r0124:** r0125 has no schema change, so r0124 code runs against the same
  database. Rows written by r0125 remain valid for r0124:
  * bulk edits;
  * relinked paths;
  * "not a duplicate" decisions;
  * SystemEvents;
  * expired preview markers.

  `.locks/<id>.deleted` files are ignored by r0124. A source rollback is nevertheless outside
  Update Center. It is a separately reviewed manual recovery decision, never a routine step.
* **What no rollback can undo:**
  * **Deleted audio.** Audio removed after a Track deletion (on r0124 or r0125) is gone. The music
    library is never part of any IsadoraAir backup. A database restore would bring back Track rows
    whose files no longer exist; reconciliation would then report them as missing.
  * **Waveforms.** These can be regenerated by the analyzer.
  * **Bulk edits and relinks.** These are ordinary data changes; reverse them with the same tools,
    not with a restore.
* **Database restore** is the disaster-recovery procedure, not a release rollback. It discards
  everything after the backup and needs its own authorization.
