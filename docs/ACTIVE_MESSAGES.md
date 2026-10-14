# Active messages — urgent / public-address domain (iPortal Phase D)

Producers' **urgent** and **public-address** messages: which are active, the
one aggregate the station airs for the whole active set, when it is next due,
and what actually happened each time it was meant to air.

Phase D keeps the legacy OGRemote *product* behavior and replaces its
*mechanics*:

| Kept from OGRemote | Replaced |
| --- | --- |
| several producers active at once; newest message per producer and kind | newest-file-by-mtime → one ACTIVE row per (producer, kind), database-enforced |
| one combined mix: stinger, urgent, then public address, oldest first | mutable `urgent_pa_mix.mp3` → an immutable aggregate per exact composition |
| insertion at the next track boundary, ordinary programming between plays | remote processing + polling + a mutable mix Track → a local, per-execution UrgentPA Track inserted by the same `insert_urgent` splice, with exact execution correlation (D2B) |
| periodic replay (default 20 minutes) until recalled | JSON state file + remote `processed` flag → PostgreSQL authority, backed up with the database |
| a producer recalls their own message | file deletion → an exact-UUID clear |

**OGRemote stays authoritative** until Phase G's cutover. Phase D ships
dormant: `ActiveBroadcastPolicy.native_airing_enabled` is False on every
installation, and D1 refuses to set it (`NATIVE_AIRING_AVAILABLE`). The
domain services work with the gate closed (a message can be activated and an
airing materialized), but nothing dispatches to the engine, so nothing airs;
D1 ships no operator surface that calls them.

Weather and AMBER alerts are **not** active messages. Their `WxAlert` →
`insert_urgent` path is unchanged and stays separate throughout Phase D.

## Phase split

| Step | Scope | Can it change on-air audio? |
| --- | --- | --- |
| **D1** | domain authority: policy/generation, messages, executions, events, media binding, composition, due/replay/expiry, authz, admin diagnostics | **no** — no renderer, no engine path |
| **D2A** | constrained `assemble` render operation (`active_message_mix`), exact provenance, rendition reuse, the worker, `bind_execution_mix` (PENDING_RENDER → READY) | **no** — no engine command |
| **D2B** (this) | publish the READY aggregate as an ordinary UrgentPA Track; the existing `insert_urgent` splice for that exact Track; deck-open authority; the deck's own evidence → STARTED/COMPLETED/INTERRUPTED | only through the existing A/B decks, and **only with the native-airing gate open** (closed in production until Phase G) |
| D3 | recorder adapter, iPortal operator board, monitoring | gate still closed |

## Authority

| Authority | Owner |
| --- | --- |
| immutable message / aggregate bytes | `ProductionMedia` (PROTECT, bound under `retention.lock_for_binding`) |
| which messages are active | `ActiveBroadcastMessage.state` |
| what an aggregate would contain right now | `ActiveBroadcastPolicy.generation` + the ACTIVE rows |
| exactly what one airing contains | `ActiveBroadcastExecution.composition_snapshot` / `composition_digest` (frozen) |
| the aggregate authorized for one airing | `ActiveBroadcastExecution.mix_media` (D2A) |
| whether an airing is due | `next_due_at` of the ACTIVE rows |
| playback representation | an ordinary library `Track` (UrgentPA) and `LogItem` -- representations, never message authority (D2B) |
| delivery to the engine | the existing engine-command FIFO and `insert_urgent` (D2B) |
| whether the engine may still play it | the deck-open authority's fresh database re-read (D2B) |
| what actually aired | the deck's own evidence: `LogItem.played_at` / `PlayEvent` (first real post-primer buffer) and its terminal ledger |
| what happened | `ActiveBroadcastExecution` state + `ActiveBroadcastMessageEvent` (append-only) |
| operator attention | `monitoring.SystemEvent` (never the history) |
| music royalty evidence | `PlayEvent` with the existing UrgentPA classification (kind Spot) — never counted as music |

**Correction (D2B).** D1/D2A stated that active messages create no Track,
LogItem or PlayEvent; that belonged to the rejected special-interstitial
design. Under the adopted architecture an airing IS ordinary library content:
D2B publishes one UrgentPA Track per airing, the engine's own urgent splice
creates its LogItem, and the ordinary deck writes the ordinary PlayEvent.
Message and execution authority stay in `active_messages`; Track and LogItem
are playback representations. (With the native-airing gate closed -- every
production installation until Phase G -- none of them is ever created.) No
SpokenItem/SpokenAsset is ever created, and no path is ever authority.

