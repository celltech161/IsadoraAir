# Roadmap 2.27 Phase B — net-deficit projection and advisory refill proposals

Status: **Phase B, read-only proposals.** Base: accepted Phase A `172c524`.
Nothing in this phase inserts, appends, reorders, reserves, claims or plays
anything. `engine.py` is unchanged and nothing in the engine calls Phase B.
No migration. `HourRefillPolicy` keeps `dry_run_only=True` (Phase A check
constraint, unchanged).

Code: `library/services/hour_refill_projection.py` (snapshot, timing,
boundary proof, cause scoping, projection — pure) and
`library/services/hour_refill_proposal.py` (database reads, category
sources, strict candidate pools, deterministic search, PSA stage,
placement, result). Phase A's contract
(`2_27_PHASE_A_CAUSALITY_CONTRACT.md`) is unchanged: causes are best-effort
hints; Phase B recomputes the shortage from the queue, the decks and a
proven boundary, and never reads a missing cause as "nothing happened".

## 1. Snapshot (`PlaybackWindowSnapshot`)

`capture_playback_window(engine, now=...)` copies primitives out of engine
memory. It reads attributes and **already-loaded** ORM fields only
(`_loaded` / `_cached`, Phase A helpers), plus one
`engine._get_deck_position()` per live deck (a GStreamer query). No
database, lock, disk I/O, print or hashing. Anything not in memory is
recorded in `capture_errors`, which makes the snapshot invalid (tested under a
connection blocker). It is an adapter for a later worker; Phase B wires no
timer or call site into the engine.

| Field | Meaning |
| --- | --- |
| `captured_at` | aware station-local instant (naive => invalid) |
| `engine_instance` | `_engine_session_id` |
| `active_log_id/date/hour` | `current_log` |
| `cursor`, `queue` | `_queue_cursor` and every in-memory `log_items` row as `ItemTiming` |
| `forced` | `_forced_next_items` (dedication follow-up, urgent items) |
| `decks` | per occupied slot: generation, occurrence, position, live/paused, on-air, continuation |
| `poison_skip` | `_poison_skip_identities` |
| ownership | `manual_mode`, `remote_dj`, `mic_live` (`_any_mic_live()`), `vt_phase`, `start_next_active`, `next_triggered`, `resume_pending`, `live_fill_in_progress` |

`ItemTiming` carries LogItem id, log id, position, Track id, **slot**
`category_id` and its kind code (`None` = not loaded => unknown), `origin`,
cue-in / next-start / duration, and claimed / played flags.

`snapshot.digest()` is a SHA-256 over the active log, cursor, the ordered
queue `(id, log, position, track, category, claimed, played)`, forced items,
deck occupancy `(slot, generation, item, track)` and the poison set.
Playheads and the clock are excluded. A reorder, a same-count Track swap or a
new claim changes it. It is a proposal token, not a lock.

## 2. Boundary proof (`resolve_boundary`)

The boundary is the next programming takeover after the **active log's**
hour (`N` = active hour + 1):

1. an **approved** log for `N`: its earliest persisted `scheduled_time`,
   evaluated with the engine's own `PlaybackEngine._takeover_eligibility`
   (inside `N`'s hour => `partial_takeover` at that instant; otherwise
   `ordinary_next_hour` at `N:00`). An approved log with no items is
   **unknown**;
2. otherwise the live schedule resolution the engine itself uses before
   materialisation (`resolve_schedule_segments` for the active profile): the
   first segment's minute (0 => ordinary, >0 => partial);
3. otherwise (blank next hour, no profile) **unknown** => `no_safe_boundary`.

Nominal top-of-hour is never assumed: it is used only when one of those
sources says `N` starts at minute 0. Every v1 boundary is a **natural
handoff** (`docs/SCHEDULE_PROFILES.md`: the playing deck crosses, no hard
cut). Phase B still allows **zero** proposed overrun. `scheduled_time` of an
ordinary row is never treated as a post.

The active log owns the window when the wall hour equals its hour, or when
the wall hour is `N`, `N` is partial and its takeover is still ahead (a
continuation). Anything else (early rollover, a passed takeover, an ordinary
boundary for an older log) is `wrong_hour`. Windows that straddle a UTC
offset change, or use a wall hour that does not exist (checked by a real
UTC round trip), abstain. Windows of 60 s or less (`imminent_takeover`,
the queue is about to swap) or over 2 h abstain.

## 3. Runway (the engine's own trigger arithmetic)

The committed sequence is the single live deck, then forced items, then
`queue[cursor:]`. Each LogItem is counted once: the deck's occurrence is never
counted again, forced ids are removed from the queue slice (the dedication
song is in both), and poison-guarded and deleted-Track rows are skipped, as
the engine skips them.

For each element and its successor's cue-in `c` (unknown => 0),
`element_span` mirrors `_poll_position`:

```
trigger = end - c ; if trigger < 10: trigger = end      # the clamp
trigger = max(trigger, 5.0)                             # poll guard
trigger = max(trigger, position)                        # live deck only
span    = trigger + c - (position if deck else cue_in)  + 0.25
```

