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
2. Choosing a result shows the target and the replacement side by side with the length change. Nothing is
   sent until **Swap track** is pressed. Cancel / Escape changes nothing.
3. The browser sends `POST /api/engine/queue/replace/` with the row's `item_id`, the track it showed
   (`expected_track_id`), the replacement `track_id` and the `engine_instance` of the status it was
   rendered from. On success the dialog closes and the queue refreshes.

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
  (`LogItem.category` kind `music`) of an ordinary origin (generated schedule, build-time fill, live fill,
  operator insert or Hour Refill install -- never an engine insertion such as an urgent alert, Active
  Message or dedication intro, a DST replay, or a row without an origin), and **not reserved** by a
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
`_claim_playback_occurrence` therefore re-reads `track_id` under the same row lock it takes to claim: if
the committed track differs (and the row is not yet claimed) it claims nothing, and `_start_next_track`
re-reads the occurrence (`_reread_changed_occurrence`) and opens the committed track in the same slot. The
claim and the replacement serialize on the `LogItem` row lock: either the swap commits first (the engine
opens the replacement) or the claim does (the swap is refused as `claimed`). A deck that already holds a
track is never modified.

### Web Requests

A row reserved for a listener's request is refused, so no request is unlinked or falsely fulfilled. A
waiting request is unaffected. Request scheduling can still later reserve a swapped music slot for a
listener request (as it can an operator-inserted one); the listener's request then replaces the operator's
choice.

### Airtime

A Swap never inserts or tops up anything itself. It changes only what the engine's authoritative
committed runway reads after its next reload: `_committed_future_runway_seconds` sums the leading deck's
remaining playout and each future item's `effective_airtime_seconds` (`next_start - cue_in`), not raw
file durations. The established live fill (`_try_extend_live_log_async`) compares that runway with the real
time left before the top of the hour when the current hour's queue runs out, and fills only a genuine
shortage (beyond `DURATION_FIT_MARGIN`, or the end-of-hour closing fill), with its existing generation,
prior-count, Hour Refill coordinator and later-hour guards against duplicate or late insertion. A shorter
replacement that leaves enough runway adds nothing; a longer one creates no deficit. If the next hour's log
is already approved, the existing early-rollover policy applies instead, as for any other underrun.

Roadmap 2.27 Hour Refill is unchanged: a Swap is not a `request_substitution` cause, records no refill
evidence, and does not touch its policy, dry-run or arming controls.
