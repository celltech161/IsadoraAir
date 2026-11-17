# Roadmap 2.25 B1-B2: VT timing, scheduled audio admission and lifecycle infrastructure

Status: **B1-B2 engineering-review gate.** The infrastructure is implemented and validated in isolation.
It is **not connected** to live automatic transitions:
- the r0144 VT state machine (`_vt_maybe_enter` and its phases) is unchanged and still the only live VT
  path;
- nothing in production calls the `_vt2_*` methods;
- no ordinary deck is opened in scheduled mode.

B3 and later need separate authorization.

**Ancestry:** r0144 `e4003aa` → Phase A `6ad5222` → B0 `ced54ed` → **B1 `6f66222`** → B2 (the commit that
adds this document).

## 1. Contract clarifications incorporated

These are written into `2_25_PHASE_A_VT_TRANSITION_CONTRACT.md` §4 and §11.

| Item | Clarification |
|---|---|
| D5 | A song started as a direct consequence of an operator Eject is an operator-initiated start, so it gets no evergreen intro VT (`StartKind.EJECT_REPLACEMENT`). |
| D10 | No usable Intro Until window: the whole VT, then 150 ms, then the song (`U = V + M`). |
| N1 | VT transitions may land precisely at their planned segue boundary. Ordinary transitions keep r0144 timing. |
| D13 | Intro VTs are suppressed during interrupted-occurrence recovery. Previously aired **or uncertain** outro VTs are suppressed for the resumed occurrence. |

## 2. B1: pure planner (`library/services/vt_transition.py`)

**Purity.** No ORM, GStreamer, GLib, clock or engine state; enforced by `ModuleIsPureTests`, which walks
the AST.

**Inputs** are frozen dataclasses:

| Type | Fields |
|---|---|
| `Recording` | `attached`, `playable`, `duration`, `source` (the existence of a record is **not** eligibility) |
| `Markers` | `duration`, `cue_in`, `next_start`, `outro_starts`, `intro_until` |
| `LiveContext` | `auto_mode`, `mic_live`, `remote_dj_live` |
| `OutroFacts` | `recording`, `markers`, `generation_start_position`, `is_continuation`, `resume_staleness_s`, `already_aired` |
| `IntroFacts` | `recording`, `markers`, `start_kind` (`StartKind`: automatic, start_next, manual, eject_replacement, manual_to_auto_handoff, resume, continuation), `role` (`ItemRole`: ordinary, urgent, dedication_intro, dedication_followup, active_message), `already_claimed` |
| `TransitionInput` | `segue_at` (A_seg), `outro`, `intro` (`SideEligibility`), `incoming_markers`, `outro_at` (O_s), `gap_s`, `duck_db`, `ramp_s`, `next_is_urgent` |

**Outputs:**
- `SideEligibility(side, eligible, reason, duration)`, independently for each side. The reason is one of
  the `Reason` constants:
  - no recording, recording unplayable, invalid VT duration;
  - invalid track duration, invalid Cue In;
  - outro marker missing, outro marker invalid, outro marker at or after the segue;
  - not Auto, mic live, Remote DJ live;
  - operator start, Manual-to-Auto handoff;
  - interrupted-occurrence recovery, continuation generation, already claimed;
  - outro already aired, outro delivery uncertain, generation started past the marker;
  - protected urgent, protected dedication, protected Active Message;
  - urgent-next no-hold.
- `TransitionPlan`:
  - `music_at` (A_I) and `deck_offset` (A_I − C);
  - `hold_until`;
  - `outro_start` / `outro_end`, `intro_start` / `intro_end`;
  - `underlap` (U), `vocal_at`, `clearance`;
  - `duck_gain`, `duck_points`;
  - per-side reasons.
- `fire_deadlines(admission, V)`: ready by admission − 50 ms, first audio by admission + 1.0 s, complete by
  admission + V + 2.0 s.
- `outro_resume_staleness(state_age)`: the D13 bound. An unknown evidence age is treated as unbounded, so
  the outro is suppressed.

**Behaviour.**
- Markers that are wrong but plausible become **reasons**.
- Non-physical inputs raise `InvalidTransitionInput`: non-finite running times, a negative gap or ramp, a
  positive duck in dB, a non-positive V.
