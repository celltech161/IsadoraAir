# Roadmap 2.27 Phase A — hour-refill causality contract

Status: **Phase A, evidence only.** Baseline r0119 (`9e7adc0`). Nothing in this
phase selects, installs, appends, reorders or plays anything. The engine's
on-air behaviour is unchanged from r0119.

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

### 2.2 `operator_eject` — contract defined, live capture **deferred**

Eligible only for a **completed** Eject: the exact deck generation the command
targeted (honouring `expected_log_item_id`), `claim_completion("eject")`
returned true, the occurrence is a scheduled music `LogItem` that is already on
air (first real buffer recorded), it is the only live deck (no crossfade or
overlap), no Voice Track transition, Manual hold or Remote DJ ownership is
active, the deck is not paused, and the **remaining listener-facing airtime**
at the Eject instant is positive:

```
remaining = (next_start or duration) − max(position, cue_in)      (> 0)
```

Full file length is never used as the amount lost. The identity is
`operator_eject:<engine_instance>:<slot>:g<deck_generation>:li<log_item>`;
the deck generation is a per-process serial, so engine instance + generation is
unique across restarts.

The classifier (`classify_eject_observation`) and its fixtures are implemented.
The live hook is **not** wired; see §7.

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
Eject of the outgoing deck of a crossfade, an Eject during a VT transition and
an Eject of a non-music item.

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
* The engine's last-second playback-control caller does **not** opt in. Its
  default path performs no Phase A policy query, savepoint, displaced-Track
  lookup or cause insert, whether the policy row is On or Off. It is SQL/lock
  equivalent to r0119. If only that last-second safety net performs a swap, the
  cause can be missed; this is an intentional best-effort evidence tradeoff.
* With the policy Off (or no policy row), the non-realtime periodic reconciler's
  opt-in path performs `SAVEPOINT` + one primary-key policy read + `RELEASE`,
  only when it is actually making a swap. There is no insert.
* The displaced Track row is read (one primary-key `SELECT`) only when capture
  is on. No network, file or solver work happens under the lock.
* Collapsed requests are not listed on the cause; they are recoverable from
  `SongRequest.log_item_id`.

## 7. Eject capture: why the live hook is deferred

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

The blocker is **reliable scheduled-occurrence provenance**:

* Writing on the GLib thread adds a blocking DB round trip to the Eject path.
  The workorder forbids that.
* Joining the existing `_persist_deck_duration` transaction would add the
  insert to a transaction that is already synchronous on the GLib thread. That
  is still an added blocking statement. It also couples the evidence to a path
  that returns `False` with no `PlayEvent`, and would change the
  accounting-path code the workorder says not to change.
* Handing the envelope to a bounded background writer is nonblocking. An
  immediate hard engine-process crash may lose the queued cause; that is an
  explicitly accepted v1 limitation. No journal, synchronous fallback or
  exactly-once replay is required. An orderly stop should attempt only a short,
  bounded drain.
* A scheduled and an operator-inserted music `LogItem` can become
  indistinguishable after queue position normalization. Phase A must not invent
  that provenance. Any Eject during an active overlap/crossfade is excluded for
  both incoming and outgoing decks, and Start Next itself is never a cause.

Phase A therefore ships the envelope, the source key, the classifier and its
fixtures, and **no engine change**. The minimal remaining prerequisite is a
durable, conservative scheduled-versus-inserted occurrence marker. Once that
exists, a single-purpose bounded in-process queue and one persistence worker
can be added without any ORM work on the engine thread; hard-crash replay is
not required for v1.

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
* For Eject: everything in §7.

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
