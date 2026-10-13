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
| insertion at the next track boundary, ordinary programming between plays | single-slot command file / `insert_urgent` → its own execution ledger (D2B) |
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
| **D1** (this) | domain authority: policy/generation, messages, executions, events, media binding, composition, due/replay/expiry, authz, admin diagnostics | **no** — no renderer, no engine path |
| D2A | constrained multi-input `active_message_mix` renderer recipe, mix binding with provenance check, the worker | no (fake engine receiver) |
| D2B | `active_message_play`, engine re-read, interstitial playback, evidence, restart reconciliation | yes — its own acceptance gate |
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
| delivery to the engine | the existing engine-command FIFO (a wake-up only; D2B) |
| whether the engine may still play it | a fresh database re-read by the engine (D2B) |
| what happened | `ActiveBroadcastExecution` state + `ActiveBroadcastMessageEvent` (append-only) |
| operator attention | `monitoring.SystemEvent` (never the history) |
| music royalty evidence | `PlayEvent` — Phase D never writes one |

No Track, LogItem, SpokenItem/SpokenAsset or PlayEvent is ever created by this
domain, and no path is ever authority.

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
   component is urgent.

The snapshot holds message, media and reference identities with SHA-256s and
producer snapshots — never a path. The **digest** covers only what determines
the audio (ordered message/kind/media/sha256 + stinger pin), so an unchanged
set keeps its digest across replays and D2A can reuse its aggregate. If the
configured stinger is unavailable, the airing proceeds without it
(`stinger_unavailable` in the snapshot) and a SystemEvent warning is raised:
an urgent message is never held back for its stinger.

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
| `max_aggregate_seconds` | 600 | 1..1200 | **1200 s**: the legacy (and default) 20-minute replay cadence — an aggregate any longer would put more PA audio than ordinary programming between plays |

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

## Intended D2 contract (not implemented in D1)

* **D2A:** `bind_execution_mix` — under policy → media → execution, verify the
  rendition's provenance names exactly the snapshot's ordered inputs and
  stinger pin, require the current generation, **refuse a mix whose measured
  duration exceeds the policy's aggregate limit** (fail closed — never
  truncate), then PENDING_RENDER → READY. Render failure → FAILED with a code.
* **D2B:** command `active_message_play` {execution_id, attempt_token,
  policy_generation, composition_digest, mix_media_id} over
  `enqueue_engine_command` — never a path or a Track. The engine re-reads the
  execution (gate open, unfinished, current generation, digest and media
  match, media present and valid) with a bounded wait equal to the 250 ms
  playlist-lock contract; if authority cannot be established promptly the
  airing stays pending. Re-read again at the boundary before opening media.
  Interstitial transition (not an overlay, no FX bus): program transitions out,
  the aggregate owns program output, then the exact next ordinary queue item
  transitions in; the queue cursor is not consumed. Weather/AMBER `insert_urgent`
  wins a contested not-yet-started boundary; an already-STARTED aggregate is
  never torn down for it. Remote DJ, studio mic/PTT or an independent Manual
  hold: never seized — the airing waits and raises operator attention.

## Release notes for D1

* `active_messages.0001_initial` — CreateModel ×4 (additive, automatic).
* `authz.0008_active_message_capabilities` — RunPython: Phase D1's one manual
  operation; ship a migration-authorization companion or approve locally
  (docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
* No new service, unit, protected-runtime change, setting or file path.