`end` is `next_start` or `duration`. In the ordinary case a queued span is
exactly `effective_airtime_seconds()`, and the deck's is `end - position`,
the same value as `_leading_deck_eta_seconds`. The clamp, the 5 s floor and
one 250 ms poll tick per transition are added, so the runway is an **upper
bound**. That errs toward less fill.

```
window = boundary.instant - captured_at
net_shortage_seconds = max(0, window - runway)     overfill = max(0, runway - window)
```

Never `original - replacement`, and never the sum of Eject remainders:
`observed_shortening_seconds` is reported separately as a diagnostic.
Queued items whose file has vanished are counted as runway; the engine
would skip them. That biases toward less fill.

## 4. Eligibility and outcomes

Evaluated in this order, with no writes for any outcome: `off`, `invalid_snapshot`,
`wrong_hour`, `blocked_transport` (Manual, Remote DJ, local or remote mic,
VT, Start Next, `_next_triggered`, pending resume, live fill in flight, paused
deck, two live decks), `unsafe_window` (no live deck, unknown or implausible
position, unreliable durations, DST, imminent takeover), `no_safe_boundary`,
`no_qualifying_cause` (net still reported), `already_satisfied` (net 0),
`below_threshold`, `eligible`.

**Cause scoping** reuses Phase A's `build_causality_trace` (causal order,
duplicate keys, a second cause of the same type for the same LogItem). A
cause then qualifies only if:

* it belongs to this `PlaylistLog` id, date and hour;
* it is a music slot;
* its occurrence is still in the in-memory queue;
* for a request, that slot still holds the replacement Track;
* for an Eject, the occurrence is behind the cursor and off the decks.

The reasons a cause is excluded are listed, and nothing is deleted: `other_hour`, `stale_log`,
`missing_occurrence`, `superseded`, `occurrence_still_pending`,
`not_music_slot`, `unknown_cause_type`, the duplicate reasons.

**Thresholds** (strictly greater than, applied to the combined net):

* any qualifying `request_substitution`: **> 60 s**; exactly 60.000 s is
  not eligible;
* Eject-only cause sets: **> 30 s**. This is a provisional Phase B policy
  choice aligned with the ordinary fit margin, not a roadmap requirement, and
  is for operator review.

**Feature Off.** Without `offline_dry_run=True`, `evaluate_hour_refill` reads
`HourRefillPolicy.enabled` (one query, without creating the row) and returns
`off`. The `offline_dry_run` flag runs the evaluation without touching the
persisted policy.

## 5. Allowed music categories

There are two sources, ranked in this order:

* **(A)** the scheduled `LogItem.category` of each qualifying cause, in
  causal order;
* **(B)** the category of the **last remaining music row** in play order
  (deck, forced, queue).

Source (B) ignores trailing PSA, legal ID, imaging, underwriting and talk
rows. If any row after it has an unknown kind, (B) is unknown.
Identical categories are merged and keep every provenance. Each category is
re-read to confirm it still exists and is `music`. `LogFillConfig`, the
requested Track's own category and any generic pool are never used. With
no usable source the result is `no_eligible_music_category`. Categories are
resolved before placement.

## 6. Strict candidates and deterministic search

Each source's pool is built with existing primitives:

* **Pool:** `_tracks_for_category`, which covers Ready-to-Air plus primary or
  additional category.
* **Excluded rows:** tracks blocked in any hour slot the window touches, and
  tracks tagged with an active holiday.
* **Recency:** the category's full `get_separation`, through
  `get_recent_exclusions` (actual airs only).

**Hard exclusions** are never relaxed (no `pick_track` loosening ladder):

* every Track in the active log, the decks and the forced items, plus all
  their artist identity keys (primary and related);
* the qualifying causes' original and replacement Tracks;
* Tracks of pending, no-slot or scheduled `SongRequest`s;
* the next approved hour's Tracks.

**Timing:** each candidate needs a reliable positive effective airtime of
at least 30 s and no more than the net shortage. A reserved spoken or
active-message file name is excluded.

**Pool limit:** at most 5,000 rows per source; truncation is reported.

**Search.** One- and two-track sequences only:

* every single candidate is tried;
* pairs: for each first track (ascending id), up to 8 valid partners at or
  below the remaining gap (bisect over effective airtime, at most 64
  scanned), both orders;
* bounded by a **count** budget of 50,000 evaluations, not a clock.

Each sequence is timed exactly with `FitFrame`: the prefix and suffix
committed around the insertion point, with the clamp at every new
adjacency.

**Ranking:**

1. zero overrun (residual ≥ 0);
2. smallest residual (ms);
3. original-cause category (an exact tie goes to the original source);
4. fewer tracks;
5. ascending Track ids.

There is no randomness, so the same inputs give the same proposal.

**Media.** A winner's file is checked (`Path.is_file`). On failure it is
dropped and the search re-runs, at most 8 times. `no_fit` is a valid
result.

## 7. Optional residual PSA

The PSA stage runs only after a music proposal, and only when **all** of
these hold:

