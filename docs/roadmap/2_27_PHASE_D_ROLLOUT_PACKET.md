# Roadmap 2.27: station rollout and timing-validation packet

**Status: the software is released; field activation has not been authorized.**

Hour Refill shipped default-Off in two accepted releases:
* [r0120](../releases/r0120.md), with its [installation guide](../releases/r0120_installation.md);
* [r0121](../releases/r0121.md), with its [installation guide](../releases/r0121_installation.md).

Their engineering merge, migrations and reviewed manual-operation approvals are history. They are summarized below
and not repeated as future steps.

What remains is **station activation**:
* category audit;
* shadow observation;
* a timing review;
* host arming;
* install mode.

This document does not say which release, schema or policy any station currently runs. Read that from the
station itself before each stage.

Installing or publishing a release never authorizes activation. Each stage below needs its own explicit approval.
Approval of one stage does not carry over to the next, or from KOGR to WRJE.

## Historical release record (complete; do not repeat)

| Release | Migrations | Manual operations and their authority |
|---|---|---|
| r0120: Eject and request-substitution causes | `library.0093_hour_refill_cause_policy`, `0094_logitem_origin`, `0095_hour_refill_install` (additive); `0096_hour_refill_install_gate` | 0096 index 0 `RemoveConstraint` and index 1 `AlterField`, both manual. Authorized only by the trusted companion `deploy/migration_authorizations/r0120.json`, pinned to 0096's bytes. Removing the Phase A CHECK moved the "always dry run" guarantee from the database to the application gates below. |
| r0121: Start Next, automatic-skip and media-failure causes | `library.0097_hour_refill_start_next_cause`, `0098_hour_refill_automatic_failure_cause` | Exactly seven manual operations: 0097 indices 4 (`RemoveConstraint`), 5 and 6 (`AddConstraint`); 0098 indices 6 (`RemoveConstraint`), 7, 8 and 9 (`AddConstraint`). Authorized only by the trusted r0121 companion, pinned to the migration file hashes in the r0121 notes. |

* **How a station received these:** through the trusted Update Center workflow, one explicit installation
  authorization per station, never by a manual `manage.py migrate`.
* **Restarts:** Gunicorn and the engine (the runner thread lives in the engine).

## The five cause types

| Cause (policy switch) | Introduced | Provisional threshold on the hour's net shortage |
|---|---|---|
| Shorter web-request substitution (`capture_request_substitution`) | r0120 | more than 60 s whenever any cause in the set is a request substitution |
| Completed operator Eject of scheduled music (`capture_operator_eject`) | r0120 | more than 30 s for Eject-only sets |
| Completed Start Next fade (`capture_start_next`) | r0121 | more than 30 s for Start Next-only sets |
| Automatic skip of missing scheduled media (`capture_automatic_skip`) | r0121 | more than 60 s whenever any cause in the set is an automatic skip |
| Confirmed media failure, before air or after playback began (`capture_media_failure`) | r0121 | more than 60 s whenever any cause in the set is a media failure |

* **Exact limits do not qualify:** 30.000 s or 60.000 s exactly does not trigger.
* **Mixed causes:** a set of mixed causes is evaluated only when **every** cause type in it is switched on. It is
  never trimmed to a subset.
* **Provisional:** these thresholds, the 20 s minimum insertion lead and the 5 s + 1 s-per-item safety reserve are
  proposals, not approved field values. Cause capture is best effort and never blocks the Eject, substitution,
  Start Next or skip it observes.

## Safety gates (all default Off; nothing here changes them)

* No `HourRefillPolicy` row means Off; installing a release creates no row.
* On a policy row:
  * `enabled` (master) is off;
  * all five cause switches are off;
  * `dry_run_only` (**Observation only**) is checked.
* `HOUR_REFILL_INSTALL_ARMED` is absent or false in the station's `.env` (host not armed).
* `HOUR_REFILL_PSA_CATEGORY_ID` is absent (residual PSA off).
* **Install mode needs all four:**
  1. a host armed and restarted;
  2. the master switch on;
  3. Observation only unchecked;
  4. every qualifying cause type on.

  The installer re-reads the policy under a row lock inside each install transaction.

## Stage 1: confirm the station's released, Off state (read-only)

Before any activation stage, record from the station itself:

* **Release:** the installed release and HEAD, and that `library.0093`–`0098` are applied. A station without them
  first needs its own authorized release installation, which is not part of this packet.
* **Health:** a fresh nightly backup and a verified recovery payload; Update Center, Gunicorn, engine, encoders and
  monitoring healthy; no failed update job.
* **Off state:** the Hour Refill Policy page shows **Off**:
  * no row, or `enabled` off;
  * all five switches off;
  * Observation only checked.
* **Host and runner:** "host armed" reads *no* (web and engine), and the runner reads *running*.
* **Quiet journal:** no `Hour-refill` journal lines while Off.

If the station shows anything else, stop and reconcile it before Stage 2. Do not assume a configuration that was
not observed.

## Stage 2: category audit (read-only)

The station operator confirms, for the active schedule profile:

* Every legal ID, PSA, underwriting, imaging and dedication category has a non-`music` kind. A protected closer filed
  as `music` would not be recognised as a closing block, so a refill could be placed after it.
