# Roadmap 2.25 Phase B0: transition contract finalization and GStreamer timing feasibility

Status: **B0 feasibility-review gate.** No production code changed: no engine, model, migration, template
or static change. B1 and production engine work need separate authorization.

| Item | Value |
|---|---|
| Baseline | r0144 `e4003aa7f075b940ec5680bf6864f08530ceed05` (tree `ca941ed1…`) |
| Phase A (accepted) | `6ad5222f283747ff30d42d23d6aaf2e3623de3e0` |
| Branch / worktree | `feature/2.25-phase-a-vt-transition-audit` / `~/isadoraair-2.25-phase-a` |
| Contract (updated, approved decisions) | `docs/roadmap/2_25_PHASE_A_VT_TRANSITION_CONTRACT.md` |
| Lab (test support) | `library/tests/_vt_b0_lab.py` |
| Experiments | `library/tests/test_vt_b0_timing_feasibility.py` |
| Evidence | `~/isadoraair-2.25-evidence/runs/B09_full.*` (final), B03-B08 (development runs) |

## 1. Answer

**The approved timing contract is feasible on the deployed GStreamer architecture, and the ±10-15 ms target
is demonstrated, not assumed.**

Every scheduled onset, speech end, underlap, separation and vocal clearance was measured in the rendered
program PCM within **±0.1 ms of plan**: 0.0000-0.0001 s, at the analysis resolution. B09 ran 27 tests, all
OK. This held
at 44.1 and 48 kHz, for WAV and FLAC, and for the incident fixture.

The mechanism is the one the engine already uses for decks and FX/VT fires: a **pad running-time
offset**. Its value becomes the **planned** instant, and it is set **before** the branch starts. Today's
value is "now".

B0 also found three things the implementation must respect:
- **Retire after the last rendered sample.** r0144 tears VT, FX and deck branches down on the EOS probe,
  which discards the queued tail: **52-157 ms of every VT** (F14), and **71-177 ms of every deck**
  (F14b).
- **Prove the incoming viable before speech.** A primed deck cannot do this for FLAC or resampled media
  until `P + C` before its music. The recommended **direct scheduled admission** proves it in about 3 ms.
- **Never leave a deck-mixer pad empty.** The deck mixer is non-live; an empty pad (a gated hold, or a
  stalled decode) freezes every deck (F15). Holds must be anchored, and a re-admission must be done by
  regeneration.

## 2. Methodology

**Tested on:**
- GStreamer 1.28.2 (`audiomixer` 1.28.2);
- PyGObject 3.56.2, Python 3.14.4;
- Linux 7.0.0-38;
- `flac` 1.5.0.

The KOGR engine runs at **44100 Hz** (journal: "Pipeline sample rate -> 44100"). 48000 Hz, the model
default and the other deployed configuration, was also exercised.

**Topology.** The r0144 program graph, from the 2.23 transport harness:
- deck bins → `mixer` (non-live) → `vt_duck_gain` → `duck_gain` → `master_mixer`;
- VT/FX → `fx_submix` (live silence bed) → `master_mixer`;
- a live silence bed;
- a live sine "microphone" for E13;
- `master_mixer` → convert → caps (F32, 10 ms buffers) → clock-synced `appsink`.

Decks were opened through the **real `_create_deck`**, with its 0.3 s silence prime, `concat`, probes,
generation and teardown coordinator. For a scheduled deck, only the value given to `_apply_pad_offset`
was replaced.

**Signals.** Synthetic stereo tone media, every tone a multiple of 100 Hz, so all are orthogonal in the
10 ms analysis window:

| Signal | Frequency |
|---|---|
| Outgoing music | 400 |
| Incoming music before Intro Until | 600 |
| Vocal entry (from Intro Until) | 1400 |
| VT speech | 1000 |
| The first 150 ms of every VT (head marker) | 2200 |
| Outro VT speech | 800 |
| Microphone | 3000 |

Cue In is real leading silence in the media.

