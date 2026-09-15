# P2 1.6 Phase C — durable playback-duration semantics

## Authority and locked semantics

Phase C is based on canonical r0075
`a5392ecfeb1086d61e2dee5efecf743ed2bbb170`, Phase A
`631738e772f41597e91f6d26f68d40bfd7d8d88b`, and Phase B
`003b896f9e90b6062c0b3be11b39c621772843b4`.

Phase A's boundary and Phase B's logical occurrence contract remain locked.
`playback_claimed_at` is control ownership, not air evidence. `played_at` and
`PlayEvent.started_at` are the first real-content buffer observed at
`concat.src` while `concat.active-pad` is the real sink (or the equivalent
non-primed output boundary). One LogItem occurrence still produces at most one
play count, request fulfillment, and occurrence-keyed PlayEvent.

## Lifecycle audit

| Path | Generation ends? | Logical occurrence ends? | Continuation expected? | Close segment? | Finalize PlayEvent? |
|---|---:|---:|---:|---:|---:|
| Natural EOS | yes | yes | no | yes, at mixer detach | yes |
| Crossfade/ordinary next | yes | yes | no | yes, at mixer detach | yes |
| Manual seek | yes | no | yes | yes | no |
| Pause | contribution ends | no | yes | yes, at mixer unlink | no |
| Resume recreation | old generation ends | no | yes | old closes; new opens after confirmed contribution | no |
| Gated replacement | yes | no | yes | close any contributing generation | no |
| Rejected seek | requested generation continues from truthful zero | no | yes | no segment before gate release; then normal segment | no |
| Never-prerolled/unconfirmed seek | yes | no | zero fallback | no segment for rejected generation | no |
| Watchdog/error/poison/eject/log reload | yes | yes | no | yes | yes |
| Clean SIGTERM/SIGINT or updater restart | yes | no if exact resume remains valid | yes | yes, at detach | no at shutdown |
| Auto-resume | new generation | no | yes | open after confirmed post-seek contribution | no |
| Crash/SIGKILL | process disappears | unknown | maybe | checkpoint remains; stale segment becomes interrupted | no fabricated end |
| Host reboot (`/run` hint lost) | prior process disappears | unknown | no proven continuation | stale segment becomes interrupted | no fabricated end |

## Evidence model

`PlayEventSegment` is an additive child ledger. Each process-local deck
generation receives a process-global UUID, used as the durable uniqueness and
idempotency key. The integer deck generation, slot, LogItem snapshot, start
reason, termination reason, and evidence state remain diagnostic/auditable.
Deleting or pruning a LogItem cannot delete a PlayEvent or its segments because
the relationship is from segment to PlayEvent and occurrence identity is a
numeric snapshot.

`PlayEvent.duration_played_seconds` is the transactionally recomputed sum of
all child `confirmed_duration_seconds` values. It is never derived from
`ended_at - started_at`. Absolute totals plus the UUID uniqueness constraint
make checkpoint and close retries idempotent; a transaction holds the parent
row lock and changes segment evidence and the aggregate together.

Historical PlayEvents receive only `duration_evidence_state="legacy"`. The
migration creates no segments and changes no existing duration or timestamp.

## Start and end boundaries

The fresh segment uses the exact timestamp captured for the Phase B air-start
transaction. Its in-memory accumulator is enabled in that same boundary probe
before the real buffer reaches the deck ghost output. The 300 ms concat primer
crosses while accumulation is disabled and therefore contributes zero.

Every deck ghost-output buffer is measured by its declared GStreamer duration,
not by wall time. Missing, sentinel, negative, or implausibly large durations
are ignored conservatively.

A manual seek, pause resume, and automatic restart resume use the existing
buffer-only gated-seek state machine. Decode/preroll and pre-seek gate hits do
not enable duration. Once the requested position is accepted, its post-seek
buffer is confirmed, and the pad running-time offset is established, the
segment is enabled while that exact buffer remains held. The gate is removed
last. A rejected seek is rebased truthfully to zero and begins only when its
held zero-position buffer is released. A never-prerolled or unconfirmed
generation contributes nothing; its position-zero fallback starts on that
fallback generation's first real output buffer.

