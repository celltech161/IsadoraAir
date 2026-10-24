# r0117 installation and acceptance (operator handoff; not executed)

**Status: preparation only.** Nothing here has been run on a station. Installing r0117 at KOGR or WRJE
requires all of the following:
- the operator's D3 acceptance (the `playout.start_next` grants) and the Remote DJ gate carry-forward
  decision, both recorded;
- an independent review of the candidate;
- publication of R and C in **one** ref update (`git push github-write <C>:refs/heads/main`), under
  separate authorization;
- the preflight below passing on **that** station.

Release notes: `docs/releases/r0117.md`.

## 1. Supported starting points

| Installed release | Live HEAD (must be exactly this commit) | Offline qualification |
|---|---|---|
| r0116 | `a873eb91f8c03ecd095a4195b179509111e6a2c0` | the installed generation-15/runtime-13 code plans r0117 alone: no blockers, no protected-runtime transition |
| r0115 | `fd3ac805e774f6039636f3f8178f5770ea038572` | r0116 and r0117 in one job. This includes r0116's generation-15 runtime handoff and worker unit; review that station's plan. |

Do not presume either station's release, and never repair a station's Git state as part of this
install. Both stations reported r0116 on 2026-10-21; establish that on the station itself.

## 2. Station preflight (read-only; all must pass)

1. **Identity.** `/opt/isadoraair` HEAD is exactly `a873eb9…`, the r0116 release commit, on `main`.
   The active protected runtime is generation 15 (Update Center → status).
2. **Clean checkout.** `git -C /opt/isadoraair status --porcelain` is empty, and none of the paths
   r0117 adds exist untracked. Get the list with
   `git diff --name-only --diff-filter=A a873eb9 <r0117 C>`; for this candidate it is:
   - `authz/migrations/0009_playout_start_next_capability.py`
   - `library/migrations/0091_playouttransportconfig.py`
   - `library/migrations/0092_remote_dj_start_next_access.py`
   - `library/services/deck_fade.py`
   - `library/services/start_next_identity.py`
   - `library/templates/library/_console_icons.html`
   - `docs/releases/r0117.md`, `docs/releases/r0117_installation.md`, `docs/releases/r0117_verify.sql`
   - `deploy/releases/r0117.json`, `deploy/migration_authorizations/r0117.json`
   - the new test modules under `library/tests/` and `updatecenter/tests/`.
3. **No pending migrations other than r0117's:** run `manage.py showmigrations --plan` as the
   application user; only `authz.0009`, `library.0091` and `library.0092` are unapplied.
4. **Seed preconditions:**
   - the Roles "Station Administrator" and "Remote Host" exist;
   - the `remote_dj` group has a GroupAccess row.

   If either is missing, the data migrations skip silently. Report it, because Start Next would then
   not be granted.
5. **Backup and recovery assurance are green for THIS station, and its runtime recovery payload matches
   generation 15.**
   - Take a fresh nightly-equivalent backup and confirm its verify/round-trip before the install
     window.
   - Deployment sequence:
     1. qualify the application release;
     2. separately authorize canonical publication;
     3. install and accept on **KOGR** (payload repaired 2026-10-22) when scheduled;
     4. handle **WRJE** independently, under its own later installation authorization and recovery
        preflight. Its generation-15 payload refresh was still outstanding when this candidate was
        prepared. That is a WRJE installation gate, not a release-candidate blocker. WRJE is not
        certified ready by this document.
6. **Air-time window.** The install restarts the engine. Schedule it when a few seconds of
   interruption with auto-resume are acceptable, with no Remote DJ connected (the restart ends the
   session) and no VT sequence about to air.

## 3. Expected Update Center plan and milestones

- **Plan:**
  - releases `[r0117]`;
  - migrations `authz.0009`, `library.0091`, `library.0092`;
  - manual operations `authz.0009 #0 RunPython` and `library.0092 #0 RunPython`, both covered by the
    r0117 companion, so central approval applies;
  - no collectstatic, unit, package or nginx action;
  - restarts Gunicorn, then the engine;
  - protected-runtime transition: none.
- **Milestones:**
  - migration authorization (central, companion);
  - checkpoint;
  - the three migrations;
  - source fast-forward to the r0117 release commit;
  - restarts;
  - postflight.

Stop if anything deviates: an extra migration, a manual operation not covered, or a runtime
transition.

## 4. Post-install acceptance

**Health**
1. Update Center job **succeeded**; the station's HEAD is the r0117 release commit; `manage.py check`
   is clean; `showmigrations` shows the three migrations applied.
2. Run `psql -f docs/releases/r0117_verify.sql` (read-only, rolled back). It shows:
   - `playout.start_next` is granted to exactly Station Administrator and Remote Host;
   - `/api/engine/start-next/` is in `remote_dj`'s exact paths;
   - the `PlayoutTransportConfig` table exists.
3. `systemctl is-active isadoraair-engine isadoraair-gunicorn` are both active, and the engine journal
   shows a clean start and auto-resume of the interrupted occurrence.

**Deck transport and queue**

4. Full Console: the Start Next pill sits between the decks and names the route (e.g. "Deck A → Deck
   B"). Eject and Pause/Resume behave as before.
5. Press Start Next with stale or incomplete status (for example, during an engine restart): the
   control is unavailable and nothing is sent.
   With a normal song playing, press Start Next once:
   - the status goes Queued → Fading → "Next item started";
   - the next queued item starts (cue-in at the end of the fade);
   - exactly one queue row advances.
6. Admin → Config → Playout Transport Config shows fade 2000 ms and "switches Manual to Auto" off.

**Manual/Auto and microphones**

7. In Manual, press Start Next: Manual stays on, the next item plays, and switching back to Auto does
   **not** start another item.
8. Studio Mic on (Manual held by the mic), then Start Next: the mic and Manual are unchanged; the mic
   is not ducked or faded differently.

**Remote DJ**

9. A Remote Host on `/remote-dj/`:
   - sees Start Next (mobile: between the live deck and the compact preview);
   - sees **no** Eject/Pause and no Studio Mic;
   - a Start Next press works within their scheduled window.
10. Remote DJ connect and owner state behave as in r0116/Phase 1B; the Remote Mic gate behaves as in
    r0116 (see the risk decision).

**Playback continuity and actual-air evidence**

11. Logs/PlayEvents:
    - the faded item has one PlayEvent whose closing segment's termination reason is `start_next`;
    - the started item has a new PlayEvent;
    - no item between them was claimed.
12. VT (if VTs are configured): a normal VT transition airs outro → gap → intro → next song exactly
    once, and the duck releases.

## 5. Failure and manual intervention

- **Before the source fast-forward:** a job that stops before `migration_started` changed nothing. A
  migration failure is rolled back by the protected executor's database checkpoint rules. Do not
  retry a terminal job; start a fresh one after diagnosing.
- **After installation:** if Start Next misbehaves, disable its use operationally and keep Eject:
  - remove `playout.start_next` from the Remote Host role in Admin;
  - Station Administrators can simply not press it.

  Nothing else depends on it. There is no supported downgrade of application source through Update
  Center. Recovery follows the protected updater's documented checkpoint and backup paths only; no
  ad-hoc rollback.
- **After any protected-runtime generation change (not this release):** refresh the runtime recovery
  payload.