* The intended residual PSA category (if any, later) has kind `spot`.
* Hour logs end with the expected protected closing blocks (legal ID last).
* Dedication intro→song pairs are adjacent in recent logs.

Record any mis-filed category and fix it through normal library administration **before** Stage 3. 2.27 never
repairs categories.

## Stage 3: shadow observation (dry run)

**Approval needed:** an operator sets the master switch On, selects the cause types to observe, and leaves
**Observation only (dry run)** checked. Evidence capture starts at the same time.

* **Choosing causes:** the five types can be observed together or added one at a time.
* **Per-type evidence:** each type needs its own evidence, in particular the r0121 types (Start Next, automatic skip,
  media failure) and their thresholds.

**Proposed window:** 14 days or 30 qualifying causes, whichever is later. It must cover weekday and weekend
programming and at least one partial-hour takeover. The window extends until each cause type to be approved has
enough qualifying events.

**What to collect (read-only):**

1. The `Hour-refill evaluation` journal lines (engine unit). Each has:
   * `captured_at`
   * `committed_runway_seconds`
   * `boundary_instant`
   * `net_shortage_seconds` and `threshold_seconds`
   * `cause_ids` (each cause's type is on its Hour Refill cause record in admin)
   * the status
   * the proposed Track ids
2. The `PlayEvent` rows of the evaluated hour and of the next hour's first item.
3. The admin page counters, including dropped evidence and live-fill conflicts.

**Timing measurement:** use each evaluation where nobody intervened afterwards in that hour: no Manual, Eject, Start
Next, request swap, automatic skip, media failure or live fill. The projection error is

`error = actual start of the first item after the runway − (captured_at + committed_runway_seconds)`.

* **Meaning:** a positive error means the hour ran longer than projected, which is what the reserve protects against.
* **Report:** min, median, 95th percentile and max, overall and per cause type. Report separately the cases ending at
  a protected closer and at a partial-hour takeover.
* **Reload delay:** not observable without installing. It is first measured in Stage 5 (the adoption field of the
  status and the journal).

## Stage 4: go / no-go review for install mode

Go only if **all** of the following hold, with the evidence attached:

* No playback incident attributable to 2.27 during Stage 3. Gaps, stalls, double plays and late IDs from other causes
  must be explained.
* Projection error at the 95th percentile ≤ 3 s, and maximum ≤ 5 s. The reserve is 5 s + 1 s per item.
  * If the maximum exceeds 5 s, either the reserve is raised in code (a reviewed change) or the decision is no-go.
  * The margin is never lowered to fill more.
* Advisory proposals respected categories, recency, requests and protected closers, in a manual review of at least
  10 proposals.
* Dropped evidence and runner errors are rare and explained.
* Category audit (Stage 2) clean.
* Each cause type to be enabled for install has its own qualifying evidence. Its provisional threshold is accepted,
  or changed through a reviewed code change.

## Stage 5: install mode (limited)

**Approvals needed, in order:**

1. **A host change:** add `HOUR_REFILL_INSTALL_ARMED=True` to the station's `.env`, then restart the engine and
   Gunicorn at an approved quiet time. The engine restart interrupts audio and ends any Remote DJ session. The page
   still shows *Observation* (dry run is still checked), and both "host armed" rows read *yes*.
2. **An operator unchecks Observation only (dry run).** Start with one cause type, preferably the operator Eject
   cause, which has the fewest side effects. Add others only through separate approvals.

**Observation window:** 7 days per newly enabled cause type. Watch the page daily.

**Stop conditions (disable at once, then investigate):**

* any late legal ID, gap, double play or stall near an installed refill;
* a committed install whose rows were placed after a protected closer;
* any `duplicate_install` outcome that was not the result of a race;
* an install whose adoption was not observed while the engine stayed on that log;
* a request whose state changed because of a refill;
* repeated runner errors.

**Disable path (fast, no restart):** check **Observation only (dry run)**, or switch the master Off.
* The next install transaction re-reads the policy under a row lock and refuses.
* The runner stops evaluating within 15 s.
* Committed rows already in the log stay and play like any scheduled row. An operator can remove them with the
  normal log tools before they air.

**Full rollback:**

1. Master switch Off.
2. Remove `HOUR_REFILL_INSTALL_ARMED` from `.env` and restart the engine.
3. Leave the schema in place. Reversing a migration is a separately reviewed recovery path, never a routine
   rollback step:
   * 0096's reverse succeeds only while every policy row has `dry_run_only = True`.
   * 0097/0098 reversal can be refused once their evidence exists, and deleting evidence to make it pass is
     prohibited (see the [r0121 notes](../releases/r0121.md)).

## Stage 6: the second station

Only after the first station's Stage 5 report has been reviewed and the second station is separately approved:

* Repeat Stages 1–5 there, with its own Stage 1 record, category audit, shadow window and go/no-go.
* Of KOGR and WRJE, the one that goes second makes its own decisions; the first station's evidence does not replace
  them.

## Optional residual PSA

Stays off (`HOUR_REFILL_PSA_CATEGORY_ID` unset) until a separate decision after Stage 5. When it is approved:
* set the variable to a `spot`-kind category id in `.env` and restart the engine;
* a PSA is only proposed after a music fit, with a residual of at most 60 s, and never alone.