**Measurement.**
- A recursive sliding DFT over the recorded program PCM, with a window of `rate/100` samples.
- An onset or end is the 50 % amplitude crossing, corrected to the window centre. This locates an abrupt
  start or stop to a fraction of a millisecond.
- Every value is compared with the **planned running time**. A timer firing or a state change is never
  treated as proof.

**Sanity.** The timer-based approaches are measurably late (1-5 ms), and the r0144 tails are measurably
cut (50-160 ms). So the instrument resolves errors well inside the target.

**Execution.** Only through `production/tests/run_with_validation_service.sh`, via the evidence
`run.sh`:
- bwrap sandbox, disposable PostgreSQL;
- umask 0022, stdin `/dev/null`;
- one suite at a time.

## 3. Scheduling approaches compared (ApproachComparisonTests, B09)

A 0.55 s VT with a 150 ms head marker, made audible at a chosen running time `T`. Four trials per
approach.

| Approach | 44.1 kHz WAV, error vs T | 48 kHz FLAC, error vs T | Head heard (of 150 ms) |
|---|---|---|---|
| r0144 style: build at a GLib timer, offset = now | +0.9 to +2.4 ms | +1.3 to +3.5 ms | **148.0** to 150 |
| **Scheduled pad offset** (offset = T, built 0.3 s early) | **0.0** | **0.0** | 150 |
| Gated + GLib timer (blocking probe; at T, offset = now, unblock) | +1.9 to +2.4 ms | +1.5 to +2.4 ms | 150 |
| Gated + clock thread (unblock on the pipeline clock) | 0.0 | 0.0 to +0.1 ms | 150 |

**Why the scheduled offset is exact.** The aggregator renders a buffer at `offset + pts`. A branch built
early delivers its first buffer to the aggregator pad long before `T`, where it waits. Timer jitter then
never reaches the audio. The link-now approach also lost up to **2.0 ms of the head** (B09; 3.4 ms in
B04) when decoding was slower than the mixer.

(`Gst.Clock.id_wait_async` cannot be used from PyGObject: a GstClockID cannot be marshalled. The
clock-driven variant is a thread waiting on the pipeline clock.)

## 4. Deck admission and readiness

**Scheduled offset through the real `_create_deck`** (DeckScheduledAdmissionTests): holds of 0.4 s,
1.0 s and 1.6 s, at 44.1 and 48 kHz, WAV and FLAC. Music onset error **0.0000 s** in all 12 cases.

**When can viability be proven?** Measured as the first decoded buffer at the real stage
(DeckReadinessTests, 2.0 s hold):

| Media / rate | Primed deck: ready after open | Primed deck: lead before music | Direct scheduled: ready after open | Direct: lead before music |
|---|---|---|---|---|
| WAV 44.1 → 44.1 (passthrough) | 10.2 ms | 1.99 s | 2.0 ms | 4.49 s |
| FLAC 44.1 → 44.1 | 1.448 s | **0.552 s** | 2.8 ms | 4.49 s |
| WAV 44.1 → 48 (resampled) | 1.451 s | **0.549 s** | 2.0 ms | 4.50 s |
| FLAC 44.1 → 48 | 1.449 s | **0.551 s** | 4.0 ms | 4.49 s |

**Why the primed deck is late.** For any media that is not a passthrough, the primed deck's `concat`
holds the decoder's serialized negotiation until the silence prime starts. Readiness is then provable
only `P + C` before the music. With an underlap `U > P + C`, speech would start **before** the deck is
proven:
- E04b measured the primed deck's readiness **0.85 s after speech started**;
- the direct scheduled deck proved readiness 0.44 s **before** speech (E04c).

This is why the contract (§6) admits a VT-planned incoming deck **without the prime**, by a scheduled
offset.

## 5. Required experiments (B09)

`err` is measured minus planned, in rendered PCM. Results for 44.1 kHz WAV unless stated; the other
configurations are in the B09 metrics and gave the same results.

