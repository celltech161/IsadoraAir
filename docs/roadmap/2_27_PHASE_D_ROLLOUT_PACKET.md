# Roadmap 2.27: station rollout and timing-validation packet

**Status: a plan for later operator approval. Nothing here has been done.**
No station has been contacted, migrated, armed, enabled or measured. KOGR
and WRJE are **not** ready until each gate below has evidence and a
recorded go decision.

Each stage needs its own explicit approval. Approval of one stage does not
carry over to the next, or from KOGR to WRJE.

## Stage 0: release and review (no station change)

1. Independent review of Phase A–D, then merge and a normal release cut.
2. The release plan must show migrations `library.0093`, `0094` and `0095`
   as additive, and `library.0096_hour_refill_install_gate` as **manual**
   (RemoveConstraint + AlterField). The approver reviews 0096's exact
   operations against the station's production schema in the Update Center's
   Tier 3 reviewed-migration approval. Without that approval the release
   does not install, which is the intended outcome.
3. Confirm the release does not add `HOUR_REFILL_INSTALL_ARMED` or
   `HOUR_REFILL_PSA_CATEGORY_ID` to any station `.env`.

## Stage 1: KOGR install, feature Off

**Preflight (read-only):**

* A fresh nightly backup and a verified recovery payload. Refresh the payload
  if the protected-runtime generation changes.
* Update Center, Gunicorn, engine, encoders and monitoring healthy; no failed
  update job.
* Production schema at the expected migration; `HourRefillPolicy` has no row,
  or `enabled = False`.

**Install:** the release through the Update Center, with the Tier 3 approval
for 0096. The engine restart is part of the normal release.

**Verify within 15 minutes:**

* The Hour Refill Policy page shows **Off**. The database gate reads *removed
  by reviewed migration 0096*. Host armed reads *no* (web and engine). The
  runner reads *running*; status *unknown* is acceptable only until its first
  write (≤60 s).
* Ordinary playback, live fill, requests and transitions behave normally for
  at least one full hour boundary. The journal has no "Hour-refill"
  lines while Off.

**Rollback:** reinstall the previous release (the policy row is untouched).
The 0096 reverse migration is not needed while `dry_run_only` is True.

## Stage 2: KOGR category audit (read-only)

Before any observation, the station operator confirms for the active
schedule profile:

* Every legal ID, PSA, underwriting, imaging and dedication category has a
  non-`music` kind. A protected closer filed as `music` would not be
  recognised as a closing block, so a refill could be appended after it.
* The intended residual PSA category (if any, later) has kind `spot`.
* Hour logs end with the expected protected closing blocks (legal ID last).
* Dedication intro→song pairs are adjacent in recent logs.

Record any mis-filed category and fix it through normal library
administration **before** Stage 3. 2.27 never repairs categories.

## Stage 3: KOGR shadow observation (dry run)

**Approval needed:** an operator sets the master switch On, selects the cause
types to observe, and leaves **Observation only (dry run)** checked. Evidence
capture starts at the same time.

**Proposed window:** 14 days or 30 qualifying causes, whichever is later,
covering weekday and weekend programming and at least one partial-hour
takeover.

**What to collect (read-only):**

1. The `Hour-refill evaluation` journal lines (engine unit). Each has
   `captured_at`, `committed_runway_seconds`, `boundary_instant`,
   `net_shortage_seconds`, the status and the proposed Track ids.
2. The `PlayEvent` rows of the evaluated hour and of the next hour's first
   item.
3. The admin page counters, including dropped evidence and live-fill
   conflicts.

**Timing measurement:** for each evaluation where nobody intervened afterwards
(no Manual, Eject, Start Next, request swap or live fill in that hour), the
projection error is

`error = actual start of the first item after the runway − (captured_at + committed_runway_seconds)`.

A positive error means the hour ran longer than projected, which is what the
reserve protects against. Report min, median, 95th percentile and max, and
separately the cases ending at a protected closer and at a partial-hour
takeover.

**Reload-delay measurement:** not observable without installing. It is first
measured in Stage 5 (the adoption field of the status and the journal).

## Stage 4: go / no-go review for KOGR install mode

Go only if **all** of the following hold, with the evidence attached:

* No playback incident attributable to 2.27 during Stage 3. Gaps, stalls,
  double plays and late IDs from other causes must be explained.
* Projection error at the 95th percentile ≤ 3 s, and maximum ≤ 5 s. The
  reserve is 5 s + 1 s per item. If the maximum exceeds 5 s, the reserve is
  raised in code (a reviewed change) or the decision is no-go. The margin is
  never lowered to fill more.
* Advisory proposals respected categories, recency, requests and protected
  closers in a manual review of at least 10 proposals.
* Dropped evidence and runner errors are rare and explained.
* Category audit (Stage 2) clean.

## Stage 5: KOGR install mode (limited)

**Approvals needed, in order:**

1. A host change: add `HOUR_REFILL_INSTALL_ARMED=True` to KOGR's `.env` and
   restart the engine and Gunicorn at an approved quiet time. The page still
   shows *Observation* (dry run is still checked), and both "host armed"
   rows read *yes*.
2. An operator unchecks **Observation only (dry run)**. Optionally start with
   one cause type, the Eject cause, which has fewer request side effects.

**Observation window:** 7 days. Watch the page daily.

**Stop conditions (disable at once, then investigate):**

* any late legal ID, gap, double play or stall near an installed refill;
* a committed install whose rows were placed after a protected closer;
* any `duplicate_install` outcome that was not the result of a race;
* an install whose adoption was not observed while the engine stayed on that
  log;
* a request whose state changed because of a refill;
* repeated runner errors.

**Disable path (fast, no restart):** check **Observation only (dry run)**, or
switch the master Off. The next install transaction re-reads the policy under
a row lock and refuses; the runner stops evaluating within 15 s. Committed
rows already in the log stay and play like any scheduled row; an operator can
remove them with the normal log tools before they air.

**Full rollback:**

1. Master switch Off.
2. Remove `HOUR_REFILL_INSTALL_ARMED` from `.env` and restart the engine.
3. Optionally reverse migration 0096 only after every policy row has
   `dry_run_only = True` (the reverse `AddConstraint` fails otherwise).

## Stage 6: WRJE

Only after KOGR's Stage 5 report has been reviewed and WRJE is separately
approved. Repeat Stages 1–5 on WRJE, with its own category audit, shadow
window and go/no-go. KOGR's evidence does not replace WRJE's.

## Optional residual PSA

Stays off (`HOUR_REFILL_PSA_CATEGORY_ID` unset) until a separate decision
after Stage 5. When it is approved: set the variable to a `spot`-kind
category id in `.env` and restart the engine. A PSA is only proposed after a
music fit, with a residual of at most 60 s, and never alone.
