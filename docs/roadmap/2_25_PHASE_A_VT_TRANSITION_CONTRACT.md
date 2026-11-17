# Roadmap 2.25 Phase A: Evergreen Voice Track transition contract and engineering audit

Status: **Phase A audit and contract-review gate.** No engine behaviour was changed. This branch
(`feature/2.25-phase-a-vt-transition-audit`) adds this document and the characterization module
`library/tests/test_vt_transition_2_25_phase_a.py` only.

- Audited baseline: r0144, canonical `e4003aa7f075b940ec5680bf6864f08530ceed05`, tree
  `ca941ed15e937cbf0508f2b03aa6545fb39f8c01`. It is identical to `/opt/isadoraair` and canonical main at
  the time of the audit.
- Line references are to `library/services/engine.py` at that commit unless another file is named.

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

## 4. Timing model (all in pipeline running time)

| Symbol | Meaning |
|---|---|
| `P` | Silence prime of a fresh deck, 0.300 s. A deck is audible `P + cue_in` after it is created. |
| `t_seg` | The instant the ordinary path starts the incoming: the trigger (outgoing at `next_start − cue_in_I`, or `next_start` when under 10 s), or the outgoing's natural completion when nothing triggered. |
| `c_I`, `u_I` | Incoming Cue In and Intro Until (media positions). |
| `W` | Usable intro, `u_I − c_I`. |
| `d_O`, `d_I` | Validated VT durations. |
| `O_s`, `O_end` | Outro VT start (the outgoing crossing `outro_starts`) and its confirmed end. |
| `gap` | `VoiceTrackConfig.min_gap_ms` (speech separation, unchanged meaning). |
| `g` | Post guard before the vocal entry. Proposed engine constant 0.15 s; **decision D11**. |

Unified plan. Every term exists only if that side is eligible:

```
S_I0 = max(t_seg, O_end − P − c_I)               incoming deck created + claimed (segue hold while the outro VT talks: D3)
S_v  = max(S_I0 + P + c_I, O_end + gap)           intro VT start: front-timed to audible music (D1), after speech separation
S_I  = max(S_I0, S_v + d_I + g − P − u_I)         incoming audio start; U = S_I − S_I0 is the underlap delay (D2)
```

The incoming deck is always created and claimed at `S_I0 ≤ S_v`. The intro VT therefore always fires for
an **already-claimed, live** occurrence; this is the F2 fix. If `U > 0`, the deck's audio is delayed by
`U` at its mixer pad. With an invalid or missing Intro Until, `u_I := c_I`: the whole VT airs before the
music (D10).

Precision available on this pipeline (measured):

| Item | Precision |
|---|---|
| VT fire | ±7 ms (R10) |
| Deck audible onset | `P + cue_in` ± one 10 ms aggregator buffer (R10b) |
| Ordinary trigger | 250 ms poll, plus a wall-clock position estimate (`_get_deck_position` 10743-10757) that lags creation |

The r0144 ordinary segue lands **+0.78-0.79 s** after Next Start (R0, A02/A03). That baseline stays **unchanged**.

VT transitions are planned on the decks' running-time pad offsets, so they can be held to about ±10 ms.
The r0144 timers chain from callback times (EOS idle callback → retirement → `min_gap_ms` timer → intro
timer), so their errors add up. EOS is also observed before the last audio sample: R8 measured an
effective gap of 0.11 s against a configured 0.20 s.

### Worked examples (incident incoming, `g = 0.15`)

```
t_seg = outgoing 255.818594

A. Incident, intro VT only (d_I = 6.456229, W = 14.883283; d_I + g ≤ W, so U = 0)
   outgoing ─────────────────────────────╮ (its own tail continues to 261.53, unducked except under speech)
   incoming          ▒▒(P .300)(c .125)♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪♪ VOCAL @ t_seg+15.308
   intro VT                         [====== 6.456 ======]          ends t_seg+6.881, clear of the post by 8.427 s
   r0144: the intro VT never aired.  r0144 with outgoing Outro Starts 250.0: outgoing ducked 250.0→261.53 with
   no speech, incoming audible at 262.16 (5.9 s after the ordinary segue).

B. Long intro VT (d_I = 18.0): U = 18.0 + 0.15 − 14.883 = 3.267 s
   intro VT starts t_seg+0.425 over the outgoing's tail (5.712 s remain, so no dry air);
   incoming audible t_seg+3.691; VT ends t_seg+18.425; vocal post t_seg+18.575 (g = 0.15).

C. Both, outro VT 4.0 s at 250.0: outro VT ends 254.0 < t_seg → the ordinary segue is unchanged;
   intro VT at t_seg+0.425 ≥ 254.0 + gap.

D. Both, outro VT 9.0 s at 250.0 (ends 259.0 > t_seg): S_I0 = 258.575 → incoming audible at 259.0, when the outro VT ends;
   intro VT at 259.2 (gap 0.2); ends 265.656; post at 273.883.

E. Outro VT outlasting the outgoing (15.0 s at 250.0, ends 265.0; outgoing ends 261.53):
   dry outro VT for 3.47 s, then incoming audible at 265.0 (D3).

F. Short outgoing (a 4 s ID; no poll ever evaluates it): t_seg = its completion; same formulas. The intro VT airs (F8 fix).

G. Missing Intro Until: u_I := c_I → U = d_I + g. The VT airs over the outgoing's tail or silence,
   and the music is audible g after the VT ends.
```

