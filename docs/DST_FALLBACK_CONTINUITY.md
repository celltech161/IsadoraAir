# DST fall-back repeated-hour playout continuity (roadmap 1.21, P0)

## The outage (November 1, 2026, America/Chicago)

| UTC | Station time | What the engine did (r0130) |
|---|---|---|
| 06:59:30 | 01:59:30 CDT | The next-hour lookahead built and approved the 02:00 log and installed it early as the active queue (the established early rollover). |
| 07:00:00 | 01:00:00 CST | The clock went back. The 02:00 programming kept playing through the repeated hour: its occurrences aired (`played_at`, PlayEvents). |
| ≈07:58 | 01:58 CST | The queue ran out. "Next hour" still resolved to the same `(2026-11-01, 02)` log, so the rollover re-installed it at position 0 and handed out its first, already aired occurrence again (audio, uncounted). |
| 08:00:00 | 02:00:00 CST | The approved 02:00 log is now the current hour's authority, but every occurrence is played. Loaders put the cursor on the first unplayed item, which is past the end. Nothing followed until the 03:00 lookahead at 02:59:30. |

The result was about an hour of silence, without an engine restart.

**Confirmed causes** (`library/services/engine.py`):
* The lookahead (`_ensure_upcoming_logs` → `_advance_to_next_hour_log`) installs the 02:00 log at 01:59:30 CDT.
* `PlaylistLog(date, hour)`, `_current_hour_schedule_state` and `_peek_next_hour` all key on the wall label `(date, hour)`. The two physical 01:00 hours collapse into one key, so the "next hour" during the repeated hour is the active log itself.
* `_roll_over_to_next_hour` and `_advance_to_next_hour_log` set the cursor to 0.
* `_load_log_for` and the ~3 s `_reload_queue_if_changed` position the cursor after everything with `played_at`.

`played_at` is the one-way actual-air gate of a LogItem, and `PlayEvent.log_item_id_snapshot` is unique. An occurrence that has aired can never become a new, counted airing.

## The correction

**1. Transition identity: actual instants, never the wall label or `fold`**

`library.services.dst_fallback.fallback_successor(date, hour)` decides whether a log follows a fall-back repeated hour. That holds when, in the station time zone:
* the whole preceding wall hour is repeated: ambiguous at its first and last instant, with the earlier instance exactly one hour ahead;
* the log's own `hour:00` is unambiguous.

For America/Chicago, only `(2026-11-01, 2)` qualifies:
* repeated-hour instances 06:00 and 07:00 UTC;
* the log's real hour starts at 08:00 UTC (02:00 CST).

02:00 CST is *not* a `fold=1` time, so recognition never reads `now.fold`. Spring-forward gaps, ordinary days, other hours and half-hour shifts never qualify.

**2. A bounded window**

The replay window is `[08:00 UTC − 30 s, 09:00 UTC)`: from the moment an ordinary next hour would be installed early (the same 30 s lookahead) to the end of the real hour.
* *Before it,* during the repeated hour, the first pass plays exactly as before.
* *From 03:00 CST,* ordinary forward progression owns the queue.

**3. One replay pass of new occurrences**

`dst_fallback.ensure_replay`, called by the engine (`_ensure_dst_fallback_replay`):
* **When:** on its 10 s scheduler tick before idle recovery, and when the queue runs out.
* **Lock:** under the approved PlaylistLog's row lock, with a 250 ms bound. If the lock is busy, nothing is committed and it retries on the next tick.
* **What it creates, in one transaction:**
  * a `DstFallbackReplayPass` record: the authorization itself (see "Durable replay authority" below);
  * for every ordinary programming occurrence of the log that aired *before the real hour began*, one new LogItem in the same approved log:
    * the same Track, Category, title/artist snapshot and `scheduled_time`;
    * appended after every existing row, in order;
    * `origin = dst_replay`, `replay_pass` = the pass, and `replay_source_id_snapshot` = the id of the original occurrence it replays.
