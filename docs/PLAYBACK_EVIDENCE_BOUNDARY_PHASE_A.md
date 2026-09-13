# [P2] 1.6 Phase A -- authoritative playback-evidence boundary

Status: **Phase A only -- observational.** No production playback or
accounting semantics were changed. `LogItem.played_at`, `Track.
last_played_at`, `Track.play_count`, `mark_song_requests_aired`, and
`PlayEvent` all still commit exactly where and when they always have --
synchronously inside `_create_deck`, at deck-commit time, before any
proof of real output. Phase B (a real cutover) is explicitly out of
scope for this document and was not started.

Baseline: `a5392ecfeb1086d61e2dee5efecf743ed2bbb170` (packaged r0075),
verified identical across `main`, `origin/main`, and `github-write/main`
before any work began. Branch/worktree:
`feature/p2-1.6-phase-a-playback-boundary` in
`/home/jreed/isadoraair-p2-1.6-phase-a`.

## 1. Why this phase exists

r0075 writes several different meanings of "played" from `_create_deck`,
synchronously, immediately after the deck is linked into the live mixer
and `sync_state_with_parent()` has merely *requested* PLAYING (an async
request, not a completion) -- all still well before any evidence that
real track PCM has actually reached the deck's output boundary:

* `LogItem.played_at = timezone.now()`
* `Track.last_played_at` / `Track.play_count += 1`
* `mark_song_requests_aired(...)`
* `PlayEvent.objects.create(started_at=...)`

`library/services/log_builder.py::get_recent_exclusions()` already
documents this precisely: *"`_create_deck` writes `played_at` when it
commits a track to a deck, BEFORE the audio actually starts."*

The existing `Deck.mark_media_buffer()` counter (incremented from a probe
on `real_stage_src`) is real decoder/branch activity, but for a
silence-primed fresh start that pad sits **upstream of `concat`** --
decode can produce (and, per the production topology, GStreamer's
internal per-pad queueing can hold) a buffer destined for concat's real
sink well before `concat` has actually switched away from serving the
300 ms silence primer. It was never proven equivalent to "real content
has crossed the deck's output boundary," and this phase confirms that
gap is real (Finding 1 below).

## 2. Required initial inspection -- confirmed on this baseline

All three claims the task asked to verify before making any change were
confirmed true by direct code reading; none required stopping:

1. **The current accounting block is earlier than proven real output.**
   Confirmed: `library/services/engine.py`'s `_create_deck`, in the
   `resume_position_ns is None` branch, executes `log_item.save()`,
   `Track.objects.filter(...).update(...)`, `mark_song_requests_aired`,
   and `PlayEvent.objects.create` immediately after
   `deck_bin.sync_state_with_parent()` -- which only requests the async
   PAUSED->PLAYING transition, it does not wait for it -- and before any
   pad probe has observed a single real buffer.
2. **`mark_media_buffer()` is upstream of the complete post-primer deck
   output.** Confirmed: for a silence-primed deck, `real_stage_src` (the
   pad `mark_media_buffer()` is probed on) is `real_caps`'s own src pad,
   linked into `concat`'s real sink -- strictly upstream of `concat`'s
   src / the deck's ghost pad, and of `concat`'s own active-pad switch
   decision.
3. **Auto-resume (`_resume_hint`) is logically different from a manual
   `resume_position_ns` recreation.** Confirmed: auto-resume leaves
   `resume_position_ns` as `None` on the `_create_deck` call itself (only
   the separate, internal `_auto_resume_position_ns` is set), so it
   takes the silence-primed branch, runs the full accounting block as a
   normal fresh start, and only issues its `seek_simple()` afterward,
   against the already-linked, already-accounted deck. A manual
   `resume_position_ns` recreation (`_begin_gated_seek` /
   `_seek_deck` / `_resume_deck`) skips the silence prime entirely,
   skips the accounting block entirely, and is held behind a
   `BLOCK|BUFFER` gate on the ghost pad until a real post-seek buffer is
   confirmed before ever joining the live mixer.

## 3. Actual deck topology observed

For a fresh (silence-primed) deck, `_create_deck` builds, inside one
`Gst.Bin`:

```
filesrc -> decodebin -> audioconvert -> audioresample -> real_caps(capsfilter) --\
                                                                                    concat --> ghost_pad("src") --> mixer.sink_%u
audiotestsrc(wave=silence, num-buffers=1, ~0.3s) -> silence_caps(capsfilter) ---/
```