## 5. Transition contract

### Eligibility (attached ≠ eligible)

A side is **eligible** only if all of the following hold. Otherwise it is skipped, with one deduplicated
event giving the reason.
- `VoiceTrack(track, position)` exists, and `playable_audio()` returns a present file with a validated
  duration > 0. A legacy fallback is allowed exactly as `playable_audio()` defines it.
- The markers are valid:
  - finite;
  - `0 ≤ cue_in < intro_until ≤ duration` for an intro;
  - `cue_in < outro_starts < next_start ≤ duration` for an outro (D7);
  - checked with the existing `library.services.cue_markers` ordering rules.
- The engine is in Auto. No studio mic and no Remote DJ gate is live.
- For an **outro**: this deck generation's own playback crossed `outro_starts` (its start position was
  before the marker), and this occurrence's outro VT has not aired in this process.
- For an **intro**: the occurrence is a fresh, automatic start (D5); not a resume occurrence, not a
  dedication intro and not its follow-up song.

The two sides are evaluated **independently**. The outgoing's Outro Starts has no bearing on the
incoming intro VT, and the incoming's Intro Until has no bearing on the outgoing outro VT.

### Behaviour matrix

| Case | Behaviour |
|---|---|
| Neither side | Ordinary path **byte-for-byte unchanged**: trigger, clamp, crossfade, EOS advance, timing (R0 baseline). |
| Intro VT only | The outgoing is untouched (no early duck, no EOS wait). The incoming is created at `t_seg`. The intro VT is front-timed and ends ≥ `g` before the vocal post, delaying the incoming's audio by `U` when needed. |
| Outro VT only | The outro VT fires as this generation crosses `outro_starts`. The duck covers the speech only. The segue is unchanged if the outro VT ends before `t_seg`; otherwise it is held until the outro VT ends (D3). The incoming is never made to wait for the outgoing's EOS. |
| Both | As above, with the intro VT ≥ `gap` after the outro VT ends. Two recorded voices never overlap. |
| Unusable audio on a side | That side is ineligible. The other side proceeds alone; if neither remains, the ordinary path applies. |
| A fire fails or runs late at runtime | Terminal "failed", after which the transition proceeds as if that side had ended (see §7). |
| Missing, zero or invalid markers | Missing/invalid Intro Until: dry VT before the music (D10). Missing/invalid or late Outro Starts: the outro VT is ineligible (D4, D7). |

**Ducking.** `vt_duck_gain` is owned by **audible VT speech**:
- attack begins `duck_ramp_ms` before a planned VT start;
- release begins at the confirmed end of the last speech interval;
- intervals closer than 2 × `duck_ramp_ms` merge.

`program_duck_db`, `duck_ramp_ms` and `min_gap_ms` keep their names, admin forms and meaning. There is no
schema change.

## 6. Authoritative incoming occurrence (F2)

- **Plan, read-only.** The lookahead (`_peek_playable_at_cursor` / `_peek_next_hour`) is used only to
  estimate timing (`d_I`, `W`) and to decide whether a segue hold is needed. It never decides what airs.
- **Commit.** The intro VT is resolved inside the deck-open path, **after** the occurrence is final:
  request scheduling, dedication splice, forced/urgent/Active Message items, rollover or live-fill
  adoption, unplayable skip, claim contention retry and Swap adoption under the claim's row lock. It is
  resolved before the deck's mixer-pad offset is applied, so the underlap delay `U` can be applied
  there.
- **Fire.** The intro VT is owned by the incoming deck generation, by `(slot, log_item_id, generation)`.
  It fires only if that generation is still live, Auto is on, no mic is live, and the transition sequence
  is current.
- **Change of plan.** If the committed track differs from the planned track, the plan is recomputed for
  the committed track. If that needs an underlap that was not planned for, the intro VT is skipped
  rather than started late.

The queue is never frozen. The only lock is the existing one-row claim lock (`lock_timeout 250ms`). Queue
mutation guards, Hour Refill, live fill and the native Active Message restrictions are untouched.

