# Roadmap 2.27 Phase C: guarded refill installation

Status: **Phase C, dormant installer.** Base: accepted Phase B `eef0e62`.

This phase adds:

* a worker-side installer, `library/services/hour_refill_install.py`;
* one append-only ledger table (`HourRefillInstall`, migration 0095);
* a new `LogItem.origin` value, `refill_installed`;
* a small coordinator shared with the ordinary 1.1 live fill.

Nothing calls the installer automatically: there is no timer, Console
control or engine call site. It refuses unless the policy allows
installation, and the Phase A check constraint
(`dry_run_only=True`) makes that impossible on every station until a
separately approved Phase D. Tests reach installation only by patching
`install_permitted()`. There is no bypass in production code.

**Principle:** a missed refill is acceptable. A delayed deck, a duplicate
occurrence, a displaced ID or a wrong request outcome is not.

## 1. What already exists, and why the design uses it

`PlaybackEngine._reload_queue_if_changed()` runs on the GLib thread from
`_poll_position` at most every 3 s, while a log is active. It is r0119
behaviour and is unchanged here. Each run it:

* re-reads the active `PlaylistLog`'s items from the database, ordered by
  position;
* replaces `log_items`;
* sets the cursor just after the last item that is on a deck or has
  `played_at`, or, with both decks empty, follows the previously next item.

Every other database-side queue edit already relies on this reload:
operator insert and reorder, and request Track swaps.

**Consequence.** Any committed `LogItem` in the active log becomes engine
memory within one reload. There is no "committed but not adopted" state that
lasts longer than that. A staged, inactive-row design would need every reader
(this reload, the restart loader, previews, the request reconciler) to learn a
new filter. Phase C does not do that. Instead it guarantees that committed rows
are **safe whenever any reload adopts them**, and treats the database COMMIT
as the only linearization point. The Phase C worker never mutates GLib memory.
Phase C adds no GLib-side splice; the "adoption" is the existing guarded
reload.

## 2. Coordinator (`RefillCoordinator`, one per engine)

All state is in memory and is changed only under the engine's existing
`self._lock`. Every critical section is a few attribute reads and writes:
no I/O, query, wait or print.

| Field | Meaning |
| --- | --- |
| `refill_owner` | token of the single in-flight causal attempt, or None |
| `legacy_callbacks_pending` | generations of ordinary live-fill callbacks that have been scheduled and have not run yet |
| `pending_adoption` | `(log_id, frozenset(installed LogItem ids))` after a causal commit, until those ids are in `log_items` |
| `attempts` | per-log attempt count (cap 3) |

Ordinary live fill is busy when `_live_fill_in_progress` (dispatch until
the worker ends) **or** `legacy_callbacks_pending` is non-empty. Generation
identity means an older callback cannot clear a newer callback's marker.
Together they cover dispatch, worker and late callback without a gap: the
worker marks the generation *before* `GLib.idle_add`, and clears
`_live_fill_in_progress` in `finally`.

* `reserve(engine)` (worker): fails when legacy is busy, another refill owns
  the coordinator, or the log's adoption is pending.
* `_try_extend_live_log_async` (GLib, inside its existing `self._lock`
  block): when `blocks_legacy(log_id, log_items)` says a refill owns the
  coordinator or a committed refill has not been reloaded yet, it does not
  dispatch. This is a set-membership check over in-memory ids.
* `_install_live_fill` (GLib): clears its own pending generation. If a refill
  commit for that log is still awaiting reload, it discards its proposal,
  which is already guaranteed by mutual exclusion; this is defence in depth.

With 2.27 Off the coordinator is never reserved, so `blocks_legacy` is
always False. Ordinary live fill then behaves exactly as r0119 apart from
adding and removing its generation from one in-memory set (tested by the unchanged
`test_async_live_fill` and `test_live_fill_deficit` suites).

## 3. Install sequence (worker W, GLib G, database D)

