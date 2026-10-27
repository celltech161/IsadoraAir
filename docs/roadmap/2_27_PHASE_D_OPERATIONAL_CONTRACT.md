# Roadmap 2.27 Phase D: dormant operational integration

Status: **Phase D engineering, dormant.** Base: accepted Phase C `9bb844c`
(Phase B `eef0e62`, Phase A `172c524`, r0119 `9e7adc0`).

Phase D connects the accepted Phase A evidence, Phase B projection and Phase C
installer to one supervised caller, gives operators a control and status page,
and authors the migration that makes installation *possible*. It does not
make installation happen. After this phase, every station still has:

* `HourRefillPolicy.enabled = False` (or no row): the caller does nothing but
  read the policy;
* the Phase A database check constraint `dry_run_only = True`, until
  migration 0096 is applied through a reviewed Update Center approval;
* no `HOUR_REFILL_INSTALL_ARMED` entry in the station `.env`, which keeps
  installation refused even if the first two are changed.

**Principle (unchanged):** a missed refill is acceptable. A delayed deck, a
duplicate occurrence, a displaced ID or a wrong request outcome is not.

Legend used below: **[now]** implemented and tested in this phase;
**[dev]** validated only in the isolated development harness; **[station]**
still requires station validation under a separate rollout approval.

## 1. Topology (verified from source)

* `isadoraair-engine.service` runs `manage.py run_engine`, which builds exactly
  one `PlaybackEngine` and blocks in `engine.start()` (the GLib main loop).
  One engine process per station. Gunicorn, the request reconciler
  (`ingest_web_requests` timer, every 20 s) and the admin are other processes.
* Inside the engine process the existing Phase A `HourRefillEvidenceRecorder`
  is a daemon thread with its own database connection, started in `start()`
  and stopped at the end of `stop()`.
* Request causes arrive by two paths (Phase A §6.1): the reconciler writes
  them in its own process; the engine's last-second swap hands them to the
  recorder. Eject causes always come from the recorder.

So the caller must live in the engine process (it needs GLib snapshots of this
engine) and must poll the database for reconciler-written causes; an event
alone cannot see them. **[now]**

## 2. The caller: `HourRefillRunner` (one per engine)

`library/services/hour_refill_runner.py`. One daemon thread, constructed in
`PlaybackEngine.__init__`, started next to the evidence recorder and stopped
first thing in `stop()`. It owns its database connection.

```
                     GLib thread (engine)                     runner thread                      database
start()  ───────────────────────────────────────────►  start (idempotent)
evidence recorder (its own thread) records a cause ──►  wake()  (Event.set, coalesced)
every 15 s, or on wake:
                                                        read policy (1 pk SELECT)  ───────────────► off -> publish, sleep
                                                        active log id: engine._lock, attribute read
                                                        cause ids for that log, ids already in
                                                          ledger installs (2 indexed SELECTs) ────► none new -> publish, sleep
                                                        per-cause-set backoff due?
   idle_add(capture)  ◄──────────────────────────────── request snapshot (≤1 outstanding, wait ≤1 s)
   copy primitives, put_nowait ───────────────────────►
                                                        Phase B projection (fresh boundary, causes) ─► not eligible -> publish
                                                        every qualified cause type switched on?   ─► cause_type_disabled
                                                        observation: Phase B proposal (advisory)
                                                        install:     Phase C HourRefillInstaller.attempt()
                                                                       (its own S1/S2, CAS-1, lock, CAS-2,
                                                                        policy re-read FOR UPDATE, COMMIT)
                                                        publish status file (tmpfs), counters
   ≤3 s: existing _reload_queue_if_changed adopts committed rows   (adoption observed on a later tick)
```

**Thread contract.** **[now]**

* GLib: the only Phase D work is the Phase B capture callback, at most one
  outstanding at a time (a second request while one is queued is not
  scheduled). It copies primitives and calls `put_nowait`. There is no new
  timer, query, file access, hash, wait, print or log on GLib. `start()` and
  `stop()` add only thread start / Event set / a bounded join, outside any
  GLib callback (before `loop.run()` and after it returns).
