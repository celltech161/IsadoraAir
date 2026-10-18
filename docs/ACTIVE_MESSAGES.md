# Active messages — urgent / public-address domain (iPortal Phase D)

Producers' **urgent** and **public-address** messages: which are active, the
one aggregate the station airs for the whole active set, when it is next due,
and what actually happened each time it was meant to air.

Phase D keeps the legacy OGRemote *product* behavior and replaces its
*mechanics*:

| Kept from OGRemote | Replaced |
| --- | --- |
| several producers active at once; newest message per producer and content type | newest-file-by-mtime → one ACTIVE row per (producer, exact content type), database-enforced (D3A) |
| one combined mix: stinger, urgent, then public address, oldest first | mutable `urgent_pa_mix.mp3` → an immutable aggregate per exact composition |
| insertion at the next track boundary, ordinary programming between plays | remote processing + polling + a mutable mix Track → a local, per-execution UrgentPA Track inserted by the same `insert_urgent` splice, with exact execution correlation (D2B) |
| periodic replay (default 20 minutes) until recalled | JSON state file + remote `processed` flag → PostgreSQL authority, backed up with the database |
| station-defined producer content types, several per user | the legacy category table → native `produced_content` types and assignments (D3A) |
| a producer recalls their own message | file deletion → an exact-UUID clear (when the type is recallable) |

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
| **D3A** | native produced-content authority: `ProducedContentType` (regular / public address / urgent / test) and producer assignments; active messages keyed by the exact content type; producer self-publish and recall | **no** — authority and configuration only |
| **D3B** | the producer workspace: "My Messages" (`/iportal/messages/`) and the shared iPortal recorder on one exact message (adapter `active-message`); Save audio ≠ Publish; producer recall/discard | **no** — gate still closed |
| D3C | regular produced content through a managed Phase-C spoken backend, TEST / NO AIR, "My Content" (docs/PRODUCED_CONTENT.md) -- no active-message change | gate still closed |
| **D3D** | the operator board (`/iportal/messages/board/`): review and publish across producers, clear, decline, expiry, Replay Now (unavailable while the gate is closed), airing and event visibility | **no** — gate still closed; Replay Now refuses before the service |
| D3E-A | worker lifecycle and recovery hardening: expiry while a composition is blocked, one loop cadence, signals and connections, the worker heartbeat / readiness contract | **no** — gate still closed |
| D3E-B+ | managed worker unit + Monitoring (B), retention (C), long-session board polling (D) | gate still closed |

## Authority

| Authority | Owner |
| --- | --- |
| immutable message / aggregate bytes | `ProductionMedia` (PROTECT, bound under `retention.lock_for_binding`) |
| which content types exist, their delivery mode and policy | `produced_content.ProducedContentType` (D3A) |
| who may produce which type | `produced_content.ProducedContentTypeAssignment` (scope) + `authz` capabilities |
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
  link and a username snapshot. `content_type` (D3A, PROTECT, NOT NULL) is
  the exact produced-content type — the producer slot; `kind` is the FROZEN
  broad composition class (urgent / public address) derived from it when the
  message was prepared. `message_media` is the exact media that airs; its
  edit lineage is ProductionMedia's own `derived_from`.
* **ActiveBroadcastExecution** — one logical airing of the aggregate.
* **ActiveBroadcastMessageEvent** — the authoritative history (append-only).

Every ordinary ORM write path refuses (`active_messages/guard.py`): `save()`,
`delete()`, `create()`, `bulk_*`, `QuerySet.update()`/`delete()` — except
Django's own SET_NULL of a deleted user. `active_messages.services` writes
through a private compare-and-write store with read-back.

### Database invariants (`active_messages.0001`, CreateModel-only; D3A's in 0004)