- U is quantized to the pipeline's 1 ns resolution, so an exact fit is exactly 0.

**Representative timing (asserted to 1 µs in `test_vt_transition_planner.py`):**

| Case | Inputs | Plan |
|---|---|---|
| Incident (KOGR 2026-11-16) | C 0.124717, I 15.008, V 6.456229 | W 14.883283, **U 0**, music at A_seg, clearance **8.427054** |
| Long VT | same markers, V 18.0 | **U 3.266717**, speech ends at the vocal − 0.150 |
| Exact fit | V = W − M | U 0, clearance 0.150; V + 1 µs gives U 1 µs |
| No usable window | Intro Until missing, 0, ≤ C, > duration, NaN or negative | U = V + 0.150 |
| Outro holds | Outro VT 9.0 s from 250.0, A_seg 255.943311 | hold to 259.0; intro at 259.2 (gap 0.2) |
| Urgent next | an outro VT that would hold | suppressed (`urgent_next_no_hold`); no hold |

The planner has 35 tests. Five mutations of the planner were each detected:
- no margin;
- no margin when there is no window;
- Eject allowed as automatic;
- a record treated as eligible;
- the duck attack at entry.

## 3. B2: the building blocks

| Piece | Where | Purpose |
|---|---|---|
| `VtOwner(direction, log_item_id, track_id, deck_slot, deck_generation, transition_seq, fire_id)` | `vt_audio.py` | The unambiguous owner of one VT |
| `ScheduledVtFire` | `vt_audio.py` | The B0-qualified scheduled branch with full lifecycle (§4) |
| `VtDuckCurve` | `vt_audio.py` | The speech-owned duck (§7) |
| `_vt2_prepare_fire`, `_vt2_prepare_fire_for_voicetrack` | `engine.py` | Engine glue; VoiceTrack resolution through the unchanged `_vt_resolve_audio` (validated ProductionMedia, then the legacy file) |
| `_create_deck(..., scheduled_admission_ns=, on_scheduled_event=, reopen=)` | `engine.py` | Scheduled deck admission (§5). The `_require_deck_open_authority` wrapper forwards the new arguments. |
| `_vt2_on_scheduled_deck_ready`, `_vt2_admit_scheduled_deck`, `_vt2_scheduled_deck_readiness_deadline`, `_vt2_fail_scheduled_deck` | `engine.py` | The scheduled deck lifecycle |
| `_vt2_reopen_held_occurrence` | `engine.py` | Early re-admission by regeneration (§6) |
| `_vt2_submit_null`, `_vt2_teardown_coordinator`; `_deck_teardown_tick` ticks it | `engine.py` | NULL off the GLib thread on a bounded worker (`BoundedTeardownCoordinator("vt-fires", 5 s, 32)`) |
| `_on_main_bus_error` → `_vt2_fire_for_message_src` | `engine.py` | Branch errors routed to the owning fire (F3) |
| `Deck` fields and methods | `engine.py` | `scheduled_admission_ns`, `admission_state`, `scheduled_ready_ns`, `scheduled_pending_seconds`, `scheduled_block`, `on_scheduled_event`; `activate_scheduled_segment()` |
| `_get_deck_position`, `_write_state` | `engine.py` | Planned-timeline position for scheduled decks; a new `admission` field (None for ordinary decks) |
| `_schedule_occurrence_air_start_from_probe(deck, air_started_at=None)` | `engine.py` | Accepts the exact admission instant; ordinary callers are unchanged |

**Ordinary path unchanged.** Every ordinary deck still has `scheduled_admission_ns None` and
`admission_state None`. It is primed, linked at open with offset "now", and presented at open (asserted by
`test_ordinary_deck_path_is_unchanged`). The new branches are guarded by those fields.

## 4. VT lifecycle and ownership

