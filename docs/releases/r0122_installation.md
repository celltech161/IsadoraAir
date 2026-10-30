# r0122 installation and verification

Each station needs its own operator authorization and its own Update Center action.

## No maintenance window is needed

r0122 declares no restart. The Update Center only moves the station's checkout to the
r0122 release commit. Running services keep their loaded code until their next normal
restart, and r0122 changes nothing they use.

## Preconditions (each station, before installing)

1. **Confirm the installed release.** The live checkout must be exactly the r0121 release
   commit `5eeefa6707b607692ac70a9752a536022ab26378` on `main`, with a clean worktree and
   the generation-15 protected runtime. A station on anything else must stop: its plan
   spans other releases and needs its own review.
2. **Confirm the trusted target.** It must be the reviewed, published r0122 tip, and the
   r0122 manifest must resolve to its single release-introducing commit.
3. **Review the Update Center preflight.** The plan from r0121 must show:
   - releases `[r0122]`;
   - no migrations, no manual operations and no companion dependency;
   - no collectstatic, package, unit, daemon-reload, nginx or protected-runtime action;
   - no service restarts.

   Stop if anything else appears.
4. **Keep the normal safeguards.** Preserve the normal checkpoint and the protected
   updater's rollback and recovery rules. Make no ad-hoc repair of the station's Git state.

## Install

Use the normal Update Center workflow against the reviewed trusted tip.

## After installation

1. **The Update Center job succeeded.** The station's HEAD is the r0122 release commit,
   on `main`, with a clean worktree.
2. **`manage.py check` is clean,** apart from the known pre-existing `security.W004` HSTS
   deployment warning.
3. **Services are unchanged.** `systemctl is-active isadoraair-gunicorn isadoraair-engine
   isadoraair-monitoring` reports all active, with the same main PIDs as before the
   install. Playout, the web UI and monitoring behave as before.
4. **The protected updater is still generation 15.** The Update Center shows no
   protected-runtime transition, and the nightly backup and its verification need no
   recovery-payload refresh.
5. **Do not run tests on the station.** The test suite now refuses to run from the
   production checkout and outside the kernel sandbox; qualification belongs on a
   development worktree.

Record each station separately: the installed release, exact trusted tip, job result and
verification output.