## Models (`active_messages/models.py`)

* **ActiveBroadcastPolicy** — the singleton (id 1, created on first use, no
  data migration). THE serialization root. `generation`,
  `native_airing_enabled` (default False), `default_replay_interval_seconds`
  (default 1200; 60..86400), `stinger_key` (a ReferenceAudio key or ""), and
  the three composition limits (see "Bounds"): `max_active_messages`,
  `max_message_seconds`, `max_aggregate_seconds`.
* **ActiveBroadcastMessage** — one producer's lifecycle. `producer_key` is the
  durable identity (`user:<pk>` for a native account; other namespaces are
  reserved for the Phase G import), with `producer` as an optional SET_NULL
  link and a username snapshot. `message_media` is the exact media that airs;
  its edit lineage is ProductionMedia's own `derived_from`.
* **ActiveBroadcastExecution** — one logical airing of the aggregate.
* **ActiveBroadcastMessageEvent** — the authoritative history (append-only).

Every ordinary ORM write path refuses (`active_messages/guard.py`): `save()`,
`delete()`, `create()`, `bulk_*`, `QuerySet.update()`/`delete()` — except
Django's own SET_NULL of a deleted user. `active_messages.services` writes
through a private compare-and-write store with read-back.

### Database invariants (all in `active_messages.0001`, CreateModel-only)

* at most one ACTIVE message per `(producer_key, kind)` (partial unique);
* state/timestamp shapes: PREPARED has no activation; ACTIVE has media,
  activation generation/time, interval and `next_due_at`; CLEARED/EXPIRED are
  dated; EXPIRED was active and had an expiry; only CLEARED rows are
  superseded;
* **at most one unfinished execution** (`open_slot` = 1 for unfinished, NULL
  otherwise, unique);
* an execution has its `mix_media` from READY on; ACCEPTED/CLAIMED/STARTED
  are dated in order; COMPLETED has end time and confirmed duration; air
  evidence needs a start; FAILED/SUPPRESSED carry a code;
* the digest is lowercase SHA-256; 1..24 components (the absolute ceiling);
  generation ≥ 1;
* the policy limits lie within 1..their absolute ceilings, and the
  per-message limit never exceeds the aggregate limit;
* one event per Replay Now request token, and it names its execution.

## Message lifecycle

| From | Action | To | Who |
| --- | --- | --- | --- |
| — | `prepare_message` | PREPARED | produce |
| PREPARED | `bind_message_media` (replace allowed) | PREPARED | its producer, or control |
| PREPARED | `activate_message` | ACTIVE | control |
| ACTIVE | activation of the same producer's same-kind message | CLEARED (`superseded_by`) | control |
| ACTIVE | `clear_message` | CLEARED | control |
| PREPARED | `clear_message` (discard) | CLEARED | its producer, or control |
| ACTIVE | expiry sweep (`expire_due_messages`) | EXPIRED | system |

CLEARED and EXPIRED are final. Activate and clear are idempotent (a repeated
POST returns the row unchanged). Clear acts on the exact UUID: clearing an old,
already-superseded row can never touch its replacement. An ACTIVE message's
media never changes — editing means preparing a new message and activating it.

Media binding: valid, present, not a bed, measured duration within the
policy's per-message limit (never truncated to fit), and the binder's own
media unless they hold control. Binding reads the limit for early feedback;
activation re-checks every limit under the policy lock. The replaced media is recorded by
identity in the event detail, not by foreign key, so it stays purgeable.

There is **no default expiry**. `expires_at` is optional; `set_message_expiry`
changes it (PREPARED: producer or control; ACTIVE: control).

## Generation and lock order

Lock order, everywhere: **policy → messages (UUID order) → media
(`lock_for_binding`) → execution**. Every write to an ACTIVE message, and
every change of which messages are active, holds the policy lock.