## 7. State machine and failure safety

One owner record replaces the phase dictionary:

```
transition = {seq, outgoing:(slot, log_item_id, generation), segue_hold,
              outro:{fire_id, planned_end, state}, intro:{fire_id, owner:(slot, log_item_id, generation), state}}
state: planned → firing → audible (first buffer) → ended | failed | aborted
```

- **Fires.** Each fire carries `(seq, side)`. Branch errors are routed to their fire, so
  `_on_main_bus_error` resolves FX/VT elements as well.
- **Deadlines.**
  - No first buffer within 1.0 s: failed.
  - No EOS by validated duration + 2.0 s: failed.
  - Every failure is terminal and advances exactly like EOS.
  - Late EOS and stale timers are ignored by `(seq, fire_id)`, which is the existing 2.23 mechanism.
- **Every terminal releases ownership and the VT duck.** No terminal path:
  - starts an extra deck (the existing `_live_decks()` and claim rules);
  - skips an otherwise valid song;
  - leaves `segue_hold` set. A transition deadline (planned end + 3 s) clears it and, in Auto, runs the
    ordinary start.
- **The mic duck is independent.** A VT abort releases only the VT duck.

## 8. Operator-controlled transitions (recommended)

| Event | Recommendation |
|---|---|
| Studio mic or Remote DJ gate goes live | Suppress any VT not yet audible. Abort an audible VT with a fast fade (≈50 ms) (D6). Then the existing Manual hold applies: the incoming waits (`_manual_hold_pending`). |
| Manual mode (operator) | No VT is started in Manual. An audible VT completes (it is not competing with a mic). A VT-held incoming waits for Auto like the ordinary trigger. |
| Return to Auto | The held start fires through the ordinary path (no intro VT; D5). |
| Start Next | Abort in-progress VT speech, then the 2.23 Start Next proceeds. This replaces the `busy_voice_track` refusal (D8). No intro VT on its incoming. |
| Eject outgoing during its outro VT | Keep 2.23 D9 (the outro VT completes, then the incoming), now occurrence-bound (D9). |
| Eject incoming during its intro VT | Abort the intro VT (it belongs to the ejected song; F10); no intro VT on the replacement (D5). |
| Pause, seek or resume of a deck with its VT active | Abort that side. No replay on resume or re-cross: the per-occurrence aired set plus the crossing rule. |
| Play Now / log reload | Abort everything (existing). |
| Engine restart | VT state is not persisted. A resumed generation starts past the marker, so no outro VT (F9 fix), and a resume occurrence gets no intro VT. An intro VT interrupted before its song was claimed may air again with that song (D13). |

## 9. Validation design

**L1 Pure** (planner + eligibility, no GStreamer):
- every §4 example to the millisecond;
- `d_I` = `W − g` exactly; long, short and zero durations;
- cue-in variations; missing, zero, inverted or out-of-range markers; Outro Starts before, at and after
  Next Start;
- attached-but-unplayable records; config edge cases (`program_duck_db` = 0, `min_gap_ms` = 0).

