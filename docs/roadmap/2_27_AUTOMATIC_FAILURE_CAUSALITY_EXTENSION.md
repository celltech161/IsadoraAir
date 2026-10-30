# Roadmap 2.27 — r0121 additive amendment: automatic skip and media failure

Status: **contract + local implementation, not reviewed, not released, Off.**
Parent: `65ae049` (r0120 historical-target test fix) on `831b2f3` (Start Next
cause, migration 0097). Additive to the Phase A–D contracts and to
`2_27_START_NEXT_CAUSALITY_EXTENSION.md`; neither is rewritten. Nothing here
changes playback, skipping, EOS handling, first-air evidence or the deck
error path's own behavior.

## 1. Source truth (audited at `65ae049`)

| Path | What it actually does | Consequence for causality |
|---|---|---|
| `_log_item_playable` | `(False, reason)` for: no Track (FK NULL), a spoken-projection or active-message refusal, `"track id=… has no filepath"`, `"file missing on disk: …"`. File check is a `Path.is_file()` the engine already performs. | Only the last two are positive **media** evidence. Never re-check files for causality. |
| `_get_upcoming_preview` | Display-only filter; never moves the cursor. | **Never** a cause. |
| `_peek_playable_at_cursor` | Crossfade/VT/Start Next look-ahead. For an unplayable, non-forced, non-poison row at the cursor it prints, emits a skip event and **advances `_queue_cursor`**. Forced items are only *skipped over* (not consumed). | The cursor advance over an unplayable row is the committed selection-time skip. |
| `_next_queue_item` | Resume occurrence first, then forced items (popped), then the cursor walk: same unplayable branch as above, cursor already advanced; rollover continues the walk in the next hour's log. | Same committed skip in the cursor walk. Resume and forced skips are **not** causes (resumed/foreign/forced). |
| `_reload_queue_if_changed` | Recomputes the cursor to "after the last played-or-on-deck row". | A skipped row ahead of that point is **revisited**; if its file returned it plays. A skip is therefore re-validated at projection time (§4). |
| `_apply_poison_skip` / `_is_poison_guarded` | Materialization filter removes guarded rows before any walk; the walk's poison check is defense in depth and silent. A guarded row poisoned a deck in a **previous process** — it may already have aired partially. | **Deferred** (§6): absence from a list is not abandonment, and partial prior air is not excluded. |
| `_start_next_track` recursions | `SCHEDULING_CONTENDED`, unpaired dedication intro, `_create_deck` → None. | None is media evidence. |
| `_create_deck` → None | Claim refused, spoken deck-open authority unavailable/refused, deck-open lock lost. | Never media evidence; **never** a cause. |
| first-air evidence | Streaming probe sets `air_start_dispatched` at the first real post-primer buffer (fresh, `air_start_eligible` decks only) → GLib `_record_occurrence_air_start` commits `played_at`, then sets `air_start_recorded`. `playback_claimed_at` is only a claim. | Post-air needs `air_start_recorded`; pre-air needs **no** dispatch, no record and zero media buffers. Dispatched-but-unrecorded is ambiguous → abstain. |
| `_on_deck_error` (GLib, main bus) | Stale/retiring and paused generations are ignored; then `claim_completion("error")` (once per generation) wins the occurrence; the deck is retired via `idle_add(_retire_deck_error_and_record)`; the claimed occurrence is never handed out again. | The **exact terminal boundary** for a media failure. |
| EOS (`_on_deck_eos_probed`, deferred post-seek re-check, `_handle_deck_finished`) | Plausibility, seek rejection, deferred re-check. | **Never** a cause, including early EOS against stale metadata. |
| `_check_stuck_decks` | Watchdog stall containment, may occur after the expected end; position is an estimate. | **Never** a cause (deferred, §6). |

## 2. Event table

| Event | Positive evidence boundary | Cause type | Source key | Time lost | Dedupe family | GLib cost (On) | Worker |
|---|---|---|---|---|---|---|---|
| Committed cursor skip of a scheduled row whose Track exists but has no filepath / missing file | the `_queue_cursor` advance in `_peek_playable_at_cursor` or `_next_queue_item`'s walk, with the engine's own reason | `automatic_skip` | `automatic_skip:li<LogItem>:t<Track>` (logical occurrence; no engine/session, so restarts and reloads collapse) | full effective airtime `(next_start or duration) − cue_in` | termination | cached-flag read, loaded-field reads, one `put_nowait` | bounded queue; unique key |
| Whitelisted GStreamer media error on a fresh deck before any media buffer | successful `claim_completion("error")` in `_on_deck_error` | `media_failure`, phase `pre_air` | `media_failure:li<LogItem>:<slot>g<gen>:<engine_instance>` | full effective airtime | termination | as above (no position read) | as above |
| Whitelisted error on a fresh deck **after** recorded air start | same | `media_failure`, phase `post_air` | same | `(next_start or duration) − max(position, cue_in)` from the existing `_get_deck_position` read | termination | as above + that one position read | as above |

Whitelisted (`gst-resource-error-quark`): `NOT_FOUND`, `OPEN_READ`, `READ`.
Whitelisted (`gst-stream-error-quark`): `TYPE_NOT_FOUND`, `WRONG_TYPE`,
`CODEC_NOT_FOUND`, `DECODE`, `DEMUX`, `FORMAT`. Everything else — including
the generic `STREAM FAILED` ("internal data stream error", not-linked/
negotiation flow), `SEEK`, core and library errors — is **not** media evidence.
GStreamer posts the specific element error first; that is the one that wins
the claim, later duplicates are ignored by the existing code.

