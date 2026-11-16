# Console queue: NEXT placement and Swap

## Layout

The Console and `/remote-dj/` "Coming Up" table columns are:

`Drag | NEXT | Swap | # | Artist | Title | Cat | Dur | Intro | Time On`

NEXT is the existing pill (`setNext()` → `POST /api/engine/queue/set-next/`, unchanged). NEXT, Swap and
the drag handle appear only on upcoming rows (the engine's live queue). Played, on-air and on-deck rows
(Show played items) carry none of them. Pressing NEXT or Swap never starts a drag; dragging starts only
from the handle. While the console is stale (no status for 3 s) every queue control is disabled.

## Swap

The Swap button (two opposed arrows, `currentColor`) replaces **one upcoming occurrence in place** with
another track:

1. Swap opens a dialog showing the target row and a search field. The search is the same request, filter
   (Ready to Air) and result summary as **Search to add track** (`fetchQueueSearchResults`).
2. Choosing a result shows the target and the replacement side by side with the **planned effective
   airtime** change, read from the server (`GET /api/engine/queue/replace/?item_id=&track_id=`): next start
   minus cue-in, or the file length where a track has no next-start marker (said so), or "can't be estimated"
   when neither is set. A shortening says IsadoraAir will evaluate the remaining hour with its existing
   scheduling and fill rules and that additional material is **not guaranteed**. Nothing is sent until
   **Swap track** is pressed. Cancel / Escape changes nothing.
3. The browser sends `POST /api/engine/queue/replace/` with the row's `item_id`, the track it showed
   (`expected_track_id`), the replacement `track_id` and the `engine_instance` of the status it was
   rendered from. On `200 {ok}` the dialog closes and the queue refreshes. Any 4xx is a definite refusal
   (the server refuses before changing anything). Anything else -- a network error, a 5xx, an unreadable
   reply -- leaves the outcome **unknown**: the request is never repeated automatically. After a short delay
   the dialog reads the row's committed state (GET above, from the database, not the engine's ~3 s
   snapshot): the chosen track confirms the swap; the original track (still unclaimed) means it was not
   applied and may be tried again explicitly (a repeat of a swap that did commit is an idempotent no-op);
   anything else -- a different track, the row gone, the station unreachable -- stays unknown with
   **Check again**.

The dialog is bound to the row it was opened for. If that row leaves the queue (the engine started loading
it), changes track, or the engine instance changes, Swap track is disabled and the reason is shown; a final
refusal from the server is shown the same way. A response to an older search never replaces newer results,
and a second press while a request is in flight sends nothing.

The button is enabled when the account holds `playout.queue_manage` **and** its GroupAccess reaches
`/api/engine/queue/replace/` (`CONSOLE_CAPS.queue_swap`, presentation only). The seeded `remote_dj` group
lists exact paths and does not include this endpoint, so on `/remote-dj/` Swap is shown but unavailable
until an operator grants the path.

### Server contract (`library.services.queue_replace`)

`playout.queue_manage` with the real schedule policy, CSRF required. The browser's display is never the
authority; every part is re-checked:

- the engine's published state (`/run/isadoraair/engine_state.json`) must be **fresh** (≤ 5 s), from the
  **same engine instance**, list the item in its live upcoming queue, and not hold it on a deck;
- then, under row locks in the Web Requests / live-fill order (`PlaylistLog`, then `LogItem`, 1 s lock
  wait), the occurrence must be unplayed and **unclaimed** (`playback_claimed_at` is set by the engine
  before any deck opens), in an **approved** log, still carrying `expected_track_id`, a **music** slot
  (`LogItem.category` kind `music`) of an ordinary origin (generated schedule, build-time fill or operator
  insert -- never an engine insertion such as an urgent alert, Active Message or dedication intro, a DST
  replay, a **live fill** row (it may be a deadline-qualified closing fill), an **Hour Refill install**
  (bound to its immutable ledger), or a row without an origin), and **not reserved** by a
  listener's `SongRequest` (`status="scheduled"`, which also covers dedication follow-up songs);
- the replacement must satisfy Web Requests' `track_is_available`: ready to air, music, media file present.

