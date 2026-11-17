# Roadmap 2.25: Evergreen Voice Track transition contract (approved) and Phase A engineering audit

Status: **Phase A accepted (6ad5222). All fifteen operator decisions approved (Phase B0 workorder).
Phase B0 timing feasibility complete; see `2_25_PHASE_B0_TIMING_FEASIBILITY.md`.** No production engine
behaviour has changed. Production implementation (B1 onward) needs a separate engineering gate.

- Audited baseline: r0144, canonical `e4003aa7f075b940ec5680bf6864f08530ceed05`, tree
  `ca941ed15e937cbf0508f2b03aa6545fb39f8c01`. It is identical to `/opt/isadoraair` and canonical main at
  the time of the audit.
- Line references are to `library/services/engine.py` at that commit unless another file is named.
- Sections 1-3 are the accepted Phase A audit (unchanged). Sections 4-11 are the approved contract,
  reconciled with the decisions in section 11 and the B0 measurements.

## 1. Confirmed production incident (KOGR, 2026-11-16 20:45:04)

The incident is preserved as the synthetic fixture `INCIDENT_2026_11_16` in the test module. It uses
metadata only; no station music or recording is needed.

| | Outgoing 18911 *Rain Down Tears* | Incoming 37652 *'99 Mustang (promo)* |
|---|---|---|
| Duration | 261.530612 | 209.798186 |
| Cue In | 0.274376 | 0.124717 |
| Next Start | 255.943311 | 200.745 |
| Outro Starts | **NULL** | 182.145 |
| Intro Until | NULL | 15.008 |
| VoiceTracks | none | intro VT 6 (6.456229 s, 0 dB, valid ProductionMedia `4b39b81c-…`) |

The ordinary trigger is the outgoing's Next Start minus the incoming's Cue In:
255.943311 − 0.124717 = **255.818594**. The journal recorded 255.8.

**Root cause.** Voice Track entry is evaluated only inside the outgoing deck's poll:
`outro_start is not None and pos >= outro_start` (`engine.py:16160-16166`). The outgoing marker was NULL,
so `_vt_maybe_enter` never ran and the incoming intro VT was never looked up. The ordinary trigger then
started '99 Mustang at 255.8 (`16168-16178`). That matches the journal exactly: no VT enter, fire,
completion, abort or lookup message.

**Reproduced.** R1a uses the exact incident markers with the position patched to 255.85. R1b is a
real-time, real-audio run of the same shape:
- the VT was looked up zero times and fired zero times;
- 0.00 s of intro-VT audio reached program, against more than 2 s of incoming music;
- in the control case, the only change is an Outro Starts marker on the outgoing. With it, the same pair
  is evaluated and VT mode owns the transition.

## 2. Audit: where each responsibility lives today

| Responsibility | Where (r0144) | Observation |
|---|---|---|
| Entry and timing | `_poll_position` 16154-16166 | Single entry point. It needs the outgoing's `outro_starts`, a non-NULL `next_item` (16144), Auto mode, VT idle, an untriggered segue, deck age ≥ 5 s and position > 5 s (16095-16112). |
| Incoming identity | `_vt_maybe_enter` 9832-9851 | Read-only `_peek_playable_at_cursor()` at entry. It is stored as `incoming_track` and **never revalidated**. |
| Outgoing identity | 9875; 16047-16049, 16500-16503, 15618-15621 | Matched by **track id**, not by occurrence (LogItem and generation). |
| Eligibility | 9829-9864, `_vt_resolve_audio` 9810-9818 | An attached record is enough. A record whose `playable_audio()` is None still enters VT mode with nothing to fire. |
| Dedication suppression | 9846-9851 | Sees only a SongRequest **already** scheduled into the peeked row. A request scheduled at start is not seen (`_start_next_track` 7178). |
| Phase machine | 9945-10061 | `outro_playing` → `outro_tail` → `inter_gap` → `intro_playing` → idle. The incoming waits for the outgoing's **EOS**, then `min_gap_ms`, then intro phase. |
| Intro fit | `_vt_start_intro_phase` 10004-10008 | `delay = vt_duration − intro_until` when positive. Because a deck plays from media 0 after a 0.3 s silence prime (`SILENCE_PRIME_SECONDS`, 890), this ends the VT 0.3 s before the vocal post. Cue-in is already inside the media timeline (verified: cue-in is never sought; 16145-16146, 6918). |
| Incoming start | `_vt_start_incoming_now` 10011-10032 | Calls `_start_next_track` **regardless of Manual mode or a live mic**. If another deck is live it skips the start but still lets the intro VT play. |
| Occurrence authority | `_start_next_track` 7139-7232 → `_create_deck` → `_claim_playback_occurrence` 7332-7410 | Request scheduling (7178), dedication splice (7194), urgent/forced items (`_next_queue_item` 7003-7035) and Swap adoption under the claim lock (7354-7369) all happen **after** the VT lookahead. |
| Fire lifecycle | `_vt_fire_file` 9738-9807, completion probe 9637-9679 | Completion is the EOS pad probe only. A branch error is **not routed**: `_on_main_bus_error` 3353-3377 resolves decks, the mic and output slots, and returns True for FX/VT elements. There is no first-buffer check and no deadline. |
| Ducking | `vt_duck_gain` 5040-5048, ramp 9707-9736 | Attack at **entry**, before any speech. Release only at `_vt_complete`/`_vt_abort`. The topology is `mixer → vt_duck_gain → duck_gain(mic) → program_fx_mixer(+FX/VT)`, so the mic duck never attenuates VT speech. |
| Operator paths | Start Next 10189 (`busy_voice_track` refusal); Eject 15604-15637 (2.23 D9); reload abort 15688; pause/resume/seek 14400-14480 | Pause, resume and seek have no VT handling. Mic/PTT (`_set_mic_ptt` 11577, `_apply_mic_mode_hold` 11592) and Manual (`_set_manual_mode` 11650) have no VT handling. |
| Observability | `_write_state` 17069 | VT phase is not published. `reload_voicetrack_config` is a no-op (10872). |