```
              prepare() refused (admission < now + 50 ms)
 PLANNED ─────────────────────────────────────────────────────────▶ FAILED
    │ prepare(): bin built, ghost offset = admission, linked to fx_submix, started
    ▼
 PREPARING ──(first decoded buffer reaches the mixer pad and waits)──▶ READY
    │  ready deadline (admission − 50 ms) | bus error | state change failure
    ▼                                                                  │ admission running time
 FAILED ◀── first-audio deadline (admission + 1 s) ── ADMITTED ◀───────┘
    ▲                                                   │ EOS (last buffer QUEUED, not rendered)
    │ completion deadline (admission + V + 2 s)          ▼
    └────────────────────────────────────────────── DRAINING ──(last_end + 30 ms)──▶ ENDED
 cancel(): before admission → CANCELLED (detach at once); audible → CANCELLED (50 ms fade, then detach)
 every terminal: serial += 1 (all older timers stale) → detach on the GLib thread → NULL on the worker
```

**Owner checks.** Every timer, probe handoff, bus-error route and completion callback carries
`(fire_id, serial)`.
- A terminal transition increments the serial, so every older callback is stale.
- Stale callbacks are ignored and recorded in `fire.ignored`.
- Callbacks reach the owner layer only through `on_event(fire, state)`. B3/B4 then check `VtOwner`
  against the current transition before touching decks or the duck.

## 5. Scheduled deck admission contract

1. **Open.** `_create_deck(slot, item, scheduled_admission_ns=T)` works as follows:
   - It is refused for a seek, resume or continuation, and for an item that matches the pending resume
     hint. The hint is left for the ordinary path (D13).
   - The claim is the existing one: request scheduling, dedication splice and Swap adoption are untouched,
     because they live in `_start_next_track` and under the claim lock.
   - The bin has **no silence prime**. Its last stage is a capsfilter with a blocking probe holding the
     first decoded buffer.
   - The ghost pad offset is set to T.
   - The bin is **not linked** to the deck mixer.
   - Presentation (now-playing, RDS) is deferred.
2. **Ready (viability).** The first decoded buffer reaches the hold, about 3-4 ms after open in every
   format and rate measured. Then `_vt2_on_scheduled_deck_ready`:
   - links the bin to a new `mixer` request pad;
   - releases the hold, so the buffer waits in the mixer pad for T;
   - sets state `scheduled`;
   - arms the admission timer;
   - emits `ready`.

   **Recorded speech may be prepared only after `ready`.**
3. **Admission.** At T, `_vt2_admit_scheduled_deck`:
   - sets state `audible`;
   - records `FIRST_REAL` evidence;
   - writes now-playing and RDS;
   - starts accounting at the exact wall-clock instant of T;
   - credits the buffers that were queued for T;
   - dispatches the authoritative air start;
   - emits `audible`.
4. **Failure.** If there is no readiness by open + 100 ms (`SCHEDULED_DECK_READY_S`), or readiness comes
   less than 50 ms before T, or the link fails:
   - state `failed`;
   - `_remove_deck(occurrence_terminal=False)`, which detaches first;
   - emits `failed`.

   The generation is claimed but never played, nothing is persisted, and nothing advances. The owner falls
   back to an ordinary start (B4).
5. **Identity.** Every timer is keyed by `(slot, generation)` and the expected `admission_state`. A
   retired or replaced generation's timers are ignored.

## 6. Early re-admission (regeneration)

`_vt2_reopen_held_occurrence(slot, T')` applies only to a generation that is `preparing` or `scheduled`,
has never aired, and has `played_at` None.

**Steps:**
1. It is refused for an audible generation.
2. If the occurrence is not playable now (the file has gone), it fails **without retiring the held
   generation**, whose file is already open.
3. Attempts are bounded to 2 per occurrence; a third is `exhausted`.
4. The held generation is detached and retired.
5. The **same LogItem object** is reopened as a fresh scheduled generation:
   - a new generation serial;
   - the occurrence order is preserved (`reopen=True`);
   - `_claim_playback_occurrence` is idempotent, so `playback_claimed_at` keeps its original timestamp
     (verified in PostgreSQL).

**What it does not do:**
- The queue cursor is not touched.
- `_start_next_track` is not called, so there is no request scheduling, dedication splice or second queue
  item.
- No air start or PlayEvent exists for the retired generation.

**Races covered by tests:**
- the stale admission of the retired generation (ignored);
- a failed reopen (the held generation is kept, with no claim or advance);
- repeated reopens (bounded).