`generation` increases by exactly one per composition change — activation
(including its supersession), clear of an active message, an expiry sweep
that expires anything — and per output-affecting policy change (stinger,
native-airing gate, composition limits — one step per `set_limits` call however
many limits it changes). Replay never changes it. In the same transaction, every
PENDING_RENDER/READY execution built for an older generation is SUPPRESSED
(`stale_generation`). Executions the engine already holds (ACCEPTED and
later) are left to the engine's own re-read (D2B).

## Composition (`active_messages/composition.py`)

1. urgent, then public address;
2. oldest activation first;
3. message UUID as the tie-breaker;
4. the stinger (the current registration of `stinger_key`) first, only when a
   component is urgent. Blank means no stinger. Django admin offers current
   ReferenceAudio keys; paths are never configuration authority, and every
   update goes through `services.set_stinger_key`.

The snapshot holds message, media and reference identities with SHA-256s and
producer snapshots — never a path. The **digest** covers only what determines
the audio (ordered message/kind/media/sha256 + stinger pin), so an unchanged
set keeps its digest across replays and D2A can reuse its aggregate. If the
configured stinger is unavailable, the airing proceeds without it
(`stinger_unavailable` in the snapshot) and a SystemEvent warning is raised:
an urgent message is never held back for its stinger.

Materialization may resolve whichever ReferenceAudio registration is current
at that moment. Once the execution exists, its `stinger` object is an exact
immutable `{key, reference, media, sha256}` pin. D2A must render that pin and
must never re-resolve the key and substitute a newer registration. Its
adversarial suite must replace a ReferenceAudio registration after execution
materialization but before render and prove the frozen pin is honored. If a
valid frozen pin itself becomes unusable before render, D2A fails that
occurrence `stinger_pin_unavailable` (its snapshot is never edited) and
reconciles it into one RECONCILE successor built from current authority --
see "Aggregate render (D2A)".

## Executions, due time and cadence

`PENDING_RENDER → READY → ACCEPTED → CLAIMED → STARTED → COMPLETED`, or
SUPPRESSED / FAILED / INTERRUPTED. **CLAIMED is not air evidence**; only
STARTED (a real post-primer program buffer crossed the output) is.

* **Activation** sets `next_due_at = now`: due immediately, aired at the next
  safe program boundary (D2B).
* **Materialization** (`materialize_due_execution`, the D2A worker): expiry
  runs first in the same transaction; if an unfinished execution exists it is
  returned (coalesced); otherwise, if any ACTIVE message is due, one
  PENDING_RENDER execution is created for the WHOLE active set. Concurrent
  workers get the same occurrence.
* **Replay Now** (`request_manual_replay`) is idempotent on a client
  `request_token` UUID: the same token always resolves to the same execution,
  even after it aired. An unfinished airing of the current composition absorbs
  the request.
* **Cadence — completed air to next due.** After a COMPLETED airing, every
  still-active participant is next due `ended_at + its interval`. Legacy
  OGRemote measured request to request (`last_insert_iso`), so native
  start-to-start spacing is longer by the boundary wait plus the aggregate
  length (≈ 23–25 min for a 20-minute interval). This is intentional; a
  wall-clock policy could be added later without changing the evidence.
* A message activated while an aggregate is on air was not in it: it keeps
  its own due time, so the next aggregate (with everyone) follows.
* **Clear while others stay active:** the remaining messages are NOT re-aired
  immediately (legacy did, as a side effect of rebuild-on-change); they wait
  for their next due time unless an operator chooses Replay Now.
* **INTERRUPTED** (really started, did not end cleanly): no immediate
  re-airing; participants are next due one interval after the last air
  evidence.
* **Clear / expiry** act on future airings only. An airing already STARTED
  finishes; it is never cut.

D1 creates executions only as PENDING_RENDER. The evidence services
(`record_air_completed`, `record_air_interrupted`) exist so these rules are
reviewable now; nothing in D1 can move an execution to STARTED.

## Bounds

The composition limits are **operator policy** on `ActiveBroadcastPolicy`,
changed only through `services.set_limits` (control) — editable in Django
admin, whose form hands valid values to that service — within **absolute
ceilings** fixed in code (`ABSOLUTE_MAX_*` in `active_messages/models.py`) and
enforced three times: the service, the admin form's validators/constraints,
and the database.

