# Roadmap 2.25 B3-B4: independent outro and intro VT playback, integrated

Status: **B3-B4 engineering-review gate.** The accepted B1 planner and B2 scheduled-audio infrastructure
are connected to the engine's real transition path. The r0144 VT entry (`_vt_maybe_enter`) is retired:
it is never entered, so the remaining r0144 phase handlers are inert until B6 deletes them. Local
engineering only: nothing is published, integrated or deployed.

**Ancestry:** r0144 `e4003aa` → Phase A `6ad5222` → B0 `ced54ed` → B1 `6f66222` → B2 `f55febe` →
**B3 `2e5da2f`** (outgoing side) → **B4** (the commit that adds this document; incoming side). Neither B3
nor B4 alone is a deployment candidate.

## 1. Architecture

One **`VtTransitionRecord`** (`engine._vtt`) owns VT timing for one transition. It replaces the r0144
`_vt` phase dictionary as the owner; `_vt` now only mirrors `{"phase": "vt_transition"}` while a
transition is busy, for its existing readers (Start Next's `busy_voice_track`, Eject of an empty slot, the
idle-restart guard, Hour Refill). The legacy phase names are never written, so no legacy branch can act.

| Piece | Where | Role |
|---|---|---|
| `VtTransitionRecord` | `engine.py` | Bound to the outgoing `(slot, LogItem, generation)`; once committed, to the claimed incoming generation (identity of its scheduled-deck callback). Bounded event log. |
| `_vtt_poll` | `_poll_position`, before the 5 s gates | Validates, plans, re-plans. Cached VoiceTrack reads (one per occurrence and side), one pure plan, at most a few timers. |
| `_vtt_plan` | | Outro eligibility (D4, D6, D7, D13, F6) and the **look-ahead estimate** of the incoming side, used only to decide *when* to commit. Creates a record only when the plan contains speech or a hold. |
| `_vtt_prepare_outro` | timer, 1.0 s before Outro Starts renders | Outro fire at the exact marker running time (pad offset of the outgoing). |
| `_vtt_commit` | timer at `min(S_v, A_I − C) − 0.6 s` | Runs the existing `_start_next_track` path (request scheduling, dedication splice, forced/urgent selection, rollover, unplayable skip, contention, Swap adoption under the claim lock). |
| `_vtt_admission_for_claim` | `_create_deck`, right after the claim | The **authoritative** decision: the claimed occurrence's own VoiceTrack, start kind (D5), role (D14) and claim history (D13) → final plan → scheduled admission, or `None` (r0144 primed start). |
| `_vtt_unplanned_admission` | same hook | An automatic start the look-ahead did not plan (ordinary trigger, natural/error/watchdog completion, next-hour adoption) whose claimed occurrence has an eligible intro VT. |
| `_vtt_incoming_ready` | B2 scheduled-deck `ready` | Only now (viability proven) is the intro fire prepared. |
| `_vtt_readmit` | intro/outro failure, cancel, live talent, deadline | Re-admission by regeneration (B2), re-planning the intro when the outro failed. |
| `_vtt_fallback_ordinary` | B2 scheduled-deck `failed` | The same claimed occurrence as an ordinary primed start, timed so its music lands at the plan; bounded to one. |
| `_vtt_refresh_duck` | every fire event | The one VT duck controller: the speech-owned curve of live speech. |

### Start kinds (D5) and the claim

`_start_next_track` callers declare their start kind through `_vtt_starting(kind)`; it is read at the claim.
Automatic: the ordinary trigger, natural/error/watchdog completion and the transition's own commit.
Eject replacement, Manual-to-Auto handoff, Start Next, manual and every unlabelled start: no intro VT.
`_start_next_track` also records how the occurrence was selected (`is_forced`, native Active Message) for
the D14 role.

### Lifecycle

```
            poll (≤ 45 s before the segue or Outro Starts)
 (none) ──────────────────────────────────────────────▶ PLANNING ──(nothing eligible / released)──▶ done
                                                          │  outro prepare timer (O_s − 1.0 s)
                                                          │  commit timer (min(S_v, A_I − C) − 0.6 s)
                                                          ▼
                                  claim (existing path) → final plan for the CLAIMED occurrence
                                                          ▼
                                                      COMMITTED ── incoming: preparing → ready → scheduled → audible
                                                          │            ready: intro fire prepared (viability first)
                                                          │            failed: ordinary fallback of the same occurrence
                                                          │  fire failed/cancelled: re-admit held music (regeneration)
                                                          ▼
                                              all fires terminal ∧ incoming audible → done (duck released)
 transition deadline (planned end + 3 s) → cancel live speech, re-admit held music or start ordinarily → done
```

Ordinary transitions (neither side eligible) never create a record; the r0144 trigger, primed deck and
timing are unchanged.

## 2. F1-F15 disposition

