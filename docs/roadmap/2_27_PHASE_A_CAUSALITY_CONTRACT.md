# Roadmap 2.27 Phase A — hour-refill causality contract

Status: **Phase A, evidence only.** Baseline r0119 (`9e7adc0`). Nothing in this
phase selects, installs, appends, reorders or plays anything. The engine's
on-air behaviour is unchanged from r0119: its only additions are in-memory
reads and one bounded `put_nowait` handoff, and only when capture is On.

This document defines what counts as a **cause** for a later skip-aware or
request-aware hour refill, how a cause is captured and stored, and what Phase A
does *not* know. Phase B (boundary-aware projection) consumes these records;
it is not built here.

## 1. Vocabulary

| Term | Meaning |
| --- | --- |
| **Cause** | One immutable record saying a specific, positively-identified event shortened the programming planned for a specific `LogItem`. Stored in `library.HourRefillCause`. |
| **Observed shortening** | The seconds that event removed from that one occurrence's planned listener-facing airtime. It is a per-cause fact. |
| **Net hour deficit** | How far the whole hour's committed programming now falls short of its *safe boundary*, allowing for existing overfill, crossfades, forced dedication pairs, partial-hour takeover and already-installed fill. **Phase A never computes this.** Phase B does. |
| **Effective airtime** | `library.services.log_builder.effective_airtime_seconds(track)` = `max(0, (next_start or duration) − cue_in)`. The only airtime definition causes may use. |

Observed shortening is **not** permission to fill. Example (illustrative only,
not runtime evidence): a 5:00 song replaced by a 3:30 request is 1:30 of
observed shortening; if the hour was already 0:50 over its boundary, the net
deficit is 0:40 and a request-triggered music refill must not happen.

## 2. Eligible causes (v1)

### 2.1 `request_substitution` — implemented in Phase A

Eligible **only** when, inside the committed `maybe_schedule_song_request()`
transaction, an approved, unclaimed, unplayed **music** `LogItem` changes from
Track A to a **different** Track B **and** `effective_airtime(B) <
effective_airtime(A)` (compared after rounding to milliseconds).

Captured, immediately before `locked_item.track` is overwritten:

* source key `request_substitution:li<log_item>:req<request>:t<A>-<B>`;
* `PlaylistLog` id, date and hour; `LogItem` id and position;
* the **scheduled** `LogItem.category` (id, code, kind code) — never the
  requested Track's own primary category;
* Track A id/title/artist snapshot and its effective airtime;
* Track B id and its effective airtime;
* the primary `SongRequest` internal id (no name, message, IP or text);
* `occurred_at` = the scheduling transaction's `now`.

A swap into a **future** hour is recorded with that hour's date/hour. It is a
future cause only; it never authorises repair of the current hour.

### 2.2 `operator_eject` — implemented (best effort, via the evidence worker)

Eligible only for a **completed** Eject: the exact deck generation the command
targeted (honouring `expected_log_item_id`), `claim_completion("eject")`
returned true, and **all** of the following:

* the occurrence's `LogItem.origin` is `generated_schedule` (§7.1) on a fresh
  deck (no `continuation_reason`: not a restart resume or seek recreation);
* its **scheduled** `LogItem.category` kind is `music`;
* it is already on air (`air_start_recorded`, the first real buffer committed);
* it is the only live deck (no crossfade or overlap, **either** deck);
* no Start Next transition existed when the Eject arrived, and nothing else
  owned "what starts next" (`_next_triggered`: VT, Start Next, a held trigger);
* no Voice Track phase, Manual mode, live microphone (local or remote) or
  connected Remote DJ session;
* the deck is not paused; and the **remaining listener-facing airtime** at the
  Eject instant is positive:

```
remaining = (next_start or duration) − max(position, cue_in)      (> 0)
```

Full file length is never used as the amount lost. The identity is
`operator_eject:<engine_instance>:<slot>:g<deck_generation>:li<log_item>`;
the deck generation is a per-process serial, so engine instance + generation is
unique across restarts.

Every input is read from memory; anything not already loaded (for example
the category of a restart-resumed item, which is loaded without it) fails
closed. The live hook is wired; see §7.

## 3. Not causes in v1