| Limit | Default | Allowed | Absolute ceiling — why |
| --- | --- | --- | --- |
| `max_active_messages` | 16 | 1..24 | **24**: each component is one sealed input descriptor in a single aggregate render (D2A), plus the stinger; and at the aggregate ceiling 24 components already average only 50 s — typical PSA length — so more could not carry meaningful messages anyway |
| `max_message_seconds` | 300 | 1..600, ≤ aggregate | **600 s**: a 10-minute "message" is programming, not an announcement; half the aggregate ceiling, so at least two producers always fit |
| `max_aggregate_seconds` | 600 | 1..1200 | **1200 s**: an exceptional absolute resource and playout ceiling for one aggregate; it does not promise an airtime ratio, because the configured replay interval may be shorter |

Aggregate accounting is every component's validated decoded duration **plus
the stinger's** when an urgent component brings it in (the stinger is
ReferenceAudio, so its validated duration is known in D1). The snapshot
records `duration_seconds` per component, `stinger_seconds` and
`accounted_seconds` (outside the digest). The exact *rendered* duration exists
only once D2A has rendered the aggregate; D2A must re-check it against the
aggregate limit before the mix may air (see the D2 contract).

* Media is **never truncated** to satisfy a limit.
* **Activation** refuses (`CompositionIneligible`: `too_many_active`,
  `message_too_long`, `aggregate_too_long`, with a detail naming the limit and
  the offending message/amount) any activation whose resulting active set
  breaks a limit. Supersession does not add to the count.
* Changing a limit is output-affecting: one generation step, and still-pending
  airings of the old generation are suppressed.
* Lowering a limit below the current active set clears nothing. That set
  **fails closed**: due materialization and Replay Now raise
  `CompositionIneligible` (any expiry done in the same pass still commits), an
  operator-visible SystemEvent (`error`) explains which limit, and
  `snapshot_active_composition()["ineligible"]` reports it — until an operator
  clears messages or raises the limit.

Fixed safety constants, never operator input: snapshot at most 32 KiB; event
detail at most 4 KiB.

## Authorization

`active_message.produce` (prepare, bind) and `active_message.control`
(activate, Replay Now, clear, expiry on active messages, policy), seeded to
Station Administrator only by `authz.0008`; staff/superuser keep
`authorize()`'s compatibility bypass. Every service checks `authorize()`
itself. The admin is read-only diagnostics, except the policy's three
composition limits, which it changes only through `services.set_limits`
(control).

## Aggregate render (D2A)

D2A turns a PENDING_RENDER execution into READY: one immutable, validated
aggregate rendition of exactly its FROZEN composition, bound with proven
provenance. Nothing airs: no engine command exists, the gate stays closed,
READY executions wait for D2B.

**Renderer operation.** The Phase C `render` protocol is one source plus at
most four keyed references -- it cannot express an ordered list. D2A adds a
separate `assemble` operation to isadoraair-render (production.services.render:
protocol, client, executor, service) -- identities and read-only descriptors
only, never a path, command, filter, URL or parameter:

* inputs: at most one stinger (first, and only ahead of urgent material),
  then 1..24 messages, urgent before public address, each message once; one
  descriptor per input, in order (at most 25); every input's size, SHA-256
  and duration, ≤ 1200 s each and in total, ≤ 1 GiB of bytes; the service
  re-checks all of it, snapshots each descriptor into a sealed memfd while
  proving its size and SHA-256, and refuses anything else before a step runs;
* assembly `active_message_mix` v1 (code-defined, `provenance.ASSEMBLIES`):
  each input decoded alone, in its own confined step, to raw 48 kHz stereo
  s16 PCM; the executor appends the samples in order -- no gap, overlap, bed
  or loudness processing (legacy concat parity) -- and one step encodes FLAC
  48 kHz stereo 16-bit (bitexact; identical inputs give identical bytes);
* the service's ceilings bind the ACTUAL decoded audio, not the claimed
  durations: each decode is cut off by the step itself just past what may
  still be allowed (`-t`, from the ceilings only), and each input's real
  duration -- its PCM byte count, 4 bytes a 48 kHz frame -- must be ≤ 1200 s
  and the running total ≤ 1200 s, or the assembly is refused
  (`decoded_too_long`) before that input is appended. Declared and decoded
  durations may differ slightly; only the ceilings are absolute (and
  binding still re-checks the measured rendition against the current
  policy limit);