**Existing coverage.**
- `test_transport_safety_2_23.py` (real GStreamer, via `_transport_harness.py`):
  - Eject E5/E7/E8;
  - natural EOS, watchdog, stale timers and reload;
  - the intro-only characterization (341-375).
- `test_fx_fire_completion.py` (12) and `test_fx_fires_state.py` (22): the FX/VT branch lifecycle.
- `test_voicetrack_production_media.py` (41), plus the iportal, enforcement, browser, preview-cache and
  no-air-media tests: media binding and `playable_audio()`.
- **Gaps:** nothing asserts VT timing or audio content, the incoming occurrence, fire failure, live
  talent, pause/seek, or restart.

## 3. Confirmed defects and findings

Every row except F10-F13 was reproduced on r0144 by `test_vt_transition_2_25_phase_a.py`. It has 17
tests: 14 defect characterizations plus the R0, R10 and R10b measurements. Each characterization passes on
r0144 and asserts the defective behaviour. F10 is the existing 2.23 E8 characterization; F11-F13 are source findings.

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | **P1** | An incoming intro VT is evaluated only when the **outgoing** has Outro Starts. This is the incident. | R1a, R1b; 16160 |
| F2 | **P1** | The intro VT is bound to the lookahead, not to the occurrence that starts. Each case below aired song 2's intro VT over something else. | R2, R3, R3b; 9832 vs 7155-7216 |
| | | (a) An urgent Weather/AMBER insertion at the cursor: the VT aired over the alert (0.93 s overlap). | R2 |
| | | (b) A Web Request swap at start: the VT aired over the requested song. | R3 |
| | | (c) A dedication spliced at start: **two recorded voices** at once (0.63 s overlap). | R3b |
| | | Also affected by source analysis (same mechanism): Swap adoption at claim, Active Message splice, forced follow-ups, rollover, live-fill and skip. | |
| F3 | **P1** | A VT fire that never completes strands the transition. The fire's branch error is unrouted and there is no deadline. | R4a, R4b; 3353-3377, 9637-9679 |
| | | Undecodable **outro** VT: `outro_tail` forever, **dead air** (≥ 5 s observed, unbounded), program left ducked. | R4a |
| | | Undecodable **intro** VT: the incoming song and every later song stay **6 dB ducked**; VT is busy forever, so Start Next is refused permanently. | R4b |
| F4 | **P1** | A live mic or Manual mode does not stop a VT sequence. The intro VT fires and the next song starts while the mic is live and Manual is on. VT speech also bypasses the mic duck (topology above). | R5; 10011-10032, 5040-5061 |
| F5 | P2 | Intro-only VT on an outgoing *with* Outro Starts has two problems. | R8 |
| | | The outgoing tail is ducked with **no speech** over it: 3.1 s measured; the incident pair with a 250.0 s marker would give 11.5 s. | |
| | | The Next Start segue is dropped: the incoming waits for the outgoing's EOS plus the gap. Measured 2.4 s later than the ordinary segue; about 5.9 s for the incident pair. | |
| F6 | P2 | An attached but unplayable VT (no ProductionMedia, no legacy file) still takes over the transition. It ducks with nothing to air and suppresses the segue. | R11; 9856-9864 |
| F7 | P2 | Outro Starts at or after the trigger: the ordinary segue fires first and the outro VT is lost silently. | R6 |
| F8 | P2 | An outgoing item shorter than the 5 s poll gate (IDs, sweepers, spots) never evaluates the next song's intro VT. | R7; 16095-16112, 16510-16528 |
| F9 | P2 | Nothing records that an outro VT aired. A deck resumed past its marker after an engine restart **fires its outro VT again**. | R9 |
| F10 | P2 | Eject of the incoming deck during its intro VT: the ejected song's VT keeps playing over the replacement. This is the 2.23 E8 characterization; it is a wrong-occurrence case under this contract. | `test_transport_safety_2_23.py:188` |
| F11 | P3 | Pause, resume and seek of a deck inside a VT sequence have no VT handling. A seek back keeps the outgoing ducked for its remainder. | source 14400-14480 |
| F12 | P3 | Identity by track id: a back-to-back repeat of the same track can be misattributed. | 16047, 16500, 15618 |
| F13 | P3 | VT phase is not in `engine_state.json`. VT lookups and three `VoiceTrackConfig.load()` calls run as DB work on the GLib thread at the poll. | 17069; 9827-9866, 9977, 10035 |

**Not defects (measured):**
- **VT fire precision.** A fire is audible **0 ± 7 ms** after `_vt_fire_file`, and its first 150 ms is
  intact, for WAV and FLAC alike (R10, 8 runs).
- **The r0144 intro fit never overruns the vocal post.** It ends the VT exactly one silence prime
  (0.3 s) early (R10b: music audible 0.560 s after the VT, against 0.3 + 0.25 cue-in expected).