| # | Experiment | Result |
|---|---|---|
| 1 | Ordinary non-VT segue (r0144 path, N = 5) | +0.770 to +0.780 s after Next Start (Phase A: 0.78-0.79). Unchanged; B0 alters no production path. |
| 2 | Incident short VT (`C` 0.124717, `I` 15.008, `V` 6.456229) | `U = 0`. VT onset 0.0, music onset 0.0, **speech 6.4562 s heard of 6.4562**, head 150 ms, vocal 0.0, **clearance 8.4270 s** (plan 8.427054). |
| 3 | Exact fit (`V = W − M`), 44.1 WAV and 48 FLAC | `U = 0`, clearance **0.1500 s**, speech 1.6000 of 1.6000. |
| 4 | Incident markers with an 18.0 s VT | `U` planned 3.266717, **measured 3.2667**; speech 18.0000; clearance 0.1499; vocal 0.0. |
| 4b | Underlap scaled (`U` 1.45) with a **primed** incoming: 44.1 FLAC, 48 WAV, 48 FLAC | Timing exact. Readiness **after** speech start (−0.85 s), the reason for direct admission. |
| 4c | The same with **direct scheduled** admission | Timing exact. Readiness 0.44 s **before** speech. |
| 4d | The same with a gated hold (rejected) | Outgoing rendered for only **55 %** of its span (47 % in B07/B08): the deck mixer starves on the empty pad. |
| 5 | Cue In 1.2 s, 44.1 WAV and 48 FLAC | All onsets 0.0; clearance 0.400 (plan 0.4). |
| 6 | Intro Until missing, or before Cue In | `U = V + M = 1.15` measured 1.1500; whole VT, then music `M` later. |
| 7 | Outro VT outlasting the outgoing (D3) | Outro on and off at 0.0; music admitted at `O_end` (+2.2 ms); 1.28 s of dry speech after the outgoing ended. |
| 8 | Outro + intro, `gap` 0.2 s, 44.1 WAV and 48 FLAC | Separation **0.2000 / 0.2001 s**; all onsets 0.0. |
| 9 | Eject the incoming during its VT, music held (`U` 1.35) | Held music aired **0.00 s**; speech ended 15 ms after the command (50 ms fade); Eject took 1.4 ms on the main loop; VT detach 0.03 ms; NULL 0.49 ms off-loop; pads and elements back to baseline. |
| 9b | Bring a held deck forward by changing its offset | Primed and direct scheduled decks: **music lost**, because the queued buffer anchors the old time. Gated: works, but see 4d. |
| 9c | NULL before detach on a held branch | Blocked **1237 ms**, with 1199 ms of hold remaining. Detach-first is mandatory. |
| 9d | Re-admission by regeneration (retire the held generation, reopen at now + 0.10 s) | New target **0.0000**; music `0.10 + C` after the command; replacement ready in 3.9 ms; outgoing continuity 0.99. |
| 10 | Late cancel during audible speech | Speech ended **≤ 20 ms** after the command (19.8 ms B09, 1.6 ms B07); music returned from 0.200 (−6 dB) to 0.400. |
| 11 | Invalid or failed VT branch | **Corrupt file:** routed bus error, failed 0.35 s before admission, 0 head aired, music at plan, never ducked. **Stalled preparation:** failed by the ready deadline 18 ms before admission, 0 aired. **Overrun of the validated duration:** completion watchdog at +2.501 s (plan `V` + 2.0 = 2.5). |
| 12 | Delayed or failed incoming deck | **Primed deck stalled 1.0 s:** VT gate refused (readiness not proven); music onset late 0.885 s with the vocal still on time, i.e. **the music head was lost**; outgoing rendered for only 0.82 s of 2.2 s (F15). **Missing file:** `_create_deck` returns None; no VT. |
| 12c | Direct deck stalled 1.0 s, readiness deadline at open + 100 ms | **Control (no deadline): 0.82 s of outgoing missing. With the deadline: detached 103 ms after open, 0.07 s missing.** No VT for the unviable deck. |
| 13 | Mic live during speech | Speech ended **2.5 ms** after the command (1.5 ms B07). Mic audible +0.5 to +22 ms (live-source buffer). Music under the mic at 0.1199, the mic duck alone (expected 0.1200; VT duck released). 0.00 s of speech while the mic was live. **Mic before speech:** 0 head, 0 speech, branch cancelled. |
| 14 | 16 repeated transitions (alternating WAV/FLAC, A/B decks) | VT and music error **0.0000**, first and last cycle. Deck pads 0 → 0, FX pads 1 → 1, pipeline children 12 → 12, fds 108 → 108, threads 14 → 16 (bounded worker threads). |
| F14 | r0144 `_vt_fire_file` tail (production path) | **Cut by 80-153 ms** in B09 (WAV 80-82, FLAC 133-153); 52-157 ms across B07-B09. |
| F14b | r0144 deck tail (production path, `_on_deck_eos_probed` → retirement) | **Cut by 71-177 ms** (WAV 71-80, FLAC 169-177). Every experiment's outgoing ends 12-114 ms early (`out_tail_err`). |