`concat`'s sink pads are requested in a fixed order (silence first, so it
becomes `sink_0`; real second, `sink_1`) -- `concat` always drains
`sink_0` to its own internal EOS before switching to `sink_1`, and (per
its own default `adjust-base=true`) shifts the second segment's
timestamps to be contiguous with the first. `concat`'s src pad exists
immediately (no dynamic pad-added needed), so the ghost pad's target is
fixed to it right away.

For a non-primed deck (`resume_position_ns` given -- manual seek/resume
recreation only), there is no primer and no `concat` at all: the ghost
pad's target is `audioresample`'s own src pad directly.

Existing milestones (`Deck.mark_milestone`, a bounded in-memory dict, no
per-buffer logging) already instrument several points around the *end*
of a track's life -- `A_DECODER_AUDIO_EOS` through `O_NULL_TRANSITION_*`
-- but, before this phase, nothing instrumented the *start* boundary this
task is about.

## 4. The empirical question, and how it was answered

A plain `BUFFER` probe on `concat.src` (or the ghost pad once its target
is `concat.src`) sees the 300 ms silence-prime buffer first -- that
buffer must never be called "real." Distinguishing the two without using
amplitude (real source audio can itself be digitally silent) required
knowing, precisely, how `concat` behaves at the moment it switches sinks.