## 4. Timing model (approved; pipeline running time)

Every instant below is a **pipeline running time at which the sample is rendered by the program
mixers**. It is not the time a command is issued. B0 measured these instants in rendered PCM.

| Symbol | Meaning (approved) |
|---|---|
| `V` | Validated playable VT duration (`playable_audio().duration_seconds`). |
| `C` | Effective incoming music start: its Cue In (media position). |
| `I` | Intro Until marker (media position). |
| `W` | `max(0, I − C)`, the usable instrumental introduction. |
| `M` | 0.150 s vocal safety margin (D11, fixed engine constant, no admin field). |
| `U` | `max(0, V − W + M)` when `W > 0`; `V + M` when there is no usable window (D10). The music underlap. |
| `A_seg` | Running time at which the outgoing's Next Start is rendered (the planned segue point). |
| `O_s`, `O_end` | Outro VT start (the outgoing's Outro Starts rendered) and `O_s + V_O`. |
| `gap` | `VoiceTrackConfig.min_gap_ms` between two recorded voices (unchanged meaning). |

Plan. A term exists only if that side is eligible.

```
A_0  = max(A_seg, O_end)          segue point, held while an eligible outro VT talks (D3)
S_v  = max(A_0, O_end + gap)      intro VT speech starts (D1 front-timed to the music, after separation)
A_I  = S_v + U                    incoming music (media position C) rendered (D2 / D10)
A_I + W                           the vocal entry (Intro Until) is rendered
S_v + V  ≤  A_I + W − M           speech always ends at least M before the vocal (equality when U > 0)
```

- With no intro VT, `A_I = A_0`.
- With neither side eligible, nothing here applies. The **r0144 ordinary path runs unchanged**, and its
  segue lands +0.78 to 0.79 s after Next Start (R0; B0 E01).
- A VT transition is **exact**. Its music lands at `A_seg` itself, not at r0144's late ordinary point.
  See note N1 in the B0 report.

**Admission (B0 result).** Each branch is admitted at its planned instant by a pad offset set before the
branch starts:
- the intro and outro VT branches use offset `S_v` (or `O_s`);
- the incoming deck uses offset `A_I − C` (direct scheduled admission, see §6).

The first buffer of each branch arrives early and waits in the aggregator pad. The mixer renders it at
exactly the offset, with nothing lost from the start of the recording.

### Worked examples (incident fixture: `C = 0.124717`, `I = 15.008`, so `W = 14.883283`)

```
A. Incident VT, V = 6.456229:     U = max(0, 6.456229 − 14.883283 + 0.15) = 0
   speech S_v .. S_v+6.456229; music rendered at S_v; vocal at S_v+14.883283; clearance 8.427054 s.
   (r0144: the intro VT never aired.)

B. Synthetic long VT, V = 18.0:   U = 18.0 − 14.883283 + 0.15 = 3.266717 s
   speech starts at S_v = A_seg (over the outgoing's tail); music rendered at S_v+3.266717;
   speech ends S_v+18.0; vocal at S_v+18.150000 (clearance exactly M).

C. Exact fit, V = W − M:          U = 0, clearance exactly M.

D. Missing / unusable Intro Until (I ≤ C or NULL): W = 0, U = V + M:
   the whole VT, then music M after it.

E. Outro VT 9.0 s from Outro Starts 250.0 (outgoing Next Start 255.943311, so O_end > A_seg):
   A_0 = O_end; with an intro VT, S_v = O_end + gap and the music follows by U.

F. Outro VT outlasting the outgoing: dry speech after the outgoing ends, music at O_end (D3),
   subject to the urgent rule (§5) and bounded recovery (§7).
```

Measured precision (B0, rendered PCM, 44.1 and 48 kHz, WAV and FLAC): see the B0 report. The approved
target of ±10-15 ms is **demonstrated**. Every scheduled onset and speech boundary measured within ±0.1 ms
of plan.

## 5. Transition contract (approved)

### Eligibility (attached ≠ eligible)

A side is **eligible** only if every condition below holds. Otherwise it is suppressed, with one
deduplicated event giving the reason (D4, D7).

**Media.** `VoiceTrack(track, position)` exists, and `playable_audio()` returns a present file with a
validated duration greater than 0. The legacy fallback is allowed exactly as `playable_audio()` defines
it. Eligibility is decided from playable media, never from the record alone (F6).

**Markers.** Each marker must be finite and follow the existing `library.services.cue_markers` ordering
rules.
- **Outro:** `cue_in < outro_starts < next_start ≤ duration`.
  - A missing or invalid Outro Starts suppresses the outro VT (D4).
  - Outro Starts at or beyond the ordinary segue boundary also suppresses it, and does not delay the
    ordinary transition (D7).
- **Intro:** `0 ≤ cue_in < intro_until ≤ duration` gives a usable window.
  - A missing or unusable Intro Until does not suppress the intro VT. The whole VT plays before the
    music (D10).
  - iPortal's existing requirement is unchanged: it still needs the marker before an intro VT can be
    recorded (`voicetrack_media.py:104-111`).

**Live talent.**
- The engine is in Auto.
- No studio mic and no Remote DJ gate is live (D6).

**Outro side.**
- This deck generation's own playback crossed `outro_starts`, rising from a start position before it.
- A continuation (resume) generation qualifies only if its start position, plus the resume evidence's
  staleness bound, is still before `outro_starts`. Otherwise prior delivery is uncertain, and the outro VT
  is suppressed (D13).
- The outro VT has not already aired for this occurrence in this process.

**Intro side.**
- The incoming occurrence is a fresh **automatic** start: the trigger, natural, error or watchdog
  completion, or the end of a VT hold.
- No intro VT for (D5):
  - an explicit **Start Next**;
  - a **manual deck start**, including the replacement started by **Eject**;
  - the start released by a **deferred Manual-to-Auto handoff**.

  The next ordinary automatic transition after any of these is eligible again.
- No intro VT for (D13):
  - an occurrence that already has `playback_claimed_at` (interrupted-occurrence resume, deck
    re-creation);
  - a continuation generation.
- Protected speech paths are untouched (D14). There is no intro VT for:
  - a dedication intro or its follow-up song;
  - an urgent Weather/AMBER item (`wx_alert_playout`);
  - an Active Message item.

  Promos and IDs with an explicitly attached evergreen VT are otherwise eligible.

The two sides are evaluated **independently**. The outgoing's Outro Starts has no bearing on the incoming
intro VT, and the incoming's Intro Until has no bearing on the outgoing outro VT.

### Urgent priority (D3, D14; r0137 preserved)

Recorded speech never gains open-ended authority over an urgent announcement. Urgent insertion,
deduplication and the canonical-Track synchronization of r0137 are unchanged; this contract only limits
the VT.
- **Urgent item already next when the transition is planned:**
  - no segue hold, and no intro VT on the urgent item;
  - an outro VT is eligible only if it ends by `A_seg`; otherwise it is suppressed with a reason.
- **Urgent item committed while a transition is pending:**
  - every not-yet-admitted VT is cancelled before it is audible;
  - a segue hold is released at once, and the urgent item starts by the ordinary path.
- **Urgent item committed while outro speech is audible:**
  - the outro VT continues only up to `A_seg`, then fades out over about 50 ms;
  - the urgent item is never held past the ordinary segue point.
- **An intro VT that is already audible** belongs to the claimed incoming song. The urgent item airs after
  that song, which is the existing r0137 cursor contract. The VT does not delay it.

### Behaviour matrix

| Case | Behaviour |
|---|---|
| Neither side | **r0144 ordinary path unchanged**: trigger, clamp, crossfade, EOS advance, timing (R0 and E01 baseline). |
| Intro VT only | The outgoing is untouched: no early duck and no EOS wait. The VT starts at `A_seg` (D1). If `U > 0`, speech starts first and the music is admitted `U` later (D2). Speech ends at least `M` before the vocal. |
| Outro VT only | The outro VT starts as this generation crosses `outro_starts`. If it ends by `A_seg`, the segue is unchanged. Otherwise the music is held until speech ends (D3), within the urgent rule and the §7 bounds. |
| Both | As above. The intro VT starts at least `gap` after the outro VT ends, so two recorded voices never overlap. |
| Unusable audio on a side | That side is ineligible, and the other side proceeds alone. If neither side remains, the ordinary path applies. |
| A fire fails, stalls or overruns | Terminal "failed" (§7). Held music is admitted at once, and the transition proceeds as if that side had ended. |
| Missing or unusable markers | See Eligibility (D4, D7, D10). |

**Ducking (D12).** `vt_duck_gain` is owned by **actual VT speech**:
- the attack starts one `duck_ramp_ms` before the planned speech start;
- the release starts at speech end, or immediately on cancel or failure;
- speech intervals closer than 2 × `duck_ramp_ms` merge into one duck.

The duck is a running-time control curve on `vt_duck_gain` (B0), not a GLib stepper. The mic duck
(`duck_gain`) stays independent.

`program_duck_db`, `duck_ramp_ms` and `min_gap_ms` keep their names, admin forms and meaning. There is no
schema change.

## 6. Authoritative incoming occurrence and admission (F2)

**Plan (read-only).** The lookahead (`_peek_playable_at_cursor` / `_peek_next_hour`) only estimates:
- timing (`V`, `W`, `U`);
- whether a segue hold is needed;
- the instant `A_seg − C − lead` at which the incoming deck must be opened.

It never decides what airs. Its database reads move out of `_poll_position` (F13). They are taken once,
when the outgoing occurrence is admitted, or in the existing look-ahead/refresh path. They are cached by
`(log_item_id, track_id)`.

**Commit (the deck-open path).** Everything that can change the occurrence happens first, in the existing
`_start_next_track` → `_create_deck` path:
- request scheduling;
- dedication splice;
- forced, urgent and Active Message items;
- rollover or live-fill adoption;
- unplayable skip;
- claim-contention retry;
- Swap adoption under the claim's row lock.

The claim runs before any VT is considered. Only after it does the engine resolve the intro VT for the
**claimed** `log_item.track`. That is one indexed `VoiceTrack` read, in the same deck-open path that
already performs the claim. Eligibility is decided there, and the plan is computed for this occurrence.

**Admission.** In a VT transition the incoming deck is opened in **scheduled admission** mode:
- the existing deck bin, **without** the 0.3 s silence prime;
- its pad offset set to `A_I − C` before the bin starts, instead of "now".

A VT transition is the only thing that opens a deck this way. An ordinary start still opens a primed deck
whose offset is "now" (r0144 unchanged).

**Viability before speech.** The VT branch is prepared only after the claimed deck has produced
**readiness evidence**: its first decoded buffer at the real stage (`seek_gate_pad`).
- B0 measured that this evidence arrives about 3 ms after a scheduled deck opens, for every format and
  rate tested.
- A primed deck cannot give this evidence for FLAC or resampled media until its prime starts. That is
  only `P + C` before the music, which is after speech has started when `U > P + C` (B0 DeckReadiness).
- A scheduled deck's mixer pad is empty until its first buffer arrives, and the non-live deck mixer
  waits on an empty pad. So a stalled decode would freeze the outgoing too (F15, pre-existing in r0144
  at the prime-to-music switch; B0 E12).
- **Readiness deadline: open + 100 ms.** If no evidence by then, the deck is detached at once. Detaching
  releases the pad, and the mixer stops waiting. The intro VT is skipped, and the occurrence is re-opened
  as an ordinary primed start. B0 E12c bounds the outgoing interruption to the deadline.
- If the evidence has not arrived by `S_v − 50 ms` for any other reason, the intro VT is skipped before it
  is audible.

**Fire ownership.** The intro VT is owned by `(slot, log_item_id, generation, transition_seq, fire_id)`.
It is admitted only if, at preparation:
- that generation is the live incoming;
- Auto is on and no mic is live;
- the sequence is current.

Track id alone never identifies an owner.

**Change of plan.** If the claimed track differs from the planned track, the plan is recomputed for the
claimed occurrence. If the recomputed plan cannot be met, for example because a new underlap would need
an admission instant that has already passed, the intro VT is skipped rather than started late or
clipped.

The queue is never frozen. The only lock is the existing one-row claim lock (`lock_timeout 250ms`). Queue
mutation guards, Hour Refill, live fill and the native Active Message restrictions are untouched.

## 7. State machine and failure safety

One owner record replaces the phase dictionary:

```
transition = {seq, outgoing:(slot, log_item_id, generation), incoming:(slot, log_item_id, generation),
              plan:{A_seg, O_s, O_end, S_v, U, A_I}, hold:{until, reason},
              outro:{fire_id, state}, intro:{fire_id, state}}
fire state: planned → preparing → ready (first buffer waiting in the mixer pad) → admitted
            → ended | failed | cancelled
```

**Fires.**
- Each fire is a scheduled branch, admitted by its pad offset.
- Its probes record readiness (the first buffer) and completion (EOS, dropped at the branch).
- Branch errors are routed to the owning fire. `_on_main_bus_error` resolves VT elements, as it already
  does for decks, the mic and output slots.

**Deadlines (B0-verified).** All deadlines are in running time and owner-checked by `(seq, fire_id)`.

| Deadline | Instant | Result |
|---|---|---|
| Ready | `admission − 50 ms` | Not ready: **failed before audible**. Nothing is clipped, and nothing aired. |
| First audio | admission + 1.0 s | No buffer by then: failed. With ready-before-admission, this is a backstop that should not fire. |
| Completion | admission + `V` + 2.0 s | Failed (overrun). Speech is cut, and the next step proceeds. |

**Every terminal releases.** Whether a fire ended, failed or was cancelled, it releases its ownership and
its part of the VT duck curve. If it held the music, the held deck is **re-admitted at once by
regeneration**, so there is no dead air for `U`:
- the held generation has never been audible, and is detached;
- a replacement generation of the **same claimed occurrence** opens with admission at now + 0.10 s (plus
  `C`);
- B0 E09d measured the replacement ready in about 4 ms and the music at its new target (0.0000 s), with
  the outgoing continuous.
- Moving the pad offset of a held deck forward does **not** work. Its queued first buffer anchors the
  old time, and the music is lost (B0 E09b).
- A blocking-probe gate, which would allow that, starves the deck mixer (B0 E04d). It is rejected.

No terminal path:
- starts an extra deck (the existing `_live_decks()` and claim rules apply);
- skips an otherwise valid song;
- leaves a hold set. A transition deadline (planned end + 3 s) releases any hold, and in Auto runs the
  ordinary start.

**Retirement.**
- **Detach first, on the main loop.** Unlink the branch, then release its aggregator pad. This wakes a
  streaming thread waiting there with a held buffer.
- **NULL transitions and removal run off the GLib main loop.** For decks this is the existing
  `BoundedTeardownCoordinator`; for VT branches it is the same kind of bounded worker.
- B0 E09c measured the hazard of the other order: NULL-first on a held branch blocks for the rest of
  its hold.

**Stale callbacks.** Every timer, probe callback and bus-error route carries the owner token. A token
that is no longer current is ignored and counted. B0 records these as `ignored`.

**The mic duck is independent.** A VT cancel releases only the VT duck.

## 8. Operator-controlled transitions (approved)

| Event | Behaviour |
|---|---|
| Studio mic or Remote DJ gate goes live | Not-yet-audible VTs are cancelled. Audible speech fades out over about 50 ms (D6). The VT duck is released; the mic duck holds independently. The existing Manual hold then applies: the incoming waits (`_manual_hold_pending`). |
| Manual mode (operator) | No VT is planned or admitted in Manual. Audible speech completes. A hold waits for Auto, like the ordinary trigger. |
| Deferred Manual-to-Auto handoff | The released start is ordinary, with no intro VT (D5). The next automatic transition is eligible. |
| Start Next during a VT | Speech is cancelled with a 50 ms fade (D8). If the next occurrence is already claimed and held, it is **re-admitted by regeneration** (same occurrence, no second claim). Otherwise the 2.23 Start Next runs. Either way there is exactly one advance, with no double advance and no skipped occurrence. This replaces the `busy_voice_track` refusal. |
| Eject the outgoing during its outro VT | Keep 2.23 D9: the outro VT completes, then the incoming (D9). |
| Eject the incoming during its intro VT | The intro VT is cancelled (D9, F10). Held music never airs. The replacement is a manual start with no intro VT (D5). |
| Pause, seek or resume of a deck with a VT side active | That side is cancelled. There is no replay on resume or re-crossing (D13 rules, §5). |
| Play Now or log reload | Everything is cancelled (existing). |
| Urgent insertion | §5 urgent rule. |
| Engine restart | VT state is not persisted. Claim evidence (`playback_claimed_at`) and the resume hint (`saved_position`, `state_age_seconds`) enforce D13 without a migration (§5). |

## 9. Validation design and regression matrix (revised after B0)

### Layers

| Layer | What it runs | What it proves |
|---|---|---|
| **L1 Pure** (`vt_transition` planner, no GStreamer) | §4 examples to 1 µs; `V = W − M` exactly; long, short and zero `V`; Cue In variations; missing, zero, inverted and out-of-range markers; Outro Starts before, at and after the segue boundary; attached-but-unplayable records; `program_duck_db = 0`, `min_gap_ms = 0`; urgent-next and the D5/D13/D14 eligibility tables. | The plan and eligibility are right before any audio exists. |
| **L2 Engine state machine** (transport harness) | R1–R11 inverted; owner tokens; deadlines; cancellation; re-admission; exactly-once claims; no extra deck; the duck at unity at every terminal; stale callbacks ignored. | The state machine routes every case correctly. |
| **L3 PostgreSQL** | Real `VoiceTrack` + `ProductionMedia` (valid, invalid, missing bytes, legacy, no-air refusal); SongRequest scheduling and dedication splice at claim; a Swap committed between plan and claim; urgent and Active Message splice; next-hour adoption and live-fill install; claim-contention retry. | The intro VT is that of the claimed occurrence, or none. |
| **L4 Rendered audio** (the B0 lab promoted to engine tests) | B0's tone signals, sample-accurate onset and end measurement, 44.1 and 48 kHz, WAV and FLAC. | Every timing claim, measured in rendered PCM (table below). |

### Regression matrix: every finding mapped to its Phase B proof

| Finding | r0144 characterization (must fail after the fix) | Phase B assertion (L-layer) |
|---|---|---|
| F1 outgoing-marker dependency (incident) | R1a, R1b | L2 + L4: the incident fixture airs its intro VT at `A_seg` ±15 ms with no outgoing marker. The control (outgoing marker added) gives the same intro result. |
| F2 wrong occurrence | R2, R3, R3b, plus the 2.23 E8 characterization | L2 + L3: an urgent, request, dedication, Swap, Active Message, rollover, live-fill, skip or contention case either carries the **claimed** track's VT or no VT. L4: 0.00 s of a foreign VT over an alert, request or dedication. |
| F3 failed fire strands | R4a, R4b | L2: corrupt, stalled and overrun fires reach "failed" by deadline (B0 E11). L4: no dead air, held music re-admitted, duck back to unity, Start Next accepted. |
| F4 mic / Manual ignored | R5 | L2 + L4: mic live cancels pending speech, fades audible speech within 50 ms + 20 ms (B0 E13), releases the VT duck while the mic duck holds, and leaves the incoming waiting. |
| F5 speechless duck, dropped segue | R8 | L4: the duck starts no earlier than one ramp before speech and releases at speech end. An intro-only VT with an outgoing marker keeps the segue at `A_seg`. |
| F6 attached-but-unplayable | R11 | L1 + L2: ineligible with a reason; the ordinary path is unchanged. |
| F7 outro marker past the trigger | R6 | L1 + L2: suppressed with a reason (D7); no segue delay. |
| F8 short outgoing | R7 | L2 + L4: a 4 s ID followed by a song with an intro VT airs the VT. |
| F9 outro re-fire on resume | R9 | L2: a continuation generation at or near the marker gets no outro VT (D13). |
| F10 ejected incoming's VT continues | 2.23 E8 | L2 + L4: Eject of the incoming cancels its VT (B0 E09: 0.00 s of held music). |
| F11 pause/seek | (source) | L2: the side is cancelled, with no replay. |
| F12 identity by track id | (source) | L2: a back-to-back repeat of one track is owned by occurrence and generation. |
| F13 unpublished state, DB on GLib | (source) | L2: VT state in `engine_state.json`. The poll path makes no VT DB call (query-count assertion). |
| **F15 (new, B0; pre-existing) a stalled deck freezes the deck bus** | B0 E12 (primed deck stalled 1.0 s: outgoing rendered 0.83 s of 2.2 s) | L4: a scheduled deck that misses its readiness deadline is detached within 100 ms; the outgoing loses at most that (B0 E12c). The ordinary primed path is not changed in 2.25; record it as a transport follow-up. |
| **F14b (new, B0; pre-existing) r0144 deck tails cut** | B0 `R0144DeckTailTests` (71-177 ms) | Not changed by 2.25 unless separately approved: it changes every ordinary song end. B2 plans retirement for VT branches only. |
| **F14 (new, B0) r0144 VT/FX tails cut** | B0 `R0144TailTruncationTests` | L4: every VT renders its whole recording (`vt_len` within ±15 ms of `V`). Retirement happens after the last sample is rendered. Also check whether the FX cart path should get the same fix, separately scoped. |
| Ordinary path preserved | Phase A R0, B0 E01 | L4: segue distribution equal to r0144 (B0 E01: +0.770 to +0.780 s), N ≥ 10 runs. |

### Rendered-audio acceptance (L4; B0 demonstrated every row)

| Quantity | Bound |
|---|---|
| VT and music onset vs plan | ±15 ms (B0 measured ±0.1 ms) |
| Underlap `U` | ±15 ms |
| Clearance before the vocal | `≥ M − 15 ms`; equals `M` ±15 ms when `U > 0` or `V = W − M` |
| Speech length | `V` ±15 ms (whole recording, nothing cut from start or end) |
| Duck depth | `program_duck_db` ±0.5 dB |
| Duck attack and release | Starts one `duck_ramp_ms` before speech; releases at speech end ±20 ms |
| Speech separation | `min_gap_ms` ±15 ms |
| Cancel and mic fade | Audible speech ends ≤ 70 ms after the command (B0: ≤ 20 ms) |
| Speech while a mic is live | None |
| Failed VT | Leaves the program running at unity with no gap |
| Ordinary segue | Equal to r0144 |
| Repeated cycles | No drift, and no pad, element, thread or fd growth (B0 E14) |

**Negative and mutation controls.** Each mutation below must make at least one L2/L4 test fail:
- re-gating the intro on `outro_starts`;
- binding the intro VT to the lookahead;
- dropping the readiness gate;
- removing deadlines or error routing;
- removing the mic check;
- removing the aired set;
- releasing the duck at entry;
- retiring a branch on EOS before its last sample;
- NULL-before-detach.

## 10. Phase B plan, change surface and restarts (refined after B0)

| Step | Content | Fixes | Gate |
|---|---|---|---|
| **B0** (done) | Isolated real-GStreamer feasibility: scheduled admission, readiness, cancellation, retirement, duck curve, mic, repeated cycles | — | This review |
| **B1** | Pure planner and eligibility | F6, F7 (rules) | L1 |
| **B2** | Admission and fire-lifecycle primitives, dormant (nothing calls them in production yet) | F3, F14 (primitives) | L2 + L4 |
| B3 | Outro side: crossing within the generation, aired set, D13 resume rule, D3 hold within the urgent rule | F7, F9, F12 | L2 + L4 |
| B4 | Intro side: claim → resolve → readiness → admit; re-admission on cancel or failure | **F1**, **F2**, F5, F8, F10 | L2 + L3 + L4 |
| B5 | Live talent and operator commands (§8) | **F4**, F10, F11 | L2 + L4 |
| B6 | Remove the r0144 phase machine and `_vt_maybe_enter`; update the 2.23 characterizations (intro-only, E8); publish VT state; move VT reads off the poll | F13 | L2 |
| B7 | Phase A characterizations inverted, mutation controls, full qualification, r0145 candidate | — | Release review |

### B1: pure planner and eligibility (no engine change)

New module `library/services/vt_transition.py`. It is pure: no Django ORM, no GStreamer, no clock.
- **Types.**
  - `SideEligibility(eligible, reason)`;
  - `TransitionInput`: markers, `V_O`/`V_I` from `playable_audio()`, the `A_seg` and `O_s` running times, `gap`, `ramp`, the urgent-next flag and the start kind;
  - `TransitionPlan(A_0, S_v, U, A_I, O_s, O_end, hold_until, duck_points, reasons)`.
- **Rules.** It implements exactly §4 and §5:
  - D4, D7 and D10 marker rules (using `cue_markers` ordering);
  - D5 start kinds (`automatic`, `start_next`, `manual`, `eject_replacement`, `manual_to_auto_handoff`, `resume`);
  - D13 continuation and staleness rule (`saved_position`, `state_age_seconds`);
  - D14 protected paths (dedication intro and follow-up, wx_alert, Active Message);
  - the urgent rule;
  - duck-point merging (D12).
- **Tests.** L1 only, as `library/tests/test_vt_transition_planner.py`. It covers every §4 example (incident `U = 0`, 18 s `U = 3.266717`, exact fit, missing marker), every eligibility reason, and property checks: `S_v + V ≤ A_I + W − M` always, and `A_I ≥ A_0`.
- **Exit.** No engine import of the module yet. No restart is needed to ship B1 alone.

### B2: admission and fire-lifecycle primitives (dormant)

All in `engine.py`, behind methods that no production path calls until B3/B4:

- **`_vt_prepare_fire(path, gain_db, *, owner, admission_ns, validated_duration)`.** This is the
  ScheduledBranch from B0:
  - its pad offset is set before the elements start, and a target less than 50 ms in the future is
    refused;
  - readiness, last-buffer-end and EOS probes;
  - owner-checked deadlines: ready (`admission − 50 ms`), first audio (+1.0 s), completion (`+V + 2.0 s`);
  - retirement drains first: only after the last sample's running time, which fixes F14 for VTs;
  - detach-first teardown on the main loop, NULL off the main loop.
- **`_on_main_bus_error`.** Routes VT branch elements to the owning fire (F3).
- **`_create_deck(..., scheduled_admission_ns=None)`.** When set:
  - no silence prime;
  - pad offset `scheduled_admission_ns`;
  - readiness from the first real-stage buffer;
  - `started_at` and the air-start evidence anchored to the scheduled render time, not the
    (early) first buffer. Without that, `played_at` would be early by the hold.

  When unset, the deck is r0144 byte-for-byte; a test asserts the primed path's elements and offsets are
  unchanged.
- **`_readmit_held_deck(deck, now_ns)`.** Regeneration (B0 E09d), for cancel, failure and Start Next:
  - detach the never-audible held generation;
  - open a replacement generation of the same claimed occurrence, with admission at now + 0.10 s.

  Accounting must treat the held generation as never aired, with no segment and no air start. Only the
  replacement's scheduled render time is the air start. B4 decides how the claim path recognises its own
  claimed-but-unaired occurrence.
- **Readiness deadline for scheduled decks.** Open + 100 ms; on a miss, detach and fall back to an
  ordinary start (F15 bound, B0 E12c).
- **`_vt_duck_curve`.** A single `InterpolationControlSource` bound once to `vt_duck_gain.volume`. It is
  never removed (cf. the GstController binding-removal crash note) and only its points are replaced. It
  replaces the GLib stepper `_vt_ramp_duck_to_db` when B4 lands.
- **Tests.**
  - L2 lifecycle (tokens, deadlines, stale callbacks, re-admission, pad and element counts);
  - L4 rendered audio (the B0 experiments promoted to engine-level tests against these methods);
  - an F14 test for the new VT path.
- **Exit.** The ordinary path is unchanged (E01 distribution), and nothing calls the primitives.
  Shipping B2 alone would need an engine restart. Recommend landing B1+B2 together with B3/B4 in r0145
  rather than shipping dormant code separately.

### Files

- `library/services/engine.py`:
  - the VT section 9704-10061 (replaced in B4/B6) and `_vt_fire_file`;
  - `_fx_install_completion_probe` and `_fx_fire_completed` (VT branch drain; FX carts are out of scope
    unless approved, see F14);
  - `_on_main_bus_error`;
  - `_create_deck` (scheduled admission);
  - `_poll_position`, `_handle_deck_finished`, the watchdog hand-over;
  - `_eject_deck`; pause, resume and seek;
  - `_set_mic_ptt`, `_apply_mic_mode_hold`, the Remote DJ gate path;
  - `_start_next_track`, the Start Next guard;
  - `_insert_urgent_next_inner` (the urgent rule hook only; r0137 semantics unchanged);
  - `_write_state`.
- New: `library/services/vt_transition.py`.
- Tests:
  - `_transport_harness.py`;
  - `_vt_b0_lab.py` (promoted);
  - `test_transport_safety_2_23.py` (intro-only and E8 updated in B6);
  - `test_vt_transition_2_25_phase_a.py` (inverted in B7);
  - new L1–L4 modules.
- `library/templates/library/dashboard.html`: only if the `busy_voice_track` text is removed (D8).
- Docs.
- `library/models.py`: **no change**. A help_text edit alone would generate a migration.

### Expected migrations and restarts

- **No migration.** D13 is enforced from existing evidence: `LogItem.playback_claimed_at`, `played_at`,
  and the resume hint's `saved_position` and `state_age_seconds`. Nothing new is persisted.
- No protected-runtime change, no iPortal or ProductionMedia change, and no updater change.
- An **`isadoraair-engine` restart** (engine code) interrupts program audio, so the r0144 restart
  safeguard applies.
- **`isadoraair-gunicorn`** restarts only if `dashboard.html` changes (D8 text).

The exact inventory is proved at release with the import-graph check.

## 11. Operator decisions (approved, B0 workorder)

| # | Approved contract | Where |
|---|---|---|
| D1 | A short intro VT starts with the audible incoming music, not back-timed from the vocal entry. | §4 |
| D2 | A long intro VT starts first; the incoming music is delayed by `U` so the whole VT fits before Intro Until. | §4 |
| D3 | An eligible outro VT running past the segue point may hold the incoming until speech ends, subject to bounded failure recovery, operator control and urgent priority. | §5, §7 |
| D4 | An outro VT without a valid Outro Starts is suppressed, with a diagnostic reason. | §5 |
| D5 | Start Next and manual deck starts bypass evergreen intro VTs. A deferred Manual-to-Auto handoff bypasses the immediate intro VT. Later automatic transitions are eligible. **Interpretation for review:** the replacement started by Eject counts as a manual start. | §5, §8 |
| D6 | A live mic suppresses pending VTs and fades active speech out in about 50 ms. | §8 |
| D7 | Outro Starts at or beyond the ordinary segue boundary does not delay the transition. The outro VT is suppressed, with a reason. | §5 |
| D8 | Start Next during a VT cancels the VT and honours operator intent, with no double advance and no skipped occurrence. | §8 |
| D9 | Outgoing-deck Eject behaviour is preserved. Ejecting the incoming cancels its intro VT. | §8 |
| D10 | With no valid Intro Until, the whole VT plays before the song. **Interpretation:** music follows `M` after the VT, i.e. `U = V + M`, the approved formula with `W = 0`. With an insufficient window, the underlap applies. iPortal's marker requirement is preserved. | §4, §5 |
| D11 | Fixed 150 ms vocal safety margin; no admin setting. | §4 |
| D12 | The duck anticipates actual speech by the configured ramp, and releases at speech end or cancel. | §5 |
| D13 | No intro VT replay on interrupted-occurrence resume. No outro VT replay for an occurrence. Suppress where delivery is uncertain. Achieved without a migration. | §5, §10 |
| D14 | Evergreen VTs may be used on promos and IDs, but never on protected urgent or dedication speech paths. | §5 |
| D15 | Target ±10-15 ms in the qualified harness; ordinary segue preserved. **Demonstrated in B0:** ±0.1 ms measured (B09). | §4, §9 |