**Watchdog values verified:**
- **First audio 1.0 s:** never reached in any healthy case. With scheduled admission, readiness precedes
  admission by 0.3 s or more, and the ready deadline (admission − 50 ms in the contract, 20 ms in the lab)
  catches stalls before anything is audible.
- **Completion `V` + 2.0 s:** fired at +2.501 s, which bounds an overrun. Keep both values.

## 6. Recommended architecture

**Scheduled admission by pad offset.** It is the smallest change, and it is consistent with existing
pipeline ownership:
- decks still join `mixer` through `_create_deck`;
- VT fires still join `fx_submix`;
- the only new idea is that the offset is a planned instant instead of "now".

**The pieces:**
1. **VT fire = scheduled branch.**
   - Prepared at least 0.3 s ahead, after the incoming deck is proven ready.
   - Owner token `(slot, log_item_id, generation, seq, fire_id)`.
   - Deadlines: ready (admission − 50 ms), first audio (+1.0 s), completion (`V` + 2.0 s).
   - Bus errors routed to the fire.
   - **Drain-then-retire** at the last buffer's running time.
   - Detach on the main loop; NULL off the main loop.
2. **Incoming deck in a VT transition = direct scheduled admission.**
   - No prime; offset `A_I − C`.
   - Readiness about 3 ms after open, with a readiness deadline of open + 100 ms. On a miss: detach,
     skip the VT, and fall back to an ordinary start.
   - Ordinary decks stay primed with offset "now": r0144 unchanged.
3. **Held music is anchored, never gated.** Re-admission (cancel, failure, Start Next) is by
   **regeneration**: detach the never-audible generation and reopen the same claimed occurrence at
   now + 0.10 s.
4. **Speech-owned duck.**
   - One `InterpolationControlSource` bound once to `vt_duck_gain.volume`; only its points are replaced,
     never the binding.
   - Points are planned in running time: the attack one ramp before speech, the release at speech end.
     On a cancel or failure, the curve is replaced from "now".
   - Measured depth: 0.200 for 0.400 (−6.02 dB).
   - The mic duck (`duck_gain`) stays independent.
5. **Mic.** Live talent cancels pending fires before admission, and fades audible speech (≤ 16 ms with
   10 ms steps on the fx pad volume).

**Not recommended:**
- **Link-now plus timers** (r0144): jitter, and head loss.
- **Gated holds on the deck mixer:** starvation (E04d), and the gate blocks decode negotiation.
- **Clock-ID callbacks:** not available from PyGObject. A clock thread works, but adds a thread for no
  gain over scheduled offsets.

## 7. Cancellation and branch lifecycle demonstrated

| Case | Result |
|---|---|
| Before audible: fire not yet admitted | Detach-first. Nothing aired (E13 pending, E11 stalled). |
| Before audible: held deck (E09) | Detach-first. Held music never aired; pads and elements returned to baseline. |
| During audible speech | A 50 ms fade, then retirement. Ended ≤ 20 ms (E09, E10, E13). |
| Stale callbacks | Every timer, probe and error route is token-checked. A superseded token is ignored and recorded (`ScheduledBranch.ignored`). E14 ran 16 generations of the same side with no cross-talk. |
| Retirement order | Detach (unlink and release the request pad) on the main loop: **0.03-0.07 ms**. NULL off the main loop: **0.5-1.8 ms**. NULL-first blocked for the whole hold (E09c, 1237 ms). |
| No resource growth | E14: pads, elements and fds unchanged; threads bounded. |