The segment-end timestamp is captured at mixer unlink, before request-pad
release or hazardous asynchronous bin destruction. Buffer summation stops at
that same isolation boundary. Pause and planned replacement close only the segment.
Natural EOS, next-track retirement, eject, error, poison, watchdog, and log
replacement also finalize the occurrence. `PlayEvent.ended_at` is consequently
the terminal logical end, never an intermediate generation end.

## Restart, checkpoints, and interruption

Clean stop writes the non-secret Track + LogItem + position resume hint before
deck retirement. It then closes the active segment as `clean_shutdown` without
finalizing the occurrence. An exact next-process Track + LogItem match uses the
same gated seek as manual seek and attaches a new segment to the same
occurrence-keyed PlayEvent. Service downtime has no buffers and cannot count.
A clean open occurrence with no matching hint is finalized at its latest known
closed segment boundary during startup reconciliation.

GLib's Unix signal sources are the sole steady-state SIGTERM/SIGINT authority.
They are registered before startup can construct a contributing deck, so a
signal received before `loop.run()` remains pending for safe main-context
dispatch. No Python `signal.signal()` handler competes for those signals or
raises an asynchronous exception inside an arbitrary GLib callback. The GLib
handler idempotently requests loop exit; the current callback returns, the loop
unwinds, and `stop()` runs exactly once.

That orderly path unlinks a contributing generation at the known mixer
boundary and closes it `complete` with termination reason `clean_shutdown`. If
the occurrence auto-resumes and later reaches terminal EOS, the parent becomes
`complete`, receives the EOS terminal `ended_at`, and aggregates the complete
segments while excluding the restart gap. If startup finds no valid
continuation, the existing reconciliation rule instead finalizes the clean
occurrence at its last known closed-segment boundary.

The hint is considered resumable only when the exact Track + LogItem is still
present in the loaded/forced startup queue; a stale hint for a regenerated or
missing occurrence cannot keep an event open.

Active duration is checkpointed every five seconds. Streaming probes only add
buffer durations under an in-memory lock; ORM work occurs on the GLib timer.
The checkpoint stores the absolute total, so it is neither per-buffer nor
double-additive. Database errors are caught and cannot stop audio. A crash can
lose only the unknown tail since the last successful checkpoint.

On startup, any stale active segment is marked `interrupted`, keeps its last
confirmed total, keeps `ended_at` null, and records `process_interrupted`. The
parent is marked interrupted and never uses next-startup time as an end. An
exact auto-resume adds a new segment to the same PlayEvent without erasing the
old uncertainty. A mismatch or lost `/run` hint leaves the old interrupted
occurrence auditable with an unknown terminal end.

This stale-active rule remains the abrupt-crash contrast to orderly shutdown:
SIGKILL, host loss, or another disappearance that never reached the mixer
unlink/close transaction permanently leaves that segment and parent
`interrupted`, even if a later continuation itself finishes normally.

## Reporting and external identity

SoundExchange's existing report-time 30-second threshold remains unchanged:

* complete confirmed duration below 30 seconds is excluded;
* complete confirmed duration at least 30 seconds is included;
* interrupted confirmed duration at least 30 seconds is definitely qualified
  and included;
* interrupted confirmed duration below 30 seconds is excluded from automatic
  export but explicitly warned as threshold-ambiguous in the operator summary.

No missing seconds are invented. Raw CSV retains its existing leading columns
and appends occurrence id, evidence state, segment/interruption counts, and
termination reasons. Admin exposes both the parent aggregate and a read-only
segment ledger.

TuneIn continues to use the PlayEvent id as song-change identity. Seek,
pause/resume, and restart attach segments to the existing event, so none emits
a new TuneIn identity; the next actual occurrence creates the next event.

## Remaining Phase D work

Phase D must independently accept the complete Phase A–C chain under production
conditions: observe real station transitions, seek/pause/restart behavior,
checkpoint write cadence and database-failure visibility, crash recovery, raw
audit output, SoundExchange review, and TuneIn behavior. Packaging, publishing,
deployment, production restart, and operational acceptance are intentionally
outside Phase C.