* the result goes through the sole rendition intake (validated by
  isadoraair-validation) as an immutable `rendition` whose `derived_from` is
  the first message's media.

**Provenance.** The rendition's `recipe_params_digest` is the canonical SHA-256
of `{assembly, version, engine, params (fixed output format), inputs: [ordered
{role, key, reference, media, sha256} | {role, message, kind, media,
sha256}]}`. It therefore proves the complete ordered input identity -- the
exact stinger registration UUID and bytes included -- and nothing is resolved
by key, filename, path or mtime.

**The frozen stinger.** The client opens the stinger by its pinned
ReferenceAudio *registration UUID* and refuses unless that registration
still names the pinned media and SHA-256 and the stored bytes hash to it; it
never asks the key for its current registration. A newer registration under
the same key does not change the render. If the frozen pin is unusable
(missing, invalid, corrupt bytes), that occurrence FAILS
`stinger_pin_unavailable` -- its snapshot is never edited, and a Replay Now
request token keeps resolving to it -- and in the same worker pass
`services.reconcile_stinger_failure` records exactly ONE outcome for it (a
`reconciled` event on it, so retries and crash recovery never make a second
successor; each worker pass also reconciles any such failure a crash left
behind):

* the composition moved on since it was built (clear, expiry, replacement,
  policy change -- a new generation) → no successor: nothing cleared or
  expired is resurrected, and the D1 rules for the new composition govern;
* an unfinished current occurrence exists → it absorbs this one;
* nothing is active → no successor;
* otherwise ONE successor, reason `reconcile`, due now and independent of
  cadence -- so a Replay Now made while nothing was due still airs -- built
  from CURRENT authority: the current active set and the key's current
  stinger (an unusable one omitted with a warning, a newer valid
  registration pinned). Its `reconciles` event names the predecessor.

A current set that breaks a policy limit fails closed (CompositionIneligible,
operator alert) and records nothing until an operator change. Urgent material
is never stranded behind its stinger.

**Binding** (`services.bind_execution_mix`, policy → media
(`lock_for_binding`) → execution, one short transaction): the execution must
be PENDING_RENDER of the CURRENT generation (a stale one is suppressed
`stale_generation`), its snapshot must still hash to its composition digest,
the rendition must be valid and present with a stored provenance digest equal
to the assembly of the frozen inputs, derived from the first message's media,
by the render executor -- and its MEASURED duration must be within the
CURRENT aggregate limit (else `aggregate_too_long`: never truncated, never
READY, messages untouched). Then `mix_media`, `ready_at`, READY and the
`mix_bound` event commit together.

**Reuse.** A rendition is reused only when its stored provenance digest
proves it is exactly the required aggregate (a composition-digest string
match is not enough), it is valid and present, AND its stored bytes still
re-hash to its recorded SHA-256 (`media_io.verify_integrity`, done with no
database lock held); a missing, size-inconsistent or corrupt candidate is
never reused or bound -- a fresh render follows. Lookup is bounded through
the indexed `derived_from` foreign key, then the exact digest, kind,
recipe key/version, executor marker, validity and presence.

**Worker** (`active_messages.worker`, `manage.py active_message_worker`): a
PostgreSQL session advisory lock (tried, never waited for) keeps a second
worker from rendering the same occurrence; correctness never depends on it.
Each pass: obtain the one current occurrence from
`materialize_due_execution`; honour its retry backoff; reuse or render
**with no database lock held**; bind. Authority that moved on during the
render (clear, replacement, expiry, a limit change, another worker) binds
nothing; the finished rendition stays an immutable, unbound orphan for
ordinary retention. A crash between render and bind is recovered by reuse.
D2A ships no systemd unit for it -- deploying the worker is a D2B/release
decision (a new managed unit is a signed protected-policy change).

**Outcome codes** (stable, bounded; detail ≤ 200 chars, no paths or tool
output):