* at most one ACTIVE message per `(producer_key, content_type)` (partial
  unique; D3A replaced D1's `(producer_key, kind)`), and every message has
  its content type;
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
| — | `prepare_message(content_type=…)` | PREPARED | control; or produce + assigned to the exact type |
| PREPARED | `bind_message_media` (replace allowed) | PREPARED | its (still assigned) producer, or control |
| PREPARED | `activate_message` | ACTIVE | control; or its own assigned producer when the type allows self-publish |
| ACTIVE | activation of the same producer's message of the **same exact type** | CLEARED (`superseded_by`) | whoever activated |
| ACTIVE | `clear_message` | CLEARED | control; or its own producer when the type is recallable |
| PREPARED | `clear_message` (discard) | CLEARED | its producer, or control |
| ACTIVE | expiry sweep (`expire_due_messages`) | EXPIRED | system |

CLEARED and EXPIRED are final. Activate and clear are idempotent (a repeated
POST returns the row unchanged). Clear acts on the exact UUID: clearing an old,
already-superseded row can never touch its replacement. (D3D) `activate_message`
takes an optional `expected_revision` and `set_message_expiry` an optional
`expected_expires_at`, compared under the message row lock: a stale page
changes nothing. An ACTIVE message's
media never changes — editing means preparing a new message and activating it.

Media binding: valid, present, not a bed, measured duration within the
policy's per-message limit (never truncated to fit), and the binder's own
media unless they hold control. Binding reads the limit for early feedback;
activation re-checks every limit under the policy lock. The replaced media is recorded by
identity in the event detail, not by foreign key, so it stays purgeable.

There is **no default expiry**. `expires_at` is optional; `set_message_expiry`
changes it (PREPARED: its assigned producer or control; ACTIVE: control).

## Produced-content types and producer scope (D3A)

Legacy OGRemote has station-defined producer content types, many per user
(`usd239min`, `cal_events`, `3min_ctr`, `lcl_psa`, `news_item`, `usd239sc`,
`urgent_pa`, `public_address`, `test_audio`, …). Their native authority is the
`produced_content` app (never the legacy `ogremote` app):

* **`ProducedContentType`** — `key` (durable slug; never changes), `name`,
  `description`, `enabled`, `sort_order`, and one **delivery mode**:
  * `regular` — ordinary library content: `target_category` (PROTECT),
    `output_mode` (`single` / `accumulating`), an optional LOGICAL
    `recipe_key` and `artist_tag`. Processed and delivered (D3C) by the
    Phase-C spoken pipeline through an explicit managed backend
    (docs/PRODUCED_CONTENT.md); never an active message.
  * `public_address` / `urgent` — an active message: `producer_self_publish`
    (default yes), `recallable` (default yes), optional
    `replay_interval_seconds` override (60..86400; NULL = the policy default,
    frozen onto the message at activation).
  * `test` — explicitly non-airing: none of the delivery settings, and never
    an active message (D3C: a producer's saved TEST / NO AIR take is
    temporary ProductionMedia only).

  No field is executable (no paths, scripts, dotted imports or commands; the
  recipe key's shape forbids dots, slashes and spaces). Incompatible field
  combinations are refused by `clean()` (admin forms) **and** by database
  CHECK constraints. The delivery mode of a type that active messages
  reference cannot change (their kind was derived from it). Deletion:
  Django Admin does not offer deleting a type; a type referenced by an
  `ActiveBroadcastMessage` is protected by the database (PROTECT); the
  normal station lifecycle is to disable a type rather than delete it.
* **`ProducedContentTypeAssignment`** — `(user, content_type)`, unique. Scope,
  not a role: the broad capability still comes from `authz`.

Both are managed in Django Admin (types with an assignment inline; a separate
assignment list).

**Active messages are keyed by the exact type.** `prepare_message` takes the
exact `content_type` (instance, id or durable key); the service re-reads it,
requires it to be enabled and urgent or public address, authorizes the actor
for that exact type, and stores its delivery mode as the frozen `kind`.
Composition order (urgent, then public address), rendering and playout read
only `kind` and the frozen execution snapshot — never the mutable type
configuration. Regular and test types are refused
(`not_an_active_message_type`); unknown and disabled ones too
(`unknown_content_type`, `content_type_disabled`).

One producer may have several public-address types, several urgent types, or
both, ACTIVE together; activating a replacement supersedes only that
producer's ACTIVE message of the **same exact type** — never another one
merely because both are public address. Several producers may each have the
same type active.

| Action | CONTROL (incl. staff/superuser) | Ordinary producer (`active_message.produce`) |
| --- | --- | --- |
| prepare / bind / set a draft's expiry | any enabled urgent/PA type | assigned to the exact enabled type; own draft only |
| activate | any valid PREPARED message | own message, assigned, type enabled, `producer_self_publish` on — otherwise it stays PREPARED (awaiting publication) for control |
| clear an ACTIVE message | any | own message when the type is `recallable` — no assignment or self-publish needed, so losing an assignment never strands obsolete material; never someone else's |
| discard a PREPARED message | any | own |
| Replay Now, stinger, limits, the gate | yes | no |

A disabled type refuses new production and activation for everyone,
including control; recall and clear still work. Every check is made server
side on every call (an assignment removed between steps takes effect at the
next step).

**Compatibility types.** `active_messages.0003` creates two native types if
absent — `urgent_pa` (urgent) and `public_address` (public address) — with
D1's authority unchanged (no producer self-publish, not recallable, policy
replay default), set explicitly rather than taken from the model defaults
(which give a NEW public-address/urgent type self-publish and recall), and
backfills every existing message from its frozen kind.
Nothing is imported from legacy OGRemote (category table, users,
assignments, submissions, files): that belongs to the migration/cutover
phases.

## Producer workspace (D3B)

**My Messages** — `/iportal/messages/` (the iPortal hub's entry for this tool;
the hub lists it for control, and for a producer with at least one enabled
assigned public-address/urgent type). For each enabled urgent/public-address
type the user may produce (assigned types; every enabled one for control —
never regular or test types) it shows the class, replay interval, whether
the producer may self-publish and recall, the producer's own ACTIVE message
of that type and **every** PREPARED draft (several drafts are shown, never
guessed between). The producer's own unfinished messages of types no longer
offered (assignment removed, type disabled) stay listed so they can still be
withdrawn. Non-staff users reach it through the station's GroupAccess
allowlist (grant `/iportal/` to producer groups); staff bypass it.

| Action | How | Service |
| --- | --- | --- |
| new draft | POST, exact content type key, optional title | `prepare_message` |
| record / import / edit / **Save audio** | the shared recorder on `?message=<uuid>` | `bind_message_media(expected_revision=…)` |
| **Publish** / Publish replacement | POST on the exact UUID; offered only when the type allows self-publish (control: always) — otherwise the draft shows "Awaiting operator approval" | `activate_message` |
| Recall | confirmation page naming type and title, then POST on the exact UUID (offered when recallable) | `clear_message(expected_state=active)` |
| Discard draft | confirmation page, then POST on the exact UUID | `clear_message(expected_state=prepared)` |

Saving audio **never** publishes: a PREPARED message with audio is still a
draft (and, when the type has no producer self-publish, it awaits operator
approval — PREPARED is the review state; there is no other approval
lifecycle). Every POST is CSRF-protected and authorized again by the
services; another producer's message is a 404. `expected_state` makes a
stale "discard" unable to recall a message that was activated meanwhile.
A control user's My Messages page links to the operator board.

**The recorder subject is one exact message UUID** (`active_messages/iportal.py`):
never a producer/kind pair, "the current message", a type name, a path or a
generic key. Per message (the recorder's subject-aware check, before any
intake, preview, source or commit work):

* PREPARED — control on an enabled type, or its own producer while holding
  `active_message.produce` and still assigned to the exact enabled type, may
  record/import/edit/save; otherwise it opens read-only with the reason;
* ACTIVE — read-only: its owner or control may preview/export its exact bound
  audio; a change means preparing and publishing a replacement;
* CLEARED/EXPIRED — read-only;
* anyone else — 404; `remove` is never offered (discard is a lifecycle
  action on the Messages page).

Media access: one's own takes, and the audio bound to this exact message —
assignment to the same type never grants another producer's takes. Intake
uses the shared `MediaPolicy`: engine-decodable, at most the current
`max_message_seconds`, within the platform byte ceiling; `bind_message_media`
remains the final validation.

**Optimistic revision.** `services.message_revision(message)` is an opaque
token derived from durable facts — the state, the bound media and the id of
the latest `media_bound` event (so A→B→A still changes it); nothing extra is
stored. The recorder's commit passes it to `bind_message_media`, which
compares it **under the message row lock**: a second editor holding an older
revision gets `RevisionConflict` (HTTP 409) and the first binding stands; a
save racing an activation can never change an ACTIVE message's media.

While native airing is unavailable (every installation until Phase G), the
page and the recorder say so: "Native Active Message airing is not enabled on
this installation." The workflow itself still works (prepare, save, publish,
recall) and dispatches nothing to the engine.