**L2 Engine state machine** (transport harness, fakesink): R1–R11 inverted, plus:
- neither side (R0-identical claims);
- intro-only, outro-only and both;
- urgent, request, dedication, Swap adoption, Active Message, rollover, live fill, skip and claim
  contention (**intro VT always that of the claimed deck's track, or none**);
- mic before and during a VT; Remote DJ gate; Manual and return to Auto;
- Start Next, Eject of either side, pause/seek/resume, restart resume;
- fire failure at open, decode and preroll, and a missing EOS;
- exactly-once claims, no extra deck, the duck at unity at every terminal.

**L3 PostgreSQL integration:**
- real `VoiceTrack` + `ProductionMedia`: valid, invalid, missing bytes, legacy and no-air refusal;
- real SongRequest scheduling and dedication splice at claim;
- a Swap committed between plan and claim;
- an urgent or Active Message splice during a VT;
- next-hour adoption and live-fill install;
- the claim-contention retry.

**L4 Real audio** (appsink, tone-identified signals as in this module):
- the incoming media carries an 800 Hz "vocal" from `intro_until`;
- the intro VT is audible front-timed and **absent from the vocal post on**, with clearance ≥ `g` −
  10 ms;
- underlap delay = `U` ± 15 ms;
- duck depth `program_duck_db` ± 0.5 dB and ramp `duck_ramp_ms` ± 20 ms;
- speech separation `min_gap_ms` ± 20 ms;
- no speechless duck longer than one ramp;
- no VT audio while a mic is live;
- an undecodable VT leaves program running at unity;
- **ordinary segue distribution equal to the r0144 R0 baseline** (N ≥ 10 runs).

**Negative controls.**
- R1–R11 run against the r0144 engine must fail the Phase B assertions. The L2 incident case must fail on
  r0144 (the outgoing-marker dependency) and the R2/R3/R3b cases must fail on r0144 (the wrong
  occurrence).
- Mutation controls must each be detected:
  - re-gating intro on `outro_starts`;
  - binding the intro to the lookahead;
  - removing fire deadlines or error routing;
  - removing the mic check;
  - removing the aired set;
  - releasing the duck at entry instead of speech end.

## 10. Phase B plan, change surface and restarts

| Step | Content | Fixes |
|---|---|---|
| B0 spike (isolated) | Future-dated mixer-pad offsets for a VT branch and for a deck's audio delay (accuracy, deck position estimate, EOS and watchdog interaction); VT-branch error and first-buffer detection | Chooses between running-time pad offsets and anchored GLib timers |
| B1 | New pure `library/services/vt_transition.py` (eligibility + plan) + L1 | — |
| B2 | Fire lifecycle: owner token, bus routing, first and last buffer probes, deadlines; speech-owned duck | F3, F5 (duck), F6 |
| B3 | Outro side: generation crossing + aired set, independent of the incoming; segue hold | F7, F9, F12 |
| B4 | Intro side: in the deck-open path after claim, before the pad offset; audio delay `U`; automatic starts only | **F1**, **F2**, F5, F8, F10 |
| B5 | Live talent and operator commands (§8) | **F4**, F10, F11 |
| B6 | Remove the r0144 phase machine and `_vt_maybe_enter`; update the 2.23 characterizations (intro-only, E8); publish VT state | F13 |
| B7 | L2–L4 suites, negative and mutation controls, full qualification | — |

**Files.**
- `library/services/engine.py`:
  - the VT section 9704-10061;
  - `_poll_position`; `_handle_deck_finished`; the watchdog hand-over;
  - `_eject_deck`; pause, resume and seek;
  - `_set_mic_ptt`, `_apply_mic_mode_hold`, the Remote DJ gate path;
  - `_on_main_bus_error`; `_start_next_track` / `_create_deck`;
  - the Start Next guard; `_write_state`.
- New: `library/services/vt_transition.py`.
- Tests:
  - `library/tests/_transport_harness.py` (Intro Until in queue specs);
  - `test_transport_safety_2_23.py`;
  - new L1–L4 modules.
- `library/templates/library/dashboard.html`, only if D8 removes the `busy_voice_track` text.
- Docs.
- `library/models.py`: no field or help_text change (a help_text edit would itself generate a
  migration).

**Expected migrations and restarts.**
- **No migration** and no protected-runtime change.
- **`isadoraair-engine` restart** (engine code). This interrupts program audio, and the r0144 restart
  safeguard applies.
- **`isadoraair-gunicorn`** only if a template or web module changes.
- The exact inventory is to be proved in Phase B with the release import-graph check.

## 11. Decisions requiring operator approval

| # | Decision | Recommendation |
|---|---|---|
| D1 | Intro VT alignment | Front-timed to the incoming's audible start (r0144 started it 0.3 s + cue-in earlier); not back-timed to the post |
| D2 | Long intro VT | Delay the incoming's audio by `U`. The VT starts at the segue point and never talks over the outgoing before its Next Start |
| D3 | Outro VT running past the segue or the outgoing's end | Hold the incoming until the outro VT ends (dry outro tail if the outgoing ended); not music under the outro VT |
| D4 | Outro VT with no Outro Starts | Ineligible, with an event; no fallback position |
| D5 | Which starts carry an intro VT | Automatic only: trigger, natural/error/watchdog completion, VT hold release. Not Start Next, Eject, Play Now, Return to Auto or a restart resume |
| D6 | Mic live during an audible VT | Abort with an ≈50 ms fade |
| D7 | Outro Starts at or after Next Start | Outro VT ineligible (marker inconsistent), with an event |
| D8 | Start Next during a VT | Abort the VT and proceed (replaces `busy_voice_track`) |
| D9 | Eject of the outgoing during its outro VT | Keep 2.23 D9; Eject of the incoming aborts its intro VT |
| D10 | Missing or invalid Intro Until | Dry VT entirely before the music |
| D11 | Post guard `g` | Engine constant 0.15 s (no new admin field, no migration) |
| D12 | Duck attack | Pre-attack one `duck_ramp_ms` before the speech; release at the speech's confirmed end |
| D13 | Intro VT interrupted by a restart before its song was claimed | May re-air with that song (accepted) |
| D14 | Intro VT on non-music incoming (promos, IDs) | Allowed: the binding is to the Track, as recorded |
| D15 | Precision | VT transitions use running-time planning (±10 ms); ordinary segues keep the r0144 timing exactly |