| Code | Meaning | Execution |
| --- | --- | --- |
| `render_unavailable` (event `render_retry`) | busy, unavailable, sandbox, timeout, unvalidated rendition, rendition lost before binding | stays PENDING_RENDER; backoff 15 s doubling to 10 min; attention after 3 |
| `stale_generation` | authority moved on | SUPPRESSED (D1 rule) |
| `input_unavailable` | a frozen message's media missing/invalid/corrupt | FAILED |
| `stinger_pin_unavailable` | the frozen stinger pin unusable | FAILED, then at most one RECONCILE successor |
| `provenance_mismatch` | the rendition does not prove the frozen aggregate | FAILED |
| `rendition_invalid` | the aggregate failed validation | FAILED |
| `aggregate_too_long` | measured aggregate over the current limit, or the service's `decoded_too_long` | FAILED |
| `render_failed` | any other permanent render failure | FAILED |

After a permanent failure other than the stinger's, the worker does not
re-materialize the same composition at the same generation (`blocked`):
identical inputs cannot succeed. An operator change makes a new generation.
SystemEvent is operator attention only; the history is the execution and its
events.

## Ordinary playout (D2B)

D2B changes **where OGRemote's processing happens, not how IsadoraAir plays the
result**. There is no active-message player, lane, mixer, bin or command: a
READY aggregate becomes the same kind of library content Weather/AMBER and
legacy OGRemote already deliver, and the existing automation plays it
(`active_messages/playout.py`; hooks in `library/services/engine.py`).

```
READY execution
 → publish: exact mix_media bytes (verified) → LIBRARY_ROOT/UrgentPA/am-<execution hex>.flac
            (per-execution, immutable, placed with os.link: never overwritten)
            → sync_track_file (normal Track, analysis, waveform), Ready-to-Air OFF
 → dispatch: {"command": "insert_urgent", "category": "UrgentPA", "track": <id>,
              "active_execution": <uuid>, "attempt": <uuid>}
 → engine: _insert_active_message → _locked_playlist_log → admit → _splice_log_item_db_at
           → record_insertion (same transaction)                         [ACCEPTED]
           → _apply_log_item_insert (+ forced queue, behind Weather/AMBER)
 → _next_queue_item → _start_next_track → _create_deck
           (deck-open authority: authorize_open)                          [CLAIMED]
 → first real post-primer buffer: LogItem.played_at + PlayEvent           [STARTED]
 → clean EOS: terminal ledger, termination "eos"/"eos_deferred"           [COMPLETED → D1 cadence]
   anything else after a real start (skip, error, crash, shutdown)        [INTERRUPTED, no replay at once]
```

