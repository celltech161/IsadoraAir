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
* **Remote DJ (r0149):**
  * A connected Remote DJ whose microphone is Off does not block a set whose qualifying causes are **all** Start
    Next.
  * It still blocks every other set: Eject, request, skip or media-failure causes, or any mix.
  * A live microphone (studio or Remote DJ), or a Remote DJ that has asked for its microphone while the gate is
    held muted, blocks every set, as does Manual.
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
* **Per-type evidence:** field evidence is required for the cause type proposed for activation, not for every
  supported type. The r0121 types (Start Next, automatic skip, media failure) each need their own.

**Minimum window:** at least **14 days** from the activation timestamp, covering weekday and weekend programming.
There is no minimum cause count (the earlier "30 qualifying causes" rule is withdrawn). The window extends until:
* the cause type proposed for activation has captured real causes that the reviewer accepts as sufficient; and
* every available unique relevant proposal has been reviewed.

**Small samples:** real causes are rare; a station may see a handful in two weeks. A few samples are supporting
evidence, never statistical assurance. Every report states the number of **independent cause-hours** separately from
the number of evaluation lines (the runner logs up to six evaluations per cause set: repeated readings of one hour).

**Partial-hour takeovers:**
* Where the station schedules none, the isolated partial-hour regressions qualify the pilot: the hour-refill
  projection, proposal and install suites, run on the installed release through the guarded test runner.
* Before a station deliberately relies on partial-hour takeovers, it needs its own operational qualification of
  that pattern.

**What to collect (read-only):**

1. The `Hour-refill evaluation` journal lines (engine unit). Each has:
   * `captured_at`
   * `committed_runway_seconds`
   * `boundary_instant`
   * `net_shortage_seconds` and `threshold_seconds`
   * `cause_ids` (each cause's type is on its Hour Refill cause record in admin)
   * the status
   * the proposed Track ids
2. The `PlayEvent` and `PlayEventSegment` rows of the evaluated hour and of the next hour's first item, including
   **every** segment of each occurrence, with its `start_reason`, `evidence_state` and `termination_reason`.
3. The admin page counters, including dropped evidence and live-fill conflicts.
4. The other engine journal lines for that hour: `Trigger:`, and every seek, pause, resume or restart-recovery
   line. `Seek/preroll verified` is the one that records a continuation's confirmed media position. Collect these
   while the journal still holds them.

### Timing measurement

The measurement compares the **same event** on both sides: the moment the successor of the runway's final committed
occurrence becomes audible.

**Predicted endpoint.** P = `captured_at + committed_runway_seconds`. This is the instant the successor of the final
committed occurrence F becomes audible, as the engine's own trigger arithmetic models it. Each modelled transition
includes one 250 ms poll allowance, so P is a slight upper bound.

#### What the recorded times mean (verified in the engine source)

* **A segment is one deck generation.** A `PlayEventSegment` records one generation's contribution to program
  audio. A pause closes it, and a seek, resume or restart recovery plays on in a replacement generation, so one
  `PlayEvent` can have several segments.
* **Fresh start (`start_reason = fresh_air_start`).** `started_at` is the wall-clock time of the generation's first
  real buffer (after any silence primer) as it leaves the deck. The media there is **source position 0**, not the
  audible start. `PlayEvent.started_at` is the same value.
* **Continuation (any other `start_reason`).** `started_at` is when the replacement generation's program accounting
  began. Its source position **can be nonzero, and the segment does not store it**.
* **Not positions:** `ended_at` is when a generation's contribution closed. `confirmed_duration_seconds` is the
  amount of audio counted for one segment. Neither gives a source position, and neither does adding segments up: a
  seek jumps.
* **Markers:** E, the **effective end point**, is `next_start`, else `duration`. A missing `cue_in` counts as 0.
  The segment stores no markers. The values that count are those the engine had loaded for that hour. A reviewer
  reads the Track later, so confirm that `cue_in`, `next_start`, `duration` and the media are unchanged since the
  hour. If that cannot be confirmed, exclude the sample.
* **Position to time:** while a generation that started at source position s plays on uninterrupted, source
  position m (m ≥ s) is reached at `started_at + (m − s)`.
  * So a fresh start (s = 0) is audible at `started_at + cue_in` and reaches its effective end at
    `started_at + E`.
  * **Never count cue-in twice.** E is a source position, so `started_at + cue_in + E` is wrong.
* **Trigger:** the engine starts a successor when F reaches F's `next_start − successor cue_in` (F's E itself when
  that value is under 10 s). In the ordinary case the successor becomes audible as F reaches E.
* **EOS is not the transition.** Physical EOS (`ended_at`, `termination_reason = eos`) comes after the effective
  transition whenever the file has an outro beyond E. It is never the comparison point.
* **Not measured:** the small delay between a buffer leaving the deck and being heard.

#### Zero-position eligibility

Use `started_at + cue_in` or `started_at + E` only when **all** of these hold:
1. the segment's `start_reason` is exactly `fresh_air_start`;
2. it is that occurrence's first segment, with no pause, seek, resume or restart before the instant being computed;
3. the segment is not `interrupted`, and its generation was still contributing at that instant (it has no
   `ended_at`, or `ended_at` is not earlier);