## 7. Speech-only duck (`VtDuckCurve`)

- One `InterpolationControlSource`, bound once to `vt_duck_gain.volume` the first time it is used. It is
  never created by the r0144 path, so the r0144 stepper is untouched until B4 replaces it.
- Points come from `vt_transition.duck_points`:
  - the attack one `duck_ramp_ms` before speech;
  - the release at speech end;
  - intervals merged.

  Depth and ramp come from the unchanged `VoiceTrackConfig`.
- Running-time points are mapped to the element's stream time through an offset read from its own
  SEGMENT event.
- **One owner at a time.** `apply(owner, points)` takes over from the current value. `release(owner, ramp)`
  ramps back to unity from "now", and is ignored for any other owner. So a stale callback can never
  restore or alter the current duck, and a failed or cancelled VT can always be released.
- The mic and Remote DJ duck (`duck_gain`) is a separate element and is never touched.
- **Foundation for B5:** a mic cancel is `fire.cancel(fade_ms=50)` plus `curve.release(owner, ramp)`.

## 8. Playback accounting and restart recovery

| Surface | Scheduled generation before admission | At admission |
|---|---|---|
| `LogItem.played_at`, `Track.play_count`, SongRequest aired, PlayEvent | **none** (no air-start dispatch) | exactly once, `played_at` = the exact admission instant (PostgreSQL: error 0.0000 s) |
| Duration segment | inactive; queued buffers accumulate in `scheduled_pending_seconds` | activated at the admission instant; pending credited (confirmed 1.7000 s of a 1.7 s file, PlayEvent 1.70 s) |
| now-playing, RDS | not written | written |
| `_get_deck_position` | 0.0 (planned timeline) | elapsed since media 0 rendered |
| `engine_state.json` | `admission: preparing/scheduled`; `actually_playing` false (it requires `played_at`), so it is **never resume evidence** | `admission: audible`; resume evidence as for any deck |
| Watchdog (`_check_stuck_decks`) | position 0 means no duration overrun; there is no EOS before admission | ordinary |
| Retired or failed generation | `_persist_deck_duration` finds no PlayEvent and writes nothing | — |

**D13 interfaces (no migration).**
- An occurrence that is claimed but unaired keeps `playback_claimed_at`. On restart it is resumed by the
  ordinary path, which `_create_deck(scheduled=…)` refuses to schedule. The planner classifies it
  `already_claimed` or `resume`, so it gets no intro VT.
- The outro decision for a continuation generation uses the resume hint's `saved_position` and
  `state_age_seconds` (`outro_resume_staleness`).