## Operator board (D3D)

**Operator board** — `/iportal/messages/board/`, for `active_message.control`
only (staff/superuser through `authorize()`'s bypass); everyone else gets 403
on every board URL, and GroupAccess still gates the `/iportal/` prefix. The
hub keeps ONE Active Messages entry and lists "My messages · Operator board"
under it for control (`RecordingAdapter.hub_links`); a control user's My
Messages page links to it too. Producers keep My Messages.

**Layout** (the iPortal density philosophy, docs/IPORTAL.md "Page density"):

* a one-line status strip: active urgent and public-address counts, drafts
  with audio to review, the current or last airing (its real state; **ON AIR**
  only for a STARTED airing), the native-airing gate, an over-limit alert when
  the active set breaks a policy limit, and **Replay now**;
* filters (Current — review first, then the active set; Active — in
  composition order; Review — every PREPARED draft, those with audio first;
  History — cleared/expired, newest first) and a search (title, producer,
  type name or key, exact UUID); 25 rows a page, so history is never loaded
  unbounded;
* one compact row per message: class (URG/PA), title, exact type, producer,
  time, audio length or problem, expiry/next due, state. On a phone that is
  two lines (a third only for an expiry); from 900 px it is one line of
  columns. A row expands in place (native `<details>`): browser-local audio
  preview, facts (class, type and key, producer, state — "not airing (native
  airing closed)" for an ACTIVE message while the gate is closed —,
  publishing rule, expiry, what a publication would replace, UUID), the
  actions, an expiry editor and the latest five events. "History" opens the
  message's page (up to 200 events and the airings it was part of, 25 a
  page); "All airings" lists every airing, 25 a page; each airing has a page with its reason, state, failure code, timestamps
  (accepted and claimed are labelled "not air"; only STARTED is air
  evidence), components in airing order, digest and events;
