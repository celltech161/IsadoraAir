# Roadmap 2.25 B7: final engineering qualification and timing hardening

Status: **B7 qualification gate.** B7 corrects the defects that qualification found on the accepted
B6 correction (`f2f7f33`), selects the re-admission lead from measurement, closes the media-test
coverage gap with an isolated test-tooling topology, and qualifies the exact final revision. Local
engineering only: nothing is published, integrated or deployed.

**Ancestry:** r0144 `e4003aa` -> A `6ad5222` -> B0 `ced54ed` -> B1 `6f66222` -> B2 `f55febe` -> B3 `2e5da2f`
-> B4 `d279cb8` -> B5 `2dc58eb` -> B6 `c380671` -> B6 correction `f2f7f33` -> **B7** (corrections,
test isolation, measured lead, qualification tooling, this document).

## 1. Corrections

| Id | Defect (reproduced with real audio on f2f7f33) | Correction |
|---|---|---|
| C1 | A scheduled incoming that misses readiness falls back to an ordinary primed start of the same occurrence. The deferred start was an unguarded timer: after reload or abort it opened the replaced program's song; Manual or the mic hold were ignored (automatic start in Manual); an outgoing EOS, Eject or watchdog during the wait started the NEXT queue item and the timer then opened the claimed one (two songs); the music waited A_seg + U for intro speech that was already cancelled (underlap-length dead air); Start Next was refused (`busy_transition_recovery`) for the whole wait, U + 0.2 s, unbounded by VT length; urgent insertion and Auto talk-over did not release it. | Until its start, the claimed occurrence IS the B5 held occurrence carrying a fallback token; the deferred start is token-guarded. Manual, the mic hold and an outgoing pause/seek keep it (next authorized start); Start Next starts it now; reload discards it (claim kept, never aired, as r0144 retires a claimed not-yet-audible deck); the outgoing finishing is owned by the transition; its music lands after audible intro speech or an outro that still airs, otherwise at the ordinary segue (no dead air); urgent and talk-over start it as soon as the music may. `busy_transition_recovery` remains only for the bounded slot wait (<= 50 x 10 ms). `held_occurrence.automatic_start_pending` (additive). |
| C2 | A Track adopted at the claim (a Swap committed after the engine's queue copy, up to ~3 s before the segue) got its intro VT only if it fitted with U = 0, and the late U = 0 path ignored the outro gap. | The ordinary planner applies (outro and gap, margin, underlap, missing-Intro-Until fallback). A later music start re-admits the same occurrence by regeneration, only with the song not on air and >= REOPEN_LEAD_S of lead; otherwise (a genuine deadline miss) `intro_known_too_late` and the music never moves. |
| C3 | Start Next within HOLD_MIN_LEAD_S (120 ms) before a held song's admission, with no other live deck, faded that song as if it were the outgoing and started the following item. | The imminent held song is the operator's next: it keeps its admission (`vt_admit`, `kept`). After admission it is on air (played_at recorded) and Start Next advances past it (2.23). |
| C4 | A refused Pause/Seek of a waiting deck was visible only in `engine_state.json`; reload silently dropped a held claim; a held claimed WxAlert looked "claimed, never aired, not on a deck" to the alert sync, which would queue a retry and air it twice. | One deduplicated operator event per refusal naming the alternatives (Start Next, Eject); reload logs the held claim it discards; the alert sync counts a held claim as owned (after reload it is retried as before). |
| C5 | Re-admission lead 100 ms (R01 failure). | 150 ms, measured (section 2). |
| C6 | A transition that finished before committing (released to the ordinary path, aborted) published its predicted side as `planned` in `recent` (found by the soak's outcome accounting). | A finished transition publishes `not_attempted` with a reason (`released_before_commit` / `finished:<reason>`); never `planned`. |
| T1 | Test isolation: Active Message serialized-rollback classes failed `setUpClass` after any plain TransactionTestCase (ContentType key collision, Django 5.2). | Test-only: flush with post_migrate inhibited before the snapshot loads. |

## 2. Re-admission lead (C5)

A re-admission by regeneration retires the held generation, re-claims (idempotent PostgreSQL row
lock), builds the scheduled bin, waits for its first decoded buffer, and links it from a GLib
callback that must run >= READY_LEAD_S (50 ms) before the admission. With a 100 ms lead that whole
chain has 50 ms. `library/tests/measure_vt_b7_readmission.py` measures it with the real B2 path and
PostgreSQL, in program PCM, under: idle; representative CPU (one core at 75 % plus three real-time
encoders, the StereoTool + Liquidsoap shape); saturated CPU (all eight allowed cores); concurrent
PostgreSQL (aggregates, row churn and short row locks in the same log); decode + encode at full speed
(three FLAC -> MP3/Opus/Vorbis pipelines); Remote DJ session churn (two webrtcbin peers: session build,
DTLS certificate, SDP offer/answer, Opus, teardown every 3 s -- ICE cannot connect inside the network-
less sandbox); a busy GLib main loop (15 ms of work every 50 ms on the engine's own loop); combined.

Readiness after the call is 25-38 ms median and <= 53 ms worst in every representative condition (0
misses at 100, 150 and 200 ms; 360 trials). A busy main loop moves it to ~50 ms median: 100 ms then
misses READY_LEAD_S in 50-60 % of trials, 150 ms never (20/20). Under the synthetic worst case (busy
loop plus every load) 150 ms missed 3/80 and 200 ms 0/80. Every miss recovered through the ordinary
fallback in 0.57-0.80 s with exactly one PlayEvent and the claim unchanged; scheduled onsets that met
readiness were exact (|error| <= 0.02 ms). Ordinary (non-VT) segue timing does not use this lead.

## 3. Operator policy evidence (section 4 of the workorder)

* Waiting-deck Pause/Seek: refused, published, one operator event, nothing changed.
* Start Next during recovery: the deferred fallback is a held claim Start Next starts at once;
  measured refusal window 0.023 s (one press: none). `busy_transition_recovery` only for the bounded
  slot wait; the obsolete `busy_voice_track` is never emitted (the dashboard still has its label).
* Near admission: Manual and Start Next swept at -200, -135, -105, -40 and +60 ms around the actual
  admission (first-real milestone). Before: never skipped (held when > 120 ms, kept within 120 ms);
  after: on air (2.23). A regenerated deck whose decode is delayed past READY_LEAD_S falls back to
  the same occurrence (0.70 s after the press, one air start).
* Late Swap: underlap and missing-Intro-Until eligibility apply when the answer leaves lead (VT and
  music both 0.0000 s); an answer after the deadline suppresses only the VT (music 0.0000 s).
* Holds: operator Manual + mic, mic hold + operator Manual, studio mic + Remote DJ gate: releasing one
  never admits while another applies; the deferred handoff starts the same occurrence ~0.55 s after
  the last release, with no intro VT.
* Reload: discards the held claim exactly as r0144 retires a claimed not-yet-audible deck; forced
  follow-ups (urgent items, dedication songs) survive; a held WxAlert is retried after reload.

## 4. Coverage of the 91 previously blocked media tests

The station's validation service confines each validator with Landlock "read beneath /" of ITS
mount namespace and reopens the received media descriptor through `/proc/self/fd/<n>`. In the
guarded test runner the validator runs in its own bubblewrap sandbox, so a descriptor opened in the
test's sandbox belongs to another mount tree and is refused (`unreadable_container`). On a station
the web and validation services share the host mount namespace.

Test-tooling variant (evidence directory, NOT part of the candidate): `tooling/run_with_validation_
service.shared_ns.sh` is the checkout's runner with one change -- the validation and render services
start inside the test command's own sandbox (same namespaces, the delegated scope's `supervisor`
cgroup, leaves beside it as under `DelegateSubgroup=supervisor`). The disposable PostgreSQL, the
preflight, the sandbox, the real validator and confinement code are unchanged. No disposable VM was
available: LXD is installed on this host but uninitialized, and initializing it would change the
on-air host's networking and storage; a VM remains the recommended independent environment.

## 5. Asynchronous lookups, state and observers

The B7 module exercises worker start/stop and connection release on the worker thread, bounded
shutdown with work outstanding (0.20 s; queued work abandoned), stale-answer rejection, expiry and
re-request of planning answers, configuration refresh, replaced and invalid media after the answer,
owner retirement before the answer, engine shutdown with outstanding lookups (0.50 s), and an
unavailable worker (ordinary segue). A static audit proves VoiceTrack / VoiceTrackConfig reads and
VoiceTrack media resolution happen only in the worker's resolver. `engine_state.json` keeps all 36
r0144 top-level keys; `vt_transition` is the only addition.

The Hour Refill observer change (`_engine_vt_phase`) lives in functions that take a live engine
object and run only in the engine process; the web (Gunicorn) imports those modules but never calls
them. Restart inventory: `isadoraair-engine` only (with the r0144 safeguard); no template, view,
migration, static or protected-runtime change.

## 6. Soak

`library/tests/soak_vt_b7.py` (run explicitly): one engine, real time, PostgreSQL accounting, real
VoiceTrack rows and the real lookup worker, ~10-minute soak hours rolled over by the engine's own
exhausted-log path, scripted Start Next, Eject, Manual/Auto, studio mic, Remote DJ gate + reconnect,
slow and failing lookups, invalid VT and music media, urgent insertion, the Hour Refill observer every
30 s, and per-minute samples (resources, pipeline elements, RSS, lookup queue/cache, teardown health,
silence analysis against the scripted holds). Seams: no station I/O; live fill not exercised (complete
hour logs, one short hour every fifth); the Remote DJ media path is a gate stand-in.

## 7. Hardware

See the evidence README: the nine-case loopback procedure, reviewed, with two B7 cases added; not
run (no isolated off-air hardware; live audio may not be touched).

## 8. Supplemental qualification (B7S)

| Id | Defect | Correction |
|---|---|---|
| C7 | A waiting scheduled deck counted as "on air" whenever its admission was within a fixed HOLD_MIN_LEAD_S (120 ms). Measured: withdrawing it rendered NOT ONE sample until the non-live deck mixer had pushed output covering the admission -- the mixer runs 7-16 ms ahead of the clock -- so Manual / the mic hold 20-120 ms before admission let a still-recallable song start (contract violation). | "On air" is decided from pipeline evidence: a src-pad probe records how far the deck mixer has pushed; a scheduled deck is on air when that position plus MIXER_RECALL_GUARD_S (20 ms: one buffer being aggregated, one being pushed while the GLib thread decides) reaches its admission. Measured with the rule (90 cases: Manual idle / busy loop, mic hold): every held deck rendered nothing; every on-air deck played in full with one air start; no partial burst. The latest hold was 29.9 ms before admission (was 120 ms). Start Next keeps a deck it cannot recall (vt_admit, kept); never skips a never-audible occurrence. |
| C8 | A pending fallback start (C1 token) survived engine shutdown. | `_vtt_shutdown` invalidates the token; the deferred attempt never runs on a stopped engine. |

Evidence-only test tooling in the candidate's test tree (no product effect):
`measure_vt_b7s_admission_boundary.py` (the boundary sweep), `_vt_b7s_remote_lab.py` +
`test_vt_b7s_remote_dj_media.py` (the engine's REAL Remote DJ session over a real WebRTC loopback
connection: ICE limited to 127.0.0.1 through libnice's own API, DTLS-SRTP, Opus both ways, a
synthetic 3200 Hz remote microphone), `test_vt_b7s_supplemental.py` (held-occurrence authority:
shutdown, hour adoption, queue reload, dedication follow-up, pending alert through reload),
`soak_vt_b7s_livefill.py` (the normal live-fill soak on real wall-clock hours) and
`memsoak_vt_b7s.py` (the portable memory workload run unchanged on r0144 and the candidate).