## 8. Urgent priority, restart recovery, performance (architecture constraints)

**Urgent.**
- Pending VTs are cancellable up to their admission, and audible speech can be faded within one 10 ms
  step.
- The contract (§5) therefore limits a VT's authority:
  - no hold when the next item is urgent;
  - an outro VT that cannot end by `A_seg` is suppressed;
  - an urgent item committed during a hold releases it at once;
  - audible outro speech fades at `A_seg`.
- r0137 insertion, deduplication and canonical-Track synchronization are untouched.

**Restart (D13) without a migration.**
- An intro VT starts only after the claim. Any occurrence with `playback_claimed_at` is therefore never
  "fresh", so a resume gets no intro VT.
- A continuation generation fires an outro VT only if `saved_position + state_age_seconds` + margin is
  still before Outro Starts. Otherwise delivery is uncertain, and it is suppressed.
- A new scheduled occurrence of the same Track is a different LogItem, and stays eligible.

**Performance.**
- The only new database work is one indexed VoiceTrack read in the deck-open path that already performs
  the claim.
- The poll path loses its three `VoiceTrackConfig.load()` calls and the lookahead VT queries (F13).
- All new GLib work is O(1): timers, token checks, and replacing the curve points.
- Branch preparation is 2-4 ms (decode readiness).

## 9. Remaining limitations and items not demonstrated

**N1. VT transitions are exact, not r0144-late.** The ordinary segue lands +0.78 s after Next Start; a VT
transition lands **at** Next Start. This is intended by the contract (§4), but operators will hear VT
segues about 0.8 s tighter than ordinary ones.

**N2. F14 and F14b are pre-existing r0144 defects, outside the VT contract.**
- The FX cart tail and the deck tail are cut by the same mechanism.
- B2 fixes the VT path.
- The FX cart and deck paths need their own approval and their own tests, because fixing them changes
  ordinary playback.

**N3. F15 is pre-existing r0144 behaviour: a stalled deck freezes the deck bus.**
- The ordinary primed path is unchanged in 2.25.
- B0 shows a bounded mitigation only for scheduled decks (readiness deadline).
- A general fix needs its own design: for example a live silence bed with `ignore-inactive-pads` on
  `mixer`, which changes the ordinary path.

**N4. Real device output.**
- B0 measures the program at the appsink, in running time.
- The real ALSA and StereoTool outputs add a constant latency that is common to every source, so relative
  timing is unaffected.
- Confirm once on real hardware during B7 qualification, with a loopback capture.

**N5. Remote DJ (WebRTC) gate.**
- The mic stand-in is a local live source.
- The Remote DJ gate's own latency, about 40 ms jitterbuffer, affects when talent is heard, not VT
  cancellation.
- To be covered at L2 with the existing Remote DJ harness in B5.

**N6. Accounting of scheduled and regenerated decks.**
- The first-real-buffer probe (`_schedule_occurrence_air_start_from_probe`) fires when the buffer is
  *queued*, which can be seconds before it is rendered.
- `started_at` (`_get_deck_position`) assumes "now + P".
- Both must be anchored to the scheduled render instant (B2). A regenerated (held, never-audible)
  generation must record no air start. Not exercised in B0, which used harness stubs for accounting.

**N7. Interpretations for operator confirmation.**
- D5: the Eject replacement counts as a manual start, so it gets no intro VT.
- D10: with no usable window, the music follows `M` after the VT.

**Infeasible.** Nothing in the approved contract was found infeasible.

## 10. Production remains unchanged

- No engine, model, migration, template, static, iPortal, ProductionMedia or updater file changed.
- `/opt/isadoraair` and the production database, media, services and station configuration were not
  touched.
- Nothing was published, deployed or restarted.
- The CSV and NWS candidates were not integrated.
- The B0 commit adds only test-support code, tests and documentation.