* a bounded refresh (every 20 s while visible, at most 90 times; at once when
  a page is restored from the browser cache) on the board and on the message,
  airing and airings pages. It updates the counts, the airing indicator (text
  and class) and **every ON AIR indication** -- rows, row facts, airing
  labels -- from the server's one interpretation, which the page render uses
  too: only the STARTED airing (ACCEPTED/CLAIMED are labelled as such, never
  as air) and exactly the messages frozen in its composition, never the
  current active set. An airing that leaves STARTED loses ON AIR on the next
  refresh; a later STARTED airing shows its own components. Only the newest
  response is applied (a late, older one is ignored); a failed refresh or
  the 90-refresh limit restores nothing and shows "on-air indications may be
  out of date — Reload". "Reload" is also offered when anything changed. It
  never re-renders rows, so expanded rows, focus, filters and typed input
  stay.

**Bounded reads.** Board reads take no lock, and the 20-second refresh does
constant work however long the history grows: unfinished messages only (the
`(state, …)` indexes), the one unfinished airing by its `open_slot` unique
index (also the source of ON AIR), the newest airing by
`active_execution_recent` `(created_at, id)`, the newest event by primary
key. Airing lists are newest first, deterministic on equal timestamps
(`created_at, id`), keyset-paginated (a deep page costs what the first one
does). "Airings with this message" uses containment on the frozen snapshot
(`composition_snapshot @> {"components": [{"message": …}]}`) through the
`active_execution_members` jsonb_path_ops GIN index, run first in a
MATERIALIZED CTE so the planner cannot trade it for a newest-first scan that
tests every airing: its work is bounded by that message's own airings.

| Action | How | Service |
| --- | --- | --- |
| Publish / Publish replacement | POST on the exact UUID with the draft's revision as shown | `activate_message(expected_revision=…)` |
| Decline (a PREPARED draft) | confirmation page naming message and producer, optional reason, POST | `clear_message(expected_state=prepared)` |
| Clear (an ACTIVE message) | likewise | `clear_message(expected_state=active)` |
| Set / change / remove expiry | POST with the station-local time and the expiry as shown | `set_message_expiry(expected_expires_at=…)` |
| Replay now | POST with the request token issued with the page | `request_manual_replay_outcome(request_token=…)` |
| Record / edit a draft | the shared recorder on `?message=<uuid>&from=board` (its Back returns to the board, for control) | the D3B adapter, unchanged |