* **All or nothing:** any failure before commit (`failed`) or a lock timeout (`busy`) leaves neither the pass nor any occurrence behind.
* **What it never does:** it never rebuilds, edits or deletes the approved programming. Original rows, their `played_at` and their PlayEvents are not touched.

**4. Everything else is existing machinery**

The new rows are ordinary unplayed occurrences:
* each is claimed when a deck is prepared;
* each gets its own `played_at`, play count and PlayEvent at its own first real output buffer, exactly once, through the existing transaction;
* that PlayEvent's `source` is `scheduled` even when the occurrence was appended at position ≥ 9999: the engine classifies by provenance first (`replay_pass` set means it is scheduled programming), then by the established position rule for every other row. So a replay appended after a position-9999 insert is `scheduled`, and the insert itself stays `insert`;
* the ~3 s queue reload and every restart (`_load_log_for`: first unplayed) put the cursor on them;
* duplicate air-start callbacks cannot count twice.

**5. No third airing**

The pass record is the durable authority. Once it exists, no process ever creates another for that log and hour, whatever happens to the occurrences. After any restart it is found again: `already_handled`, or `damaged` if occurrences were deleted or changed. A completed pass leaves nothing unplayed.

**6. Two narrow queue-safety corrections that apply everywhere**

* **The active log is never its own next hour** (`_is_active_log` in `_peek_next_hour`). Only the fall-back repeated hour makes the next wall hour resolve back to the active log. In ordinary hours the next wall hour is always a different log.
* **Every queue installation starts at its first unplayed occurrence** (`_first_unplayed_index`, already the rule of `_load_log_for`). A rollover or advance therefore never hands out an aired occurrence as fresh. For every ordinary next-hour queue nothing has aired, so this is position 0, exactly as before.

### Not replayed

These keep their single first-pass airing, so no one-shot action is credited twice:
* occurrences that never aired, or that aired in the real hour;
* operator and engine inserts: urgent alerts, native Active Messages, dedication intros, Play Now — any origin other than generated schedule, build-time fill or live fill;
* Hour Refill installs;
* positions ≥ 9999;
* the Dedications category;
* Active Messages aggregate Tracks;
* rows a web request or its dedication intro is attached to.

### Durable replay authority (migration `library/0100_dst_fallback_replay_pass`)

The first implementation (`dc8432b`, `1c3ae46`) inferred a replay from an `engine_inserted` row matching an original's (Track, Category, `scheduled_time`). Codex showed that inference was not authoritative:
* an unrelated identical engine insert suppressed the pass;
* edited or deleted replay rows let a new pass be authorized;
* duplicate originals collapsed into one tuple.

**What replaced it:**
* **`DstFallbackReplayPass`** (new table): one row per approved PlaylistLog and physical successor hour.
  * *Database-enforced:* the unique constraint `library_dst_replay_pass_once_per_log_hour` on `(playlist_log, successor_hour_start)`, where `successor_hour_start` is the real UTC start (08:00 UTC).
  * *Contents:* the transition instant, time zone, authorization instant and trigger, and exactly what it authorized (`source_log_item_ids`, `replay_count`).
  * *Lifetime:* cascades with its PlaylistLog. While any of its occurrences exist it cannot be deleted on its own (`RESTRICT`).
* **LogItem provenance** (new nullable, non-editable columns):
  * `replay_pass` and `replay_source_id_snapshot` are set once by the engine.
  * The id snapshot survives deletion of the original.
  * A partial unique constraint (`library_logitem_one_replay_per_source`) gives one replay per original per pass, so duplicate originals with identical fields stay two sources.
  * A check constraint (`library_logitem_dst_replay_provenance`) makes `origin = dst_replay` and the provenance columns exist only together.