Only `track`, `track_title` and `track_artist` change -- the same three fields a Web Requests swap writes.
The LogItem id, position, `scheduled_time`, `category` and `origin` are preserved and nothing is inserted.
`LogItem.category` is the slot's **scheduling provenance** (what the builder, live fill and Hour Refill
read); the Console's Cat column shows the replacement **track's** category. A repeated request after a
committed replacement answers `changed: false`. Refusals answer `{ok: false, reason, error}`: 409 for a
stale or protected row (`engine_unavailable`, `engine_changed`, `not_upcoming`, `on_deck`, `claimed`,
`played`, `log_not_live`, `item_changed`, `not_replaceable`, `request_reserved`, `contended`), 404
`track_not_found`, 422 `track_ineligible`. A committed swap is recorded as the Monitoring event "Queue
item swapped by operator" (row, previous and new track, user, effective-airtime change).

`PATCH /api/log/item/<id>/swap/` (draft logs, `schedule.edit`) is unchanged and is not used for this.

### Engine adoption and the claim race

The engine re-reads the active log every ~3 s (`_reload_queue_if_changed`). A replacement that commits
after that read but before the occurrence starts would otherwise be opened from the stale in-memory copy.
`_claim_playback_occurrence` therefore re-reads `track_id` under the same row lock it takes to claim and,
if the committed track differs (and the row is not yet claimed), **adopts it atomically with the claim**:
the committed Track is loaded and checked with the ordinary playability gate inside that transaction, the
in-memory occurrence takes it, and `_create_deck` opens what the claimed row commits to. However many swaps
committed before the claim, the claim reads the last one; once claimed, no swap is accepted. A committed
replacement that is not playable keeps the existing unplayable-occurrence handling.

A claim that cannot take the row lock within its 250 ms wait (a swap or Web Request transaction is writing
the row) is **contended**, not failed: `_start_next_track` puts the same occurrence back as the next item
(the cursor, or the front of the forced list, exactly where it came from) and arms one 300 ms retry; the
crossfade trigger may also retry on its next tick, whichever runs first. Nothing else is skipped or
claimed. The retry is inert after shutdown, in Manual mode (the occurrence simply stays next), after a
rollover or any start that consumed it. After 8 consecutive contended attempts the occurrence is skipped
with the error event "Occurrence skipped after persistent claim contention" (visible, bounded). An
already-claimed occurrence (resume, deck re-creation) keeps its existing handling. A deck that already
holds a track is never modified.

### Web Requests

A row reserved for a listener's request is refused, so no request is unlinked or falsely fulfilled. A
waiting request is unaffected. **Limitation (unchanged Web Requests priority):** while the occurrence is
still unclaimed, request scheduling may later reserve a swapped music slot for an eligible listener request
(as it can an operator-inserted one), and the listener's request then replaces the operator's choice. There
is no operator lock on a swapped row.

### Airtime

A Swap never inserts or tops up anything itself. It changes only what the engine's authoritative
committed runway reads after its next reload: `_committed_future_runway_seconds` sums the leading deck's
remaining playout and each future item's `effective_airtime_seconds` (`next_start - cue_in`), not raw
file durations. The established live fill (`_try_extend_live_log_async`) compares that runway with the real
time left before the top of the hour when the current hour's queue runs out, and fills only a genuine
shortage (beyond `DURATION_FIT_MARGIN`, or the end-of-hour closing fill), with its existing generation,
prior-count, Hour Refill coordinator and later-hour guards against duplicate or late insertion.

A live-fill proposal is bound to the queue it was computed from: the dispatcher records the ordered
`(LogItem id, Track id)` identity of the queue for that generation. The installer discards the proposal if
the in-memory queue differs, then -- in one transaction, holding the `PlaylistLog` lock (250 ms wait, the
same order as Swap, Web Requests and Hour Refill install) -- compares the committed queue (minus
poison-guarded rows, as the engine's queue is) and appends only if it is unchanged. A swap or reorder that
committed after dispatch therefore voids the proposal (a later dispatch re-evaluates the shortage); a lock
timeout discards it without inserting; the engine queue is extended only after the append commits. Writers
that take the `PlaylistLog` lock (Swap, Web Requests, Hour Refill install) are fully serialized with the
install. Operator insert, reorder and set-next do not take it: a change they commit before the install's
read is detected; one committing in the sub-millisecond window between that read and the commit is not
(reorder and set-next do not change airtime; an operator insert is a new operator action). A shorter
replacement that leaves enough runway adds nothing; a longer one creates no deficit. If the next hour's log
is already approved, the existing early-rollover policy applies instead, as for any other underrun.

Roadmap 2.27 Hour Refill is unchanged: a Swap is not a `request_substitution` cause, records no refill
evidence, and does not touch its policy, dry-run or arming controls.