An isolated, hardware-free harness --
`scratchpad/playback_boundary_p2_1_6/harness_concat_boundary.py` --
reproduces the exact same topology (silence sink requested first, real
sink second, `concat.src -> fakesink`) completely outside Django/the real
engine, with independent per-branch buffer-identity ground truth (a probe
on each branch's own pre-`concat` pad) and a fixed, known buffer count
per branch.

Representative captured ordering (t=0 is pipeline start; full log in the
harness's own output):

```
t=4.126ms  concat.src BUFFER #1  pts=0.0ms     active-pad-AT-THIS-INSTANT=SILENCE
t=4.457ms  notify::active-pad fired -- active-pad is now REAL
t=4.694ms  concat.src EVENT_DOWNSTREAM: stream-start   (fresh event sequence for the new segment)
t=4.862ms  concat.src EVENT_DOWNSTREAM: caps
t=5.036ms  concat.src EVENT_DOWNSTREAM: segment
t=5.144ms  concat.src EVENT_DOWNSTREAM: tag
t=5.328ms  concat.src BUFFER #2  pts=0.0ms     active-pad-AT-THIS-INSTANT=REAL
t=5.411ms..6.123ms  concat.src BUFFER #3..#6   active-pad-AT-THIS-INSTANT=REAL (every one)
t=6.202ms  notify::active-pad fired -- active-pad is now None (all sinks exhausted)
t=6.300ms  concat.src EVENT_DOWNSTREAM: eos
```

Two findings fall out of this directly:

* **Finding 1 (confirms the initial-inspection concern):** `concat` does
  send a complete fresh `stream-start`/`caps`/`segment`/`tag` sequence on
  every sink switch -- so segment-event counting is a viable, if
  slightly more fragile, alternative signal. But `notify::active-pad`
  fires *before* that new segment (and before the first real buffer)
  land, so a signal-callback-based milestone would be marking a
  buffer-instant slightly early relative to when the buffer itself
  crosses `concat.src`.
* **Finding 2 (the chosen mechanism):** `concat` exposes its switch as a
  plain, synchronously-readable `active-pad` GObject property. Reading
  it *from inside* the very `BUFFER` probe invocation for each buffer at
  `concat.src` is buffer-instant-correlated by construction -- in every
  run, buffer #1 (the primer) reads back the silence sink, and every
  buffer from the real branch reads back the real sink, with zero
  ambiguous buffers observed. This requires no threshold, no PTS-based
  guess, and no dependency on segment-event ordering being preserved by
  a future GStreamer version -- it is asking `concat` directly which
  sink it considers active at that instant, which is exactly the fact in
  question.

`active-pad` is read-only from the pad-probe's perspective (a plain
property getter, not a blocking call), so this check is safe on the
GStreamer streaming thread under the same non-blocking constraints the
existing probes already observe.

## 5. What was implemented

Both additions are pure in-memory, per-generation observability,
extending the existing `Deck.mark_milestone` machinery -- no new
telemetry subsystem, no per-buffer logging, no ORM/filesystem/network
access from a streaming thread, no ORM writes anywhere in this change.

**New milestone name** (`library/services/engine.py`):

```python
FIRST_REAL_POST_PRIMER_MILESTONE = "REAL_CONTENT_CROSSED_POST_PRIMER_BOUNDARY"
```

**Primed path** (fresh start / auto-resume -- silence prime + `concat`
present): a new, dedicated `BUFFER`-only probe on `concat_src`, added
alongside the pre-existing `D_CONCAT_SRC_EOS` `EVENT_DOWNSTREAM` probe:

```python
def real_output_boundary_probe(pad, info):
    if info.type & Gst.PadProbeType.BUFFER:
        if concat.get_property("active-pad") == real_concat_sink:
            deck.mark_milestone(FIRST_REAL_POST_PRIMER_MILESTONE)
            return Gst.PadProbeReturn.REMOVE
    return Gst.PadProbeReturn.OK
```

Self-removes (`PadProbeReturn.REMOVE`) the instant it fires -- true
one-shot per generation, zero added cost for the remainder of the track
once real content is already flowing. Deliberately **not** added to
`probe_handles` (the list `_remove_deck`'s teardown walks to explicitly
`pad.remove_probe()` every other probe in this function): this probe's
lifecycle is already fully self-managed -- it either fires and removes
itself, or never fires and is discarded along with the pad/element when
the bin is torn down -- so registering it there as well would only make
teardown's own removal call race a probe that may already be gone
(harmless, since that call is wrapped in `try/except`, but GStreamer logs
a noisy warning for it). Confirmed no such warning appears in Tier 1 with
this probe left out of `probe_handles`.

**Non-primed path** (manual `resume_position_ns` recreation -- no primer,
no `concat`; the real decode pad IS the ghost target): the existing
`real_stage_probe` (already there for `mark_media_buffer()` and the
`B_REAL_LEG_EOS_BEFORE_CONCAT` milestone) gained a one-shot flag --
there is no primer to distinguish from here, so the very first buffer
observed already is, by definition, at the deck's real output boundary:

```python
if not silence_primed and not first_real_marked["done"]:
    deck.mark_milestone(FIRST_REAL_POST_PRIMER_MILESTONE)
    first_real_marked["done"] = True
```

`concat`, `real_concat_sink`, and `deck` are all fresh, per-`_create_deck`-call
local closures (never reused across generations or slots), so both
probes are inherently generation-safe with no additional locking beyond
what `Deck.mark_milestone`'s own internal lock already provides.

## 6. Deterministic evidence (Tier 1: `library/tests/test_engine_playback_boundary_evidence.py`, 9 tests, all real GStreamer, hardware-free)

**A. Ordinary fresh start** --
`test_ordering_and_timing_deck_creation_primer_then_real_output`.
Milestone fires exactly once, strictly after deck creation
(`first_ms > 0`), with `media_buffer_count > 0` confirming real decode
activity accompanied it. Representative timing (hardware-free,
`sync=False` harness, so wall-clock-fast, not representative of real
300 ms primer pacing): `first_real_post_primer_ms=3`.

**B. Digitally silent real media** --
`test_digitally_silent_real_media_still_crosses_boundary`. A WAV file
whose decoded samples are exactly zero (`_write_silent_wav`) still fires
the milestone exactly once -- proof the mechanism is buffer-flow
evidence, not amplitude detection.

**C. Never-real / failed start** --
`test_never_real_start_never_fires_boundary`. A FIFO source held open by
a writer that never sends a byte (same technique as the existing
`NeverPrerolledTests`) lets the silence primer flow normally (self-
contained, no dependency on the real branch) while decode genuinely never
produces a single buffer. Confirmed: `deck.media_buffer_count == 0` and
`FIRST_REAL_POST_PRIMER_MILESTONE` never appears, even after a bounded
1.5 s observation window.

**D. Teardown before real output** --
`test_teardown_before_real_output_leaves_boundary_absent_and_replacement_unaffected`.
A deck retired via `_remove_deck` before the pipeline is even told to
play never gets the milestone; a fresh replacement generation in the same
slot reaches it normally and independently, and the retired generation's
own (absent) record is untouched by the replacement's activity.

**E. Very short valid media** --
`test_very_short_valid_media_reaches_boundary_once`. A ~4.5 ms real asset
(200 frames at 44.1 kHz, shorter than the primer itself) still reaches
the boundary exactly once before the deck completes.

**F. Crossfade overlap** --
`test_concurrent_generations_each_get_independently_attributed_boundary`.
Two decks (slot A / slot B, one with ordinary real audio, one digitally
silent) created and played concurrently in the same real mixer. Each
reaches its own milestone exactly once, correctly and independently
attributed to its own `Deck` object/generation -- confirmed by construction
(each deck's `concat`/`real_concat_sink` are separate GStreamer objects
captured in separate closures) and directly asserted.

**G. Manual seek recreation** --
`test_gated_seek_recreation_fires_once_as_media_flow_evidence`. A real
`_begin_gated_seek` (the shared primitive behind `_seek_deck` /
`_resume_deck`) recreation fires the milestone exactly once. **Important
documented nuance, not silently assumed:** the milestone is observed on
`real_stage_src`, which for a non-primed deck sits *upstream* of the
gated-seek mechanism's own separate `BLOCK|BUFFER` gate on the external
ghost pad. It therefore fires on the pre-seek preroll buffer the gate
itself waits for as its own readiness signal -- *before* the flushing
seek discards that buffer and decode restarts at the actual target
position. This is real, generation-correlated media-flow evidence that
decode has started for this generation; it is **not** proof that content
at the specific resumed/sought position has crossed the boundary, and
Phase B accounting must not treat it as confirmation of a second logical
occurrence play.

**H. Auto-resume preparation** --
`test_auto_resume_hint_keeps_silence_prime_and_fires_boundary_pre_seek`.
Confirmed directly: an `_resume_hint`-driven recreation keeps
`silence_primed=True` (per Section 2, point 3) and reaches the milestone
through the *same* primed-path `concat`/active-pad mechanism as an
ordinary fresh start -- which happens before the auto-resume's own
`seek_simple()` is even issued. **Documented nuance:** as with G, this
means the milestone reflects content from the start of the track, not
from the resumed position, for this specific path.

**I. Pause/resume** --
`test_pause_does_not_refire_and_resume_creates_independently_attributed_generation`.
Confirmed the milestone count stays at exactly 1 through a `_pause_deck`
call (no new buffers flow while paused). Also confirmed and documented:
in this codebase, `_resume_deck` **always** tears down and recreates a
fresh generation via `_begin_gated_seek` (see its own docstring) rather
than literally resuming the same bin from `Gst.State.PAUSED` --
so "resume" is architecturally identical to scenario G, and correctly
produces its own independently-attributed milestone on the new
generation without touching the original (retired) generation's already-
recorded one.

## 7. Recommended Phase B authoritative boundary

> The first confirmed real-content buffer crossing `concat`'s src pad
> (gated on `concat`'s own `active-pad` property already reporting the
> real-content sink pad as active) toward the deck's ghost pad and the
> live `audiomixer` request pad it feeds -- for a silence-primed fresh
> start or auto-resume recreation; or, for a non-primed manual
> `resume_position_ns` recreation (no primer/`concat` present at all),
> the first buffer observed at the real decode stage's own output pad,
> which for that path already IS the ghost pad's target.

This is deliberately scoped to "crossed a pad boundary inside the
process's own pipeline," not "reached the physical transmitter" or "was
audible to a listener" -- neither of those was measured here and neither
should be claimed.

## 8. Rejected candidate boundaries

* **`Deck.mark_media_buffer()` / `media_buffer_count > 0`** (the
  pre-existing counter). Rejected as the authoritative signal: proven
  upstream of `concat` for a primed deck, so it fires on
  decode/branch activity that may still be sitting behind the active
  silence primer.
* **A plain `BUFFER` probe on `concat.src` / the ghost pad, with no
  further qualification.** Rejected: sees the silence-prime buffer
  first; calling that buffer "real" was the exact mistake this task
  exists to avoid.
* **`notify::active-pad` signal-based marking.** Considered and
  measured, not chosen: fires deterministically but reliably *before*
  the buffer it corresponds to (see Section 4's timing), which is a
  looser correlation than reading the same property from directly
  inside the buffer's own probe invocation.
* **A PTS/timestamp threshold (e.g. "first buffer with `pts >=
  SILENCE_PRIME_SECONDS`").** Not implemented: while `concat`'s
  `adjust-base=true` behavior makes this plausible, it would require
  tuning a threshold and would still be an indirect inference rather
  than asking `concat` directly which sink is active -- strictly weaker
  than Finding 2's direct property read for no offsetting benefit.

## 9. Threading / generation safety

Both probes only call `Deck.mark_milestone` (already lock-protected,
allocation-bounded, no I/O) and a plain GObject property getter
(`concat.get_property("active-pad")`, not a blocking call). No Django
ORM access, filesystem access, or network call occurs from either probe.
Nothing here required GLib-main-thread marshaling beyond what the
existing milestone-marking probes already establish as the safe pattern
for this exact kind of bounded, in-memory update.

## 10. Unresolved technical ambiguity / known caveats

* **G and H's "pre-seek/pre-resume-position" firing** (Section 6) is a
  real limitation of observing on `real_stage_src` rather than on the
  gated-seek mechanism's own post-seek confirmation. It is fully
  documented and tested here rather than silently smoothed over; Phase B
  will need to decide whether the manual-seek/auto-resume paths deserve
  their own, separately-named "confirmed post-seek buffer" milestone
  (the gated-seek mechanism already captures almost exactly that, as
  `op["confirmed_buffer_pts"]`, for its own internal purposes) rather
  than reusing this one.
* An earlier draft of this change registered the self-removing boundary
  probe in `probe_handles` like every other probe, which produced a
  cosmetic (non-raising) `GStreamer-WARNING ... pad has no probe with
  id` during teardown whenever the probe had already fired and removed
  itself. Fixed by simply not registering that one probe in
  `probe_handles` (see Section 5) -- its lifecycle is already fully
  self-managed. Confirmed clean (no such warning) in the final Tier 1
  run.
* This phase did not attempt to observe anything beyond the deck's own
  ghost pad / `audiomixer` request pad -- no claim is made about the
  aggregator's own output, StereoTool, ALSA, or the physical transmitter.

## 11. Test results

* Tier 1 (new, focused): `library/tests/test_engine_playback_boundary_evidence.py` -- **9/9 pass**.
* Tier 2 (directly affected existing suites): `test_engine_deck_lifecycle.py`,
  `test_engine_seek_audio_flow.py`, `test_engine_eos_plausibility.py`,
  `test_continuation_hour_orchestration.py` -- **137/137 pass**, run alone
  (no concurrent test processes) to avoid this suite's own real-time/
  `sync=True` mixer-timing assertions flaking under CPU contention (two
  transient failures were observed and diagnosed while three test
  processes were running concurrently on this machine during
  development -- one a stale `inspect.getsource` line-number cache from
  editing `engine.py` while an already-imported test process was still
  running, one a genuine wall-clock-deadline miss in a `sync=True`
  seek-timing test under load; both reproduced as failures only under
  contention and passed cleanly in isolation, and neither touches the
  code this phase changed).
* Tier 3 (broader engine/playback subset): `test_engine_mic_clock_policy.py`,
  `test_engine_mic_recovery.py`, `test_engine_output_recovery.py`,
  `test_engine_queue_eta.py`, `test_engine_remote_dj_connection_failures.py`,
  `test_engine_remote_dj_vu_meter.py`, `test_engine_runtime_commit.py` --
  **149/149 pass**.
* `manage.py check`: clean.
* `makemigrations --check --dry-run`: no changes (no migration expected
  or created).
* `py_compile`: clean for both changed/added Python files.
* `git diff --check`: clean.

## 12. Files touched

* `library/services/engine.py` -- the two probes and the milestone
  constant described in Section 5. Nothing else in this file changed.
* `library/tests/test_engine_playback_boundary_evidence.py` -- new,
  Section 6's 9 tests.
* `scratchpad/playback_boundary_p2_1_6/harness_concat_boundary.py` --
  new, the isolated `concat`-only harness from Section 4.
* `scratchpad/playback_boundary_p2_1_6/README.md` -- new, harness usage
  notes.
* `docs/PLAYBACK_EVIDENCE_BOUNDARY_PHASE_A.md` -- this report.

No migration. No changes to `LogItem.played_at`, `Track.last_played_at`,
`Track.play_count`, scheduler recency policy, request fulfillment timing,
`PlayEvent`, royalty reporting, TuneIn, `_write_now_playing`, RBDS
metadata timing, dedication semantics, queue/log selection, crossfade
policy, production audio topology, or the GStreamer package version.