4. the markers are confirmed as above.

An occurrence with no segment (a dedication intro records no `PlayEvent`) cannot supply an observed time.

#### Observed endpoint

O is one of two kinds. Label every sample, and report the two kinds separately.

1. **Directly observed successor timing.** The ordinary trigger started the next item while F was still playing:
   * O = successor `started_at` + successor `cue_in`, and the successor must meet zero-position eligibility.
   * F's own starting position does not enter this formula.
2. **Reconstructed effective transition endpoint (runway exhausted).** Nothing was triggered from F:
   * **F fresh:** O = F `started_at` + F's E, with F meeting zero-position eligibility.
   * **F a continuation:** exclude it, unless **all** of the following hold. Then O = `started_at` + (E − s), and
     cue-in is not added separately.
     * The continuation segment opened **before** the evaluation's `captured_at`, so P was projected from the
       continued deck.
     * s is the `confirmed_position_ns` (in seconds) of the engine's `Seek/preroll verified` journal line, matched
       to that LogItem and to the moment the segment opened. A requested or saved position, a `Restoring … at` line
       or a `Paused at` line is **not** proof.
     * It is the occurrence's last segment, it played on uninterrupted to E, and s is not past E.
     * Its `start_reason` is not a `…fallback_zero` reason. Those follow a failed seek; exclude them.
   * Either way, if F's `ended_at` is **earlier** than the computed O, F never reached E. Compute no value. Report
     it as a marker or media finding, which must be explained and may be a genuine projection defect.
   * A reconstruction is built from F's start and markers. It is **not** an observed successor event and cannot show
     trigger or transition delay, so it is weaker evidence than a direct observation.

`error = O − P`. A positive error means the hour ran longer than projected, which is what the reserve protects against.

**When in doubt, exclude** the sample and give the reason. Do not build a more elaborate reconstruction.

#### Silence and waiting (never projection error)

When the runway is exhausted, report these intervals separately from the error:
* **Outro:** from the effective endpoint to F's EOS. This is F's audio, not silence.
* **Waiting or silence:** from F's EOS to the next item's segment `started_at`, for example while waiting for the
  next hour's log. It is a **continuity (dead-air) finding** with its own explanation. The next item's own lead-in
  up to its cue-in can lengthen the audible silence. Check independent evidence, such as the encoder audio monitor.

Comparing P with the next item that happened to air adds the outro and the wait to the error. That is not a
projection measurement.

#### Eligible evaluations

* Use evaluations after which **nothing outside the committed runway aired** before O, and no Manual or microphone
  hold, Eject, further Start Next, request swap, automatic skip, media failure, VT admission or live-fill row
  reached air. An engine-inserted dedication intro counts as such material.
* **Later interventions invalidate the projection.** Exclude the evaluation if, after its `captured_at` and before
  O, any of these happens:
  * a seek, pause or resume;
  * an engine restart, restart recovery or other replacement of a deck;
  * an operator edit that removes, replaces or reorders a committed item;
  * a marker or media change on a committed item.

  Signs include a further or `interrupted` segment on any committed occurrence. Exclude it even when O − P happens
  to look small. A later evaluation is judged on its own `captured_at`.
* A connected Remote DJ whose microphone stays Off is not an intervention.
* Where the **only** disturbance is separately measured added airtime, an excluded hour may also show an adjusted
  value with that airtime removed, labelled as adjusted. It is not a sample. There is no adjusted value for a seek,
  resume, removal or hold.
* Exclusion removes a number from the error statistics only. It never removes a continuity or defect finding from
  the report.
* **One independent sample per cause-hour:** its first eligible evaluation. Later evaluations of the same
  cause-hour are not independent samples.

#### Worked examples (illustrative values, not station evidence)

F has cue_in 2.000, E = 200.000 and duration 208.000, except in example 4.

| # | Case | Evidence | O and error |
|---|---|---|---|
| 1a | Fresh, source position 0, successor triggered | The successor (cue_in 0.500) has a `fresh_air_start` segment at 12:03:19.600. P = 12:03:20.300 | Directly observed O = 12:03:19.600 + 0.500 = **12:03:20.100**; error **−0.200 s** |
| 1b | Fresh, source position 0, runway exhausted | F `fresh_air_start` at 12:00:00.000; audible from 12:00:02.000. P = 12:03:20.300 | Reconstructed O = 12:00:00.000 + 200.000 = **12:03:20.000**; error **−0.300 s**. 12:03:22.000 would count cue-in twice |
| 2 | Continuation from a known nonzero position | The segment opens at 12:05:00.000, before `captured_at`. Its `Seek/preroll verified` line confirms s = 80.000. It is the last segment and reaches E. P = 12:07:00.250 | Reconstructed (continuation) O = 12:05:00.000 + (200.000 − 80.000) = **12:07:00.000**; error **−0.250 s**. `started_at + E` (12:08:20.000) would be 80 s late |
| 3 | Continuation from an unknown position | The same segment, but no verified line survives | **Excluded; no value.** A requested or saved position and `confirmed_duration_seconds` do not supply s |
| 4 | Exhausted runway, then a wait for the next hour | F (E = 100.000) `fresh_air_start` at 12:55:00.000, EOS at 12:56:48.000. The next hour's first segment is at 12:59:30.000. P = 12:56:40.400 | Reconstructed O = **12:56:40.000**; error **−0.400 s**. Separately: 8.000 s of outro, then **162.000 s** waiting (a continuity finding). "Next item − P" (169.600 s) is not projection error |
| 5 | A later intervention | The evaluation is captured at 12:10:00 with P = 12:45:00.000. At 12:20:00 a committed deck is paused and resumed, which adds a second segment | **Excluded**, whatever O − P would be |