| ID | Disposition | Proof |
|---|---|---|
| F1 | **Fixed.** The incoming intro side is decided at the claim (and estimated by the look-ahead) independently of the outgoing marker. | Incident regression (4 configurations + control), Phase A R1a/R1b, 2.23 intro-side tests |
| F2 | **Fixed.** Speech is authorized only for the claimed occurrence; the owner is `(LogItem, Track, slot, generation, seq, fire)`. | Swap at claim (harness and PostgreSQL), request, dedication, urgent before/after commit, next hour, Phase A R2/R3/R3b |
| F3 | **Fixed.** Routed errors and deadlines; failure re-admits held music; duck released. | Corrupt outro/intro, R4a, R4b |
| F4 | **Partly (minimum D6 safety).** A live mic or Remote DJ gate cancels unadmitted speech and fades audible speech; Manual cancels unadmitted speech. Complete policy: **B5**. | B3 mic fade, B4 mic before/during, R5 |
| F5 | **Fixed.** The duck is owned by speech; no early duck; the segue is kept. | B3 duck edges, R8 |
| F6 | **Fixed.** An attached record without playable media is never eligible. | R11, B3 unplayable, media-equivalence test |
| F7 | **Fixed.** D7: suppressed with a reason, no delay; a marker first seen too late is suppressed, never fired late. | R6, B3 D7 and late-marker tests |
| F8 | **Fixed.** Planning runs before the 5 s gates; unplanned automatic starts also evaluate the claimed occurrence. | R7, B4 short-outgoing |
| F9 | **Fixed (D13).** In-process delivery memory per LogItem; resume staleness rule. | R9, B3 resume tests |
| F10 | **Fixed.** Eject of the incoming cancels its intro VT and never re-opens it; replacement has no intro VT. | 2.23 E8 (converted), B4 Eject before/after admission |
| F11 | **Partly.** Pause/seek/resume of the outgoing cancel its outro (no replay); of a not-yet-audible scheduled incoming they are refused; of an audible incoming they cancel its intro. Complete policy: **B5**. | B3 seek, B4 refusal |
| F12 | **Fixed.** Ownership by occurrence and generation, not Track id. | Two occurrences of one Track (outro and intro) |
| F13 | **Partly.** VT reads are cached per occurrence (one read, not one per poll) and VoiceTrackConfig is cached for 5 s; VT state publication and moving reads off the GLib thread: **B6**. | — |
| F14 | **Fixed for VT** (B2 drain-then-retire, now on every live VT). F14b (ordinary deck tails) deferred. | Tail marker 0.150 s in every run |
| F15 | **Eliminated for the VT path** (scheduled deck prepared unlinked). The ordinary primed path is unchanged (deferred). | Fallback test: outgoing continuity 1.000 |

## 3. Scheduled deck, failure and re-admission (contract sections 6-7)

| Case | Behaviour |
|---|---|
| Incoming deck not ready by open + 100 ms, link failure, or a bus error before admission | The generation fails before audible (error/EOS of a never-audible scheduled generation is a failed admission, never an occurrence completion). No intro speech. The **same claimed occurrence** opens as an ordinary primed start, timed so its music lands at the planned admission; bounded to one fallback. If it cannot be opened, the existing unplayable-occurrence handling advances. |
| Intro VT fails or is cancelled before admission, U > 0 | Held music re-admitted at once by regeneration (same LogItem, claim unchanged, no request/dedication rerun). |
| Intro VT fails, U = 0 | Music at its planned instant; nothing re-opened. |
| Outro VT fails or is cancelled while it holds the committed incoming | Re-admission with the incoming's intro VT **re-planned** for the new generation (the incoming side stays independently eligible). |
| Re-admission impossible (file gone, attempts exhausted, slot teardown poisoned) | The held generation (file already open and decoded) keeps its original admission: bounded silence ≤ the planned underlap, no second occurrence, no queue advance, no loop (one attempt per trigger, at most 2 per occurrence). |
| Live mic / Remote DJ gate / Manual, transition deadline | Unadmitted speech cancelled, audible speech faded (mic/gate), held music re-admitted. |
| Eject of the held or audible incoming | The incoming is released first, then its intro VT cancelled: the ejected occurrence is never re-opened (F10). The replacement is an Eject replacement (no intro VT). |

## 4. Playback accounting

Scheduled time is when audio is intended to air; evidence comes only at the established boundary:
- a preparing or scheduled generation has no `played_at`, PlayEvent, SongRequest airing, now-playing, RDS,
  elapsed position or duration evidence; it is never the leading deck and never resume evidence;