Start Next (any fade), natural EOS, automatic crossfade, future reorder /
force-next (`api_engine_set_next`), explicit insertion (`api_engine_insert_track`),
missed / poisoned / unplayable / failed media, Manual or Remote DJ hold,
dedication intro preparation, request submission alone, a request that only
reaches `scheduled` without changing the Track (organic match or collapse onto
an already-reserved slot), an equal or longer substitution, a same-Track
"swap", a claimed or played slot, lock contention, a rolled-back transaction,
an Eject that was only queued, an Eject of an empty slot, a stale/mismatched
`expected_log_item_id`, a repeated completion (`claim_completion` false), an
Eject of **either** deck during a crossfade or overlap, an Eject during a Start
Next or VT transition, an Eject of a non-music item, and an Eject of any
occurrence whose origin is not `generated_schedule` (operator inserted, live
fill, build-time fill, engine inserted, unknown/legacy) or which is a
continuation deck.

## 4. Thresholds

The web-request **music** refill rule is strictly `net_deficit > 60 s`
(`REQUEST_MUSIC_TRIGGER_THRESHOLD_SECONDS`, `request_music_threshold_exceeded`).
Exactly 60 s does not qualify. The rule applies to a **net** deficit in Phase
B; it is never applied at cause-capture time. A 10-second shortening is still
recorded as a cause.

## 5. Storage, identity, idempotency

* `HourRefillCause` is append-only. Rows are inserted and never updated
  (`save()` refuses an update). It holds **no foreign keys**: every reference
  is an immutable integer/text snapshot. That means deleting a Track, LogItem,
  PlaylistLog or Category never erases the cause; the insert takes no
  `FOR KEY SHARE` lock on referenced rows; and no deferred FK check can fail at
  `COMMIT` and roll back the request swap.
* `source_key` is `UNIQUE`. A replayed or retried event cannot create a second
  row. For request swaps the swap and its cause commit in the same transaction,
  and a re-run sees the slot already reserved and does not swap again. If an
  operator manually restores A and later the same request changes the same slot
  A→B again, the existing row remains the one occurrence-level cause: the final
  schedule is shortened once, not twice, and the Phase A trace already
  deduplicates causes by `(cause_type, log_item_id)` for that reason.
* Check constraints require positive observed shortening, the request fields
  for `request_substitution`, the engine fields for `operator_eject`, and
  `state = 'recorded'`.
* `state` has one value in Phase A: `recorded` (evidence only). It cannot be
  confused with installed filler (`LogItem`) or aired content
  (`PlayEvent`/`PlayEventSegment`). `played_at` is never read as completion.
* Append-only is enforced by `HourRefillCause.save()` and by keeping the model
  out of editing surfaces. PostgreSQL does not have an update/delete trigger;
  direct SQL and `QuerySet.update()` can mutate a row. That explicit Phase A
  limitation is acceptable because no runtime consumer exists yet.
* Ordering for combining causes: `(occurred_at, id)`. The primary key is the
  commit-order tiebreaker.

### Policy

`HourRefillPolicy` is a pk=1 singleton, separate from `LogFillConfig`:
`enabled`, `capture_request_substitution`, `capture_operator_eject` (all default
**Off**) and `dry_run_only` (default true, and a check constraint forbids
false in Phase A). Capture code reads it without creating it; a missing row
means Off. Setting any flag in Phase A only enables **evidence capture**.
There is no consumer, install hook or live action whatever the row says.
`LogFillConfig` and the 1.1 live-fill path are untouched.

## 6. Request-swap transaction, lock order and fail-safe

The non-realtime `refresh_song_request_statuses` reconciler uses the unchanged
r0119 sequence with one addition (step 6):

1. `SET LOCAL lock_timeout = '250ms'`.
2. Lock `PlaylistLog` (approved) → lock `LogItem` (`of=("self",)`).
3. Re-validate the fresh row (unclaimed, unplayed, music, has Track).
4. Capacity check; pick the oldest eligible waiting request
   (`skip_locked`).
5. Overwrite the LogItem's Track and mark the request `scheduled`.
6. **New:** only if the policy row says capture is on, and the Track really
   changed to a strictly shorter one: in a **savepoint**, insert one
   `HourRefillCause`. Any exception, including a unique-key duplicate or a
   250 ms lock timeout on the cause's unique index, rolls back **only the
   savepoint**, prints a line and (after commit) emits a deduplicated
   `SystemEvent` warning with internal ids only. The swap proceeds exactly as
   r0119 would.
7. Collapse step, return the locked row.

Consequences:

* A committed swap has at most one cause, atomically. A rolled-back swap has
  none.
* A cause-write failure never changes scheduling, never returns
  `SCHEDULING_CONTENDED` and never marks a request differently.
* Preparation (the policy read and, when capture is on, the displaced Track
  read) runs in its own savepoint and also never raises. So even "code
  deployed before its migration" (relation missing) leaves the swap intact.