#### Report

* min, median, 95th percentile and max, overall and per cause type, with the number of cause-hours;
* directly observed and reconstructed samples labelled and counted separately, including continuation
  reconstructions;
* separately, the cases that end at a protected closer and at a partial-hour takeover;
* every excluded cause-hour, with its reason;
* every dead-air or continuity finding, with its own explanation.

A few cause-hours are **supporting evidence**. They are not a statistically comprehensive qualification, even when
each is inside the Stage 4 targets.

**Reload delay:** not observable without installing. It is first measured in Stage 5 (the adoption field of the
status and the journal).

## Stage 4: go / no-go for a supervised pilot

Go only if **all** of the following hold, with the evidence attached:

* **Observation:** the Stage 3 minimum window is complete, with weekday and weekend coverage.
* **Release:** the station runs a healthy, trusted installed release.
* **Category audit:** the Stage 2 audit is current and clean, with any operator decisions recorded. An empty
  `music` category that is legitimately classified is not by itself a failure.
* **No unintended installs:** none during shadow observation.
* **Incidents:** no playback incident is attributable to 2.27. Every relevant gap, stall, double play or late ID is
  explained.
* **Timing:** projection error on correctly matched events (Stage 3) has a 95th percentile ≤ 3 s and a maximum ≤ 5 s.
  The reserve is 5 s + 1 s per item.
  * State the sample size; make no statistical claim from a small one.
  * A genuine projection defect larger than 5 s is a no-go, unless the reserve is raised in code through a
    reviewed change.
  * The margin is never lowered to fill more.
* **Proposals:** every available unique relevant advisory proposal was reviewed and respected categories, recency,
  requests, dedication adjacency, protected closers and the reserve.
* **Cause evidence:** the cause type to be enabled has its own field evidence, and its provisional threshold is
  accepted or changed through a reviewed code change.
* **Runner health:** dropped evidence and runner errors are rare and explained.
* **Nothing unresolved:** no genuine material projection defect, unexplained relevant incident or unsafe category
  classification remains open.

Pilot readiness is not operational acceptance; that comes from Stage 5.

## Stage 5: limited install (supervised pilot)

**Approvals needed, in order:**

1. **Configure the approved cause switches** while **Observation only** stays checked.
2. **A host change:** add `HOUR_REFILL_INSTALL_ARMED=True` to the station's `.env` through the separately authorized
   station configuration procedure.
3. **Restart** the engine and Gunicorn at an approved quiet time, with no live microphone and no Remote DJ session.
   The engine restart interrupts audio and ends any Remote DJ session.
4. **Verify** that the page still shows *Observation* (dry run is still checked) and that both "host armed" rows
   read *yes*.
5. **An operator unchecks Observation only (dry run)**, as a separate authorization. Do it at a time when the
   current hour has **no** recorded cause, so the first install comes from a new, supervised event.

**First cause type:** the one with the strongest station evidence. Start Next may be first. A Start Next-only pilot
uses:
* Master On, Start Next On;
* request substitution, operator Eject, automatic skip and media failure Off;
* residual PSA Off (`HOUR_REFILL_PSA_CATEGORY_ID` unset).

**Unchanged by the pilot:**
* the strict Start Next-only threshold (more than 30 s);
* at most two music tracks;
* the 5 s + 1 s per item reserve, the 20 s insertion lead and zero modelled overrun;
* media readiness and separation;
* protected closing blocks;
* fresh snapshots, CAS and the locked transaction;
* the Manual and microphone exclusions;
* live-fill arbitration;
* host arming and Observation only as independent gates;
* the disable path below.

**Supervision:**
* The first installation is directly supervised, in an ordinary music hour.
* A Start Next pressed from the studio Console, or while a Remote DJ is connected with its microphone Off, is
  acceptable (r0149).

**After a release that changes cause eligibility** (r0149 for Start Next):
* Prior observation evidence stays valid, and the 14-day window is not reset.
* A focused post-installation verification of the newly permitted path is required before the live pilot.

**Observation window:** 7 days of limited installation for each newly enabled cause type. Watch the page daily.
* A cause type is **operationally accepted** only after at least one real refill has been installed and observed
  airing correctly.
* A cause type that has not produced a real installed refill stays in **pilot** status, whatever the elapsed time.
* Additional cause types need their own evidence review and authorization.
* Each station is qualified and authorized independently.

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