- at the admission running time (media 0 rendered, as for r0144's first post-prime buffer) the air start
  is dispatched with the exact instant; PostgreSQL: `played_at` error 0.0000 s, one PlayEvent;
- a fallback or re-admission re-claims idempotently (`playback_claimed_at` unchanged) and produces one air
  start (the audible generation's);
- a never-audible generation that fails is claimed but never played (B2), and its EOS/error never
  completes the occurrence.

## 5. Urgent priority (section 5, r0137 unchanged)

- Urgent item next when planned: no hold, no intro VT; an outro that would hold is suppressed.
- Urgent inserted before commit: re-planned at once (`_vtt_queue_changed`): unadmitted speech cancelled,
  hold released, the urgent item starts by the ordinary path.
- Urgent inserted while outro speech is audible: the outro continues only to the segue, then fades.
- Urgent inserted after the incoming song is claimed: that song and its intro VT air; the urgent item
  follows (r0137 cursor contract).

## 6. Measurements (rendered program PCM, sample-accurate)

| Quantity | Result |
|---|---|
| Outro onset vs Outro Starts, 44.1/48 kHz WAV/FLAC | 0.0000 s (4 configurations) |
| Outro end, whole recording, head/tail markers | 0.0000 s; 1.6000 s rendered of 1.6; markers 0.150 s |
| Incident (no outgoing marker), intro VT onset / music onset vs A_seg | 0.0000 / 0.0000 s (4 configurations + outgoing-marker control) |
| Incident whole VT (6.456229 s) | rendered 6.4563 s, end error 0.0000 s; planned U 0, clearance 8.427054 s |
| Long VT underlap (U planned 1.65 s) | measured 1.6500 s; vocal clearance 0.1500 s |
| No usable Intro Until | music 0.1500 s after speech end |
| Combined (outro holds the segue) | outro 0.0000, separation 0.2000 s (= min_gap_ms), intro 0.0000, music 0.0000 |
| Short outgoing (3 s ID) | intro VT and music at A_seg 0.0000 s |
| Hold (outro past the segue) | incoming music at O_end, error 0.0019 s (10 ms bin) |
| Duck | attack begins 83-86 ms before speech (ramp 100 ms), level 0.2004 (−6.0 dB), release done ~0.1 s after speech |
| Mic during speech / Eject after admission | speech gone 16 ms / 9 ms after the command |
| Failure recovery | corrupt intro (U 1.65 s): music 0.370 s after the failure; deck fallback: music 0.003 s from plan, outgoing continuity 1.000; failed re-open: music at original admission 0.0000 s |
| Ordinary segue (no VT) | +0.790 s after Next Start (Phase A R0 on r0144: +0.790) |
| 8 consecutive intro transitions | deck pads 1→1, FX pads 1→1, children 13→13, threads 28→28, fds 47→47, fire registry 0 |

## 7. Legacy VT and duck control

- `_vt_maybe_enter` is retired (returns False, never called); no legacy phase is ever entered, so
  `_vt_on_fire_eos`, `_vt_handle_outgoing_ended`, the intro phases and the legacy EOS/watchdog/Eject branches
  are inert. Their deletion is B6.
- One VT duck controller: `VtDuckCurve` (one control source bound once). `_vt_ramp_duck_to_db` refuses to
  write once the curve exists; its callers are retired.
- The mic/Remote DJ duck (`duck_gain`) is never touched by VT code.

## 8. Operator-command compatibility (minimum safety; complete policy B5)

| Command | B3-B4 behaviour |
|---|---|
| Start Next | Refused `busy_voice_track` while speech is prepared/audible or a held incoming exists (bounded by the deadlines); a merely planned transition is aborted when a Start Next is accepted. **D8 (cancel and advance) is B5.** |
| Eject outgoing | D9: an audible outro completes, then the incoming (Eject replacement); an outro not yet audible is cancelled. |
| Eject incoming | F10 (above). |
| Pause / seek / resume | Outgoing: its outro is cancelled, no replay. Scheduled (inaudible) incoming: refused. Audible incoming: its intro VT is cancelled. |
| Mic / Remote DJ / Manual | Minimum D6 (above). **The approved "incoming waits under the Manual hold" for an already-claimed held incoming is B5** (B3-B4 re-admits it at once instead of leaving silence). |
| Play Now / reload | Transition aborted, speech cancelled, duck released. |
| `_live_decks()` | A scheduled generation counts as live (it owns its claim and blocks a second start). `_leading_deck()` skips it. |

## 9. Limitations

1. B5: D8 Start Next cancel-and-advance; Manual hold of an already-claimed held incoming; Remote DJ gate
   paths beyond the minimum cancel; real-hardware output not exercised.
2. B6: VT state not published in `engine_state.json`; VT reads still run on the GLib thread (cached).
3. Continuation (seek/resume) generations map Outro Starts by the wall-clock position estimate, not the pad
   offset (the outro is usually suppressed there by D13).
4. One transition at a time: a claim made while another transition still owns speech or a held incoming
   stays ordinary.
5. The unplanned path (ordinary trigger, natural end) admits the intro VT 0.3 s after the claim (exact
   at A_seg only when the Cue In covers that lead); the planned path is exact.
6. F14b (ordinary deck tails) and ordinary-path F15 remain deferred engine defects.

## 10. Restart and migration inventory

No migration (`makemigrations --check`: no changes). `isadoraair-engine` restart required (r0144 restart
safeguard applies). No Gunicorn restart: no template, static, view or URL change.
