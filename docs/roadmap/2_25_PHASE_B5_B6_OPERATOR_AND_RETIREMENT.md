# Roadmap 2.25 B5-B6: operator controls, live talent, legacy retirement and observability

Status: **B5 committed (operator controls); B6 follows.** B5 completes the operator side of the approved contract
(D5, D6, D8, D9, D13; F4, F11) on the integrated B3-B4 lifecycle; B6 deletes the r0144 VT phase machine,
publishes the lifecycle in `engine_state.json` and moves VoiceTrack/VoiceTrackConfig reads off the GLib
callbacks (F13). Local engineering only: nothing is published, integrated or deployed.

**Ancestry:** r0144 `e4003aa` -> A `6ad5222` -> B0 `ced54ed` -> B1 `6f66222` -> B2 `f55febe` -> B3 `2e5da2f`
-> B4 `d279cb8` -> **B5** (operator controls) -> **B6** (retirement, state, asynchronous lookups).

## 1. B5 -- operator controls and transport holds

### Held claimed occurrences
A claimed incoming generation that is still being held for its scheduled admission (preparing or
scheduled, not audible, more than `HOLD_MIN_LEAD_S` = 120 ms from its admission) can be **withdrawn** by a
transport hold: the never-audible generation is retired (`occurrence_terminal=False`, nothing persisted,
no queue advance) and the claimed LogItem becomes `_vtt_held_occurrence`, the next item for the next
authorized start. `_next_queue_item` hands it out first, exactly once, as a forced item (no request
scheduling or dedication splice repeats), with its original selection context (D14 role). The claim is
never released or recreated (idempotent re-claim; PostgreSQL: `playback_claimed_at` unchanged, one
PlayEvent, `played_at` at the real admission). A held occurrence that becomes unplayable while waiting
is skipped by the existing forced-item policy (warning event `Skipped held log item`). A play-now reload
discards it with the replaced program.

| Hold | Policy |
|---|---|
| Operator Manual | VT speech not yet audible is cancelled; audible outro speech completes (the program already on air); intro speech of a song now held is cancelled (~50 ms fade). The held song is withdrawn; `_manual_hold_pending` makes the next Auto release start it (D5: deferred handoff, no intro VT; it is already claimed). Within 120 ms of its admission (or audible) it is on air and is never interrupted. |
| Studio mic / Remote DJ gate with the mic hold enabled | Same as Manual (the mic hold is Manual), plus every audible speech fades out in ~50 ms (D6). Mic release restores Auto and starts the same occurrence. |
| Mic / gate live, mic hold disabled (Auto talk-over) | Admission stays authorized: speech cancelled, the held song re-admitted at once without VT. The mic never changes the transport mode itself. |
| Pause or seek of the outgoing | Its outro speech fades (no replay, D13); a held incoming is withdrawn and stays next. A paused outgoing is a transport hold: nothing is admitted. After resume, the song follows its end (no VT: claimed / already aired). |
| Pause / seek of a not-yet-audible scheduled incoming | Refused (it has no position yet); published as a refusal. Eject cancels it (B4 order), Start Next admits it. |
| Pause / seek / Eject of an audible incoming | Its intro speech is cancelled; the command then acts as usual. |

### D8: Start Next during a VT transition
`busy_voice_track` is gone. The accepted command calls `_vtt_start_next_accepted`:

| Stage at the press | Mode | Effect |
|---|---|---|
| Outro speech before any claim (planning, prepared, audible) | `fade_and_start` (2.23) | Speech fades (~50 ms), the transition is aborted, Start Next fades the outgoing and starts the next item (no intro VT, D5). |
| Claimed incoming preparing or held, outgoing still live | `overlap` | Speech cancelled; the held occurrence is re-admitted by regeneration so its Cue In lands at the fade end; the outgoing fades. No queue item is consumed. |
| Claimed incoming held, outgoing already ended (outro speech past its end) | `vt_admit` (new) | Speech fades; the held occurrence is admitted `START_NEXT_ADMIT_LEAD_S` = 150 ms later; completes at once. |
| Held incoming within 120 ms of its admission, or already audible | `overlap` (2.23) | Treated as audible: never regenerated; its unadmitted speech is cancelled. |
| Bounded ordinary fallback of a failed scheduled admission pending | refused `busy_transition_recovery` | Only for the fallback window (bounded); nothing changed. |

Exactly once: the existing duplicate-file, duplicate-client, `busy_start_next` and identity
(`expected` live decks and generations) checks are unchanged; a command whose identity predates an EOS or
a regeneration is refused `target_changed`.

### D6/F4: live talent from the real authorities
`_vtt_live_talent_changed` runs at the END of `_apply_mic_mode_hold` (every studio-PTT and Remote DJ gate
decision: `_set_mic_ptt`, `_remote_dj_set_gate`, reconnect restore, session stop) and on an operator Manual,
so the mic hold's own Auto/Manual decision is already made. The live context reads the physical gates
(`mic_live`, `_remote_dj_gate_open()`); a gate held muted while Reconnecting is not live. As a second line,
a fire reaching its admission while a mic or gate is live is faded at once. New fires are refused while
talent is live; a VT ruled out at the claim is never resurrected when the gate closes.

### F11: exact timing after seek, resume and restart auto-resume
A confirmed gated seek records `vtt_media_zero_ns = pad offset + segment running time - achieved media
position`: the exact running time of media position 0 of that generation. Outro Starts after a seek,
resume or auto-resume is now scheduled from it (measured 0.0000 s). A generation without an exact mapping
(an unconfirmed seek) gets **no VT plan at all** (`timing_unverified`); the ordinary path owns it. A
backward seek never replays (delivery memory), a forward seek across a marker never inserts it.

### Urgent priority with a claimed, held song
r0137 claim order is unchanged (the urgent item follows the claimed song). Recorded speech no longer
extends that boundary: on an urgent insertion with the claimed song held for VT speech, unadmitted speech is
cancelled, audible speech that is holding the song (outro past the segue, long-intro underlap) fades in
~50 ms, and the same song is re-admitted once the fade has finished, never before the ordinary segue. A
song already audible is never touched; intro speech over audible music completes.

### Consecutive short items
A record whose incoming is audible and which only has speech left becomes the **draining** record
(at most one); planning of the next transition starts at once. A side of the next transition that cannot
start a configured gap after the draining speech ends is suppressed (`previous_speech_active`) -- never
delayed, never overlapping -- and, for the intro side, sticky for that transition, which keeps the start so
the song still lands exactly (music-only plan). One duck controller covers both records.

## 2. B6

Pending (separate commit).