* `0 < residual ≤ 60 s` (exactly 60 s qualifies);
* a caller-supplied `PsaResidualPolicy(enabled=True, category_id=...)`;
* that category exists and its kind is `spot`.

It adds at most one PSA under the same strict exclusions and zero overrun:
the best fit, ties going to the lowest id.

The status is one of `psa_proposed`, `psa_disabled`, `psa_not_configured`,
`psa_category_missing`, `psa_category_not_psa_kind`, `no_psa_fit`,
`not_applicable_no_residual` or `not_applicable_residual_above_60s`. There
is no persisted PSA setting (no migration), no PSA-only trigger for a web
shortage of 60 s or less, and no change to ordinary PSA live fill.

## 8. Placement (advisory)

* **`tail_append`:** used when the log's final row is music. The new items
  go after it, at `start_position = last.position + 1`.
* **`insert_before`:** used when the log ends in a non-music closing block,
  anchored at that block's first row. It is allowed only when **every** row
  of the block is still ahead of the cursor, is not next up, and is
  unclaimed, unplayed, not on a deck, not forced, not a request slot and not
  a dedication intro, and when the row before it is not an intro. This
  needs a guarded insert-before splice that does not exist yet:
  `append_fill_items` cannot do it.
* **Abstain otherwise** with `no_safe_anchor`, giving the reason.

Nothing is reordered: the closing rows keep their ids and order.

## 9. Result (`RefillProposal.as_dict()`)

The status and reasons, then the projection:

* window and runway;
* deck remaining;
* net and overfill;
* threshold and its basis;
* qualifying and excluded causes;
* observed shortening, labelled as a diagnostic;
* the boundary and its proof source.

Then the proposal itself:

* placement (mode and anchor);
* ordered music items: Track id, category id, source, effective seconds;
* the optional PSA and the PSA status;
* net before and residual after;
* category sources with their provenance;
* selection counters;
* the request digest;
* `expires_at`: 20 s, or 60 s before the boundary if that is sooner;
* uncertainties and notes.

It always carries `advisory: true` and `installed: false`. It holds internal
ids and numbers only: no requester names, messages, request text, titles or
queue objects (tested).

`proposal_staleness(proposal, fresh_snapshot=..., now=...,
active_request_track_ids=...)` is a cheap pure pre-check. It flags an
expired proposal, a changed log, a changed digest, a proposed Track that is
now requested, or one that is now committed.

## 10. Known limitations

* The runway is an upper bound. Two things are not modelled: the cue-in of
  the next hour's first item on the final transition (taken as 0), and
  pipeline preroll beyond one poll tick.
* Program-segment handoffs inside a log are not re-timed. Only the hour's
  end is projected.
* Protected closers are inferred from the slot category kind (any non-music
  kind at the tail), not from an operator-owned post model. A legal ID filed
  under a music category would not be recognised.
* Ordinary 1.1 live fill (`_try_extend_live_log_async`, nominal top of
  hour) is unchanged and is not arbitrated against Phase B. Phase C must own
  that arbitration.
* PSA configuration is supplied by the caller only. Persisting it needs a
  separately authorised migration.
* Causes are best effort (Phase A). A shortage with no recorded cause gets
  `no_qualifying_cause` even when the net is positive.

## 11. Phase C interface handoff (not implemented)

A Phase C installer must treat a proposal as advisory input only and, inside
its own `transaction.atomic()` with the established `PlaylistLog -> LogItem`
lock order and a short lock timeout, re-establish **all** of the following
from fresh engine memory (a new snapshot on the GLib thread) and fresh
database rows:

1. The same active approved `PlaylistLog` id, date and hour, still the
   current or continuation owner (`wrong_hour` rules).
2. An unchanged snapshot digest: queue order, Track content, claims, forced
   items, deck occupancy and generations, cursor. A same-count edit must
   abort.
3. The same engine instance and deck generation. Manual, Remote DJ, mic, VT,
   Start Next and `_next_triggered` all clear. A single live deck, not
   paused, and no live fill in flight.
4. Fresh cause eligibility: still current-hour, not superseded, occurrence
   still where the cause says.
5. A **fresh net shortage** above the same threshold, and a fresh total with
   the proposed items of no more than the window. Never reuse the proposal's
   numbers.
6. The same boundary kind, instant and proof source, recomputed. For
   `insert_before`, the anchor is still unclaimed, unplayed, not on a deck,
   not forced, not a request or intro, not next up, and the closing block's
   order is intact.
7. Each proposed Track: still Ready-to-Air, its file present, not blocked,
   outside recency, not committed or requested anywhere, and with no request
   reservation collision.
8. No request fulfilment, `played_at`, `PlayEvent` or claim is touched by the
   install; dedication intro -> song adjacency is preserved.
9. Duplicate-install protection: a durable unique install key per (log,
   cause set), so a restart, a retry or the ordinary live fill cannot install
   twice. Arbitration with `_try_extend_live_log_async` happens through one
   coordinator.
10. DB-then-memory adoption on the GLib thread, without restarting, seeking or
   ejecting a deck, and with cursor preservation tested for both placement
   modes.

On any mismatch: discard with zero side effects, and at most a bounded retry
from a new snapshot.
