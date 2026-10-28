# r0120 installation and verification

**This is guidance for a later, separately authorized installation.**
Publishing r0120 authorizes no KOGR or WRJE installation. Each station needs
its own explicit operator authorization and its own Update Center action. Do
KOGR first.

This guide is written without reading any station state. Every station value
named here must be confirmed on that station at the time of installation.

## Identities

| What | Value |
| --- | --- |
| Installed predecessor (expected) | r0119 release commit `9e7adc035e8eecb9ba474077afca1e3d3cd065cb` |
| r0120 release target **R** | the unique commit that introduced `deploy/releases/r0120.json`, recorded in the publication handoff |
| Trusted Git tip **C** | the metadata-only companion commit directly after R, which adds `deploy/migration_authorizations/r0120.json` |

After a successful install, the station checkout is at **R**, not C. C changes
only release metadata, and the Update Center installs release commits.

## A maintenance window is required

The install restarts the audio engine:

- playout is briefly interrupted, then resumes automatically;
- any Remote DJ session ends.

Choose a quiet window: not a live show, not a voice-track sequence about to
air, and not other critical programming.

## Preconditions (each station, before installing)

1. **Installed release.** The live checkout is exactly the r0119 release commit
   above, on `main`, with a clean worktree and the generation-15 protected
   runtime. A station on an older release plans several releases together.
   Review that plan on its own terms; do not assume it matches an r0119
   station's plan.
2. **Backup and recovery.** A fresh nightly backup succeeded and the recovery
   payload verifies. r0120 does not change the protected-runtime generation,
   so no payload refresh is expected because of r0120.
3. **Health.** The Update Center, Gunicorn, engine, encoders and monitoring are
   healthy, and no update job has failed or is pending.
4. **No Remote DJ** is connected. Stop rather than end a live session.
5. **Review the Update Center plan from r0119.** It must show:
   - releases `[r0120]`, with target release r0120 at commit **R**;
   - migrations, in order, `library.0093_hour_refill_cause_policy`,
     `library.0094_logitem_origin`, `library.0095_hour_refill_install`,
     `library.0096_hour_refill_install_gate`;
   - exactly two manual operations, both in `library.0096`: index 0
     `RemoveConstraint` and index 1 `AlterField`;
   - the trusted r0120 companion covers both;
   - no collectstatic, package, unit, daemon-reload, nginx or protected-runtime
     action;
   - restarts exactly `isadoraair-gunicorn`, then `isadoraair-engine`.

   Stop if anything else appears.

## Install

Use the normal Update Center workflow against the reviewed trusted tip.

The Update Center applies `0093`–`0096` **automatically**, under its existing:

- protected database checkpoint;
- provenance and plan-identity checks;
- pre-applied, conflict and destructive-manifest gates.

It finds the trusted r0120 companion and logs that it covers every one of the
plan's 2 manual operations. **No per-station migration approval step is
expected.**

**If the job stops with `MIGRATION_OPERATION_MANUAL` or any other migration
gate, stop and investigate.** Do not approve locally, retry, or run
`manage.py migrate` by hand as a workaround. An unexpected stop means the
station's plan differs from the reviewed one; for example, different `0096`
bytes or an extra manual operation.

The Update Center then restarts, in order:

1. `isadoraair-gunicorn`
2. `isadoraair-engine`

## After installation

1. **Health.**
   - The Update Center job succeeded. Its log records that the trusted
     companion authorization for `['r0120']` covered every one of the plan's 2
     manual operations.
   - The station HEAD is **R**, not C.
   - `manage.py check` is clean, apart from the known pre-existing
     `security.W004` HSTS warning.
   - `systemctl is-active isadoraair-gunicorn isadoraair-engine` reports both
     active.
   - The engine journal shows a clean start and auto-resume of playout.
2. **Database (read-only).** Run `docs/releases/r0120_verify.sql` as the
   application role. It runs inside one `READ ONLY` transaction that is rolled
   back. Expect:
   - the four `library` migrations `0093`–`0096`, each recorded once;
   - the constraint `hour_refill_policy_dry_run_only_phase_a` absent;
   - **no** `HourRefillPolicy` row (or, if one was created by a later admin
     visit, `enabled = false` and `dry_run_only = true`, with both cause
     switches off);
   - zero causes, zero installs, and zero LogItems with origin
     `refill_installed`.
3. **Feature Off.**
   - Admin → Config → Hour Refill Policy shows **Off**.
     - Database gate: *removed by reviewed migration 0096*.
     - Host armed: *no* for both web and engine.
     - The runner row reads *running*. *unknown* is acceptable only until the
       first status write, within 60 s.
   - Opening the page never creates the policy row. **Do not save the form**
     as part of verification.
   - The engine journal has no `Hour-refill evaluation` lines.
4. **Ordinary playout.** Playback, live fill, requests, protected closers
   (legal ID last) and hour transitions behave normally for at least one hour
   boundary. Observe this passively. Do not stage on-air Ejects or request
   swaps just to verify the release.

Record each station separately: the installed release, R and C, the job's
authorization source, the restart results and the verification output.

## What installation does not authorize

Applying `0096` automatically removes a database barrier. It does **not**
authorize any of these later stages, which each need their own explicit
approval and follow `docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md`:

1. KOGR category-kind audit (read-only).
2. KOGR shadow observation in dry run: 14 days or 30 qualifying causes,
   whichever is later.
3. Timing go/no-go: projection error 95th percentile ≤ 3 s and maximum ≤ 5 s.
4. Host arming (`HOUR_REFILL_INSTALL_ARMED=True`) and install mode, each with
   explicit approval.
5. WRJE: its own audit, observation window and decision. KOGR's evidence does
   not carry over.

Residual PSA stays off until its own decision.

## Rollback

Source rollback and schema reversal are **separate** recovery paths, and
neither is automatic.

- **Source.** Reinstalling an earlier release moves application code back
  only. The database keeps `0093`–`0096` applied. r0119 code never reads the
  new tables or the policy, and ignores the nullable `LogItem.origin` column,
  so it runs correctly on the r0120 schema.
- **Re-adding the constraint** (reversing `0096`) succeeds only if every
  `HourRefillPolicy` row has `dry_run_only = true`. It is a reviewed,
  separately authorized migration operation. It is not part of any automatic
  rollback, and it is never needed while dry run stays checked.
- Reversing `0093`–`0095` drops the hour-refill tables and the origin column,
  and with them their evidence. Do not do this as part of a routine rollback.