**Why mutable fields no longer decide anything:**
* Authorization is decided by the pass record alone, never by any LogItem field.
* An identical foreign insert is just another row.
* Editing a replay occurrence's Track, Category or time leaves the pass in place. A request swap is an edit of this kind and a legitimate new fulfilment of a real airing.
* Deleting one, several or all replay occurrences also leaves the pass in place.
* A pass whose occurrences were deleted or had their Track or Category changed is reported as **`damaged`** (one SystemEvent per process, with the missing source ids and the changed replay ids). It is never re-created: an operator who deleted replay rows keeps them off the air.

**Origin.**
* `dst_replay` is a new `LogItem.origin` value: a special, engine-authorized new occurrence derived from approved scheduled programming.
* *Hour Refill:* its causality code treats only `generated_schedule` as scheduled programming and every other origin as a non-cause source. A replay is therefore never an Eject cause, exactly like an `engine_inserted` row.
* *Other consumers:* no other code interprets `origin`. Log building, live fill, web-request substitution, dedications, Active Messages and urgent inserts do not.
* *Admin:* the provenance columns are `editable=False`, so the LogItem admin inline does not expose them.
* *Origin versus PlayEvent source:* `origin` and the PlayEvent `source` serve different purposes. The origin says who created the occurrence; the source says it aired as scheduled programming.

**Schema and release implications.**
* The migration is additive: a new table, two nullable LogItem columns with no database default (so r0130 code keeps inserting LogItems during an upgrade), the `dst_replay` choice (no column change), and two constraints that every existing row satisfies.
* Nothing is backfilled or inferred from historical rows.
* Reverting to r0130 code leaves the new table and columns unused and harmless. Removing them requires reversing the migration (`migrate library 0099`), which drops the table and columns; DST replay occurrences would then become ordinary rows.
* The r0131 release must authorize this migration through the trusted Update Center plan.

### Event

The first time a process authorizes or finds a pass, it emits one `engine` SystemEvent:
* *Titles:*
  * "DST fall-back: approved hour replay pass authorized" (warning);
  * "… already handled" (info);
  * "… replay pass damaged (occurrences deleted or changed; not re-created)" (warning).

  A creation that failed before commit emits "… replay pass creation failed (nothing committed)" (error). A busy lock emits nothing and retries.
* *Dedupe key:* `engine|dst-fallback-replay|<log id>|<outcome>`.
* *Detail:*
  * the time zone;
  * the repeated local hour, transition instant, real hour start and UTC offsets;
  * the replay window;
  * the PlaylistLog id;
  * the pass id, the source and replay LogItem ids, and for a damaged pass the missing source ids and the changed replay ids;
  * the reason, the trigger and the observed instant.

There is no per-tick event.

## Deliberate exceptions to existing invariants

* **Occurrences added to an approved log.** The engine adds replay occurrences to an approved log, as it already does for urgent alerts and dedication intros. The approved programming is reused, never rebuilt.
* **A PlayEvent source decided by provenance.** A replay occurrence's PlayEvent is `scheduled` regardless of its position. Every other row keeps the position ≥ 9999 rule.
* **One programmed item airs twice.** A programmed item can air twice on the fall-back night, once per physical hour. Each airing is its own LogItem occurrence with its own evidence. No occurrence airs twice, and no non-DST hour is affected.

## Limitations

* **Early exhaustion.** If the first pass runs out earlier in the repeated hour than the lookahead (an underfilled log), the gap until the window opens is handled by the existing live-fill path, as on any other day.
* **Occurrences prepared but not yet aired.** An occurrence on a deck, claimed but not yet aired when the pass is materialized, is not replayed. It airs once.
* **Restart during the repeated hour without a fresh resume snapshot.** The wall clock resolves the 01:00 log, as before: its unplayed tail airs, then the 02:00 log continues at its first unplayed occurrence.
* **Where it applies.** Only whole-hour fall-backs are handled; a half-hour shift (Lord Howe) is not recognized.