* Runner: every query, policy read, Phase B search, media check, transaction,
  status-file write and counter. It never touches deck or queue state. It
  briefly takes `engine._lock` to read `current_log` and, after an install,
  to compare installed ids with `log_items` (the same short, I/O-free
  sections Phase C's coordinator already uses).
* The runner reuses Phase C's `RefillCoordinator`; it does not reserve or
  arbitrate on its own. Ordinary live fill and a causal install can never
  overlap (Phase C §2).

**Cadence and bounds.** **[now]**

| Item | Value | Why |
| --- | --- | --- |
| check interval | 15 s | notices reconciler causes and policy changes; costs 1–3 indexed queries off GLib |
| wake | the recorder's successful cause insert sets an Event | an Eject is evaluated within seconds, not 15 s |
| per cause set | ≥30 s between evaluations, at most 6 evaluations | transient blocks (transition, Manual) get a few retries; never a busy loop |
| install attempts | Phase C cap: 3 per log | unchanged |
| outstanding work | one evaluation (single thread) and one GLib capture | coalesced wakes |
| status file | written on change, else every 60 s | no per-poll write |
| journal | one line per evaluation (ids and numbers), plus the 1st and every 100th runner error | evaluations are bounded by the cause-set cap; Off and "no new cause" checks print nothing |
| memory | backoff entries for the current log only | pruned on log change |

**Lifecycle.** **[now]**

* `start()` is idempotent: a second call while the thread lives starts
  nothing. Each start increments a generation; a thread whose generation is
  stale or whose stop Event is set does no further evaluation and never
  publishes.
* `stop()` sets the stop Event, wakes the thread and joins for at most 0.5 s.
  It never waits longer. A database call already in flight is not cancelled;
  if it is a Phase C commit, the result is the Phase C "process stops before
  the reload" row: the restart loads the rows, and the ledger and fresh net
  prevent a second install.
* After `loop.run()` returns, no capture callback is dispatched, so any
  attempt in progress ends at its next snapshot request
  (`snapshot_unavailable`), before any commit.
* A hard crash loses nothing the runner owns except in-memory counters and
  backoff. Phase A evidence loss in a crash stays an accepted limitation.

## 3. Policy, gates and truth table

Existing `HourRefillPolicy` singleton (no new policy table). Its fields keep
their Phase A meaning and gain Phase D meaning:

| Field | Phase A meaning | Phase D meaning |
| --- | --- | --- |
| `enabled` | master switch for evidence capture | master switch for the whole feature |
| `capture_request_substitution` | capture request causes | request causes may drive a refill |
| `capture_operator_eject` | capture Eject causes | Eject causes may drive a refill |
| `dry_run_only` | always True (check constraint) | True = observation; False = install requested |

Install additionally requires the host arming flag
`HOUR_REFILL_INSTALL_ARMED` (`settings.py`, from the station `.env`, default
**False**, not editable from the web `.env` editor, read at process start).

| Policy row | `enabled` | `dry_run_only` | host armed | cause switch for every qualified cause | Mode | Writes |
| --- | --- | --- | --- | --- | --- | --- |
| missing or unreadable | – | – | – | – | **off** (fail closed) | none |
| present | False | any | any | any | **off** | none |
| present | True | True | any | no | observation, `cause_type_disabled` | none |
| present | True | True | any | yes | **observation** (advisory proposals) | none |
| present | True | False | False | any | observation (`install_not_armed`) | none |
| present | True | False | True | no | install mode, `cause_type_disabled` | none |
| present | True | False | True | yes | **install** (Phase C decides) | ≤1 guarded install per cause set |

A cause set is refused as a whole if any qualified cause has its switch off.
It is never filtered down to a subset, because a subset could apply the
looser Eject threshold to a deficit that a request helped create.

**Where each gate is checked.** **[now]**

1. Runner tick: mode from one policy read (observation and install only).
2. `HourRefillInstaller.attempt()` start: `install_permitted()` (unchanged
   call site; now also requires the host arming flag).
3. CAS-1: every qualified cause type is switched on (`cause_type_disabled`).
4. Under the PlaylistLog lock, before any write: the policy row is re-read
   `FOR UPDATE` and must still permit installation for these cause types
   (`dry_run_only`, reason `policy_changed_before_commit`).

Step 4 makes disabling linearizable with installs: an operator save that
commits before the install transaction reads the row is always seen; a save
issued during the transaction waits (≤2 s, the Phase C commit-age bound) and
takes effect for every later attempt. Enabling takes effect at the runner's
next check (≤15 s); `enabled` also gates the Phase A evidence capture, whose
own refresh is ≤30 s.

**Migration 0096 (the gate migration).** **[now, dev only]**

* `RemoveConstraint hour_refill_policy_dry_run_only_phase_a`.
* `AlterField dry_run_only` to `editable=True` (non-database metadata) with
  an operator-facing label and help text.
* No data operation. Existing rows keep their values (`dry_run_only` stays
  True). Nothing is enabled, armed or invoked.
* Update Center classifies both operations **manual** (neither is on the
  mechanical allowlist), so a release containing 0096 needs a Tier 3
  reviewed migration approval for that exact plan on each station. This is
  intentional: applying it removes a safety barrier.
* Reverse: `AddConstraint` succeeds only while every row has
  `dry_run_only = True`. Rollback procedure: set observation (or Off) first,
  then migrate back.
* Old code on the new schema never writes `dry_run_only` and never installs.
  New code on the old schema refuses `dry_run_only = False` in the admin form
  (it checks for the constraint) and cannot install, because the constraint
  still holds.

**PSA residual.** Off by default. Optional explicit input
`HOUR_REFILL_PSA_CATEGORY_ID` (`.env`, empty by default). When empty the
runner passes Phase B a disabled `PsaResidualPolicy`. When set, Phase B's
existing validation applies (must be a `spot`-kind category, 0 < residual
≤ 60 s, only after a music fit, never alone). No migration and no admin
editing; activation is left to rollout review. **[now]**

## 4. Operator surface

Django admin, Config section: **Hour Refill Policy** (singleton), visible to
users with the model's `view`/`change` permissions (superusers and station
admins granted them); not to listeners, Remote DJ or ordinary DJ accounts.
**[now]**