Every POST is CSRF-protected, carries the operator's filter/search/page
(re-validated against a whitelist; the redirect path is fixed) and is decided
by the services under their locks; displayed state is never authority. Board
reads take no lock (in particular not `snapshot_active_composition`'s policy
lock, which the engine's bounded admission also takes); the over-limit alert
is advisory.

**Stale pages.** A draft re-saved after the operator's review fails
publication (`RevisionConflict`, under the message row lock): unreviewed
audio is never published. A clear or decline acts on the exact UUID with its
expected state: a stale clear of a superseded message reports "already
cleared" and never touches the replacement; a stale decline of a message
published meanwhile is refused. A stale expiry form (someone changed the
expiry since) is refused (`expiry_changed`). A repeated publication or clear
is reported, not repeated. The page's script sends each form once; the
services are idempotent regardless.

**Expiry** is entered in station time (the page names the zone); a local
time the station clock skips or repeats at a DST change is refused, never
guessed. The board distinguishes an approaching expiry (within an hour), an
expiry that has passed while the message is still ACTIVE ("not yet expired by
the sweep" — EXPIRED is set only by `expire_due_messages`), and EXPIRED.

**Replay Now while the gate is closed** (every installation until Phase G):
the button is disabled and names why, and the server refuses the POST BEFORE
calling the Replay Now service — no dormant execution is ever created as if
it were a live replay. With the gate open (tests patch it; Phase G's cutover
opens it), the token issued with the page makes a double click or Back +
resubmit resolve to the same airing ("already requested"); an unfinished
airing absorbs a new request ("absorbed"). The outcome --
`created` / `repeated` / `coalesced` -- comes from
`request_manual_replay_outcome`, decided inside the service's own
transaction under the policy lock (never a preflight read), so of two
simultaneous posts of one token exactly one reports "created"; refusals (nothing active, limits,
an earlier composition still with the engine, another operator's token) are
reported and create nothing; a new airing is reported as not on air until
the engine starts it. The board never opens the gate and never sends an
engine command.

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

The snapshot holds message, media and reference identities with SHA-256s,
producer snapshots and (D3A) each component's exact content type
(`content_type` id and `content_type_key`, so two public-address types stay
distinguishable in history) — never a path. The **digest** covers only what
determines the audio (ordered message/kind/media/sha256 + stinger pin), so an
unchanged set keeps its digest across replays and D2A can reuse its
aggregate. D3A deliberately left the digest unchanged: the content type adds
no audio-relevant identity (it never changes for a message, whose UUID is
already in the digest), and snapshots from before D3A simply lack the two
fields. If the
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

`active_message.produce` (prepare, bind, and -- per type -- self-publish and
recall) and `active_message.control` (activate, Replay Now, clear, expiry on
active messages, policy), seeded to Station Administrator only by
`authz.0008`; staff/superuser keep `authorize()`'s compatibility bypass.
Since D3A an ordinary producer's capability is narrowed to the exact
produced-content types they are assigned to, and each type decides whether
they may self-publish and recall (see "Produced-content types and producer
scope"). Every service checks `authorize()` and the assignment itself. The admin is read-only diagnostics, except the policy's three
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

**Publication failures are classified.** Once D2A has produced a valid READY
aggregate, a local-library delivery problem never throws it away:

| Code | Class | Effect |
| --- | --- | --- |
| `analysis_failed` (analysis/waveform, `sync_track_file` refusal) | retryable | SAME execution stays READY, same `mix_media` |
| `library_unavailable` (filesystem I/O) | retryable | 〃 |
| `category_missing` (UrgentPA not configured) | retryable | 〃 |
| `publication_failed` (any other unexpected error) | retryable | 〃 |
| `aggregate_unavailable` (stored bytes missing/corrupt/invalid) | permanent | FAILED with that code, attention |
| `destination_conflict` (other bytes at the reserved path) | permanent | FAILED with that code, attention |
| `publication_unavailable` (published Track deleted / file gone, found at re-send) | permanent | FAILED with that code, attention |

A retryable failure records a `publication_retry` event (bounded detail) and
backs off exponentially exactly like D2A's render retry (15 s doubling, capped
at 600 s), with deduplicated operator attention (`publication_unavailable`)
from the third consecutive failure. No other execution is created and nothing
is re-rendered: the worker's pass ends there (`run_once` returns `playout`),
and once the condition is corrected the same execution publishes, inserts and
airs. A permanent failure ends the execution; the next occurrence then follows
D1/D2A as usual (reusing a proven rendition when one exists).

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
LogItem from an earlier attempt are all harmless.

Recovery acts only on **positive evidence** that the current attempt can no
longer air -- never on elapsed time alone. ACCEPTED means an exact LogItem is
queued; it may legitimately wait behind long-form programming, a Manual hold,
the microphone or Remote DJ for any length of time and consumes no attempts.
CLAIMED means deck-open authority authorized it and a deck may exist; it is
never superseded by age.

| Positive evidence | Who | Action |
| --- | --- | --- |
| command not accepted within 60 s (READY, or a re-sent attempt) | worker | new attempt, re-sent |
| native item discarded as unplayable before a deck (`_next_queue_item`, `_peek_playable_at_cursor`) | engine, at once | `report_not_aired`: current → re-sent; stale → SUPPRESSED; Track/file gone → FAILED `publication_unavailable` |
| native item this process inserted is no longer reachable (queue replaced by play-now/reload/early next-hour install, cursor moved past it, consumed without a deck) | engine queue sweep (in memory, each poll tick) | 〃 |
| deck-open refused or construction returned no deck | engine, at once | ACCEPTED → re-sent (it never passed deck-open authority); CLAIMED → re-sent only if this process claimed it AND the LogItem's playback claim (committed before any deck construction) never committed and there is no air evidence; otherwise held + attention `claimed_not_aired` |
| the accepted LogItem row was deleted | worker | new attempt, re-sent |
| accepted/claimed by a previous engine process, not started | engine at start-up (the restart fences the old process's audio) | new attempt, re-sent |
| CLAIMED without air evidence for 600 s | worker | attention `claimed_not_started` only -- never re-sent |
| 50 attempts | -- | FAILED `dispatch_exhausted` |

The engine reports a discard with the LogItem id; the domain correlates it
through `ActiveBroadcastInsertion.log_item_id_snapshot`, so a ghost whose
Track FK became NULL is still recognized. Ordinary unplayable items are
skipped exactly as before (no lookup); the lookup happens only for an
already-unplayable item that this process inserted, that carries a native
file name, or whose Track is gone. A report that cannot be recorded (e.g. a
lock timeout) stays held in the engine and the sweep repeats it. Deck-open
authority never claims an occurrence whose LogItem row no longer exists.

**Priority.** The native item takes the same "next boundary" place as any
urgent insertion, but behind every not-yet-played Weather/AMBER (`WxAlert`)
item at the front of the queue or forced list; a Weather/AMBER alert that
arrives later still goes to the very front (unchanged). With a dedication
follow-up pending, the forced order is Weather/AMBER, native, follow-up. A
playing deck is never touched: once an aggregate has STARTED it finishes,
and a newly arriving alert gets the next boundary.

**Serialization.** Insertion and deck-open authority take the active-message
locks in the established order, policy → execution, exactly like every
composition change (clear, replacement, expiry, policy/limit change -- all
of which lock the policy first). `admit` takes them inside the engine's
splice transaction (PlaylistLog lock, 250 ms bound) and holds them until
`record_insertion` commits; `authorize_open` holds them in one short
transaction through the gate, generation, attempt and LogItem checks, the
claim and its event, and releases them when the claim commits -- never
through deck construction or the file open. So a change that wins the policy
lock first is always seen (no admission, no claim, no deck), and a claim that
wins first stands (a later change does not cut a claimed airing). A lock wait
beyond 250 ms fails closed: nothing is inserted (the engine retries; the
worker re-sends a command never accepted) or no deck opens (the not-aired
report recovers it). The PlaylistLog lock is the engine's outer context; no
active-message service ever takes it, so there is no reverse dependency. The
engine's advisory reads (`inspect`, `current_policy`) never wait on a lock,
and its evidence-sync hook uses the same 250 ms bound (the worker repeats a
timed-out projection).

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
and the discard report (or the worker) suppresses the execution. Clear,
replacement, expiry and any generation change all behave this way.
Song-request substitution and dedication splicing never apply (they act on
music-kind items only).

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

## Worker lifecycle and readiness (D3E-A)

**Expiry while a composition is blocked.** A permanently failed composition
(`input_unavailable`, `provenance_mismatch`, `rendition_invalid`,
`aggregate_too_long`, `render_failed`) is never re-rendered at the same
generation -- but it must never hold back the expiry that changes it. Until
D3E-A the worker decided "blocked" BEFORE calling
`materialize_due_execution`, so the expiry sweep inside it never ran while
blocked (an expired message stayed ACTIVE). Now the decision is made INSIDE
`materialize_due_execution(blocking_codes=...)`, under the policy lock and
after its expiry sweep and stale suppression: the occurrence it would create
is built, and only if the newest execution FAILED with a blocking code at the
current generation for exactly that digest is none created
(`CompositionBlocked`, raised after the sweep commits). An expiry makes a new
generation, so the changed set is assessed afresh in the same pass. One
transaction instead of two (the worker no longer calls
`snapshot_active_composition`, which took the policy lock and hashed the
stinger every blocked pass). Callers without `blocking_codes` are unchanged.

**One loop cadence.** `worker.run_forever` is the management command's loop
and the only definition of when to wait: after every pass that made no
progress -- idle (a READY aggregate behind the closed gate reports
`idle`/`gated`), busy, blocked, backoff, retry, error and every `playout`
result (publication backoff/retry, in flight, accepted, claimed, started:
their recovery is time-based and evidence-driven, never "as fast as
possible"; the engine-held step takes the policy lock) -- it waits
`POLL_SECONDS` (5 s). Progress (ready, reused, failed, superseded) continues
at once for at most `MAX_PROGRESS_PASSES` (10) passes back to back; the
10th is followed by a wait. A freshly READY
aggregate is still published and dispatched in the same pass (gate
permitting). Until D3E-A the command's own list omitted `playout`: once
native playout became reachable it would have polled the database -- and the
policy lock -- without pause.

**Signals and connections.** SIGTERM/SIGINT are honoured between passes and
within a wait (0.5 s slices), never by abandoning a pass mid-way (a render in
progress finishes or is abandoned only by process death, which the existing
reuse/retry recovery covers). A database failure closes the connection (the
session advisory lock dies with it), is reported as `error` and waits; any
other exception ends the process for its supervisor to restart. A clean stop
closes the connection. `--once` runs one pass and closes the connection.

### Heartbeat and readiness contract (for D3E-B)

The established IsadoraAir pattern (aircheck's state file, Liquidsoap's,
Monitoring's self-health): the worker process writes one small fixed-shape
JSON document, atomically and best-effort, at
`settings.ACTIVE_MESSAGE_WORKER_STATE_PATH` (default
`/run/isadoraair/active_message_worker.json`); readers only read it
(`active_messages.heartbeat`). No database row or migration: the state is per
process, and a database heartbeat would write every cycle into the database
the engine's admission path locks.

**Document (schema 2).** Every key required, no other key, at most 4096
bytes: `schema` (2), `pid`, `started_at`, `checked_at` (finite epoch seconds
in 2000..2100, `started_at <= checked_at`), `recent_starts` (1..8 such
timestamps, nondecreasing, the last equal to `started_at`, none older than the
crash window before it), `previous` (`none`, `clean`, `unclean`,
`malformed`: how the previous process ended), `phase` (`starting`,
`polling`, `rendering`, `stopped`), `cycle`, `result` (a worker outcome; ""
only before the first cycle), `code` (`[A-Za-z0-9_]{0,64}`), `execution` (""
or a UUID), `repeats` (1..cycle after the first cycle), `consecutive_errors`
(equal to `repeats` when the result is `error`, else 0), `last_progress_at`
(null or within the process's life), `render_deadline` (present exactly while
`rendering`, after `checked_at`, at most 3750 s later). Booleans are never
accepted as numbers; NaN/Infinity are refused. The strict decoder
(`heartbeat.validate`) returns a fully validated value or a stable reason; a
document that breaks any rule is `malformed` (critical) -- never partially
interpreted, never an exception, never `ok` -- and so is one stamped more than
30 s in the future. A missing or unreadable file is `no_heartbeat` (unknown).

**File safety.** Reader: `O_NOFOLLOW | O_NONBLOCK`, `fstat` (regular files
only), a 4096-byte cap before parsing; a symlink, directory, FIFO or
oversized file is `malformed`, never followed or waited on. Writer: a random
temporary name created exclusively by `tempfile.mkstemp`
(`O_CREAT|O_EXCL`: a pre-planted file or symlink at any name is never opened),
verified regular, mode 0600, written and fsync'ed through its descriptor, then
`os.replace`d over the heartbeat path in the same directory (a symlink there is
replaced, never written through); removed on any failure; the writer never
publishes a document its own decoder would reject. A concurrent reader sees
the old or the new complete document.

**Restart accounting.** A start appends to `recent_starts` only after an
UNCLEAN end (the previous document is valid and not `stopped`); a recorded
clean stop, a missing history and a malformed history each start a fresh
sequence (`previous` records which; a malformed one is reported as
`history_unverified` for the window). Previous starts more than 900 s before
the new start are dropped (at most 8 kept). The assessment counts the starts
within 900 s of the OBSERVATION time, inclusive at exactly 900 s: three or
more is `crash_looping`. So operator stop/start cycles never count, and starts
at 0, 850 and 1700 are two in the window at 1700.

| Written | When |
| --- | --- |
| `phase: starting` | at start-up |
| `phase: rendering`, `render_deadline` | just before each render (its own bounded timeout + 120 s): a long render is not a stale worker |
| `phase: polling`, the outcome, `repeats`, `last_progress_at`, `consecutive_errors` | after every loop cycle |
| `phase: stopped` | on a clean stop |

`heartbeat.assess(state, now=, gate_open=)` (pure) and
`heartbeat.readiness()` (reads the file and the gate with SELECTs only; it
never creates the policy singleton, renders, expires or materializes):

| Condition | Status | Meaning |
| --- | --- | --- |
| `idle` | ok | polling, nothing due |
| `ready_waiting_for_gate` | ok | a READY aggregate behind the CLOSED gate -- expected until Phase G |
| `working`, `rendering`, `retry_backoff`, `playout_in_flight`, `busy` | ok | progress, a render within its deadline, backing off, with publication/engine, a skipped pass |
| `history_unverified` | warning | the previous document was malformed (for 900 s after the start) |
| `blocked` | warning | the current composition failed permanently (its SystemEvent names the cause) |
| `lock_held_elsewhere` | warning | 12 consecutive `busy` passes: another holder of the worker lock |
| `database_errors` | warning | a pass failed on the database; the connection was closed |
| `malfunction` | critical | 3 consecutive database failures |
| `render_overdue` | critical | a render past its deadline |
| `stale` | critical | no record for 60 s while not rendering |
| `crash_looping` | critical | 3 unclean starts within 900 s of the observation |
| `stopped` | critical | a clean stop (a managed unit should be running) |
| `malformed` | critical | the document breaks the contract (`detail.reason`) |
| `no_heartbeat` | unknown | no readable file |

`detail.native_airing` is `closed`/`open` and never degrades the status.

**`--idle-seconds`** (the wait after a pass that made no progress) accepts 1
to 30 seconds (default 5); zero, negative, non-finite or larger values are
refused, so a waiting worker always records well within the 60 s staleness
bound. A stop during the wait takes effect within 0.5 s.

**D3E-B dependencies** (not done here): a managed `isadoraair-active-message-worker`
unit (a signed protected-policy change) running as the station user (as
Monitoring does, so the 0600 file is readable by it) and writing in the
EXISTING `/run/isadoraair` (tmpfiles.d `d /run/isadoraair 0755 <station user>`,
shared by several services) -- not a unit-owned `RuntimeDirectory`, which
would conflict with that shared directory; the document then survives worker
restarts (restart history) and is cleared at boot. Also `Restart=on-failure`;
a `TimeoutStopSec` that tolerates finishing a pass (killing mid-render is safe
-- the next pass reuses or re-renders -- but should be the exception);
ordering after PostgreSQL and `isadoraair-render`; a Monitoring probe built on
`heartbeat.readiness()` with systemd's ActiveState/NRestarts (as
`monitoring/services/self_health.py` does); dashboard presentation.

### Crash and interruption boundaries (audited in D3E-A)

| Boundary | Behaviour | Evidence |
| --- | --- | --- |
| crash before due materialization commits | nothing is created; the next pass creates it | `CrashBoundaryTests` (new) |
| crash after creation, before rendering | the same PENDING_RENDER execution is rendered next pass | `CrashBoundaryTests` (new) |
| crash during rendering / after the rendition, before binding | reuse of the proven rendition, else a fresh render | `test_a_crash_between_render_and_bind_is_recovered_by_reuse` (existing) |
| crash after READY | never re-rendered; waits behind the closed gate | `CrashBoundaryTests` (new) |
| renderer unavailable / unvalidated rendition | `render_retry`, exponential backoff, attention after 3 | existing binding tests |
| frozen stinger pin unusable; crash before reconciliation | exactly one reconciliation, on the next pass | `test_reconciliation_is_idempotent_and_survives_a_crash_before_it` (existing) |
| retryable publication failure | same READY execution, backoff, nothing else materialized | `test_playout_publication` (existing) |
| advisory-lock contention | `busy`, nothing touched | `CrashBoundaryTests`, `test_two_workers_render_one_execution_once` |
| SIGTERM idle / working | stops within a wait slice / after the pass | `LoopCadenceTests`, `CommandLoopTests` (new) |
| expiry while blocked | expired under the lock, one event, one generation | `BlockedExpiryTests`, `BlockedExpiryRaceTests` (new; previously broken) |

No engine-side playback authority or air-evidence rule changed. An uncertain
air state is never converted into a completed airing or a retry.

### Phase G requirement: OGRemote and native coexistence (recorded, not implemented)

Producers will migrate to native iPortal gradually. **OGRemote must remain
available and operating for producers not yet migrated after native airing is
introduced, until the station explicitly authorizes its retirement.** Before
simultaneous broadcast delivery is allowed, Phase G must define: exact
producer and message ownership (which authority owns each producer, and how
ownership moves); legacy aggregate membership; duplicate prevention (the same
message never airing from both); insertion scheduling between the two
aggregates and Weather/AMBER; and rollback behaviour. The global
native-airing gate alone cannot prove safe mixed-authority operation. No
authority switch or OGRemote teardown belongs before Phase G.

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
* **Required before production (Phase G): retention.** Every airing leaves one
  immutable `UrgentPA/am-<execution>.flac` and its Track; no cleanup is
  designed yet. It must never remove a publication still referenced by a
  queued or historical occurrence.

## Release notes for D3A

* `produced_content.0001_initial` -- CreateModel ×2 (additive, automatic).
* `active_messages.0003_message_content_type` -- AddField (additive) +
  RunPython (compatibility types + backfill), and
  `active_messages.0004_message_content_type_identity` -- AlterField (NOT
  NULL), RemoveConstraint/AddConstraint (the producer slot becomes
  `(producer_key, content_type)`): D3A's manual operations. Ship a
  migration-authorization companion or approve locally
  (docs/UPDATE_CENTER.md, "Trusted companion migration authorization").
* No new capability: `active_message.produce` / `control` are reused.
* `prepare_message` takes `content_type` (was `kind`); no caller outside the
  tests existed.
* No engine, render, GStreamer, service-unit or file-path change. Native
  airing stays impossible: `NATIVE_AIRING_AVAILABLE` is False.
* Not in D3A: producer pages, the recorder adapter, regular recipes and
  delivery (D3C), the operator board, OGRemote data import, cutover.

## Release notes for D3B

* No migration, no new table or field: the draft revision is derived from
  existing durable facts.
* The shared recorder gains two backward-compatible adapter hooks --
  `authorize_subject(user, subject, operation)` (default: allow) and
  `show_in_hub(user)` (default: `authorize(user, "open")`) -- and optional
  `RecordingContext.terms` wording (defaults: the original "on air" wording).
  Evergreen VoiceTrack is unchanged.
* New pages under `/iportal/messages/`; grant `/iportal/` to producer groups
  in GroupAccess for non-staff producers.
* `bind_message_media(expected_revision=...)` and
  `clear_message(expected_state=...)` are optional; existing callers are
  unchanged.
* No engine, renderer, GStreamer or service change; `NATIVE_AIRING_AVAILABLE`
  stays False.

## Release notes for D3D

* **Migration `active_messages.0005_execution_history_indexes`**: two
  `AddIndex` on `ActiveBroadcastExecution` -- `active_execution_recent`
  B-tree `(created_at, id)` and `active_execution_members` GIN
  `(composition_snapshot jsonb_path_ops)`. Additive in substance (no field,
  constraint or data change; nothing rewritten) but outside the updater's
  automatic allowlist, so both operations are **manual**: the release ships a
  migration-authorization companion (or approves locally). Atomic, plain
  `CREATE INDEX` (not CONCURRENTLY: a non-atomic migration is manual for a
  different, worse reason): it holds a SHARE lock on the execution table --
  blocking only writes to it -- for the build. With the native-airing gate
  closed nothing creates executions on a station, so the table is expected
  to be empty or tiny and the build instantaneous; the engine never writes
  it while the gate is closed.
* No new table, field, capability or setting.
* New control-only pages under `/iportal/messages/board/` (inside the existing
  `/iportal/` GroupAccess prefix); templates are self-contained (no new static
  files).
* `activate_message(expected_revision=...)` and
  `set_message_expiry(expected_expires_at=...)` are optional; existing callers
  are unchanged. New `request_manual_replay_outcome(...)` returns
  `(execution, outcome)`; `request_manual_replay` keeps its contract.
* The recorder contract gains `hub_links(user)` (default none); the hub lists
  it under the tool's one entry. The active-message adapter's Back link
  returns to the board when opened from it by control.
* No engine, renderer, worker, GStreamer, OGRemote, Weather/AMBER or service
  change; `NATIVE_AIRING_AVAILABLE` stays False and Replay Now is refused
  before the service while the gate is closed.
* Not in D3D (D3E): monitoring, retention, failure recovery and worker
  packaging.
