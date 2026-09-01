# Immutable supervisor maintenance procedure

## Why this document exists

The immutable bootstrap supervisor (`/usr/local/libexec/isadoraair-updater-bootstrap/`,
driven by `updater-bootstrapd.service`) is deliberately **outside** the
protected-runtime bundle the ordinary Update Center chain replaces. That is
not an oversight -- see `docs/UPDATE_CENTER_PHASE_D.md`'s own stated boundary:
a change to the immutable supervisor's own code still requires a privileged,
out-of-band root operation, exactly like the original final-manual-bootstrap
install. **No automated supervisor self-update mechanism exists, and this
document does not introduce one.** What follows is the smallest safe,
repeatable, root-executed procedure for patching the supervisor's own
installed source in place -- first exercised for real to ship the post-r0027
active-worker-promotion-tracking fix (commit-pending on
`fix/supervisor-post-commit-active-worker-tracking`).

## Scope of a supervisor-source change

Only `/usr/local/libexec/isadoraair-updater-bootstrap/` (the supervisor's own
Python package + `updater_bootstrapd.py`) is ever touched by this procedure.
It never touches:

- `/var/lib/isadoraair-updater-bootstrap/runtime-slots/**` (the protected
  worker generations -- untouched, keeps running the whole time);
- `/var/lib/isadoraair-updater-bootstrap/runtime-state.json` (durable
  active/previous generation record);
- `/etc/isadoraair/**` (trust policy, station config);
- anything under `/home/jreed/isadoraair-django` (the ordinary Update
  Center chain).

This is why the currently-active worker (today: slot B/gen2) is never
disturbed by staging or verifying the new supervisor files -- only the
final `systemctl restart` step below affects it, and only because
`SupervisorDaemon.start()` unconditionally launches the active worker
fresh (the same brief, already-proven-safe interruption D3-P's Django-
continuity handling covers -- socket briefly unavailable, same job/session
continuity rules apply -- not a new risk this procedure introduces).

## Procedure

All steps after staging require root.

1. **Stage.** Copy the reviewed, tested source tree (from the exact
   commit that passed the full test suite) into a new, root-owned,
   non-live staging directory, e.g.
   `/usr/local/libexec/isadoraair-updater-bootstrap.staged-<UTC timestamp>/`.
   Set ownership/modes to match the existing installed convention exactly
   (`root:root`, directories `0755`, `.py` files `0644`,
   `updater_bootstrapd.py` `0755`) -- the same convention the release-
   authoring tooling (`protected_runtime_release.py`) already enforces for
   the ordinary protected-runtime bundle.

2. **Verify before touching anything live.**
   - `python3 -m py_compile` every staged `.py` file.
   - Diff every staged file against `git show <tested commit>:<path>`
     (or a SHA-256 comparison against that same tree) -- prove the staged
     bytes are byte-identical to what the test suite actually ran, not
     merely "close."
   - Run the full test suite (`updatecenter.tests.test_phase_d2_worker_lifecycle`,
     `test_phase_d4_supervisor_daemon`, plus the wider `updatecenter.tests`
     suite) against that exact commit if not already done in this pass.

3. **Back up the currently-installed tree** (this IS the rollback
   artifact -- see below) to a timestamped, root-owned sibling, e.g.
   `/usr/local/libexec/isadoraair-updater-bootstrap.rollback-<UTC timestamp>/`,
   and record its SHA-256 per file (same pattern as the pre-r0026
   checkpoint's own `CHECKSUMS.sha256`).

4. **Swap.** With `activation` confirmed `null` (no handoff in flight --
   `GET_RUNTIME_STATE`/the real supervisor PING) and at a low-traffic
   moment: atomically replace the live directory with the staged one
   (`mv` within the same filesystem is atomic for a directory rename on
   Linux). Do not merge files in place; replace the whole tree in one
   move.

5. **Restart.** `sudo systemctl daemon-reload` (only needed if
   `updater-bootstrapd.service` itself changed; not needed for a pure
   Python-source change) then `sudo systemctl restart updater-bootstrapd.service`.

6. **Verify health.**
   - `systemctl is-active updater-bootstrapd.service` and
     `journalctl -u updater-bootstrapd.service` show a clean start, no
     immediate crash-loop.
   - Real `UpdaterClient().ping()` from Django reports `ok=True`,
     `execution_armed=True`, matching the pre-restart baseline.
   - `runtime-state.json` still shows the SAME active/previous
     slot/generation as before the restart (this procedure must never
     change which generation is active -- only which supervisor code is
     running it).
   - Journal search for `bounded restart`, `owns the job store`,
     `exhausted` over several minutes: no matches (this is the exact
     post-restart check that already confirmed the mitigation worked on
     `isadoraair`).

## Rollback

If step 6 fails (service won't start, crash-loops, or PING reports
anything other than the pre-restart-healthy baseline): `mv` the
`.rollback-<timestamp>` directory back into the live path (reversing step
4) and `systemctl restart updater-bootstrapd.service` again. This is the
same atomic-directory-swap operation in reverse, using the exact
known-good tree captured in step 3 -- never a partial file-by-file
revert.

## Integrity-checking the installed artifact against tested source

At any time, not just during a maintenance window: `diff -r` (or a
per-file SHA-256 comparison) between the live
`/usr/local/libexec/isadoraair-updater-bootstrap/` tree and
`git show <known-tested commit>:deploy/updater_bootstrap/` for the same
paths must report zero differences. This is exactly how this task's own
investigation first confirmed the pre-fix installed copy matched its
repo source exactly (`diff` returned empty) before any change was made,
and is the same check to run immediately after step 4 above, before ever
proceeding to step 5's restart.

## WRJE

If WRJE's own Phase-D final manual bootstrap has not yet happened, it
should simply install the supervisor from a commit that already includes
this fix -- no separate patch/maintenance step is needed there at all.
If WRJE's bootstrap has already happened with the pre-fix supervisor, the
same numbered procedure above applies to it independently; this document
is written station-agnostic for exactly that reason.
