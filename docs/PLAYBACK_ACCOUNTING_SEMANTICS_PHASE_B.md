# P2 1.6 Phase B — authoritative air-start and occurrence accounting

## Scope and authority

Phase B is based on canonical packaged r0075
`a5392ecfeb1086d61e2dee5efecf743ed2bbb170` and consumes the reviewed
Phase A observational commit
`631738e772f41597e91f6d26f68d40bfd7d8d88b`. The implementation branch is
`feature/p2-1.6-phase-b-air-start-semantics` in
`/home/jreed/isadoraair-p2-1.6-phase-b`.

This phase changes persisted start/accounting semantics only. It does not
package, publish, deploy, restart production, or implement Phase C's durable
cross-generation duration/restart close-out.

## Evidence consumed from Phase A

For a normal fresh, silence-primed generation, the earliest truthful start
evidence is a `BUFFER` probe invocation on `concat.src` for which concat's
buffer-instant-correlated `active-pad` property equals the real-content sink.
The event is named `FIRST_REAL_POST_PRIMER_MILESTONE`. It is independent of
amplitude, so digitally silent real media qualifies. `notify::active-pad`,
decode activity upstream of concat, decoder startup, and primer completion
alone remain rejected evidence.

Phase A also observes first-buffer flow on the non-primed decode-stage output.
Phase B does not consume that observation for accounting because every
non-primed `resume_position_ns` generation is a continuation, not a new logical
occurrence.

## Old and new semantic model

Previously `_create_deck()` immediately and independently wrote
`LogItem.played_at`, Track recency/count, request fulfillment, and a PlayEvent
after linking/synchronizing a deck. A claimed/prerolled/failed deck could thus
look aired, and failures between statements could leave partial accounting.

Phase B separates two milestones:

1. `LogItem.playback_claimed_at` means the engine committed this concrete
   occurrence to playback. It is one-way, survives deck failure/recreation,
   makes the occurrence unavailable to late scheduling/dedication mutation,
   and is explicitly not air evidence.
2. `LogItem.played_at` means the fresh occurrence's first confirmed real
   post-primer buffer crossed the deck output boundary toward the mixer. This
   timestamp is now the authoritative air-start instant.

Claim happens after request substitution, dedication selection, and the
central playability gate resolve the concrete LogItem, but before presentation
metadata or live-pipeline construction. Reclaiming the same occurrence keeps
the original timestamp. A claim failure prevents deck construction.

The semantic cutover starts at the Phase B implementation commit. No historical
`played_at` value is reinterpreted or rewritten.

## Schema and historical policy

Migration `0082_logitem_playback_claim_and_playevent_occurrence` adds:

- `LogItem.playback_claimed_at`: nullable, indexed DateTimeField.
- `PlayEvent.log_item_id_snapshot`: nullable, unique BigIntegerField.

The scalar PlayEvent key is an immutable logical-occurrence snapshot and
survives LogItem deletion. PostgreSQL permits multiple historical `NULL`
values while enforcing at most one non-null event per LogItem. Existing
LogItems and PlayEvents receive `NULL`; no guessed correlation or timestamp
backfill occurs.

## Streaming-thread handoff

At the Phase A boundary the pad probe calls
`_schedule_occurrence_air_start_from_probe(deck)`. That function only:

- checks the generation's in-memory fresh-eligibility/one-shot guard;
- captures `timezone.now()` beside the crossing buffer;
- schedules exactly one `GLib.idle_add` callback with slot, generation,
  LogItem id, and the captured timestamp.

It performs no ORM, filesystem, network, SystemEvent, or per-buffer logging.
All persistent work occurs later in `_record_occurrence_air_start()` on the
GLib/main thread. The callback rejects a stale slot, generation, occurrence,
or ineligible continuation before touching the database.

## Transaction and idempotency

`_record_occurrence_air_start()` runs one `transaction.atomic()` and locks the
LogItem with `select_for_update()` before it:

1. verifies the persistent claim and expected Track identity;
2. uses non-null `played_at` as the idempotency gate;
3. writes `LogItem.played_at` with the probe-captured timestamp;
4. updates `Track.last_played_at` to that timestamp and increments
   `Track.play_count` with `F("play_count") + 1`;
5. fulfills matching scheduled SongRequests with that timestamp;
6. creates the occurrence-keyed PlayEvent with that timestamp and immutable
   Track/category snapshots, unless the occurrence is a Dedications intro.

The row lock serializes racing callbacks; the unique PlayEvent occurrence key
is the database backstop. A duplicate callback sees non-null `played_at` and
does not repeat any mutation. A transaction error rolls all tables back. The
engine emits a bounded diagnostic and makes one same-process retry using the
original observed timestamp while that exact generation remains current;
durable recovery is deferred to Phase C.

After commit, a still-current deck receives the PlayEvent id. Ordinary
uninterrupted retirement therefore closes the event and computes duration from
the actual boundary timestamp rather than deck construction.

## Fresh versus continuation generations

Fresh eligibility is explicit on `Deck.air_start_eligible`.

- Ordinary fresh occurrence with null `played_at`: eligible.
- Manual seek or pause/resume recreation (`resume_position_ns` supplied): not
  eligible, even when the requested/fallback position is zero.
- Exact `_resume_hint` auto-resume: not eligible.
- Defensive recreation of an already-started occurrence: not eligible.