**Publication.** The source is the execution's exact `mix_media`: present,
valid, its stored bytes re-hashed to the recorded SHA-256 and hashed again
while copied (opened by identity -- never a caller's path). Under a
destination flock: stage in a hidden scratch directory beside the
destination, place atomically with `os.link`, `sync_track_file` into a
scratch waveform directory, move the waveform into the real directory,
record `ActiveBroadcastPublication` -- one transaction; any failure removes
the placed file and waveform and rolls the Track back. A file already at the
reserved path is reused only if it is exactly the aggregate (a crash between
placement and commit); other bytes there are never overwritten
(`destination_conflict`). The Track carries title/artist "Urgent PA" (legacy
parity) and is NOT Ready-to-Air, so neither rotation nor a category-only
`insert_urgent` (Weather/AMBER, legacy OGRemote's mix) ever selects it.

**Correlation** (migration `active_messages.0002`, CreateModel only):
`ActiveBroadcastPublication` (execution → media → Track, with a Track id
snapshot) and `ActiveBroadcastInsertion` (one per engine-inserted LogItem:
execution, **attempt token** (unique), LogItem with id snapshot (unique),
engine process). Both reference library rows SET_NULL with snapshots (a Track
can be deleted, a LogItem is pruned with its log). The reserved file name
`am-<32 hex>.flac` is only the database-free candidate test that keeps
ordinary Tracks query-free on the engine's hot paths (as spoken's
`spoken-<hex>.flac`); authority is always the exact insertion.

**Attempts.** Every dispatch is a new attempt token on the execution. Only
the insertion of the CURRENT attempt may open a deck, so a duplicate command
(that attempt is already inserted), a delayed or superseded one, and a
LogItem from an earlier attempt are all harmless. Re-sends:

| Situation | Who | Action |
| --- | --- | --- |
| command not accepted within 60 s (lost) | worker | new attempt, re-sent |
| accepted/claimed by a previous engine process, not started | engine at start-up | new attempt, re-sent |
| deck refused or failed to open (not stale) | engine | new attempt, re-sent |
| accepted/claimed but not started after 600 s (queue replaced) | worker | new attempt, re-sent |
| 50 attempts | -- | FAILED `dispatch_exhausted` |

**Priority.** The native item takes the same "next boundary" place as any
urgent insertion, but behind every not-yet-played Weather/AMBER (`WxAlert`)
item at the front of the queue or forced list; a Weather/AMBER alert that
arrives later still goes to the very front (unchanged). With a dedication
follow-up pending, the forced order is Weather/AMBER, native, follow-up. A
playing deck is never touched: once an aggregate has STARTED it finishes,
and a newly arriving alert gets the next boundary.

**Staleness.** Before a deck opens, the deck-open authority re-reads
committed state (bounded 250 ms lock wait): current attempt, native-airing
gate, ACCEPTED with the CURRENT generation and the same aggregate → CLAIMED
(from here it airs even if a clear commits a moment later); this engine
process's own CLAIMED/STARTED (a seek or pause continuation) → allowed;
anything else → refused (a stale ACCEPTED one is SUPPRESSED
`stale_generation`). The engine's existing playability filter applies the
same rule read-only to queued items, so a stale aggregate is skipped exactly
like any unplayable item: the next ordinary item plays, nothing is consumed
or duplicated, Weather/AMBER items and dedication follow-ups are untouched,
and the worker suppresses the execution. Clear, replacement, expiry and any
generation change all behave this way. Song-request substitution and
dedication splicing never apply (they act on music-kind items only).

**Evidence.** No new detector. Queueing, LogItem creation, deck construction
and PLAYING are not STARTED. `sync_evidence` reads the deck's own committed
ledger for the current attempt: a PlayEvent (written only at the first real
post-primer buffer) → STARTED with its exact timestamp; the terminal ledger →
COMPLETED only for a clean natural end (`complete`, `ended_at`, last segment
`eos`/`eos_deferred`) with the confirmed duration -- D1 then sets each
participant's `next_due_at` -- otherwise INTERRUPTED at the last real
evidence. The engine runs it from its own air-start and terminal-ledger
commits; the worker repeats it (crash recovery). After an engine restart the
new process closes a STARTED airing of the previous one as INTERRUPTED (even
when a clean shutdown left the ledger open) and never resumes it: no
duplicate emergency/PA audio after a crash.

**Accounting.** The ordinary PlayEvent is written with UrgentPA's existing
category kind (Spot); royalty/SoundExchange reports select only Music, so an
aggregate is never a music performance (legacy OGRemote's UrgentPA mix was
classified the same way). Nothing was special-cased.

**Gate.** Publication, dispatch, admission and deck-open all require
`services.NATIVE_AIRING_AVAILABLE` (False in code) AND the policy switch.
Tests open it only with an explicit patch. Production cutover (Phase G) needs
that code change; D2B does not make it. Legacy OGRemote stays authoritative
and untouched.

## Release notes for D1

* `active_messages.0001_initial` — CreateModel ×4 (additive, automatic).
* `authz.0008_active_message_capabilities` — RunPython: Phase D1's one manual
  operation; ship a migration-authorization companion or approve locally
  (docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
* No new service, unit, protected-runtime change, setting or file path.

## Release notes for D2A

* No migration: D1's execution model already carries `mix_media`, `ready_at`,
  FAILED and its failure code.
* isadoraair-render gains the `assemble` operation (protocol v1 unchanged for
  `render`); its descriptor bound rises from 5 to 25.
* `manage.py active_message_worker` -- no systemd unit in D2A.

## Release notes for D2B

* `active_messages.0002_playout_correlation` -- CreateModel ×2 (additive,
  automatic).
* `insert_urgent` accepts optional `track` / `active_execution` / `attempt`;
  the category-only form is unchanged.
* Engine hooks: native branch of `insert_urgent`, the deck-open authority and
  playability filter (database work for `am-<hex>.flac` candidates only),
  evidence projection after the existing air-start and terminal-ledger
  commits, start-up reconciliation. No GStreamer change.
* Native airing stays impossible: `NATIVE_AIRING_AVAILABLE` is False.