* The engine's last-second playback-control caller never uses the in-transaction
  path (Codex `10ca80e`). With request capture Off (the worker's cached flag) it
  calls `maybe_schedule_song_request(log_item)` exactly as r0119. With capture
  On it passes `refill_evidence_sink=recorder.offer`; the swap then builds the
  evidence **from objects it already holds** -- the slot category and kind from
  the locked row's `select_related`, the new Track from the candidate's
  `select_related`, the displaced Track from the engine's own queue object, used
  only if it is provably the same Track the locked row still holds -- and,
  after the swap's writes, registers `transaction.on_commit(sink, robust=True)`.
  No SQL, savepoint, lock or lazy load is added (tested by query count). A
  rolled-back swap discards the callback, so it cannot create a false cause.
  Anything not in memory fails closed: no evidence, swap unchanged.
* With the policy Off (or no policy row), the non-realtime periodic reconciler's
  opt-in path performs `SAVEPOINT` + one primary-key policy read + `RELEASE`,
  only when it is actually making a swap. There is no insert.
* The displaced Track row is read (one primary-key `SELECT`) only when capture
  is on. No network, file or solver work happens under the lock.
* Collapsed requests are not listed on the cause; they are recoverable from
  `SongRequest.log_item_id`.

### 6.1 Request coverage

| Caller | Thread / transaction | Evidence path | Durability |
| --- | --- | --- | --- |
| `refresh_song_request_statuses` (run only by `ingest_web_requests`, systemd timer every 20 s, after a successful public-site fetch) | management command; the swap's own transaction | in-transaction savepoint insert (§6) | atomic with the swap |
| `PlaybackEngine._start_next_track` last-second safety net (non-forced, non-resume items) | GLib thread; the swap's own transaction | in-memory evidence, on-commit `put_nowait` to the evidence worker | best effort: lost on queue full, worker down, policy unreadable, insert failure or hard crash before the write |

There are no other callers. Not proven from source: what share of real swaps
each path performs. The reconciler only runs after a successful public-site
fetch (`ingest_web_requests` raises before calling it otherwise), so during a
public-site outage every swap is an engine-path swap. Phase B must therefore
treat request evidence as **non-exhaustive**.

## 7. Operator Eject capture

Map (r0119):

`api_engine_deck_command` (authorises `playout.control`, validates
`expected_log_item_id`) → `_enqueue_engine_command_response` (an **accepted
command**, nothing more) → engine `_dispatch_engine_command` `deck_eject` →
`PlaybackEngine._eject_deck(slot, expected_log_item_id)` on the GLib thread:
binding check → `_start_next_cancel_for_operator_command` → empty-slot / VT
branches → `deck.claim_completion("eject")` (**the completion boundary**) →
`_retire_deck_respecting_gated_seek` → `_remove_deck` (freezes duration at the
unlink boundary, then `_persist_deck_duration(termination_reason="eject")`, a
synchronous DB transaction on the GLib thread) → `_finish` (advance unless a
newer occurrence is live or VT owns the hand-off). A gated seek in flight
defers retirement to `_finish_deferred_retirement`.

The right capture point is in `_eject_deck` immediately after
`claim_completion("eject")` returns true and **before**
`_retire_deck_respecting_gated_seek`, reading the position with
`_get_deck_position(deck)` (a GStreamer query, no DB). Building the envelope
there is pure and cheap.

Implemented exactly there. `_eject_deck` records whether a Start Next
transition existed and whether `_next_triggered` was set **before** it cancels
Start Next, then, only after `claim_completion("eject")` succeeds, calls
`_offer_eject_refill_evidence`. With capture Off (the default) that is one
attribute read. With capture On it runs `eject_evidence_from_engine`: cheap
in-memory exclusions first, then one `_get_deck_position` GStreamer query, then
one `put_nowait`. The whole call is wrapped so a snapshot error is counted and
can never affect the Eject; retirement, advancement, VT hand-off and duration
accounting are untouched. **Database operations added to the engine thread:
zero** (tested by running every Eject case under a connection blocker).

### 7.1 Occurrence provenance (`LogItem.origin`)

Set once, at creation, by the writers that can prove it; never derived from
position (operator inserts start at 9999 and are renumbered) or category:

| Writer | origin |
| --- | --- |
| `build_hour_log` (engine auto-build, admin rebuild) | `generated_schedule` |
| LogFillConfig fill picks during a build | `generated_fill` |
| `append_fill_items` (async live fill) | `live_fill` |
| `api_engine_insert_track` | `operator_inserted` |
| engine `_splice_log_item_db_at` (urgent, native, dedication intro) | `engine_inserted` |
| Play Now playlist builds, admin-added rows, legacy rows, any other writer | NULL (unknown) |

Reorder (`api_engine_set_next`, `_reposition_items`) and request Track swaps
(`update_fields` excludes it) never change it; `editable=False` keeps it out of
admin forms. Only `generated_schedule` can qualify for an Eject cause.
Build-time fill is excluded conservatively. Migration 0094 is a nullable
`AddField` with no database default, so code from before the migration keeps
inserting rows (as NULL) during an upgrade.

### 7.2 Evidence worker (`library.services.hour_refill_evidence`)

One `HourRefillEvidenceRecorder` per `PlaybackEngine`, constructed in
`__init__`, started with the engine's other workers (idempotent `start()`),
stopped at the end of `stop()` **after** the main pipeline is NULL. One daemon
thread owns every evidence database operation:

* the engine side calls `offer()`: a `put_nowait` into a queue of at most 64,
  never waiting; when the worker is not running, or the queue is full, the
  event is dropped and counted without printing or other I/O on the engine
  thread;
* the worker reads the policy at start and every 30 s and publishes two
  booleans (`capture_requests`, `capture_eject`), which are the engine's only
  gate. They are False until the first successful read and after any failed
  one (fail closed). The worker re-reads the policy before each write and
  drops the event if that trigger is now Off;
* each write is one `HourRefillCause` insert; `IntegrityError` on
  `source_key` is counted as a duplicate; any other error drops the event;
  nothing is retried;
* `stop()` turns both gates off, then waits at most 1 s while the worker
  writes what is already queued. If a database call is still in flight, it is
  not cancelled, but after it returns the worker drops the remaining queue
  rather than continuing past the bounded drain window;
* worker-side policy/write/shutdown drops print a single line on the 1st and
  every 100th occurrence per reason. Engine-side queue/snapshot drops are
  counters only, so optional diagnostics never add playback-thread I/O.

A hard crash, SIGKILL or power loss loses whatever is still queued. This is
accepted (operator decision); there is no journal, synchronous fallback or
exactly-once delivery, and the recorder must not be described as durable.

## 8. Privacy

Causes and their diagnostics carry internal ids, Track title/artist (library
metadata) and numbers only. They never carry `requester_name`,
`dedication_message`, request text, IP addresses or anything from the public
request form.

## 9. Phase A trace (dry run)

`library.services.hour_refill_causality.build_causality_trace(records,
active_log_date=..., active_log_hour=...)` is pure. It orders causes by
`(occurred_at, id)`, drops repeated `source_key`s and a second cause of the
same type for the same LogItem (reported, not counted), separates causes for
other hours (future causes are listed but not summed), lists the original
scheduled categories in causal order (deduplicated, with each cause that
contributed), and reports `observed_shortening_seconds` for the active hour.
It always reports `net_deficit_seconds = None` and `authoritative = False`.
`load_cause_records()` reads rows read-only.

## 10. What Phase A does not know

* The safe boundary: partial-hour takeover, protected closers, legal IDs and
  hard posts. `scheduled_time` is an estimate and is not treated as a post.
* Existing overfill or underfill, the 10-second crossfade-trigger clamp, and
  forced dedication pairs in the runway.
* Whether the displaced Track would actually have aired in full (an operator
  could have ejected it later anyway).
* Eject causes and engine-path request causes are best effort (§6.1, §7.2):
  absence of a cause is not evidence that nothing happened.
* Build-time fill and Play Now occurrences never produce Eject causes.

## 11. Interface for Phase B (recommendation, not built)

Phase B should consume `CauseRecord` (the frozen dataclass in
`hour_refill_causality`) and nothing else from Phase A:

`id, source_key, cause_type, occurred_at, log_id, log_date, log_hour,
log_item_id, log_item_position, scheduled_category_id,
scheduled_category_code, scheduled_category_kind_code, original_track_id,
original_effective_airtime_seconds, replacement_track_id,
replacement_effective_airtime_seconds, observed_shortening_seconds,
song_request_id, engine_instance, deck_slot, deck_generation,
eject_position_seconds`.

The projector takes the set of unconsumed causes for the active approved log
plus a fresh engine snapshot. It computes the net deficit against a proven
boundary, and records its own outcome rows. It must not mutate
`HourRefillCause`. Consumption state belongs to a new Phase B/C table keyed by
cause id.