Auto-resume now requires both Track id and LogItem id to match. An exact
auto-resume whose persisted `played_at` is unexpectedly null remains claimed
but unplayed, emits a bounded warning, and never fabricates an original-start
timestamp from pre-seek decoded buffers.

Continuation does not write another `played_at`, increment Track count,
fulfill requests again, or create another PlayEvent. Continuation-duration
accounting remains incomplete until Phase C.

## Consumer audit

| Consumer | Previous meaning/use | Phase B meaning | Changed? | Reason |
|---|---|---|---|---|
| `_create_deck()` accounting writes | Construction implied aired | Claim only; actual writes removed | Yes | Construction/preroll is not air proof |
| Phase A concat/decode probes | Observation only | Fresh concat boundary dispatches main callback; continuation remains observation only | Yes | Consume the proven boundary without streaming-thread I/O |
| `_load_log_for()` / queue cursor / reload anchoring | Non-null `played_at` means started | Non-null means authoritative actual start | No query change | These are actual-play consumers |
| Prior-date startup continuation inspection | Played history vs remaining unplayed | Same, now based on truthful starts | No | It needs actual history, not mutability |
| `log_builder.get_recent_exclusions()` and loosening pass | Non-null `played_at` enters separation | Same; claimed-only rows remain excluded | No query change | Recency is actual-play semantics |
| Track dormancy weighting | `last_played_at` resets at construction | Resets at authoritative start | Write changed; formula unchanged | Preserve policy with truthful input |
| Track `play_count` | Non-atomic construction-time increment | Atomic `F()` increment at authoritative start | Yes | Race-safe exactly-once count |
| `maybe_schedule_song_request()` | `played_at` guarded late swaps | Claim or played guards swaps | Yes | Claim owns mutability before air proof |
| Refresh command candidate/advisory queries | Null `played_at` meant available | Both claim and played must be null | Yes | Claimed rows are no longer future slots |
| Refresh self-heal/reconciliation | `played_at` proves request aired | Same authoritative evidence | No query change | Actual-play consumer; normal writes are now atomic |
| Dedication generation command | Null `played_at` allowed synthesis | Claim and played must both be null | Yes | Avoid work for a committed occurrence |
| Dedication synthesis attachment CAS | Request state only | Also requires null playback claim | Yes | Closes render-versus-claim race |
| Dedication splice lookups/CAS | Request state/`played_at` assumptions | Also require null playback claim | Yes | No mutation after engine commitment |
| Dedication presentation/admin evidence | Intro `played_at` means occurred | Same, now at authoritative boundary | No | Actual occurrence evidence |
| `mark_song_requests_aired()` | Called after construction-time write | Called inside authoritative transaction | Yes | Claimed/never-real requests stay unfulfilled |
| PlayEvent royalty ledger | Created at deck construction | Created once at authoritative start | Yes | Truthful start and occurrence identity |
| TuneIn latest-PlayEvent trigger | Visible at construction | Visible only after actual start | Natural consequence | TuneIn follows truthful PlayEvent timing |
| Royalty report 30-second policy | Filtered at report time | Unchanged | No | Short plays remain ledger evidence |
| `_write_now_playing()` / RBDS category state | Deck-creation presentation approximation | Unchanged | No | Presentation timing is intentionally distinct |

## Web Requests and dedications

A requested occurrence may be claimed and preroll without being fulfilled. It
is fulfilled only inside the authoritative transaction after real output, and
all matching scheduled requests share the same timestamp. Reconciliation may
later decide the fate of a claimed/never-real request; claim itself never
fulfills it.

Dedication artifact generation, placement, intro occurrence start, and
requested-song fulfillment remain distinct facts. A dedication intro receives
the same authoritative `played_at` and Track accounting as other occurrences,
but keeps the existing exclusion from royalty PlayEvents. Its start does not
fulfill the requested song.

## Scheduler, TuneIn, and presentation consequences

Claimed-but-never-real rows retain `played_at=NULL`, so they do not enter title
or artist separation and do not reset Track dormancy. The formulas and policy
windows are unchanged. TuneIn sees the new PlayEvent only after actual start.
Now-playing stream metadata and RBDS category state remain deck-construction
approximations during crossfades and are not accounting evidence.

## Remaining Phase C work

Phase B deliberately does not solve durable duration across manual seek,
pause/recreation, engine restart, auto-resume, crash interruption, or unclosed
PlayEvents. In particular, a continuation generation does not acquire a new
PlayEvent id, so only ordinary uninterrupted playback gets the immediately
improved actual-start-to-retirement duration. Phase C must also close durable
retry/recovery and product acceptance around restart/crash timing.

For that reason Phase B must not be deployed by itself.

## Validation and later production acceptance

Focused coverage includes claim-before-air, never-air/no-callback state,
authoritative same-timestamp writes, digital-silence and very-short Phase A
evidence, duplicate and racing callbacks, stale generations, seek/pause/auto
continuations, null-played auto-resume diagnostics, request races, dedication
exclusion, recency, uniqueness, rollback, historical null defaults, and
ordinary event close-out. Final command counts are recorded in the task
completion report after the full suite.

Before a later Phase C release can be accepted for production it must add and
validate durable duration/close-out across recreation and restart, crash and
unclosed-event handling, recovery of any persisted claimed/unplayed edge
states, TuneIn product timing, migrations on a production-like copy, and a
controlled on-air acceptance plan. Phase B alone is not a deployable release.