```
W  0  install_permitted()?  (enabled and not dry_run_only: always False today)  -> off / dry_run_only
W  1  coordinator.reserve()          [engine._lock, microseconds]       -> live_fill_pending / refill_in_flight
W  2  idle_add(capture S1) --------> G  capture_playback_window: primitives only, put_nowait, return
W  3  wait for S1, at most 1.0 s                                        -> snapshot_unavailable
W  4  attempt cap for S1's log                                          -> attempt_limit
W  5  Phase B evaluate_hour_refill(S1, safety budget)  ---------------> D  SELECTs only
W  6  is_file() for the chosen Tracks (outside any lock)                -> stale_discarded(media_missing)
W  7  idle_add(capture S2) --------> G  capture, put_nowait
W  8  CAS-1  digest(S2) == digest(S1); same engine instance and log;
       re-resolve boundary == proposal's; re-project on S2 (eligible,
       same threshold); placement == proposal's (no fallback);
       fit with budget on S2; insertion lead >= 20 s; re-run bounded Phase B
       selection and require the identical candidates             -> stale_discarded / unsafe_window / ...
W  9  BEGIN; SET LOCAL lock_timeout 250ms; PlaylistLog FOR UPDATE ----> D   -> lock_contended
W 10  CAS-2 (database under lock): ledger key absent; log still approved
       with the same date and hour; ordered rows (id, position, Track,
       category) == S2's queue (poison-filtered); no claim or play on any
       row at or after S2's cursor; Tracks unchanged and Ready-to-Air;
       category membership/kind, identity and active-holiday exclusion unchanged;
       no new active request for them; no request or intro tied to the
       closing block                                                     -> duplicate_install / stale_discarded / no_safe_anchor
W 11  INSERT ledger row; insert_before: two-pass shift of the closing
       block only; INSERT new LogItems (origin=refill_installed)
W 12  age check: at most 2.0 s since S2 was received, else raise -> ROLLBACK (stale_discarded)
W 13  COMMIT                                   <== linearization point
W 14  coordinator.publish(pending_adoption)    [engine._lock]
G 15  <= 3 s: _reload_queue_if_changed adopts the database order; cursor from decks / played_at
```

The worker never waits on GLib while holding a database lock. GLib never
waits on the worker: a capture callback that runs after the worker gave up
writes into a mailbox nobody reads. No step runs a query, transaction,
lock, file check or hash on GLib.

### What happens when each stage fails or goes stale

| Stage | Situation | Outcome | Database / memory effect |
| --- | --- | --- | --- |
| before 9 | The proposal or S1 is stale (S2 differs: reorder, same-count swap, claim, cursor, forced item, deck generation, Play Now, engine restart) | `stale_discarded` | none |
| before 9 | Ownership changes (Manual, Remote DJ, mic, VT, Start Next, overlap, pause, resume, live fill) | the Phase B outcome (`blocked_transport`, ...) | none |
| 9 | The lock is held by an engine splice or request swap for more than 250 ms | `lock_contended` | none |
| 10 | The engine advanced or claimed between S2 and the lock; a row was added or reordered by another process; a Track was edited; a new request appeared | `stale_discarded` | rollback; none |
| 10–11 | Same cause set already installed (concurrent or repeated worker) | `duplicate_install` | rollback; none |
| 11 | Unique position collision or other `IntegrityError`; deadlock or lock timeout | `stale_discarded` / `lock_contended` | rollback; none |
| 12 | The transaction took too long (S2 too old) | `stale_discarded` | rollback; none |
| after 13 | The engine moved before the reload (an ordinary advance) | rows are adopted at the next reload. The lead rule (§4) means the insertion point cannot be reached before then | none extra |
| after 13 | The engine left this log before the reload (rollover, Play Now) | the rows stay unplayed in a log that is no longer active. They are never played, never marked aired, and the ledger records `committed` only | none |
| after 13 | The operator holds Manual, or seeks back, before the rows air | the rows wait their turn. If the delay pushes them or the closing block past the queue swap 30 s before the hour, the swap drops them, exactly as it drops any unplayed row | as for any scheduled row |
| after 13 | The process crashes or restarts before the reload | the restart loads the log from the database, rows included. The ledger key blocks a second install for the same cause set, and a fresh net counts the installed rows | none extra |

**Guarantees:**

* At most one committed install per (log, qualified cause set).
* A rolled-back attempt leaves zero new rows.
* Committed rows are either ahead of the cursor at adoption, so they play in
  order before the closing block, or behind it, so they never play.
* No existing row's Track, category, origin, claim, `played_at`, request or
  intro link changes.
* The closing block keeps its identities and relative order.

**Non-guarantees:**

