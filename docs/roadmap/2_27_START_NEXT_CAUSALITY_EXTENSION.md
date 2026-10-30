# Roadmap 2.27 — r0121 additive amendment: Start Next as a refill cause

Status: **contract + local implementation, not field accepted, not released.**
Base: r0120 companion tip `877a123`. This amends — it does not rewrite — the
r0120 contracts: `2_27_PHASE_A_CAUSALITY_CONTRACT.md` §3 ("Start Next … not
causes in v1") stays true **for r0120**. From r0121 one narrowly defined
Start Next outcome becomes a third cause type, `start_next`, behind its own
default-Off switch. Nothing here changes how Start Next plays.

## 1. Source truth (audited at `877a123`)

| Fact | Where |
|---|---|
| Start Next is accepted only after the mandatory identity checks (request id, `(user, client_request_id)` replay guard, exact `engine_instance`, command age, exact `expected` live decks **and generations**), then in exactly one mode: `overlap` (2 live), `fade_and_start` (1 live, other slot free), `start_only` (0 live, maybe one paused). | `engine._start_next_command` |
| A repeated command file (same `request_id`) is ignored before any record; a repeated press (same user + client id) is `rejected/duplicate_request`. | `_start_next_command` |
| The outgoing completion becomes authoritative **only** in `_start_next_completion_due`, at fade end (pad ≤ `START_NEXT_SILENT_VOLUME`, or the bounded extra elapsed), via `deck.claim_completion("start_next")` on the exact outgoing deck object captured at acceptance. That claim is the same per-generation, once-only primitive EOS, error and Eject use. | `_start_next_completion_due`, `Deck.claim_completion` |
| If EOS/error claimed first, the claim fails and `_handle_deck_finished` routes to `_start_next_outgoing_ended` (no `start_next` claim ever happens). | `_handle_deck_finished` |
| Eject/Pause/Resume/Seek touching a transition deck cancel it **before** they act (`superseded_by_*`), so they can never follow a `start_next` claim on the same generation; an Eject after the claim fails its own claim (`already completing`). | `_start_next_cancel_for_operator_command`, `_eject_deck` |
| After the claim the deck is retired (`_retire_deck_respecting_gated_seek`) and `_start_next_finish` settles `completed` or `completed_to_silence`. | `_start_next_completion_due` → `_start_next_finish` |
| `start_only` claims a **paused** deck with `claim_completion("start_next")` at acceptance — nothing faded, the pause already ended audio. | `_start_next_command` |
| Watchdog, engine stop, reload, cancellation and failure end in `failed`/`cancelled`, never a claim by this path. | `_start_next_watchdog`, `stop`, `_start_next_cancel*`, `_start_next_fail` |
| Seek/resume recreate the deck with `continuation_reason`; a restart resume is `auto_resume`. | `_create_deck` |
| Phase A Eject evidence: in-memory reads + one GStreamer position query + `recorder.offer()` (bounded `put_nowait`), gated by one cached boolean. | `_offer_eject_refill_evidence`, `eject_evidence_from_engine` |
| Unknown cause types already fail closed in `PolicyState.allows`, `install_permitted` and `partition_causes` (`switches.get(t, False)` / `unknown_cause_type`). The evidence worker's `_write` does **not**: anything not `operator_eject` is gated by the **request** switch. | runner, install, projection, evidence |

Discrepancies from the workorder's starting points: none that block. One
correction to the workorder's model: `claim_completion("start_next")` appears
twice — the fade path (the authoritative cut) **and** `start_only`'s paused
deck (not a cut). Only the first is instrumented.

## 2. Sequence

```
api_engine_start_next ──HTTP 202 (queued only)──► command file
  └► _start_next_command  (identity, replay, target/generation, mode)
       mode=fade_and_start; active[...] records manual/remote-DJ/mic ownership at accept
       └► _start_next_anchor_due ─► _start_next_incoming_due (D7 incoming start)
            └► _start_next_completion_due
                 pad silent (or bounded extra) ─► deck.claim_completion("start_next")  ◄── AUTHORITATIVE CUT
                    │  capture_start_next Off → nothing else (one attribute read)
                    │  On → start_next_evidence_from_engine(): in-memory reads,
                    │        ONE position read, pure classification → active["refill_evidence"]
                    └► _retire_deck_respecting_gated_seek ─► _start_next_finish
                          outcome == "completed" (not to_silence) ─► recorder.offer(evidence)  (put_nowait)
evidence worker: policy re-read → capture_start_next? → INSERT HourRefillCause(start_next) (unique source_key)
   └► runner.wake ─► runner tick (transport_block refuses while Start Next/overlap/no live deck)
        └► project_refill: partition_causes (occurrence no longer pending/on deck, Track matches)
             net shortage from the FRESH window, threshold_for(...)
             └► PolicyState.allows({types}) ─► observation proposal | guarded install (S1/S2, locks, install_permitted(cause_types, lock=True))
```

The cut is observed at the claim; it is **offered** only once the same
transition settles `completed`. Accepted-only, cancelled, failed, watchdog,
to-silence and engine-stop transitions never offer.

## 3. Decision table — positive cause

A `start_next` cause is recorded only when **every** row holds (reason codes
are the classifier's abstention reasons):

| # | Condition | Abstain reason |
|---|---|---|
| 1 | `capture_start_next` cached On at the claim | (nothing runs) |
| 2 | `mode == "fade_and_start"` | `not_fade_and_start` (`overlap`, `start_only`) |
| 3 | The claimed deck is `active["outgoing_deck"]`, same slot, generation and LogItem id as `active["outgoing"]`, still mapped in its slot; `completion_reason == "start_next"` | `identity_mismatch` / `not_claimed` |
| 4 | Incoming was attempted and the phase is `advancing` (an incoming deck started) | `to_silence` / `incoming_not_started` |
| 5 | No other live deck except this transition's incoming slot | `foreign_live_deck` |
| 6 | Manual off at accept **and** at the cut; no Remote DJ session at accept or cut; no mic live at accept or cut; VT idle; no gated seek on the deck | `manual_hold` / `remote_dj_owner` / `mic_live` / `vt_transition` / `seek_in_progress` |
| 7 | Deck on air (`air_start_recorded`), not paused, no `continuation_reason` (resume, seek, already-started replay) | `not_on_air` / `paused` / `not_scheduled_occurrence` |
| 8 | `LogItem.origin == generated_schedule` (loaded field) | `not_scheduled_occurrence` |
| 9 | Scheduled `LogItem.category.kind.code == "music"` from the **cached** relation (never Track category, never a query) | `not_music` / `identity_unknown` |
| 10 | Loaded LogItem id/position/log id, cached log date/hour, Track id and timing fields; non-empty `engine_instance` and `request_id` | `identity_unknown` |
| 11 | Reliable position read at the cut; `observed = (next_start or duration) − max(position, cue_in)`, rounded to ms, **> 0** | `position_unknown` / `no_remaining_airtime` |

Fade arithmetic: the position is read at the claim, i.e. after the fade has
run, so the faded seconds are already **inside** the aired portion. Nothing
subtracts `fade_ms`; nothing uses the press time or file length. A fade that
reached `next_start` (or the end) gives 0 → no cause; EOS first means no claim.

**Manual hold.** Decided: abstain. A Start Next pressed under Manual (with or
without `release_manual`) is an operator-owned program decision; the
runner already refuses `manual_mode`, and the cause has no proven automation
deficit. Ownership is snapshotted at acceptance (three plain booleans in the
transition record; nothing else about acceptance changes) and re-read at the
cut.

**Fade to silence.** Decided: abstain, no diagnostic row. With no incoming
item the established end-of-queue authority (`_on_log_exhausted` → live fill /
next-hour retry) owns the rest of the hour; a no-live-deck snapshot is
already `unsafe_window/no_live_deck` for the runner and installer, and that is
unchanged.

## 4. Exclusions (all → no cause)

Rejected/stale/duplicate command (never accepted: no claim); accepted then
cancelled, superseded (Eject/Pause/Resume/Seek/reload), failed (slot,
incoming open failure, `fade_unsupported`), watchdog, engine stop; EOS/error
before fade completion (claim lost); position at/after the scheduled end;
`overlap`; `start_only` (incl. paused-only); `completed_to_silence`; older or
other deck generation; foreign `LogItem.origin` (inserted, build/live fill,
engine-inserted dedication intro/alert, refill-installed, legacy/unknown);
continuation (resume, seek, already started); missing cached category/kind,
Track timing, position; non-music slot; Manual, Remote DJ, mic, VT, gated seek;
ordinary automatic segue (never this path); force-next/reorder (never this
path); automatic skip / media-error recovery (separate r0121 item, not built).

## 5. Identity, dedup and races

`source_key = "start_next:li<log_item_id>:<slot>g<generation>:<h>"`, where
`<h>` = first 32 hex of SHA-256 over `engine_instance + "\n" + request_id`.
Bounded (≤ ~90 chars ≪ 160) whatever the input lengths; no user id, client id
or requester data. `engine_instance` and `request_id` are also stored in their
own columns (`engine_instance`, new `start_next_request_id`).

| Event | Effect |
|---|---|
| duplicate command file | ignored before acceptance → no second transition |
| HTTP retry / repeated press | same server-derived `request_id` (ignored) or `duplicate_request` rejection |
| late `_start_next_completion_due` timer | `_start_next_current(token)` is None or `outgoing_ended` → returns; the claim is once-only per generation |
| stale GLib callback after restart | old engine's timers die with the process; new `engine_instance` ⇒ different key; old command refused `engine_restarted` |
| EOS/error vs fade | whichever claims first wins; EOS ⇒ `_start_next_outgoing_ended`, no cause |
| Eject vs active Start Next | Eject cancels the transition before its claim (no Start Next claim); its own cause is refused `start_next_transition` (r0120). After a Start Next claim the Eject's claim fails |
| worker retry / duplicate insert | unique `source_key` → `IntegrityError` → counted `duplicate` |

Trace dedup: `build_causality_trace` already counts one cause per
`(cause_type, log_item_id)`. r0121 groups the two **early-cut** types
(`operator_eject`, `start_next`) into one family, so at most one cut per
occurrence is ever summed (`duplicate_occurrence_cause`). A request
substitution and a later cut of the same occurrence describe different
seconds (replacement shorter than the original; cut shorter than the
replacement) and both still count — exactly as r0120 does for request + Eject.

## 6. Net shortfall and thresholds

The cause's `observed_shortening_seconds` is diagnostic. Eligibility is still
Phase B's **fresh whole-window** net shortage (`window − committed runway`,
with cue/overlap allowances, existing overfill ⇒ `already_satisfied`, proven
boundary, partial hour, protected closer). The runner waits for transport
quiet: `start_next_active`, `next_triggered`, overlap, no live deck stay
`blocked_transport`/`unsafe_window`.

| Qualified set | Threshold (strictly greater) | Basis |
|---|---|---|
| any `request_substitution` | 60 s | `request_substitution_present` (unchanged) |
| `operator_eject` only | 30 s | `operator_eject_only_provisional` (unchanged r0120 rule) |
| `start_next` only | 30 s | `start_next_only_provisional` — **provisional, NOT operator-approved** |
| `operator_eject` + `start_next` | max(30, 30) = 30 s | `operator_cuts_provisional` |

`START_NEXT_MUSIC_TRIGGER_THRESHOLD_SECONDS = 30` is a separate constant so an
operator decision can move it without touching the Eject rule. The combined
threshold is the **maximum** over the participating types. Equality never
qualifies.

## 7. Policy, schema, compatibility

* `HourRefillPolicy.capture_start_next` (default False). The master switch
  still gates everything. A cause set drives a refill only if **every** type
  in it is switched on; unknown types are refused everywhere, now including
  the evidence worker (new `dropped_unknown_type`; never the request switch).
* `HourRefillPolicy.capture_flags()` keeps its r0120 two-value shape;
  `capture_switches()` adds the third value for the worker.
* Migration `library.0097_hour_refill_start_next_cause`:

| Op | Update Center class |
|---|---|
| AddField `hourrefillpolicy.capture_start_next` Boolean default False | additive |
| AddField `hourrefillcause.start_next_request_id` Char(64) default "" | additive |
| AddField `hourrefillcause.start_next_position_seconds` Float null | additive |
| AlterField `hourrefillcause.cause_type` (choices only) | additive (metadata) |
| RemoveConstraint `hour_refill_cause_known_type` | **manual** |
| AddConstraint `hour_refill_cause_known_type` (3 types) | **manual** |
| AddConstraint `hour_refill_cause_start_next_fields` | **manual** |

  The three manual operations need an exact, reviewed r0121 companion
  authorization (release + digest bound), not a blanket exemption. `0093`–
  `0096` are untouched; no data migration; existing rows satisfy every new
  check (they are not `start_next`).
* New code on the old schema: every policy read that names the new column
  raises → worker/runner/installer fail closed (Off); the request-swap path
  already tolerates a failed read. Old code on the new schema: `start_next`
  rows are `unknown_cause_type`, never counted or installed. Old code cannot
  create a fresh policy row (NOT NULL column without a DB default — the
  project-wide AddField convention); an existing row is unaffected.
* Reverse (`migrate library 0096`) re-adds the two-type constraint and fails
  while any `start_next` row exists: delete those evidence rows first
  (append-only rows are deletable by queryset, never updated).

## 8. GLib cost

Off (default): one attribute read in `_start_next_completion_due` and one
dict lookup in `_start_next_finish`; three boolean reads already in memory
at acceptance. On: in-memory attribute/dict reads, one deck position read
(the same non-database GStreamer query Eject uses), a SHA-256 of < 200 bytes,
one bounded `put_nowait`. No SQL, lock wait, file I/O, logging or solver.
Exceptions are swallowed (`snapshot_error` counter) and never change the
fade, retirement, incoming start, cue-in, Manual release or outcome.

## 9. Tests to write (mapped to workorder §4)

| Family | Module |
|---|---|
| pure classification, effective time, identity, key bounds | `test_hour_refill_start_next_2_27.py` (`StartNextClassificationTests`) |
| engine hook on the real `_start_next_completion_due` / `_start_next_finish`, no-SQL blocker, real queue-loaded LogItems | `…HookTests` |
| real GStreamer transport (fade, overlap, start_only, to-silence, EOS-first, cancel, Eject race, watchdog, duplicate) | `…TransportTests` |
| worker type dispatch, independent switch, unknown type, old flag shape | `…WorkerTests` |
| model CHECKs, old rows valid, migration ops/classification | `…SchemaTests` |
| projection partition/threshold/dedup, runner `allows`, `install_permitted` | `…PolicyProjectionTests` |
| guarded installer end-to-end with a `start_next` cause | `…InstallTests` |
| negative controls (hook removed, switch bypassed) | evidence `C-*` logs |

## 10. Not in scope

No change to fade, cue-in, controller, binding, once-only advance, VT,
dedication pairing, Manual, Remote DJ or Eject semantics; no automatic-skip /
media-failure detector; no silence filling; no station policy row, arming or
PSA residual; no release manifest, companion or historical artifact edits.