- **Uncertainty.** There is no durable record that a VT aired, and none is created. "Aired" is known only
  within the process (B3's per-occurrence aired set). After a restart, delivery is uncertain whenever the
  saved position plus its staleness bound reaches Outro Starts, and the outro is then suppressed.
  Nothing claims durable proof of VT delivery.

**Urgent and dedication interfaces.**
- `ItemRole` and `TransitionInput.next_is_urgent` carry the protected paths into the planner.
- The scheduled deck path never repeats request scheduling or dedication splicing.
- The authoritative incoming occurrence remains the one `_start_next_track` claims.

## 9. Real-audio measurements (final regression run C32, rendered PCM; C13 is identical)

| Measurement | Result |
|---|---|
| Fire onset vs plan, 44.1/48 kHz, WAV/FLAC, 8 fires | **0.0000 s** |
| Head marker / tail marker heard | **0.1500 / 0.1500 s** (of 0.150) |
| Rendered length / end vs plan | 1.0000 s / 0.0000 s |
| Fire ready before admission | 0.394-0.397 s; detach 0.06-0.10 ms |
| Distant admission (3.0 s ahead) | remains READY, ends normally, onset 0.0000 (not mistaken for a stall) |
| Scheduled deck onset, 4 configurations | **0.0000 s**; ready 3.5-9.3 ms after open |
| Scheduled deck air start vs admission | **0.0000 s** (harness and PostgreSQL); no air start, presentation or duration before admission |
| Re-admission | new target 0.0000 s; old plan 0.00 s aired; one air start (the replacement generation) |
| VoiceTrack resolution | validated ProductionMedia (FLAC) and legacy fallback both fire at 0.0000 s; an unplayable record is refused |
| Repeated cycles (12, after warm-up) | VT, music and tail error 0.0000; deck pads 0→0, FX pads 1→1, children 12→12, threads 42→42, fds 516→516; fire registry empty; 13 teardowns completed |
| Duck | 0.3999 → 0.2004 (−6.0 dB) → 0.3999; attack starts 89-93 ms before speech (ramp 100 ms); a stale release held 0.2004; VT + mic 0.0601 (= 0.4 × 0.5 × 0.3); mic only after VT release 0.1199 |
| Cancel during speech | silent 14.9 ms after the command (50 ms fade) |
| Cancel before admission | main loop 0.37 ms, detach 0.08 ms (B0 NULL-first: 1237 ms) |

## 10. F14: the whole recording reaches program

- **EOS is not treated as the end.** The fire records each buffer's render end
  (`admission + pts + duration`) and retires 30 ms after the last one. It is bounded by the completion
  deadline if it never drains.
- A synthetic 150 ms **tail** marker is rendered in full (0.1500 s) in all eight fires across rates and
  formats.
- The same file on the unchanged r0144 `_vt_fire_file` path loses 63-80 ms of the tail marker (negative
  control in the same module).
- Mutation: retire on EOS, and the tail test fails.
- **Ordinary deck retirement and non-VT FX carts are unchanged** (F14b and the FX tail are separate
  defects; §12).

## 11. F15: starvation eliminated in the VT-specific path

- The scheduled deck is prepared **unlinked**, so a stalled decode never leaves an empty pad on the
  non-live deck mixer.
- **With the incoming stalled 1.0 s:**
  - the outgoing continuity was **1.0000**, 0.000 s missing (B0 linked-at-open: 0.976, 0.07 s missing;
    primed r0144: 0.37);
  - the readiness deadline failed the deck 104 ms after open;
  - no music aired, and there was no air start;
  - no queue advance from the scheduled slot: claims only `[1, 2]`. The two queue-exhausted calls are the
    outgoing's own r0144 end-of-deck path on slot A.
- Mutation: link at open, and the loss returns (continuity 0.985, 40 ms missing; the test fails).
- **Residual exposure:**
  - between `ready` and admission, the deck holds one queued buffer in the mixer pad, so starvation is
    impossible;
  - after admission the deck is an ordinary playing deck, and F15 for a mid-song stall is the r0144
    hazard, unchanged.

## 11a. Validation results

| Check | Result |
|---|---|
| B1 planner (C02) | 35/35 |
| B1 mutations (C03) | 5 of 5 detected |
| B2 module (C13) | 19 tests, all pass (18 + the VoiceTrack resolution test, also in C32) |
| B2 mutations (C14) | 6 of 6 detected: premature EOS retirement (tail), duck wrong owner, fire stale callbacks, early playback accounting (4 failures), stale scheduled admission, link-at-open F15 (continuity 0.985) |
| Final regression C32 | **846 tests**, 34 modules, 1060 s (list below). Only failures: **44 errors**, all `production.errors.MediaRejected` in the three voice-track ProductionMedia modules. |
| r0144 baseline comparison (C21) | The 44 voice-track errors are **identical** on a clean r0144 `e4003aa` worktree: a pre-existing limit of the sandbox media validator, the same class as earlier releases. They are not caused by B1-B2. |
| Discarded run C20 | 5 extra source-inspection failures. `engine.py` was edited while that run was in progress, so `inspect.getsource` read shifted lines. None recur in C32, which ran on frozen, hash-checked sources. |
| `manage.py check` (C30) | no issues |
| `makemigrations --check --dry-run` (C31) | No changes detected |
| `git diff --check` and new-file whitespace | clean |

The C32 modules cover:
- **Engine:** deck lifecycle, EOS plausibility, mic clock policy, mic recovery, playback accounting,
  boundary evidence, duration semantics, position accuracy, queue ETA, restart resume, restart safety,
  seek audio flow, command queue.
- **FX:** fire FX cart, FX bus live reload, FX fire completion, FX fires state.
- **Operator paths:** Hour Refill Eject and Start Next, PTT auto/manual, queue Swap claim, Remote DJ gate
  binding, Start Next accounting, API, engine and fade lifecycle, transport safety 2.23.
- **Voice tracks:** enforcement, production media, no-air media.
- **2.25:** Phase A characterization (17, unchanged and passing: the live path is still r0144), B0 (27),
  B1 (35), B2 (19).

## 12. Remaining limitations

1. **Not connected.** The r0144 VT path and its defects (F1-F13) are unchanged in production until B3/B4.
   The Phase A characterizations remain as historical negative controls.
2. **Live-deck semantics for B4.** A `preparing` or `scheduled` deck is in `self.decks` and is "live" for
   `_live_decks()`, but has `mixer_pad None` while `preparing`. Start Next's pad fade, pause, seek and Eject
   must handle it explicitly; B4 cancels the VT and re-admits or retires first.
3. **The duck curve overrides the r0144 stepper once created.** B4 must remove `_vt_ramp_duck_to_db` in
   the same change that first uses the curve. B2 never creates it in production.
4. **F14b** (deck tails 71-177 ms) and the **FX cart tail** are unchanged, separate defects.
5. **Readiness deadline 100 ms.** It assumes local storage, against 3-4 ms measured; slower storage would
   fail scheduled admission and fall back to ordinary starts. It is a constant, `SCHEDULED_DECK_READY_S`.
6. **Remote DJ gate, real ALSA/StereoTool output and real-hardware loopback** are not exercised (B0 N4/N5).
7. **One VT teardown worker.** A poisoned worker (a NULL taking more than 5 s) rejects later VT teardowns
   with an operator event. Decks are unaffected because they use their own per-slot coordinators.

## 13. Refined B3-B4 work plan

**B3, the outro side, connected behind a single dispatch point:**
1. Compute `OutroFacts` when the outgoing occurrence is admitted, off the poll path.
2. On the outgoing generation's crossing of Outro Starts (the per-poll position and planned timeline):
   - prepare the fire for `O_s` with at least 0.35 s of lead;
   - keep an aired set keyed by LogItem id;
   - apply the urgent no-hold rule;
   - hold the ordinary trigger while `hold_until` is in the future, releasing it at the deadline or on a
     terminal.
3. Duck through `VtDuckCurve`, and retire the r0144 outro phases (`outro_playing`, `outro_tail`).
4. Tests:
   - Phase A R4a, R6 and R9 inverted;
   - B0 E07 and E08 as engine tests;
   - the D13 resume cases.

**B4, the intro side:**
1. In `_start_next_track`, for an `automatic` start whose plan has an eligible intro:
   - `_create_deck(..., scheduled_admission_ns=deck_offset)` for the claimed occurrence;
   - resolve the VoiceTrack for the **claimed** track (one indexed read);
   - on `ready`, `_vt2_prepare_fire_for_voicetrack` at `intro_start`;
   - on `failed`, an ordinary start;
   - on fire failure or cancel, `_vt2_reopen_held_occurrence(now + 0.1)` when `U > 0`.
2. Remove `_vt_maybe_enter`, the intro phases and the stepper.
3. Start Next, Eject, pause and seek handling of scheduled decks and fires (D5, D8, D9).
4. Tests:
   - Phase A R1-R3b, R5, R7, R8, R10b and R11 inverted;
   - L3 PostgreSQL request, dedication and Swap at claim;
   - the incident fixture end to end.

**B5-B7** are as in contract §10.

## 14. Restart and migration inventory (for an eventual release)

- **No migration**: no model, field or help_text change; `makemigrations --check` was clean.
- **`isadoraair-engine` restart** (engine code). The r0144 restart safeguard applies.
- **No Gunicorn restart** for B1-B2: no template, static, view or URL change.

## 15. Production unchanged

Nothing outside `~/isadoraair-2.25-phase-a` (code) and `~/isadoraair-2.25-evidence` (records) changed:
- no change to `/opt/isadoraair`, the production database, media, services, station configuration,
  iPortal, ProductionMedia binding or the protected updater runtime;
- nothing pushed or published;
- the CSV and NWS candidates were not integrated.