* Editable: master switch, the two cause switches, and "Observation only (dry
  run)". The last one cannot be cleared while the database gate is present.
  Saves are audited through the existing library config-change events.
* Read-only status panel:
  * **Mode**: Off / Observation (dry run) / Install requested, host not armed
    / Install permitted. Enabled and dry-run state are always shown
    separately; there is no single "Enabled" badge.
  * **Gates**: database gate present or removed (schema introspection), host
    armed (this web process) and as reported by the engine.
  * **Fixed v1 rules**: request or mixed net > 60 s; Eject-only net > 30 s
    (operator-approved provisional); 20 s insertion lead and 5 s + 1 s per
    item reserve labelled *provisional engineering margins* (not settings).
  * **Runner**: running, last check, last heartbeat age; *unknown* when the
    status file is missing or older than 3 minutes.
  * **Last outcome**: time, log/hour, cause types and count, net shortage,
    threshold, reserve and residual, Track and category ids, placement,
    status and reasons, kind (`advisory` / `abstained` / `committed`).
  * **Counters** since engine start, including dropped evidence and
    ordinary-live-fill conflicts.
  * **Recent installs** from the ledger (last 10): committed time, log/hour,
    placement, LogItem ids, and *aired* only where an existing `PlayEvent`
    has that `log_item_id_snapshot`. "Committed" is never shown as "aired".

Status meanings: `off`, `observation`, `no_active_log`, `no_new_cause`,
`no_qualifying_cause`, `already_satisfied`, `below_threshold`,
`no_safe_boundary`, `unsafe_window`, `blocked_transport`, `wrong_hour`,
`cause_type_disabled`, `proposed` (advisory), `no_fit`,
`no_eligible_music_category`, `no_safe_anchor`, `stale_discarded`,
`lock_contended`, `duplicate_install`, `live_fill_pending`,
`refill_in_flight`, `adoption_pending`, `attempt_limit`,
`snapshot_unavailable`, `dry_run_only`, `installed`, `error`.

Privacy: the status file and page carry internal ids, numbers and status
strings only: no requester name, dedication, request text, IP, URL or
credential. **[now]**

## 5. Observability levels (never conflated)

| Level | Source | Shown as |
| --- | --- | --- |
| advisory | observation-mode Phase B proposal | "advisory", never written |
| committed | `HourRefillInstall` ledger row + LogItems | "committed (not proof of air)" |
| adopted | runner sees the installed ids in `engine.log_items` | "adopted by engine at …" (best effort) |
| aired | existing `PlayEvent.log_item_id_snapshot` | "aired at …" |

Phase D writes no `PlayEvent`, `played_at`, segment or `SongRequest` state.

## 6. Timing: what is and is not known

The 20 s lead and the 5 s + 1 s/item reserve remain **provisional
engineering margins** (Phase C §4). The ~3 s queue reload is an observed
design mechanism, not a delivery guarantee. Phase D measures the worker-side
path in an isolated harness (real GLib main loop and real development
database, no audio device); those numbers are **[dev]** only and do not
establish KOGR or WRJE pipeline timing. Station timing evidence is part of the
rollout packet (§8) and is **[station]**.

## 7. What later authorization must cover (not done here)

1. Merging Phase A–D and cutting a release (normal review).
2. On each station, a Tier 3 reviewed approval for migration 0096 in that
   release's exact plan.
3. On each station, adding `HOUR_REFILL_INSTALL_ARMED=True` to the `.env` and
   restarting the engine, at an approved time.
4. An operator setting the policy to observation, then (after the
   go/no-go review) clearing "Observation only".

KOGR first; WRJE only after KOGR's results are reviewed and separately
approved. The step-by-step packet is
`docs/roadmap/2_27_PHASE_D_ROLLOUT_PACKET.md`.

## 8. Limitations

* Reconciler-written request causes are noticed by polling (≤15 s), not
  instantly.
* Evaluation per cause set is capped (6) and install attempts per log are
  capped (3); a long Manual or Remote DJ period can use them up. A missed
  refill is accepted.
* The status file is tmpfs and per process: it is empty after a reboot until
  the runner's first write, and counters restart with the engine.
* Adoption is observed only by the runner's later ticks; it is diagnostic.
* Protected closers still depend on correct category-kind filing.
* No timing figure in this phase is a station measurement.