## 3. Eligibility (all must hold; reason code otherwise)

Common: cached `capture_*` On; `LogItem.origin == generated_schedule` (loaded
field); scheduled `LogItem.category.kind.code == "music"` (cached relation);
cached `playlist_log` with loaded `status == "approved"`, `date`, `hour`, `id`
matching `playlist_log_id`; loaded Track id equal to `LogItem.track_id`;
loaded `next_start/duration/cue_in`; positive effective/remaining airtime.

`automatic_skip`: reason exactly the engine's "no filepath" or "file missing"
(else `not_media_reason`); not forced, not the resume occurrence, not poison;
`played_at` and `playback_claimed_at` both None (else `possible_replay`).

`media_failure` (deck): error domain/code whitelisted (`not_media_error`);
deck still mapped in its slot (`superseded`); not paused (existing code
already returns before the claim); no gated seek (`seek_in_progress`); no
`continuation_reason` and `air_start_eligible` (`not_fresh_occurrence`); not
the Start Next outgoing deck or a Start Next transition at all
(`start_next_transition`); VT idle (`vt_transition`); no other live deck
(`overlap`); Manual / Remote DJ / mic (`manual_hold` / `remote_dj_owner` /
`mic_live`).
- `pre_air`: not dispatched, not recorded, `media_buffer_count == 0`,
  `played_at` None.
- `post_air`: `air_start_recorded`; position read succeeds, finite, ≥ 0;
  remaining > 0 (an error at/after the scheduled end is `no_remaining_airtime`).
- dispatched-but-unrecorded, or buffers without dispatch: `air_start_unsettled`.

## 4. Identity, dedupe, net hour

* One **termination** cause per logical occurrence: `operator_eject`,
  `start_next`, `automatic_skip` and `media_failure` share one trace family
  (first in causal order counts, the rest are `duplicate_occurrence_cause`).
  A claim is once per generation, so Eject/Start Next/error cannot all own one
  generation; the family covers cross-process and reload replays.
  `request_substitution` stays a separate family (a different act).
* Unique `source_key` (DB) + trace dedupe; no in-engine registry.
* Projection (Phase B) re-validates every termination cause against the
  current in-memory occurrence: must exist in the active approved log, be
  neither pending nor on a deck, and still carry the cut/skipped Track
  (`superseded` otherwise). An `automatic_skip` whose occurrence is now
  claimed or aired is `occurrence_aired`; a `pre_air` failure whose
  occurrence aired is `occurrence_aired`.
* Causes for another hour or log are listed, never counted. Eligibility is
  still the fresh **net** shortage over the proven boundary, never a sum of
  observed losses.

Thresholds (strictly greater; mixed sets take the maximum of the participating
types):

| Set | Threshold | Status |
|---|---|---|
| any request substitution | 60 s | accepted (r0120) |
| Eject only | 30 s | accepted (r0120) |
| Start Next (± Eject) | 30 s | provisional, not approved (r0121) |
| any `automatic_skip` or `media_failure` | **60 s** | **proposed** for operator review; deliberately not the Eject rule |

## 5. Policy, schema, worker

* `HourRefillPolicy.capture_automatic_skip`, `capture_media_failure`: default
  False, separate (different evidence and risk). Master switch still gates
  all; every type in a set needs its own switch (runner, `install_permitted`,
  locked commit-time recheck). Unknown types stay dropped by the worker.
* `HourRefillPolicy.capture_switches(include_automatic=True)` returns the five
  switches for the worker; the r0120/r0121 shapes are unchanged.
* Migration `library.0098_hour_refill_automatic_failure_cause` (new; 0093–0097
  untouched): additive AddFields (two switches; `failure_phase`,
  `failure_reason`, `failure_position_seconds`), choices-only AlterField, and
  **manual** constraint operations (known-type swap plus two field checks).
  Reverse re-adds the three-type check and is **refused** while any
  `automatic_skip`/`media_failure` row exists; it never deletes evidence.
  Operator options: stay forward (code rollback leaves rows unused —
  old code treats them as `unknown_cause_type`) or restore the pre-migration
  backup. No automatic deletion.
* New code on a pre-0098 schema: every reader naming the new columns fails
  closed (Off), as with 0097.

## 6. Deferred (not causes in this slice)

| Case | Why |
|---|---|
| Poison-guard refusal | materialization removes the row before any walk; a guarded row may have partially aired in the previous process (the very failure that poisoned it); no positive "never aired" proof without a schema/poison-record change. |
| NULL Track FK | no trustworthy original Track or timing. |
| Watchdog stall (`_check_stuck_decks`) | containment, not media evidence; may fire after the scheduled end; wall-clock position. |
| Early EOS | indistinguishable from stale metadata without changing accepted EOS plausibility. |
| Forced / resume occurrence skips | foreign (dedication/alert) or a continuation of an already-aired occurrence. |
| Errors while a crossfade, VT, Start Next, gated seek, Manual, Remote DJ or mic owns program | ownership ambiguity; existing exclusions. |
| `_create_deck` → None, spoken/active-message refusals, contention | administrative, not media. |

## 7. Not in scope

No change to `_log_item_playable`, skipping, the poison gate, deck-open
authority, EOS plausibility, first-air recording, duration accounting,
PlayEvent history, Start Next/Eject/VT/Manual/Remote DJ, the installer, the
protected updater or any historical migration/release artifact.