* Committed rows are not guaranteed to air.
* Database and GLib memory are not committed atomically: memory catches up
  within one reload.
* An in-flight database call can outlive an engine shutdown.
* Phase A cause loss in a crash is unchanged.

## 4. Timing rules (fresh, on S2)

* **Net and threshold:** recomputed from S2 with Phase B arithmetic. The
  rules are unchanged: more than 60 s if any qualifying cause is a request,
  more than 30 s for Eject-only sets.
* **Safety budget:** after the installed items, the window must still have
  at least **5.0 s + 1.0 s per inserted item** left over (7 s for one track,
  8 s for two, 9 s with a PSA). Phase B's runway already includes one
  250 ms poll tick per transition and the trigger clamp. The budget is for
  what it does not model:
  * the first item of the next hour or of the closing block may have a
    cue-in (Phase B assumes 0 for the final transition);
  * per-deck pipeline preroll from creation to the first buffer;
  * `next_start` analysis error on newly inserted Tracks;
  * the time between S2 and COMMIT (at most 2 s).

  An exact-zero Phase B residual therefore never installs. These values are
  **provisional**: Phase D must check them against as-run `PlayEvent`
  timings before activation. They are not a proven universal bound.
* **Insertion lead:** the trigger that will read the first inserted row must
  be at least **20 s** after S2. That trigger is S2 plus the prefix runway,
  minus the new row's own cue-in: the engine reads the next row at its
  trigger, not at its audible start. The calculation removes the fit model's
  optional 250 ms late-poll allowance for every prefix item because this
  deadline must use the earliest possible trigger. It covers:
  * at most 2 s from S2 to COMMIT;
  * the 3 s reload throttle plus the reload's own query;
  * `CACHE_WARM_LEAD_SECONDS` (3 s) before the trigger;
  * margin.

  So the reload always adopts the rows before the preceding item reaches its
  trigger, and before an `insert_before` anchor can be claimed. The engine
  claims an occurrence with a 250 ms row lock, so the installer never touches
  a row the engine could claim during the transaction.

## 5. Placement and persistence

* **`tail_append`:** new rows at `last.position + 1 ...` after the final
  music row, which must still be the last row under the lock. No existing
  row is updated.
* **`insert_before`:** under the lock, rows with `position >= anchor.position`
  must be exactly S2's closing block, in order, unclaimed and unplayed, with
  no request or intro reference. They are shifted by `k` (the number of
  inserted rows, at most 3) in two passes: `+100000`, then `-(100000-k)`.
  This is the same collision-free technique as
  `PlaybackEngine._splice_log_item_db_at`, and touches only that block. The
  new rows take the freed positions `anchor.position ...`. There is no
  fallback to `tail_append`.
* **New rows:**
  * `origin=refill_installed`, a new choice that is not eligible for Phase
    A Eject causes;
  * `category` is the music source category, or the PSA category for the
    PSA;
  * Track title and artist snapshots;
  * `scheduled_time` is the projected start;
  * claim and `played_at` are left NULL.
* **Untouched:** `SongRequest`, `PlayEvent` and segments, forced items and
  decks.
* **Ledger** (`HourRefillInstall`, append-only, no foreign keys):
  * `install_key` is unique:
    `refill:log<id>:causes:<sha256 of the sorted qualified cause ids>`;
  * it also records the cause ids, placement mode and anchor, inserted
    LogItem ids, Track ids, net before, residual and budget, engine
    instance, snapshot digest and `state='committed'` (check constraint);
  * no listener data.
* **Cause sets:** an identical cause set (including repeated A→B evidence,
  which Phase A already de-duplicates into one cause) maps to the same key,
  so it installs at most once. A newly expanded set has a new key, but its
  fresh net counts every installed row, so it fills only what is still
  missing.

## 6. Migration 0095

* `CreateModel HourRefillInstall`: the Update Center classifies this as
  additive.
* `AlterField LogItem.origin` (choices only): non-database metadata, also
  additive.
* No data migration or default. Old code never reads the new table and
  never writes the new origin value.

## 7. Limitations (for Phase D)

* The timing budget and lead are provisional and need field verification.
* Protected closers depend on correct category-kind filing.
* PSA configuration is supplied by the caller.
* No automatic invocation exists. Phase D must add the single caller, its
  cadence and operator visibility.
